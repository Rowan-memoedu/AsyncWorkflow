import copy
from pathlib import Path

import pytest

from async_workflow import command_archive as archive, recording_types, splitter
from async_workflow.core import Attention, Manifest, Settings, atomic


def part(key, kind, text, start, stop, **kwargs):
    return dict(key=key, kind=kind, raw_text=text, content_text=text,
                source_spans=[[start, stop]], markdown='# '+text, destination='',
                document_action='append', cancelled=False, **kwargs)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path/'data', artifacts=tmp_path/'out', inbox=tmp_path/'audio',
                   codex=Path('codex'), ffprobe=Path('ffprobe'))
    cfg.prepare()
    store = Manifest(cfg, 'a'*64)
    old = [part('command', 'command', 'Add a canvas.', 0, 13),
           part('note', 'record', 'Unrelated note.', 13, 28)]
    effect = {'status': 'completed_unverified', 'thread_id': 'old-thread', 'turn_id': 'old-turn'}
    store.update(segments=old, effects={'command': effect}, provenance={'recorded_at': '2026-10-19T12:00:00+08:00'})
    atomic(store.folder/'asr.json', {'text': 'Add a canvas.Unrelated note.', 'source_sha256': 'a'*64})
    monkeypatch.setattr(splitter, 'split', lambda *args: [{**old[0], 'kind': 'todo'}, old[1]])
    monkeypatch.setattr(splitter, 'refine', lambda cfg, text: '# '+text)
    return cfg, store, old, copy.deepcopy(effect)


def test_preparation_preserves_submitted_command_and_unrelated_note(setup):
    cfg, store, old, receipt = setup
    folder, before, plan = archive.prepare(cfg, store, 'archive-v1')
    assert len(plan['segments']) == 1
    assert plan['segments'][0]['kind'] == 'todo'
    assert plan['superseded_keys'] == ['command']
    assert store.read()['segments'] == old
    assert store.effect('command') == receipt
    _, _, same = archive.prepare(cfg, store, 'archive-v1')
    assert same == plan
    assert [p['key'] for p in archive.correction_scope(before, plan)['segments']] == ['command']


def test_overlap_closure_includes_whole_old_note_but_not_unrelated_note():
    source = 'PrefixFirst.Second.Other.'
    old = [part('feedback', 'workflow_feedback', 'Prefix', 0, 6),
           part('joined', 'record', 'First.Second.', 6, 19),
           part('other', 'record', 'Other.', 19, 25)]
    new = [part('one', 'record', 'PrefixFirst.', 0, 12),
           part('two', 'record', 'Second.', 12, 19), old[2]]
    selected, keys = archive.affected(new, old, ['feedback'], source)
    assert [p['key'] for p in selected] == ['one', 'two']
    assert keys == {'feedback', 'joined'}


def test_complete_keeps_original_receipt_and_only_switches_active_view(setup):
    cfg, store, old, receipt = setup
    folder, _, plan = archive.prepare(cfg, store, 'archive-v1')
    key = plan['segments'][0]['key']
    store.save_effect(key, {'status': 'verified', 'root_id': 'new-note'})
    archive.complete(store, folder, plan, 'repair-run')
    data = store.read()
    assert data['segments'] == old
    assert data['effects']['command'] == receipt
    assert {p['key'] for p in archive.active_parts(data)} == {'note', key}
    assert archive.replacement(data, 'command')['kind'] == 'archival_correction'
    assert archive.replacement(data, 'note') is None
    assert not recording_types.fully_processed(data)
    store.save_effect('note', {'status': 'verified'})
    assert recording_types.fully_processed(store.read())


def test_repair_refuses_executable_new_parts(setup, monkeypatch):
    cfg, store, old, _ = setup
    monkeypatch.setattr(splitter, 'split', lambda *args: old)
    with pytest.raises(Attention, match='will_not_execute'):
        archive.prepare(cfg, store, 'archive-v1')


def test_control_only_feedback_creates_no_fake_note(setup, monkeypatch):
    cfg, store, old, _ = setup
    monkeypatch.setattr(splitter, 'split', lambda *args: [{**old[0], 'kind': 'record', 'content_text': ''}, old[1]])
    _, _, plan = archive.prepare(cfg, store, 'archive-v1')
    assert not plan['segments']
    assert len(plan['control_only']) == 1


def test_wrong_source_binding_is_rejected(setup):
    cfg, store, _, _ = setup
    atomic(store.folder/'asr.json', {'text': 'Add a canvas.Unrelated note.', 'source_sha256': 'b'*64})
    with pytest.raises(Attention, match='successful_source'):
        archive.prepare(cfg, store, 'archive-v1')


def test_pending_correction_does_not_hide_original_execution(setup):
    _, store, _, _ = setup
    data = store.read()
    data['command_archive'] = {'completed': False, 'superseded_keys': ['command'], 'segments': []}
    assert archive.replacement(data, 'command') is None
    assert [p['key'] for p in archive.active_parts(data)] == ['command', 'note']


def test_completed_empty_source_is_not_claimed_as_a_note():
    from async_workflow.dashboard import classify
    data = {'segments': [], 'command_archive': {'completed': True, 'superseded_keys': [], 'segments': []}}
    assert classify(data, {}, True) == 'no_content'


def test_archive_stops_recovery_of_old_unknown_command(setup):
    from types import SimpleNamespace
    from async_workflow.recovery import unresolved_completed
    _, store, _, _ = setup
    data = store.read()
    data.update(segment_runs={'command': 'old-run'}, command_archive={
        'completed': True, 'operation': 'repair', 'superseded_keys': ['command'], 'segments': []})
    data['effects']['command']['status'] = 'outcome_unknown'
    run = SimpleNamespace(id='old-run', state=SimpleNamespace(is_completed=lambda: True))
    assert not unresolved_completed(run, data)


def test_explicit_template_branch_wins_without_using_topic_mentions():
    from async_workflow.routing import explicit_daily_path
    paths = [['开发需求类'], ['其他碎片'], ['日程', 'to do list'], ['每日总结', '学习日志']]
    assert explicit_daily_path('开发需求', '正文', paths) == ['开发需求类']
    assert explicit_daily_path('', '呃在我今天的开发需求里面去加一个：修复快捷键。', paths) == ['开发需求类']
    assert explicit_daily_path('', '往其他碎片里记下这一点。', paths) == ['其他碎片']
    assert explicit_daily_path('', '我在开发需求界面看到了按钮。', paths) is None
    assert explicit_daily_path('另一份开发需求文档', '正文', paths) is None
    assert explicit_daily_path('to do list', '加一条以后做的事项。', paths) is None
