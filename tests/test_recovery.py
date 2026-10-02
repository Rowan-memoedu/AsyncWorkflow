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
