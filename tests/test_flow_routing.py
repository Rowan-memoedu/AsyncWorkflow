from pathlib import Path

import pytest

from async_workflow import flows
from async_workflow.core import Manifest, Settings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg=Settings(root=tmp_path/'state',inbox=tmp_path/'audio',artifacts=tmp_path/'out',
                 codex=Path('codex'),ffprobe=Path('ffprobe'))
    cfg.prepare()
    store=Manifest(cfg,'2'*64)
    monkeypatch.setattr(flows.Settings,'load',staticmethod(lambda:cfg))
    monkeypatch.setattr(flows,'suspend_flow_run',lambda **kw:pytest.fail('duplicate input form'))
    return cfg,store


def segment(store, **overrides):
    item={'index':0,'key':'one','kind':'command','route':'codex','risk':'normal',
          'depends_on':[],'cancelled':False,'raw_text':'A public test command.',
          'destination':'','action':'example',**overrides}
    store.update(segments=[item])
    return item


def test_command_question_stays_in_its_native_thread(setup,monkeypatch):
    cfg,store=setup
    segment(store)
    monkeypatch.setattr(flows,'perform',lambda *args:{'status':'needs_input','session_id':'native-id'})
    assert flows.process_segment.fn('2'*64,0)['session_id']=='native-id'


def test_script_requiring_a_decision_enters_thread_before_execution(setup,monkeypatch):
    cfg,store=setup
    segment(store,route='script',risk='confirmation')
    observed=[]
    def perform(cfg,store,key,item):
        observed.append(item['route'])
        return {'status':'needs_input','session_id':'decision-thread'}
    monkeypatch.setattr(flows,'perform',perform)
    assert flows.process_segment.fn('2'*64,0)['session_id']=='decision-thread'
    assert observed==['codex']


def test_failed_script_reuses_one_repair_thread_on_flow_retry(setup,monkeypatch):
    cfg,store=setup
    segment(store,route='script')
    observed=[]
    def perform(cfg,store,key,item):
        return store.effect(key) or {'status':'failed','exit_code':1,'output_dir':'saved-log-path'}
    def repair(cfg,store,key,item):
        observed.append(key)
        return {'status':'needs_input','session_id':'repair-thread'}
    monkeypatch.setattr(flows,'perform',perform)
    monkeypatch.setattr(flows.executor,'execute',repair)
    assert flows.process_segment.fn('2'*64,0)['repair_thread_id']=='repair-thread'
    assert flows.process_segment.fn('2'*64,0)['repair_thread_id']=='repair-thread'
    assert observed==['one-repair']
