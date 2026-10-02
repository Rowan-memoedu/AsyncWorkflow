import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from async_workflow import command_inbox as app
from async_workflow.core import Attention, Settings, UnknownOutcome, digest


def node(id, text, children=None):
    return {'id': id, 'text': text, 'prompt_text': text, 'raw_text': [text], 'raw_back': [], 'children': children or []}


@pytest.fixture
def fixture(tmp_path):
    cfg = Settings(root=tmp_path, artifacts=tmp_path/'audio', ffprobe=Path('ffprobe'), codex=Path('codex'))
    cfg.prepare()
    fixed = ['每个空行分隔一条命令', '仅投递百分号开头的命令']
    settings = app.CommandSettings(document_id='doc', inbox_id='in', archive_id='out', cwd=tmp_path,
        enabled=True, approved_rules_hash=digest(json.dumps(fixed, ensure_ascii=False)))
    tree = {'protocol': 'command-inbox-1', 'document_id': 'doc',
            'rules': [node('rule', fixed[0], [node('ready', fixed[1]), node('model', '一次性命令当前要求使用的模型：6.1-sol high')])],
            'inbox': node('in', '一次性命令', [node('a', '%原文：保持「A」  和换行\n第二行'), node('blank', ''), node('draft', '草稿')])}
    return cfg, settings, tree


def test_grouping_readiness_and_exact_text(fixture):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    assert source['prompt'] == '%原文：保持「A」  和换行\n第二行'
    assert source['model'] == 'gpt-6.1-sol' and source['effort'] == 'high'
    assert [n['id'] for n in source['nodes']] == ['a', 'blank']
    tree['inbox']['children'][0]['children'] = [node('child', '子项', [node('leaf', '原始细节')])]
    updated, = app.candidates(tree, settings)
    assert updated['prompt'].endswith('\n  子项\n    原始细节')
    assert updated['command_id'] == source['command_id'] and updated['revision'] != source['revision']


def test_empty_parent_with_children_is_not_a_delimiter(fixture):
    _, settings, tree = fixture
    tree['inbox']['children'] = [node('first', '%one'), node('empty', '', [node('detail', 'two')]), node('last', 'three')]
    source, = app.candidates(tree, settings)
    assert source['prompt'] == '%one\n\n  two\nthree'


def test_model_changes_are_parsed_but_unknown_rule_changes_stop(fixture):
    _, settings, tree = fixture
    tree['rules'][0]['children'][1]['text'] = '一次性命令当前要求使用的模型：gpt-6.1-sol xhigh'
    assert app.candidates(tree, settings)[0]['effort'] == 'xhigh'
    tree['rules'][0]['children'][0]['text'] = '投递所有草稿'
    with pytest.raises(Attention, match='rules_changed'):
        app.candidates(tree, settings)


class FakeCodex:
    def __init__(self, manifest, lose_start=False, lose_turn=False):
        self.manifest, self.lose_start, self.lose_turn = manifest, lose_start, lose_turn
        self.starts, self.turns = [], []

    def __call__(self, _): return self
    def __enter__(self): return self
    def __exit__(self, *_): pass

    def models(self):
        return NS(data=[NS(model='gpt-6.1-sol', supported_reasoning_efforts=[NS(reasoning_effort=NS(value='high'))])])

    def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        assert self.manifest.effect('dispatch')['phase'] == 'thread_start_pending'
        if self.lose_start: raise ConnectionError('lost thread start')
        return NS(id='real-thread-id', set_name=lambda name: None, turn=self.turn)

    def turn(self, text, **kwargs):
        saved = self.manifest.effect('dispatch')
        assert saved['thread_id'] == 'real-thread-id' and saved['phase'] == 'turn_start_pending'
        self.turns.append((text, kwargs))
        if self.lose_turn: raise ConnectionError('lost turn start')
        return NS(id='real-turn-id', run=lambda: NS(status=NS(value='completed'), final_response='自由格式回答'))


def test_new_thread_receives_only_original_text_without_schema_or_context(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    sdk = FakeCodex(manifest)
    monkeypatch.setattr(app, 'Codex', sdk)
    calls = []
    def accepted():
        assert manifest.effect('dispatch')['turn_id'] == 'real-turn-id'
        calls.append(True)
    result = app.deliver(cfg, manifest, source, accepted)
    assert result['status'] == 'completed' and len(calls) == 1
    assert len(sdk.starts) == len(sdk.turns) == 1
    assert sdk.starts[0] == {'cwd': str(settings.cwd), 'model': 'gpt-6.1-sol',
        'config': {'model_reasoning_effort': 'high'}, 'ephemeral': False,
        'sandbox': app.Sandbox.workspace_write, 'approval_mode': app.ApprovalMode.deny_all, 'service_name': 'async_workflow'}
    assert sdk.turns[0][0] == source['prompt']
    assert sdk.turns[0][1] == {'model': 'gpt-6.1-sol', 'effort': app.ReasoningEffort.high}
    app.deliver(cfg, manifest, source, accepted)
    assert len(sdk.starts) == len(sdk.turns) == 1


@pytest.mark.parametrize('lost', ['thread', 'turn'])
def test_unknown_submissions_are_never_replayed(fixture, monkeypatch, lost):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    sdk = FakeCodex(manifest, lose_start=lost=='thread', lose_turn=lost=='turn')
    monkeypatch.setattr(app, 'Codex', sdk)
    for _ in range(2):
        with pytest.raises(UnknownOutcome):
            app.deliver(cfg, manifest, source, lambda: pytest.fail('not accepted'))
    assert len(sdk.starts) == 1
    assert len(sdk.turns) == (1 if lost=='turn' else 0)


def test_archive_failure_does_not_cancel_native_execution(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    manifest.update(command_source=source)
    monkeypatch.setattr(app, 'Codex', FakeCodex(manifest))
    monkeypatch.setattr(app, 'archive', lambda *_: (_ for _ in ()).throw(ConnectionError()))
    assert app.deliver(cfg, manifest, source, lambda: app.try_archive(cfg, manifest))['status'] == 'completed'
    assert manifest.read()['archive']['status'] == 'pending'


def test_prompt_tampering_is_detected_before_native_start(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    source['prompt'] += '\n额外上下文'
    monkeypatch.setattr(app, 'Codex', lambda *_: pytest.fail('must not start'))
    with pytest.raises(Attention, match='integrity'):
        app.deliver(cfg, app.receipt(cfg, source['command_id']), source, lambda: None)


def test_partial_archive_remnant_cannot_become_a_new_command(fixture, monkeypatch):
    cfg, settings, tree = fixture
    tree['inbox']['children'].insert(1, node('second', '%second line in same block'))
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    manifest.update(command_source=source)
    manifest.save_effect('dispatch', {'thread_id': 'thread', 'turn_id': 'turn', 'status': 'completed'})
    tree['inbox']['children'].pop(0)
    monkeypatch.setattr(app, 'try_archive', lambda *_: None)
    monkeypatch.setattr(app, 'snapshot', lambda *_: tree)
    monkeypatch.setattr(app, 'queue_command', lambda *_: pytest.fail('already claimed rem'))
    assert app.scan_once(cfg, settings)['queued'] == 0


def test_no_ready_items_means_no_model_execution(fixture, monkeypatch):
    cfg, settings, tree = fixture
    tree['inbox']['children'] = [node('draft', '未优化草稿')]
    monkeypatch.setattr(app, 'snapshot', lambda *_: tree)
    monkeypatch.setattr(app, 'Codex', lambda *_: pytest.fail('no model calls'))
    assert app.scan_once(cfg, settings)['ready'] == 0


def test_pending_archive_is_reported_as_needing_attention(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    manifest.update(command_source=source, archive={'status': 'pending'})
    manifest.save_effect('dispatch', {'thread_id': 'thread', 'turn_id': 'turn', 'status': 'completed'})
    monkeypatch.setattr(app, 'try_archive', lambda *_: None)
    monkeypatch.setattr(app, 'snapshot', lambda *_: tree)
    result = app.scan_once(cfg, settings)
    assert result['status'] == 'needs_attention' and result['issues'][0]['reason'] == 'archive_pending'


def test_lost_prefect_queue_reply_reuses_the_same_idempotency_key(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    observed = []
    def enqueue(*args, **kwargs):
        observed.append(kwargs['idempotency_key'])
        if len(observed) == 1: raise ConnectionError('accepted but reply lost')
        return NS(id='b0d72d13-fb4f-4f98-8b13-108323497ecc')
    monkeypatch.setattr(app, 'run_deployment', enqueue)
    with pytest.raises(ConnectionError): app.queue_command(cfg, source)
    assert app.queue_command(cfg, source) == 'queued'
    assert observed[0] == observed[1]


def test_source_edit_between_scan_and_delivery_does_not_send_old_text(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    app.receipt(cfg, source['command_id']).update(command_source=source)
    tree['inbox']['children'][0] = node('a', '%用户的新文字')
    monkeypatch.setattr(app.Settings, 'load', lambda: cfg)
    monkeypatch.setattr(app.CommandSettings, 'load', lambda *_: settings)
    monkeypatch.setattr(app, 'snapshot', lambda *_: tree)
    monkeypatch.setattr(app, 'Codex', lambda *_: pytest.fail('stale text must not execute'))
    assert app.deliver_command.fn(source['command_id'])['status'] == 'source_changed_before_delivery'


def test_archive_retry_can_run_while_native_worker_keeps_its_delivery_lock(fixture, monkeypatch):
    cfg, settings, tree = fixture
    source, = app.candidates(tree, settings)
    manifest = app.receipt(cfg, source['command_id'])
    manifest.update(command_source=source, archive={'status': 'pending'})
    manifest.save_effect('dispatch', {'thread_id': 'thread', 'turn_id': 'turn', 'status': 'submitted'})
    observed = []
    monkeypatch.setattr(app, 'archive', lambda *args: observed.append(True))
    monkeypatch.setattr(app, 'snapshot', lambda *_: tree)
    with app.FileLock(str(cfg.root/'locks'/('command-'+source['command_id']+'.lock'))):
        app.scan_once(cfg, settings)
    assert observed == [True]
