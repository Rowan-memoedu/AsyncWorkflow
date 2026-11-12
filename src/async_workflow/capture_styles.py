"""Read user-owned, direct RemNote tags before generating a new recording body."""
from .core import Attention, read_json
from . import remnote, routing


def template_node(cfg, path):
    tree = read_json(cfg.root / 'daily-template.json', {}).get('tree', {})
    for title in path:
        matches = [n for n in tree.get('children', []) if n['title'] == title]
        if len(matches) != 1:
            raise Attention('capture_style_template_ambiguous')
        tree = matches[0]
    return tree.get('id')


async def prepare(cfg, manifest, key, item):
    """Resolve once and reuse the identical route for refinement and publication."""
    if item['kind'] == 'command':
        return ''
    async with remnote.connection(cfg) as session:
        saved = manifest.effect(key)
        route = (saved or {}).get('routing') or await routing.plan(cfg, session, remnote.call,
            item['destination'], item['raw_text'], manifest.read().get('provenance', {}), item=item)
        if not saved:
            manifest.save_effect(key, {'backend': 'independent-remnote-plugin',
                                      'status': 'not_started', 'routing': route})
        tags = read_json(cfg.root / 'refinement-tags.json', {})
        if not tags:
            return ''
        target = route.get('parent_id') if route['type'] == 'document' else None
        if route['type'] == 'daily':
            target = template_node(cfg, route['branch_path'])
        if route['type'] == 'todo':
            targets = read_json(cfg.root / 'todo-targets.json', {})
            if route['category'] == 'shopping':
                target = targets.get('shopping_id')
            elif route['category'] == 'todo':
                target = targets.get('todo_id')
            else:
                target = next((n['id'] for n in targets.get('software', [])
                               if n['title'] == route.get('software')), targets.get('information_id'))
        if not target:
            return ''
        result = await remnote.call(session, 'capture_nodes', ids=[target])
        applied = result['nodes'][0]['tags']
        selected = [style for style in ('writing', 'non_writing') if tags.get(style) in applied]
        # Conflicting manual tags have no unambiguous preset: retain Decision routing.
        preset = selected[0] if len(selected) == 1 else ''
        item['refinement_preset'] = preset
        item['refinement_tag_target'] = target
        return preset
