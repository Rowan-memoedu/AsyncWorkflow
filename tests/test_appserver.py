import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from async_workflow import codex_appserver as app, executor
from async_workflow.core import Settings, Manifest, UnknownOutcome, atomic


@pytest.fixture
def setup(tmp_path,monkeypatch):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'), project_aliases={'': str(tmp_path)},command_workspace=tmp_path,
                   command_project_id='project-fixture')
    monkeypatch.setattr(app,'check_project',lambda *a:None)
    monkeypatch.setattr(app,'bind_project',lambda c,t,w:{'project_id':c.command_project_id,'cwd':str(w)})
    cfg.prepare()
    return cfg, Manifest(cfg, '1'*64), {'route':'codex', 'destination':'', 'raw_text':'calculate',
                                      'content_text':'calculate','markdown':'# Calculate','title':'Canary'}


class FakeCodex:
    def __init__(self, store, *, lose_start=False, fail_run=False):
        self.store, self.lose_start, self.fail_run = store, lose_start, fail_run
        self.created = self.submitted = 0

    def __call__(self, config): return self
    def __enter__(self): return self
    def __exit__(self, *_): pass

    def thread_start(self, **kwargs):
        assert kwargs['ephemeral'] is False
        self.created += 1
        return SimpleNamespace(id='native-thread', set_name=lambda *_:None, turn=self.turn)

    def turn(self, *args, **kwargs):
        # A lost turn/start reply must still leave an exact native Thread ID.
        assert self.store.effect('key')['session_id'] == 'native-thread'
        assert self.store.effect('key')['phase'] == 'turn_start_pending'
        self.submitted += 1
        if self.lose_start: raise ConnectionError('lost response')
        return SimpleNamespace(id='native-turn', run=self.run)

    def run(self):
        assert self.store.effect('key')['turn_id'] == 'native-turn'
        if self.fail_run: raise ConnectionError('lost completion')
        return SimpleNamespace(status=SimpleNamespace(value='completed'), final_response=json.dumps({
            'status':'completed','summary':'done','evidence':['real output'],'remaining':[]}))


def test_new_commands_use_sdk_and_preserve_ids_before_submission(setup, monkeypatch):
    cfg, store, item = setup
    sdk = FakeCodex(store)
    monkeypatch.setattr(app, 'Codex', sdk)
    monkeypatch.setattr(executor.subprocess, 'Popen', lambda *a, **k:pytest.fail('Exec launched'))
    result = executor.execute(cfg, store, 'key', item)
    assert result['backend'] == 'app-server'
    assert result['cwd'] == str(cfg.command_workspace)
    assert result['project_assignment']['project_id']==cfg.command_project_id
    assert result['session_id'] == 'native-thread' and result['turn_id'] == 'native-turn'
    assert result['status'] == 'completed_unverified'
    assert executor.execute(cfg, store, 'key', item) == store.effect('key')
    assert sdk.created == sdk.submitted == 1


def test_lost_turn_start_response_never_creates_or_submits_again(setup, monkeypatch):
    cfg, store, item = setup
    sdk = FakeCodex(store, lose_start=True)
    monkeypatch.setattr(app, 'Codex', sdk)
    for _ in range(2):
        with pytest.raises(UnknownOutcome): executor.execute(cfg, store, 'key', item)
    assert store.effect('key')['session_id'] == 'native-thread'
    assert sdk.created == sdk.submitted == 1


def test_lost_completion_uses_read_only_reconciliation(setup, monkeypatch):
    cfg, store, item = setup
    sdk = FakeCodex(store, fail_run=True)
    monkeypatch.setattr(app, 'Codex', sdk)
    observed = []
    def reconcile(cfg, manifest, key, saved, schema):
        observed.append(saved)
        assert saved['turn_id'] == 'native-turn'
        return {**saved, 'status':'completed_unverified'}
    monkeypatch.setattr(app, 'reconcile', reconcile)
    assert executor.execute(cfg, store, 'key', item)['status'] == 'completed_unverified'
    assert len(observed) == sdk.created == sdk.submitted == 1


def test_completion_receipt_recovers_without_starting_runtime(setup, monkeypatch):
    cfg, store, item = setup
    folder = store.folder/'commands/key'
    saved = {'status':'submitted','backend':'app-server','output_dir':str(folder),'session_id':'thread',
             'turn_id':'turn','phase':'turn_started','cwd':str(cfg.root)}
    receipt = {**saved, 'status':'completed_unverified','effect_key':'key'}
    atomic(folder/'completion.json', receipt)
    store.save_effect('key', saved)
    monkeypatch.setattr(app, 'Codex', lambda *a, **k:pytest.fail('runtime should not launch'))
    assert executor.execute(cfg, store, 'key', item) == receipt


def test_desktop_writer_conflict_keeps_existing_thread(setup, monkeypatch):
    cfg, store, item = setup
    store.save_effect('key', {'status':'needs_input','backend':'app-server','output_dir':str(store.folder/'old'),
                             'session_id':'desktop-thread'})
    sdk = FakeCodex(store)
    def conflict(*args): raise RuntimeError('thread desktop-thread already has an active writer')
    sdk.thread_resume = conflict
    monkeypatch.setattr(app, 'Codex', sdk)
    result = executor.execute(cfg, store, 'key', {**item, 'followup':'continue'})
    assert result['reason'] == 'thread_owned_elsewhere'
    assert result['session_id'] == 'desktop-thread'
    assert sdk.created == sdk.submitted == 0


def test_legacy_failed_exec_keeps_native_id(setup):
    cfg, store, item = setup
    folder = store.folder/'legacy'
    atomic(folder/'events.jsonl', json.dumps({'type':'thread.started','thread_id':'legacy-thread'}), text=True)
    result = executor.collect_result(item, cfg, folder, 1)
    assert result['status'] == 'failed' and result['session_id'] == 'legacy-thread'


def test_schema_failure_is_handed_back_to_same_thread():
    result = app._complete({'session_id':'native-thread'}, 'unstructured answer', executor.RESULT_SCHEMA)
    assert result['status'] == 'needs_input' and result['session_id'] == 'native-thread'


def test_failed_native_turn_not_reported_completed():
    result = app._complete({'session_id':'native-thread'}, '', executor.RESULT_SCHEMA, 'failed')
    assert result['status'] == 'failed' and result['session_id'] == 'native-thread'


def test_prompt_uses_structured_task_without_container_instruction():
    source='新建一个 Thread，研究某项公开协议。'
    prompt=app.prompt_for({'raw_text':source,'markdown':'# 研究公开协议\n- 比较维护成本'})
    assert source not in prompt and '# 研究公开协议\n- 比较维护成本' in prompt
    assert '创建步骤已完成' in prompt and '不再为承载同一任务另建 Thread' in prompt


def test_direct_commands_are_partitioned_and_structured_once(setup,monkeypatch):
    from async_workflow import splitter
    cfg,store,_=setup
    calls=[]
    monkeypatch.setattr(splitter,'extract_content',lambda c,p:{**p,'content_text':'Compare protocols.','control_spans':[[0,15]]})
    monkeypatch.setattr(splitter,'refine',lambda c,s:calls.append(s) or '# Compare protocols')
    item={'raw_text':'Create a thread. Compare protocols.'}
    first=app.prepare_input(cfg,item,store.folder/'command')
    assert app.prepare_input(cfg,item,store.folder/'command')==first
    assert calls==['Compare protocols.']


def test_project_failure_retains_thread_and_does_not_submit_outside_project(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeCodex(store)
    monkeypatch.setattr(app,'Codex',sdk)
    def failure(*a): raise RuntimeError('project assignment failed')
    monkeypatch.setattr(app,'bind_project',failure)
    with pytest.raises(UnknownOutcome): executor.execute(cfg,store,'key',item)
    assert store.effect('key')['phase']=='thread_created'
    assert store.effect('key')['session_id']=='native-thread'
    assert sdk.created==1 and sdk.submitted==0


def test_unknown_project_fails_before_thread_creation(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeCodex(store)
    monkeypatch.setattr(app,'Codex',sdk)
    def failure(*a): raise ValueError('project root mismatch')
    monkeypatch.setattr(app,'check_project',failure)
    with pytest.raises(ValueError): executor.execute(cfg,store,'key',item)
    assert sdk.created==sdk.submitted==0
