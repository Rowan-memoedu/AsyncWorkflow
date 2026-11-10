import asyncio
from pathlib import Path
import pytest
from async_workflow import capture_routes, recording_types, routing
from async_workflow.core import Settings, Attention, atomic, recorded_time, source_metadata


@pytest.fixture
def cfg(tmp_path):
    c = Settings(root=tmp_path/'state', artifacts=tmp_path/'artifacts', inbox=tmp_path/'audio',
                 ffprobe=Path('ffprobe'), codex=Path('codex'), template_id='template')
    c.prepare()
    atomic(c.root/'capture-routes.json', {'schemaVersion': 1, 'templateId': 'template', 'routes': [
        {'key': 'd12345678', 'kind': 'daily', 'path': ['Diary', 'Health']},
        {'key': 'n87654321', 'kind': 'document', 'remId': 'exact-document', 'path': ['Tasks']}]})
    return c


def name(key='d12345678', label='Health', stamp='20261101_235959'):
    return f'{stamp}__QVR1_{key}__{label}__a123b456.m4a'


def test_capture_clock_and_legacy_names():
    assert recorded_time(name()).isoformat() == '2026-11-01T23:59:59+08:00'
    assert capture_routes.parse('20261101_235959.m4a') is None
    assert recorded_time('20261101_235959.m4a') is not None
    assert capture_routes.parse(name(stamp='20260230_120000')) is None


def test_unknown_and_malformed_tags_fail_closed(cfg):
    unknown = capture_routes.metadata(name('d00000000'), cfg)
    assert unknown['capture_error'] == 'capture_route_unknown_or_ambiguous'
    assert recording_types.kind(cfg, name('d00000000'), 30, unknown)[0] == 'retained'
    assert capture_routes.metadata(name().replace('a123b456', 'bad'), cfg)['capture_error'] == 'capture_filename_invalid'
    assert capture_routes.metadata('ordinary.m4a', cfg) == {}


def test_untyped_capture_keeps_semantic_routing(cfg):
    meta = capture_routes.metadata(name('none'), cfg)
    assert meta['capture_schema'] == 1 and 'capture_default' not in meta
    assert capture_routes.default_plan(meta, '', '2026-11-01') is None


@pytest.mark.parametrize('key,expected', [
    ('d12345678', {'type': 'daily', 'branch_path': ['Diary', 'Health']}),
    ('n87654321', {'type': 'document', 'parent_id': 'exact-document'})])
def test_default_is_exact_and_needs_no_model_or_remnote_read(cfg, monkeypatch, key, expected):
    meta = source_metadata(cfg.inbox/name(key), cfg)
    monkeypatch.setattr(routing, 'snapshot', lambda *_: pytest.fail('must use binding'))
    monkeypatch.setattr(routing, 'ask', lambda *_: pytest.fail('must use binding'))
    result = asyncio.run(routing.plan(cfg, None, None, '', 'untouched source', meta))
    assert all(result[k] == v for k, v in expected.items())
    assert result['recorded_day'] == '2026-11-01'


def test_explicit_spoken_destination_overrides_only_its_segment(cfg):
    meta = capture_routes.metadata(name(), cfg)
    assert capture_routes.default_plan(meta, 'another document', '2026-11-01') is None
    assert capture_routes.default_plan(meta, '', '2026-11-01', {'focus_kind': 'todo'}) is None
    assert capture_routes.default_plan(meta, '', '2026-11-01')['branch_path'] == ['Diary', 'Health']
    assert meta['capture_default']['path'] == ['Diary', 'Health']


def test_button_never_grants_meeting_or_long_recording_exemption(cfg):
    cfg.conversation_profiles = {'Health': {}}
    meta = capture_routes.metadata(name(), cfg)
    assert recording_types.meeting_contact(cfg, name(), meta) is None
    assert recording_types.kind(cfg, name(), 30, meta)[0] == 'ordinary'
    assert recording_types.kind(cfg, name(), 1200, meta)[0] == 'retained'
    assert recording_types.kind(cfg, name(), 30, {**meta, 'source_type': 'PHONE_CALL'})[0] == 'retained'


def test_daily_binding_detects_template_replacement(cfg):
    cfg.template_id = 'changed'
    meta = capture_routes.metadata(name(), cfg)
    assert meta['capture_error'] == 'capture_template_changed'
    with pytest.raises(Attention, match='template_changed'):
        capture_routes.default_plan(meta, '', '2026-11-01')


def test_bound_daily_node_rename_refreshes_new_and_saved_routes(cfg):
    from async_workflow.core import read_json
    catalog = read_json(cfg.root/'capture-routes.json')
    catalog['routes'][0]['templateNodeId'] = 'same-health-node'
    atomic(cfg.root/'capture-routes.json', catalog)
    atomic(cfg.root/'daily-template.json', {'template_id':'template', 'tree':{'children':[
        {'id':'diary', 'title':'Renamed diary', 'children':[
            {'id':'same-health-node', 'title':'Renamed health', 'children':[]}]}]}})
    meta = capture_routes.metadata(name(), cfg)
    assert meta['capture_default']['path'] == ['Renamed diary', 'Renamed health']
    stale = {'type':'daily', 'capture_key':'d12345678', 'branch_path':['Diary','Health'],
             'recorded_day':'2026-01-02'}
    result = capture_routes.refresh_daily_route(cfg, stale)
    assert result['branch_path'] == ['Renamed diary', 'Renamed health']
    assert result['recorded_day'] == '2026-01-02'
    assert stale['branch_path'] == ['Diary','Health']
    catalog['routes'][0]['templateNodeId'] = 'deleted-node'
    atomic(cfg.root/'capture-routes.json', catalog)
    assert capture_routes.metadata(name(), cfg)['capture_error'] == 'capture_template_node_missing_or_ambiguous'


def test_spoken_document_beats_inferred_todo_for_tagged_audio(cfg, monkeypatch):
    atomic(cfg.root/'daily-template.json', {'template_id': 'template', 'version': 'test',
                                          'tree': {'children': [{'title': '其他碎片', 'children': []}]}})
    async def choose(*args):
        return {'type': 'document', 'parent_id': 'spoken-target', 'review_eligible': False}
    monkeypatch.setattr(routing, 'choose_document', choose)
    monkeypatch.setattr(routing, 'choose_learning_target', lambda *a: None)
    meta = {**capture_routes.metadata(name(), cfg), 'recorded_at': '2026-11-01T23:59:59+08:00'}
    result = asyncio.run(routing.plan(cfg, None, None, 'A specific document', 'do this later', meta,
                                     item={'kind': 'todo'}))
    assert result['parent_id'] == 'spoken-target'
