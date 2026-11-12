import asyncio
import hashlib
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from async_workflow import capture_control as control, mirror_server
from async_workflow.capture_protocol import Mailbox, validate_request
from async_workflow.capture_catalog_sync import catalog_from_snapshot
from async_workflow.core import Attention, Manifest, Settings, atomic
from async_workflow import refinement, splitter


@pytest.fixture
def cfg(tmp_path):
    value = Settings(root=tmp_path/'state', artifacts=tmp_path/'out',
        ffprobe=Path('ffprobe'), codex=Path('codex'), template_id='template')
    value.prepare()
    return value


def hold(identity='a'*64):
    return {'id': 'b'*32, 'kind': 'hold', 'recording_id': identity}


def test_hold_blocks_processing_then_commit_permanently_suppresses_old(cfg):
    request = hold()
    assert control.execute(cfg, request) == {'status': 'held'}
    with pytest.raises(Attention, match='held'):
        with control.effect_guard(cfg, request['recording_id']):
            pytest.fail('must never enter write section')
    commit = {'id':'c'*32, 'kind':'commit', 'hold_id':request['id'],
              'recording_id':request['recording_id'], 'merged_id':'d'*64}
    assert control.execute(cfg, commit) == {'status':'committed'}
    assert control.execute(cfg, commit) == {'status':'committed'}
    with pytest.raises(Attention, match='superseded'):
        control.check(cfg, request['recording_id'])
    control.check(cfg, 'd'*64)


@pytest.mark.parametrize('status', ['verified', 'outcome_unknown', 'submitted', 'running', 'failed'])
def test_any_potential_external_effect_prevents_merging(cfg, status):
    Manifest(cfg, 'a'*64).save_effect('body', {'status':status})
    assert control.execute(cfg, hold()) == {'status':'processed'}
    control.check(cfg, 'a'*64)


def test_route_only_is_safe_and_release_recovers_original(cfg):
    Manifest(cfg, 'a'*64).save_effect('body', {'status':'not_started', 'routing':{'type':'document'}})
    assert control.execute(cfg, hold())['status'] == 'held'
    release = {'id':'e'*32,'kind':'release','recording_id':'a'*64,'hold_id':'b'*32}
    assert control.execute(cfg, release) == {'status':'released'}
    control.check(cfg, 'a'*64)


def test_publish_and_hold_are_serialized_not_check_then_act(cfg):
    started = threading.Event()
    def publish():
        with control.effect_guard(cfg, 'a'*64):
            started.set()
            time.sleep(.1)
            Manifest(cfg, 'a'*64).save_effect('body', {'status':'verified'})
    worker = threading.Thread(target=publish)
    worker.start(); assert started.wait(2)
    assert control.execute(cfg, hold()) == {'status':'processed'}
    worker.join(2); assert not worker.is_alive()


def test_request_durability_conflict_and_auth_separation(tmp_path):
    config = {'readers':{name:hashlib.sha256(name.encode()).hexdigest() for name in ('phone','desktop')},
              'publisher_hash':hashlib.sha256(b'worker').hexdigest(), 'capture_readers':['phone']}
    app = TestClient(mirror_server.create_app(tmp_path, config))
    headers = {'X-Capture-Client':'1'}
    assert app.post('/api/capture/requests',json=hold(),auth=('desktop','desktop'),headers=headers).status_code == 401
    assert app.post('/api/capture/requests',json=hold(),auth=('phone','phone')).status_code == 401
    assert app.post('/api/capture/requests',json=hold(),auth=('phone','phone'),headers={**headers,'Origin':'https://evil.test'}).status_code == 401
    assert app.post('/api/capture/requests',json=hold(),auth=('phone','phone'),headers=headers).status_code == 200
    restarted = Mailbox(tmp_path)
    assert restarted.pending() == [hold()]
    with pytest.raises(ValueError, match='different'):
        restarted.submit({**hold(), 'recording_id':'f'*64})
    assert app.get('/api/capture/worker',auth=('phone','phone'),headers=headers).status_code == 401
    assert app.get('/api/capture/worker',headers={'Authorization':'Bearer worker'}).json()['requests'] == [hold()]
    assert app.post('/api/capture/worker/'+hold()['id'],json={'status':'held'},headers={'Authorization':'Bearer worker'}).status_code == 200
    assert restarted.pending() == []
    assert app.get('/api/capture/requests/'+hold()['id'],auth=('phone','phone'),headers=headers).json()['result']['status'] == 'held'


def test_catalog_rename_move_new_child_and_tag_changes_keep_ids(cfg):
    tags = {'writing':'w','non_writing':'n','automatic':'a'}
    snapshot = {'template':{'children':[]}, 'documents':[
        {'id':'root','title':'Before','path':['Before'],'parent_id':None,'tags':['w']},
        {'id':'child','title':'Child','path':['Before','Child'],'parent_id':'root','tags':[]}]}
    first = catalog_from_snapshot(cfg,snapshot,{'routes':[]},tags)
    snapshot['documents'][0].update(title='After',path=['After'],tags=['n'])
    snapshot['documents'][1].update(path=['After','Child'])
    snapshot['documents'].append({'id':'new','title':'New','path':['After','New'],'parent_id':'root','tags':['w','n']})
    second = catalog_from_snapshot(cfg,snapshot,first,tags)
    assert [r['key'] for r in second['routes'][:2]] == [r['key'] for r in first['routes']]
    assert second['routes'][0]['title'] == 'After'
    assert second['routes'][0]['refinementPreset'] == 'non_writing'
    assert second['routes'][1]['refinementPreset'] == ''
    assert second['routes'][2]['refinementPreset'] == ''
    assert second['routes'][2]['parent'] == first['routes'][0]['key']


@pytest.mark.parametrize('preset', ['writing', 'non_writing'])
def test_tag_preset_never_calls_decisions(cfg, monkeypatch, preset):
    monkeypatch.setattr(refinement,'decide',lambda *_:pytest.fail('must skip Decision'))
    monkeypatch.setattr(refinement,'generate',lambda *a,**kw:kw['route'])
    assert refinement.refine(cfg,'A source paragraph.',preset=preset) == preset


def test_whole_recording_validation_requires_explicit_source_evidence():
    item = {'kind':'record','spans':[{'unit':0}],'reason':'fixture'}
    result = splitter.VoiceDumpSplitResult.model_validate({'items':[item,item]})
    with pytest.raises(ValueError, match='one item'):
        splitter.validate_split_request(result,'First topic. Another topic.')
    result.split_requested=True
    result.split_evidence='Split into two separate notes.'
    with pytest.raises(ValueError, match='exact'):
        splitter.validate_split_request(result,'No request to split.')
    splitter.validate_split_request(result,'Split into two separate notes. First. Second.')


@pytest.mark.parametrize('field', ['id', 'recording_id'])
@pytest.mark.parametrize('value', [None, 4, [], {}])
def test_malformed_request_types_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_request({**hold(), field: value})


def test_release_before_hold_is_retryable(cfg):
    request = {'id':'e'*32,'kind':'release','recording_id':'a'*64,'hold_id':'b'*32}
    with pytest.raises(Attention, match='not_received_yet'):
        control.execute(cfg, request)
    control.execute(cfg, hold())
    assert control.execute(cfg, request)['status'] == 'released'


def test_catalog_rejects_preferred_key_collision(cfg):
    previous = {'routes':[{'kind':'document','remId':'one','key':'n12345678'}]}
    with pytest.raises(Attention, match='collision'):
        catalog_from_snapshot(cfg, {'template':{}, 'documents':[]}, previous, {},
                              {('document','two'):'n12345678'})
