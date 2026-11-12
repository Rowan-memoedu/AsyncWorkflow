"""Opt-in recorder filename tags and locally bound default destinations."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_NAME = re.compile(
    r'(?P<stamp>\d{8}_\d{6})__QVR1_(?P<key>none|[dn][a-f0-9]{8})'
    r'__(?P<label>[^/\\:*?"<>|\x00-\x1f]{1,80})__(?P<nonce>[a-f0-9]{8})'
)


def parse(name: str):
    match = _NAME.fullmatch(Path(name).stem)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match['stamp'], '%Y%m%d_%H%M%S').replace(
            tzinfo=ZoneInfo('Asia/Shanghai'))
    except ValueError:
        return None
    return {'key': match['key'], 'recorded_at': stamp, 'nonce': match['nonce']}


def metadata(name, cfg):
    from .core import Attention
    try:
        return _validated_metadata(name, cfg)
    except Attention as exc:
        # Keep one broken/obsolete tag from aborting a sweep of unrelated audio.
        # The ordinary-recording gate retains it without ASR or any side effect.
        return {'capture_schema': 1, 'capture_error': str(exc)}


def current_daily_path(cfg, route):
    """Follow a bound template node through renames, never match a new node by name."""
    from .core import Attention, read_json
    node_id = route.get('templateNodeId')
    if not node_id:
        return list(route['path'])
    snapshot = read_json(cfg.root / 'daily-template.json', {})
    if snapshot.get('template_id') != cfg.template_id:
        raise Attention('capture_template_not_synced')
    matches = []
    def visit(node, prefix):
        path = [*prefix, node['title'].strip()]
        if node.get('id') == node_id:
            matches.append(path)
        for child in node.get('children', []):
            visit(child, path)
    for child in snapshot['tree'].get('children', []):
        visit(child, [])
    if len(matches) != 1 or not all(matches[0]):
        raise Attention('capture_template_node_missing_or_ambiguous')
    return matches[0]


def refresh_daily_route(cfg, route):
    """Refresh only a capture-bound daily destination, including saved unsubmitted effects."""
    from .core import Attention, read_json
    if route.get('type') != 'daily' or not route.get('capture_key'):
        return route
    catalog = read_json(cfg.root / 'capture-routes.json', {})
    if catalog.get('templateId') != cfg.template_id:
        raise Attention('capture_template_changed')
    matches = [r for r in catalog.get('routes', []) if r.get('key') == route['capture_key']]
    if len(matches) != 1 or matches[0].get('kind') != 'daily':
        raise Attention('capture_route_unknown_or_ambiguous')
    return {**route, 'branch_path': current_daily_path(cfg, matches[0])}


def _validated_metadata(name, cfg):
    from .core import Attention, read_json
    parsed = parse(name)
    if not parsed:
        if '__QVR1_' in name:
            raise Attention('capture_filename_invalid')
        return {}
    result = {'capture_schema': 1, 'capture_id': parsed['nonce']}
    if parsed['key'] == 'none':
        return result
    catalog = read_json(cfg.root / 'capture-routes.json', {})
    if catalog.get('schemaVersion') != 1:
        raise Attention('capture_catalog_missing')
    matches = [r for r in catalog.get('routes', []) if r.get('key') == parsed['key']]
    if not matches:
        history = read_json(cfg.root / 'capture-route-history.json', {})
        matches = [r for r in history.get('routes', []) if r.get('key') == parsed['key']]
    if len(matches) != 1:
        raise Attention('capture_route_unknown_or_ambiguous')
    route = deepcopy(matches[0])
    if route.get('kind') == 'daily':
        if catalog.get('templateId') != cfg.template_id:
            raise Attention('capture_template_changed')
        if not route.get('path') or not all(isinstance(p, str) and p.strip() for p in route['path']):
            raise Attention('capture_daily_path_invalid')
        route['path'] = current_daily_path(cfg, route)
    elif route.get('kind') == 'document':
        if not route.get('remId'):
            raise Attention('capture_document_not_bound')
    else:
        raise Attention('capture_route_kind_invalid')
    result['capture_default'] = route
    return result


def default_plan(provenance, requested, recorded_day, item=None):
    if provenance.get('capture_error'):
        from .core import Attention
        raise Attention(provenance['capture_error'])
    # Spoken destination/create instructions keep precedence. A filename never
    # authorizes command execution or changes the original transcript.
    if (requested.strip() or (item or {}).get('document_action') == 'create'
            or (item or {}).get('focus_kind')):
        return None
    route = provenance.get('capture_default')
    if not route:
        return None
    base = {'requested': '', 'recorded_day': recorded_day,
            'reason': 'capture_button_default', 'capture_key': route['key'],
            'review_eligible': False}
    if route['kind'] == 'daily':
        return {**base, 'type': 'daily', 'branch_path': list(route['path'])}
    return {**base, 'type': 'document', 'parent_id': route['remId']}
