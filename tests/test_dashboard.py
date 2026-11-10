from pathlib import Path
from uuid import UUID
import httpx
import pytest
from fastapi.testclient import TestClient
from async_workflow import dashboard
from async_workflow.core import Settings, Manifest, atomic


def test_latest_processing_uses_events_not_scan_or_inventory_time():
    manifests = {'a': {'asr': {'finished_at': '2026-11-01T10:00:00+00:00'},
                       'effects': {'note': {'updated_at': '2026-11-01T19:00:00+08:00'}}}}
    assert dashboard.latest_processing_at(manifests, {}) == '2026-11-01T19:00:00+08:00'
    assert dashboard.latest_processing_at(manifests, {'run': {'end_time': '2026-11-01T12:00:00Z'}}) == '2026-11-01T12:00:00+00:00'
    assert dashboard.latest_processing_at({'a': {'asr': {'finished_at': 'broken'}}}, {}) is None


def test_recorder_filename_time_and_mixed_timezones_sort_by_actual_instant(setup):
    cfg = setup
    Manifest(cfg, 'a'*64).update(original_name='20261102_090001__QVR1_none__未分类__1234abcd.m4a',
        provenance={'recorded_at': '2026-11-03T00:00:00Z'}, sources=['a.m4a'])
    Manifest(cfg, 'b'*64).update(original_name='utc.wav',
        provenance={'recorded_at': '2026-11-02T01:00:02Z'}, sources=['b.m4a'])
    Manifest(cfg, 'c'*64).update(original_name='20261101_235959__QVR1_none__未分类__1234abcd.m4a', sources=['c.m4a'])
    rows = dashboard.Inventory(cfg).listing()['recordings']
    assert [r['id'][0] for r in rows] == ['b', 'a', 'c']
    assert rows[1]['recorded_at'] == '2026-11-02T09:00:01+08:00'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'), enabled_at='2026-10-01T00:00:00+00:00')
    cfg.prepare()
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            return httpx.Response(200, json=[], request=httpx.Request('POST', url))
    monkeypatch.setattr(dashboard.httpx, 'Client', Client)
    return cfg


def test_all_audio_including_unprocessed_files_and_missing_source_receipts(setup):
    cfg = setup
    path = cfg.inbox/'20261030_090000.wav'; path.write_bytes(b'audio')
    store = Manifest(cfg, 'a'*64)
    store.update(original_name='old.wav', sources=[str(cfg.inbox/'old.wav')], segments=[{'key':'note','kind':'record'}], effects={'note':{'status':'verified'}})
    listing = dashboard.Inventory(cfg).listing()
    assert listing['total'] == 2
    assert listing['summary'] == {'pending':1, 'verified':1}
    assert not (cfg.root/'config.json').exists()  # read-only inventory


def test_search_matches_transcript_and_outline_and_reports_actual_route(setup):
    cfg = setup
    store = Manifest(cfg, 'b'*64)
    store.update(original_name='test.wav', sources=['public.wav'], segments=[
        {'key':'part','kind':'todo','raw_text':'请记录这个想法。','markdown':'# 画布导出'}],
        effects={'part':{'status':'verified','root_id':'note-id','routing':{
            'focus_document_title':'Open focus 事项','focus_branch_title':'to do list'}}})
    atomic(store.folder/'asr.json', {'text':'给批注工具添加白板功能。'})
    inventory = dashboard.Inventory(cfg)
    assert inventory.listing('白板')['matched'] == 1
    assert inventory.listing('画布')['matched'] == 1
    assert inventory.listing('白板 批注')['matched'] == 1
    assert inventory.listing('不存在')['matched'] == 0
    detail = inventory.detail('b'*64)
    assert detail['parts'][0]['kind'] == 'todo'
    assert detail['parts'][0]['routing']['focus_branch_title'] == 'to do list'
    assert detail['threads'] == []


def test_partial_success_and_unknown_prefect_are_not_reported_as_correct():
    data = {'segments':[{'key':'done'},{'key':'not-yet'}], 'effects':{'done':{'status':'verified'}}, 'analysis_run':str(UUID(int=1))}
    assert dashboard.classify(data, {}, False) == 'unknown'
    data['effects']['not-yet'] = {'status':'outcome_unknown'}
    assert dashboard.classify(data, {}, True) == 'blocked'
    data['effects']['not-yet'] = {'status':'completed_unverified'}
    assert dashboard.classify(data, {}, True) == 'command_done'


def test_thread_links_include_existing_maintenance_and_repair_with_valid_ids():
    tid = str(UUID(int=10)); repair = str(UUID(int=20))
    rows = dashboard.effect_threads({'feedback':{'thread_id':tid,'backend':'app-server-native-queue'},
                                     'script':{'repair_thread_id':repair}, 'bad':{'thread_id':'javascript:alert(1)'}})
    assert len(rows) == 2
    assert rows[0]['role'] == '工作流维护'
    assert rows[0]['url'] == 'codex://threads/' + tid


def test_meeting_group_is_shown_once_per_source_not_as_an_extra_recording(setup):
    cfg = setup
    Manifest(cfg, 'c'*64).update(sources=['meeting.wav'])
    Manifest(cfg, 'd'*64).update(recording_type='meeting_day', latest_effect='meeting',
        group={'members':[{'id':'c'*64,'path':'meeting.wav','name':'Meeting audio','provenance':{}}]},
        effects={'meeting':{'status':'verified'}})
    listing = dashboard.Inventory(cfg).listing()
    assert listing['total'] == 1 and listing['recordings'][0]['status'] == 'verified'


def test_loopback_host_path_and_static_assets(setup):
    client = TestClient(dashboard.create_app(setup))
    assert client.get('/').status_code == 200
    assert client.get('/assets/app.js').status_code == 200
    assert client.get('/api/recordings').json()['total'] == 0
    assert client.get('/api/recordings/not-an-id').status_code == 404
    assert client.post('/api/recordings').status_code == 405
    assert client.get('/api/recordings', headers={'host':'attacker.example'}).status_code == 400
    response = client.get('/')
    assert response.headers['cache-control'] == 'no-store'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']


def test_empty_feedback_partition_is_not_presented_as_maintenance_delivery(setup):
    store = Manifest(setup, 'e'*64)
    store.update(original_name='ordinary.wav', sources=['ordinary.wav'],
                 workflow_feedback={'source_spans':[], 'reason':'No secretary delegation'})
    detail = dashboard.Inventory(setup).detail('e'*64)
    assert detail['parts'] == [] and detail['threads'] == []
