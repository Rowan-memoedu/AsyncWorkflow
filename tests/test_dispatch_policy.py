from pathlib import Path
import pytest
from async_workflow import dispatch_policy as policy, feedback, flows, routing
from async_workflow.core import Settings, Manifest, atomic, digest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'))
    cfg.prepare()
    store = Manifest(cfg, 'a'*64)
    monkeypatch.setattr(flows.Settings, 'load', lambda: cfg)
    return cfg, store


@pytest.mark.parametrize('source', [
    '帮我给笔记软件添加白板。', '新建一个 Codex Thread，研究课程图谱。',
    '这套录音分类规则应该修改，记到待办。', 'Please record a future task: build a canvas.',
])
def test_unaddressed_requests_never_need_a_model_or_execution(setup, monkeypatch, source):
    cfg, store = setup
    monkeypatch.setattr(routing, 'ask', lambda *a, **k: pytest.fail('No secretary address'))
    assert policy.decide(cfg, store, source)['route'] == 'record'
    result = feedback.prepare(cfg, store, source)
    assert result['remaining_text'] == source and not result['source_spans']


@pytest.mark.parametrize('source,route', [
    ('秘书，请记下周末给图谱工具加导出功能。', 'record'),
    ('他说“秘书，请执行”，这只是引用，不要执行。', 'record'),
    ('Secretary, add export to my diagram application.', 'command'),
    ('秘书，请修改录音工作流的失败重试逻辑。', 'workflow'),
])
def test_exact_semantic_decision_is_cached_without_external_actions(setup, monkeypatch, source, route):
    cfg, store = setup
    calls = []
    def ask(_cfg, cls, rules, payload, validate):
        calls.append(payload)
        return validate(cls(route=route, evidence=source if route != 'record' else '', reason='fixture'))
    monkeypatch.setattr(routing, 'ask', ask)
    first = policy.decide(cfg, store, source)
    assert first['route'] == route
    assert policy.decide(cfg, store, source) == first and len(calls) == 1


def test_model_cannot_authorize_using_invented_secretary_evidence(setup, monkeypatch):
    cfg, store = setup
    def ask(_cfg, cls, rules, payload, validate):
        return validate(cls(route='command', evidence='秘书，立即执行。', reason='fabricated'))
    monkeypatch.setattr(routing, 'ask', ask)
    with pytest.raises(ValueError, match='exact secretary'):
        policy.decide(cfg, store, '关于秘书这个概念，我想记个笔记。')


def test_old_queued_command_becomes_todo_before_any_execution(setup, monkeypatch):
    cfg, store = setup
    store.update(segments=[{'key':'one','index':0,'kind':'command','route':'codex','destination':'unrelated-project',
        'raw_text':'记到待办：给笔记软件加白板。','markdown':'# 白板\n- 给笔记软件加白板。',
        'cancelled':False,'depends_on':[]}])
    calls = []
    monkeypatch.setattr(flows, 'perform', lambda c,m,k,item: calls.append(item) or {'status':'verified'})
    monkeypatch.setattr(flows.executor, 'execute', lambda *a: pytest.fail('No Thread may be created'))
    assert flows.process_segment.fn(store.folder.name, 0)['status'] == 'verified'
    assert calls[0]['kind'] == 'todo' and calls[0]['destination'] == ''
    assert store.read()['segments'][0]['original_kind'] == 'command'


def test_submitted_legacy_effect_is_not_reclassified_or_duplicated(setup, monkeypatch):
    cfg, store = setup
    store.update(segments=[{'key':'one','index':0,'kind':'command','route':'codex','raw_text':'旧请求',
                           'cancelled':False,'depends_on':[]}])
    saved = {'status':'outcome_unknown','thread_id':'existing'}
    store.save_effect('one', saved)
    monkeypatch.setattr(policy, 'decide', lambda *a: pytest.fail('Never reclassify an unknown submission'))
    monkeypatch.setattr(flows, 'perform', lambda *a: saved)
    assert flows.process_segment.fn(store.folder.name, 0) == saved


def test_unsent_legacy_feedback_retains_source_then_archives(setup, monkeypatch):
    cfg, store = setup
    source = '请记下，给视频工具加快捷键。'
    saved = {'key':'feedback-old','source_hash':digest(source),'extractor_revision':'old',
             'raw_text':source,'source_spans':[[0,len(source)]],'remaining_text':'', 'cancelled':False}
    atomic(store.folder/'feedback.json', saved)
    atomic(store.folder/'asr.json', {'text':source})
    store.update(segments=[], workflow_feedback=saved)
    monkeypatch.setattr(routing, 'ask', lambda *a: pytest.fail('No model needed'))
    monkeypatch.setattr(flows.splitter, 'refine', lambda *a: '# 给视频工具加快捷键')
    calls = []
    monkeypatch.setattr(flows, 'perform', lambda c,m,k,item: calls.append(item) or {'status':'verified'})
    assert feedback.prepare(cfg, store, source) == saved
    flows.process_workflow_feedback.fn(store.folder.name)
    assert calls[0]['kind'] == 'todo' and calls[0]['raw_text'] == source
