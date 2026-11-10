import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from async_workflow import remnote, executor, asr
from async_workflow.core import Settings, Manifest, Attention, UnknownOutcome, atomic, source_metadata

@pytest.fixture
def cfg(tmp_path):
    cfg=Settings(root=tmp_path/'state',inbox=tmp_path/'audio',artifacts=tmp_path/'out',ffprobe=Path('ffprobe'),codex=Path('codex'),default_rem_id='parent')
    cfg.prepare()
    return cfg

def test_exit_receipt_recovers_no_relaunch(cfg,monkeypatch):
    store=Manifest(cfg,'f'*64)
    folder=store.folder/'cmd'
    atomic(folder/'exit.json',{'effect_key':'key','returncode':0})
    atomic(folder/'result.json',{'summary':'saved','status':'completed','evidence':['trace'],'remaining':[]})
    atomic(folder/'events.jsonl','',text=True)
    store.save_effect('key',{'status':'submitted','output_dir':str(folder)})
    monkeypatch.setattr(executor.subprocess,'Popen',lambda *a,**k:pytest.fail('relaunch'))
    assert executor.execute(cfg,store,'key',{'route':'codex'})['status']=='completed_unverified'

def test_xiaomi_path_carries_source_and_original_name(cfg):
    path=cfg.inbox/'xiaomi/1/123/1700000000000/20231115_061320.m4a'
    meta=source_metadata(path,cfg)
    assert meta['source_type']=='PHONE_CALL'
    assert meta['asset_id']=='123'
    assert path.name=='20231115_061320.m4a'

def test_xiaomi_unknown_metadata_not_guessed(cfg):
    with pytest.raises(Attention): source_metadata(cfg.inbox/'xiaomi/20261002_120000.wav',cfg)

def test_cleanup_only_owned_prefix(cfg):
    store=Manifest(cfg,'a'*64)
    with pytest.raises(Attention): asr.cleanup(object(),cfg,store,{'object_key':'unrelated/file.wav'})

def test_reanalysis_keeps_old_effects_and_never_dispatches(cfg,monkeypatch):
    from async_workflow import proposals
    store=Manifest(cfg,'b'*64)
    store.save_effect('old',{'status':'verified','root_id':'user-edited-rem'})
    atomic(store.folder/'asr.json',{'text':'一条来源。'})
    before=store.path.read_bytes()
    monkeypatch.setattr(proposals.splitter,'refine',lambda *a:'# 新版本')
    monkeypatch.setattr(proposals.splitter,'split',lambda *a:[{'kind':'command'}])
    result=proposals.reanalyze(cfg,'b'*64)
    assert result['dispatch_allowed'] is False
    assert store.path.read_bytes()==before
    assert Path(result['proposal'],'proposal.json').exists()


@pytest.mark.parametrize('connected',[False,True])
def test_only_machine_connection_waits_resume_when_plugin_returns(cfg,monkeypatch,connected):
    from uuid import UUID
    from prefect.states import Suspended
    from async_workflow import ingest
    from prefect.client import orchestration
    import prefect.flow_runs
    ids=[UUID(int=i) for i in (1,2,3)]
    states={ids[0]:Suspended(pause_key='remnote-connection:a'),
            ids[1]:Suspended(pause_key='remnote-connection:b'),
            ids[2]:Suspended(pause_key='native-thread')}
    store=Manifest(cfg,'c'*64)
    store.update(segment_runs={str(i):str(value) for i,value in enumerate(ids)})
    checks,resumed=[],[]
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read_flow_run(self,run_id): return SimpleNamespace(id=run_id,state=states[run_id],run_count=1)
    async def ready(_cfg):
        checks.append(True)
        return connected
    monkeypatch.setattr(orchestration,'get_client',lambda **kwargs:Client())
    monkeypatch.setattr(remnote,'is_ready',ready)
    monkeypatch.setattr(prefect.flow_runs,'resume_flow_run',lambda run_id:resumed.append(run_id))
    ingest.recover(cfg)
    assert resumed == (ids[:2] if connected else [])
    assert len(checks)==1


@pytest.mark.parametrize('message,count,retry',[
    ('Flow run encountered an exception: APIConnectionError: Connection error.',1,True),
    ('Flow run encountered an exception: APIConnectionError: Connection error.',30,True),
    ('Flow run encountered an exception: AuthenticationError: denied',1,True),
    ('Flow run encountered an exception: UnknownOutcome: appserver_outcome_unknown',1,True),
    ('Flow run encountered an exception: Attention: daily_duplicate_branch',1,True),
])
def test_all_failures_retry_same_run_without_attempt_limit(cfg,monkeypatch,message,count,retry):
    from uuid import UUID
    from prefect.states import Failed
    from prefect.client import orchestration
    from async_workflow import ingest
    run_id=UUID(int=99)
    Manifest(cfg,'d'*64).update(analysis_run=str(run_id))
    scheduled=[]
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read_flow_run(self,value):
            assert value==run_id
            return SimpleNamespace(id=run_id,state=Failed(message=message),run_count=count)
        def set_flow_run_state(self,value,state,force): scheduled.append(value)
    monkeypatch.setattr(orchestration,'get_client',lambda **kwargs:Client())
    ingest.recover(cfg)
    assert scheduled==([run_id] if retry else [])


def test_bad_run_does_not_block_other_recordings_or_duplicate_native_runs(cfg, monkeypatch):
    from uuid import UUID
    from prefect.states import Failed, Scheduled
    from prefect.client import orchestration
    from async_workflow import ingest
    broken, good = UUID(int=201), UUID(int=202)
    Manifest(cfg,'e'*64).update(analysis_run=str(broken), segment_runs={'one':str(good),'duplicate':str(good)})
    calls = []; state = [Failed(message='unknown persistent failure')]
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read_flow_run(self, value):
            if value == broken: raise OSError('unavailable')
            return SimpleNamespace(id=value,state=state[0],run_count=99)
        def set_flow_run_state(self,value,new_state,force):
            calls.append(value); state[0] = Scheduled()
    monkeypatch.setattr(orchestration,'get_client',lambda **kw:Client())
    first = ingest.recover(cfg); second = ingest.recover(cfg)
    assert calls == [good]
    assert len(first['errors']) == len(second['errors']) == 1


def test_stalled_run_requires_absent_process_evidence(cfg):
    from datetime import datetime, timedelta, timezone
    from uuid import UUID
    from prefect.states import Running
    from async_workflow.recovery import orphaned
    state = Running(timestamp=datetime.now(timezone.utc)-timedelta(hours=2))
    run = SimpleNamespace(id=UUID(int=301),state=state)
    assert orphaned(run, {}, (set(), True))
    assert not orphaned(run, {}, ({str(run.id)}, True))
    assert not orphaned(run, {}, (set(), False))
