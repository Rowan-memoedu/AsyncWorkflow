"""RemNote owns route names, membership, nesting and direct refinement tags."""
import json
from filelock import FileLock
from .core import Attention, atomic, digest, now, read_json
from . import remnote


def catalog_from_snapshot(cfg, snapshot, previous, tags, preferred=None):
    identities = {(r['kind'], r.get('remId') or r.get('templateNodeId')): r['key']
                  for r in previous.get('routes', [])}
    identities.update(preferred or {})
    if len(set(identities.values())) != len(identities):
        raise Attention('capture_route_key_collision')
    used = set(identities.values())

    def key(kind, identity):
        pair = (kind, identity)
        if pair not in identities:
            prefix = 'd' if kind == 'daily' else 'n'
            counter = 0
            while True:
                value = prefix + digest(kind + ':' + identity + ':' + str(counter))[:8]
                if value not in used:
                    identities[pair] = value
                    used.add(value)
                    break
                counter += 1
        return identities[pair]

    routes = []

    def daily(node, parent='', path=None):
        for child in node.get('children', []):
            if not child['title'].strip():
                continue
            child_key = key('daily', child['id'])
            full = [*(path or []), child['title']]
            routes.append({'key': child_key, 'title': child['title'][:120], 'parent': parent,
                'kind': 'daily', 'templateNodeId': child['id'], 'path': full})
            daily(child, child_key, full)

    daily(snapshot['template'])
    for doc in snapshot['documents']:
        styles = [style for style in ('writing', 'non_writing') if tags.get(style) in doc['tags']]
        routes.append({'key': key('document', doc['id']), 'title': doc['title'][:120],
            'parent': key('document', doc['parent_id']) if doc['parent_id'] else '',
            'kind': 'document', 'remId': doc['id'], 'path': doc['path'],
            'refinementPreset': styles[0] if len(styles) == 1 else ''})
    if len(routes) > 300:
        raise Attention('capture_catalog_too_large')
    return {'schemaVersion': 1, 'templateId': cfg.template_id, 'routes': routes, 'updatedAt': now()}


async def sync_catalog(cfg, *, preferred=None):
    tags = read_json(cfg.root / 'refinement-tags.json', {})
    previous = read_json(cfg.root / 'capture-routes.json', {})
    if not tags:
        return previous
    async with remnote.connection(cfg) as session:
        snapshot = await remnote.call(session, 'capture_catalog',
            tag_ids=[tags[name] for name in ('writing', 'non_writing', 'automatic') if tags.get(name)],
            template_id=cfg.template_id)
    with FileLock(str(cfg.root / 'locks/capture-catalog.lock')):
        previous = read_json(cfg.root / 'capture-routes.json', {})
        history = read_json(cfg.root / 'capture-route-history.json', {'routes': []})
        registry = {r['key']: r for r in history['routes'] + previous.get('routes', [])}
        stable = {**previous, 'routes': list(registry.values())}
        value = catalog_from_snapshot(cfg, snapshot, stable, tags, preferred)
        registry.update({r['key']: r for r in value['routes']})
        atomic(cfg.root / 'capture-route-history.json', {'routes': list(registry.values())})
        tree = snapshot['template']
        atomic(cfg.root / 'daily-template.json', {'template_id': cfg.template_id, 'tree': tree,
            'version': digest(json.dumps(tree, ensure_ascii=False, sort_keys=True)), 'synced_at': now()})
        atomic(cfg.root / 'capture-routes.json', value)
    return value


async def create_document(cfg, request):
    previous = read_json(cfg.root / 'capture-routes.json', {})
    history = read_json(cfg.root / 'capture-route-history.json', {})
    claimed = next((r for r in previous.get('routes', []) + history.get('routes', [])
                    if r['key'] == request['key']), None)
    # Retries of this request reuse the plugin's creation receipt. A key owned
    # by another request must never rebind an existing recording destination.
    owner_path = cfg.root / 'capture-route-owners' / (request['key'] + '.json')
    with FileLock(str(cfg.root / 'locks/capture-route-owners.lock')):
        owner = read_json(owner_path)
        if (owner and owner != request) or (claimed and not owner):
            raise Attention('capture_route_key_collision')
        atomic(owner_path, request)
    parent = next((r for r in previous.get('routes', []) if r['key'] == request['parent']), None)
    if request['parent'] and not parent:
        raise Attention('capture_parent_not_synced')
    kind = 'daily' if parent and parent['kind'] == 'daily' else 'document'
    if not request['key'].startswith('d' if kind == 'daily' else 'n'):
        raise Attention('capture_route_kind_mismatch')
    parent_id = (parent.get('templateNodeId') if kind == 'daily' else parent.get('remId')) if parent else None
    async with remnote.connection(cfg) as session:
        created = await remnote.call(session, 'create_document', effect_key='capture/' + request['id'],
                                    title=request['title'].strip(), parent_id=parent_id)
        if not created.get('is_document') or created.get('parent_id') != parent_id:
            raise Attention('capture_document_readback_failed')
        if kind == 'document' and parent is None:
            tags = read_json(cfg.root / 'refinement-tags.json', {})
            if not tags.get('automatic'):
                raise Attention('capture_membership_tag_missing')
            await remnote.call(session, 'capture_enroll', rem_id=created['document_id'], tag_id=tags['automatic'])
    catalog = await sync_catalog(cfg, preferred={(kind, created['document_id']): request['key']})
    route = next(r for r in catalog['routes'] if r['key'] == request['key'])
    return {'status': 'created', 'route': route}
