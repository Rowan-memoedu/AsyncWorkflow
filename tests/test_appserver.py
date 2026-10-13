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
    monkeypatch.setattr(app,'Thread',lambda c,t:SimpleNamespace(id=t,turn=c.turn,set_name=c.set_name))
    cfg.prepare()
    return cfg, Manifest(cfg, '1'*64), {'route':'codex', 'destination':'', 'raw_text':'calculate',
                                      'content_text':'calculate','markdown':'# Calculate','title':'Canary'}


class FakeClient:
    def __init__(self, store, *, lose_start=False, fail_run=False):
        self.store, self.lose_start, self.fail_run = store, lose_start, fail_run
        self.created = self.submitted = self.resumed = 0
        self.inputs = []
        self.names = []
        self.starts = []
        self.project_id = 'project-fixture'
        self.finished = False

    def __call__(self, config):
        self.cwd = config.cwd
        return self
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def initialize(self): pass

    def response(self):
        return app.ProtocolObject(thread={'id':'native-thread', 'cwd':self.cwd,
                                          'projectId':self.project_id})

    def thread_start(self, params):
        assert params['ephemeral'] is False
        assert params['projectId'] == 'project-fixture'
        assert params['approvalPolicy'] == 'never'
        self.starts.append(params)
        self.created += 1
        return self.response()

    def thread_resume(self, thread_id):
        assert thread_id == 'native-thread'
        self.resumed += 1
        return self.response()

    def set_name(self, name):
        assert self.finished, 'turn/start may return before the rollout is flushed'
        self.names.append(name)

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
        self.finished = True
        return SimpleNamespace(status=SimpleNamespace(value='completed'),
                               final_response='建议先比较维护成本。\n\n- [官方资料](https://example.org/spec)')


def test_new_commands_use_sdk_and_preserve_ids_before_submission(setup, monkeypatch):
    cfg, store, item = setup
    sdk = FakeClient(store)
    monkeypatch.setattr(app, 'CodexClient', sdk)
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
    sdk = FakeClient(store, lose_start=True)
    monkeypatch.setattr(app, 'CodexClient', sdk)
    for _ in range(2):
        with pytest.raises(UnknownOutcome): executor.execute(cfg, store, 'key', item)
    assert store.effect('key')['session_id'] == 'native-thread'
    assert sdk.created == sdk.submitted == 1


def test_lost_completion_uses_read_only_reconciliation(setup, monkeypatch):
    cfg, store, item = setup
    sdk = FakeClient(store, fail_run=True)
    monkeypatch.setattr(app, 'CodexClient', sdk)
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
    monkeypatch.setattr(app, 'CodexClient', lambda *a, **k:pytest.fail('runtime should not launch'))
    assert executor.execute(cfg, store, 'key', item) == receipt


def test_desktop_writer_conflict_keeps_existing_thread(setup, monkeypatch):
    cfg, store, item = setup
    store.save_effect('key', {'status':'needs_input','backend':'app-server','output_dir':str(store.folder/'old'),
                             'session_id':'desktop-thread'})
    sdk = FakeClient(store)
    def conflict(*args): raise RuntimeError('thread desktop-thread already has an active writer')
    sdk.thread_resume = conflict
    monkeypatch.setattr(app, 'CodexClient', sdk)
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
    sdk=FakeClient(store)
    monkeypatch.setattr(app,'CodexClient',sdk)
    executor.execute(cfg,store,'key',item)
    reply='接下来比较它们的维护成本。'
    result=executor.execute(cfg,store,'key',{**item,'followup':reply})
    assert sdk.created == sdk.resumed == 1 and sdk.submitted == 2
    assert sdk.inputs == [item['markdown'],reply]
    assert result['project_assignment']['project_id'] == cfg.command_project_id


def test_followup_without_thread_cannot_create_a_second_task(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeClient(store)
    monkeypatch.setattr(app,'CodexClient',sdk)
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
    sdk=FakeClient(store)
    sdk.project_id=None
    monkeypatch.setattr(app,'CodexClient',sdk)
    with pytest.raises(UnknownOutcome): executor.execute(cfg,store,'key',item)
    assert store.effect('key')['phase']=='thread_created'
    assert store.effect('key')['session_id']=='native-thread'
    assert sdk.created==1 and sdk.submitted==0


def test_unknown_project_fails_before_thread_creation(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeClient(store)
    monkeypatch.setattr(app,'CodexClient',sdk)
    def failure(*a): raise ValueError('project root mismatch')
    monkeypatch.setattr(app,'check_project',failure)
    with pytest.raises(ValueError): executor.execute(cfg,store,'key',item)
    assert sdk.created==sdk.submitted==0
    assert store.effect('key') is None


def pre_turn_receipt(cfg, store):
    folder = store.folder/'commands/key'
    saved = {'status':'outcome_unknown', 'backend':'app-server', 'phase':'thread_created',
             'session_id':'expired-empty-thread', 'thread_id':'expired-empty-thread',
             'output_dir':str(folder), 'cwd':str(cfg.command_workspace), 'effect_key':'key'}
    store.save_effect('key', saved)
    return store.effect('key')


def test_missing_pre_turn_thread_recovers_once_and_preserves_evidence(setup,monkeypatch):
    cfg,store,item=setup
    saved=pre_turn_receipt(cfg,store)
    sdk=FakeClient(store)
    def missing(tid):
        sdk.resumed += 1
        raise app.InvalidRequestError(-32600, f'no rollout found for thread id {tid}')
    sdk.thread_resume=missing
    monkeypatch.setattr(app,'CodexClient',sdk)
    result=executor.execute(cfg,store,'key',item)
    assert result['session_id']=='native-thread'
    assert result['replaced_empty_threads']==['expired-empty-thread']
    audit=json.loads((Path(saved['output_dir'])/'pre-turn-recoveries.json').read_text())
    assert audit[0]['previous_receipt']==saved
    assert executor.execute(cfg,store,'key',item)==store.effect('key')
    assert sdk.created==sdk.resumed==sdk.submitted==1
    assert sdk.inputs==[item['markdown']]


@pytest.mark.parametrize('error', [ConnectionError('transport lost'),
    app.InvalidRequestError(-32600,'thread not loaded: expired-empty-thread'),
    app.InvalidRequestError(-32600,'no rollout found for thread id different-thread')])
def test_ambiguous_resume_failure_does_not_replace_thread(setup,monkeypatch,error):
    cfg,store,item=setup
    pre_turn_receipt(cfg,store)
    sdk=FakeClient(store)
    def failure(tid): raise error
    sdk.thread_resume=failure
    monkeypatch.setattr(app,'CodexClient',sdk)
    with pytest.raises(UnknownOutcome): executor.execute(cfg,store,'key',item)
    assert sdk.created==sdk.submitted==0
    assert store.effect('key')['phase']=='thread_created'


@pytest.mark.parametrize('change', [{'phase':'turn_start_pending'}, {'turn_id':'known-turn'}, {'is_followup':True}])
def test_missing_rollout_does_not_authorize_replay_after_submission(change):
    saved={'phase':'thread_created','session_id':'native-thread',**change}
    error=app.InvalidRequestError(-32600,'no rollout found for thread id native-thread')
    assert not app.recoverable_empty_thread(saved,error)


def test_name_failure_does_not_abort_running_turn(setup,monkeypatch):
    cfg,store,item=setup
    sdk=FakeClient(store)
    def failure(name): raise RuntimeError('name unavailable')
    sdk.set_name=failure
    monkeypatch.setattr(app,'CodexClient',sdk)
    result=executor.execute(cfg,store,'key',item)
    assert result['status']=='completed_unverified'
    assert (Path(result['output_dir'])/'title-failure.json').exists()


def test_desktop_continuation_prevents_replaying_original_body(setup,monkeypatch):
    cfg,store,item=setup
    saved=pre_turn_receipt(cfg,store)
    sdk=FakeClient(store)
    sdk.thread_resume=lambda tid:app.ProtocolObject(thread={'id':tid,'projectId':cfg.command_project_id,
        'cwd':str(cfg.command_workspace),'turns':[{'id':'desktop-turn'}]})
    monkeypatch.setattr(app,'CodexClient',sdk)
    result=executor.execute(cfg,store,'key',item)
    assert result['reason']=='thread_continued_elsewhere'
    assert result['session_id']==saved['session_id']
    assert sdk.created==sdk.submitted==0
