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
        self.created = self.submitted = self.resumed = 0
        self.inputs = []

    def __call__(self, config): return self
    def __enter__(self): return self
    def __exit__(self, *_): pass

    def thread_start(self, **kwargs):
        assert kwargs['ephemeral'] is False
        self.created += 1
        return SimpleNamespace(id='native-thread', set_name=lambda *_:None, turn=self.turn)

    def thread_resume(self, thread_id):
        assert thread_id == 'native-thread'
        self.resumed += 1
        return SimpleNamespace(id=thread_id, turn=self.turn)

    def turn(self, *args, **kwargs):
        assert not kwargs  # No output schema or other per-turn overrides.
        self.inputs.append(args[0])
        # A lost turn/start reply must still leave an exact native Thread ID.
        assert self.store.effect('key')['session_id'] == 'native-thread'
        assert self.store.effect('key')['phase'] == 'turn_start_pending'
        self.submitted += 1
        if self.lose_start: raise ConnectionError('lost response')
        return SimpleNamespace(id='native-turn', run=self.run)

    def run(self):
        assert self.store.effect('key')['turn_id'] == 'native-turn'
        if self.fail_run: raise ConnectionError('lost completion')
        return SimpleNamespace(status=SimpleNamespace(value='completed'),
                               final_response='建议先比较维护成本。\n\n- [官方资料](https://example.org/spec)')


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
    assert sdk.inputs == [item['markdown']]
    assert result['result']['summary'].startswith('建议先比较维护成本。')
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
    def reconcile(cfg, manifest, key, saved):
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


@pytest.mark.parametrize('answer', ['这是自然语言回答。\n\n- 一个要点', '你想比较哪两个方案？', '{"status":"needs_input"}'])
def test_chat_answers_are_preserved_without_parsing_status_from_their_text(answer):
    result = app._complete({'session_id':'native-thread'}, answer)
    assert result['status'] == 'completed_unverified' and result['session_id'] == 'native-thread'
    assert result['result']['summary'] == answer
    assert 'reason' not in result


@pytest.mark.parametrize('native,status', [('failed','failed'), ('interrupted','cancelled')])
def test_unsuccessful_native_turn_not_reported_completed(native,status):
    result = app._complete({'session_id':'native-thread'}, '正文仍保留。', native)
    assert result['status'] == status and result['session_id'] == 'native-thread'
    assert result['result']['summary'] == '正文仍保留。'


def test_prompt_uses_structured_task_without_container_instruction():
    source='新建一个 Thread，研究某项公开协议。'
    body='# 研究公开协议\n- 比较维护成本'
    prompt=app.prompt_for({'raw_text':source,'markdown':body,'target_project_path':'D:/example',
                           'project_unresolved':True})
    assert prompt == body


def test_normal_chat_followup_uses_same_thread_and_exact_user_text(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeCodex(store)
    monkeypatch.setattr(app,'Codex',sdk)
    executor.execute(cfg,store,'key',item)
    reply='接下来比较它们的维护成本。'
    result=executor.execute(cfg,store,'key',{**item,'followup':reply})
    assert sdk.created == sdk.resumed == 1 and sdk.submitted == 2
    assert sdk.inputs == [item['markdown'],reply]
    assert result['project_assignment']['project_id'] == cfg.command_project_id


def test_followup_without_thread_cannot_create_a_second_task(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeCodex(store)
    monkeypatch.setattr(app,'Codex',sdk)
    with pytest.raises(app.Attention,match='followup_requires_existing_thread'):
        executor.execute(cfg,store,'key',{**item,'followup':'继续'})
    assert sdk.created == sdk.submitted == 0


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
