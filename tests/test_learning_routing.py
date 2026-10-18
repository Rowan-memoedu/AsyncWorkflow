import asyncio
from pathlib import Path

import pytest

from async_workflow import routing
from async_workflow.core import Settings, atomic


@pytest.fixture
def cfg(tmp_path):
    cfg=Settings(root=tmp_path/'state',ffprobe=Path('ffprobe'),codex=Path('codex'),template_id='template')
    branches=[{'title':title,'children':[]} for title in
        ('每日碎碎念','认知科学相关','一些注定重复的思考')]
    atomic(cfg.root/'daily-template.json',{'template_id':'template','version':'v1','tree':{'children':[
        {'title':'每日总结','children':[{'title':'学习日志','children':branches}]},
        {'title':'其他碎片','children':[]}]}})
    return cfg


def path(leaf):
    return ['每日总结','学习日志',leaf]


def plan(cfg, text, requested=''):
    async def forbidden(*a,**kw): pytest.fail('daily learning routing must not search or write documents')
    return asyncio.run(routing.plan(cfg,None,forbidden,requested,text,
        {'recorded_at':'2026-10-17T17:00:00+00:00'}))


def test_subject_thought_goes_to_repeat_branch_before_default_rule(cfg,monkeypatch):
    text='While studying orthogonal projection, I noticed diagrams help me remember the proof.'
    def ask(c,s,instruction,payload,validator):
        assert s is routing.DailyChoice
        return validator(s(branch_path=path('认知科学相关'),cross_category=False,reason='thought while studying',
            learning_basis='study_idea',learning_evidence='I noticed diagrams help me remember the proof',
            subject_evidence='studying orthogonal projection'))
    monkeypatch.setattr(routing,'ask',ask)
    monkeypatch.setattr(routing.routing_rules,'choose',lambda *a:pytest.fail('learning route already determined'))
    result=plan(cfg,text)
    assert result['branch_path']==path('一些注定重复的思考')
    assert result['recorded_day']=='2026-10-18'
    assert result['subject_evidence']=='studying orthogonal projection'


@pytest.mark.parametrize('evidence',['','invented subject'])
def test_implicit_learning_requires_concrete_subject_source(cfg,monkeypatch,evidence):
    def ask(c,s,instruction,payload,validator):
        return validator(s(branch_path=path('一些注定重复的思考'),cross_category=False,reason='generic thought',
            learning_basis='study_idea',learning_evidence='An idea while studying',subject_evidence=evidence))
    monkeypatch.setattr(routing,'ask',ask)
    with pytest.raises(ValueError,match='concrete subject'):
        plan(cfg,'An idea while studying: organize life differently.')


def test_generic_learning_words_do_not_admit_learning_log(cfg,monkeypatch):
    def ask(c,s,instruction,payload,validator):
        return validator(s(branch_path=path('认知科学相关'),cross_category=False,
            reason='no concrete study or explicit destination',learning_basis='none'))
    monkeypatch.setattr(routing,'ask',ask)
    assert plan(cfg,'The app has a learning log feature and memory hints.')['branch_path']==['其他碎片']


def test_explicit_log_root_chooses_corresponding_subcategory_without_document_search(cfg,monkeypatch):
    requested='Record this in my learning log.'
    def ask(c,s,instruction,payload,validator):
        if s is routing.LearningTarget:
            return validator(s(branch_path=['每日总结','学习日志'],evidence='learning log',reason='explicit log'))
        return validator(s(branch_path=path('每日碎碎念'),cross_category=False,reason='learning progress',
            learning_basis='explicit_request',learning_evidence=requested))
    monkeypatch.setattr(routing,'ask',ask)
    result=plan(cfg,'I practiced solving cubic equations for thirty minutes.',requested)
    assert result['branch_path']==path('每日碎碎念')
    assert result['learning_evidence']==requested


def test_explicit_approximate_leaf_resolves_existing_static_path(cfg,monkeypatch):
    def ask(c,s,instruction,payload,validator):
        assert s is routing.LearningTarget
        return validator(s(branch_path=path('一些注定重复的思考'),evidence='注定重复的想法',reason='same branch'))
    monkeypatch.setattr(routing,'ask',ask)
    assert plan(cfg,'A thought about prime factorization.','学习日志下面注定重复的想法')['branch_path']==path('一些注定重复的思考')


def test_explicit_cognitive_branch_overrides_default_thought_subtype(cfg,monkeypatch):
    def ask(c,s,instruction,payload,validator):
        return validator(s(branch_path=path('认知科学相关'),evidence='认知科学相关',reason='specified leaf'))
    monkeypatch.setattr(routing,'ask',ask)
    assert plan(cfg,'A thought while studying geometry.','学习日志/认知科学相关')['branch_path']==path('认知科学相关')


def test_other_document_with_learning_log_in_its_name_keeps_document_route(cfg,monkeypatch):
    def ask(c,s,instruction,payload,validator):
        return validator(s(branch_path=[],reason='a separate project document'))
    async def choose(*a): return {'type':'document','parent_id':'project-document'}
    monkeypatch.setattr(routing,'ask',ask)
    monkeypatch.setattr(routing,'choose_document',choose)
    assert plan(cfg,'A log system design.','学习日志软件设计') ['parent_id']=='project-document'


@pytest.mark.parametrize('target,evidence',[
    (['学习日志','invented child'],'学习日志'),
    (path('每日碎碎念'),'fabricated quote'),
])
def test_spoken_learning_target_requires_real_path_and_source(cfg,monkeypatch,target,evidence):
    def ask(c,s,instruction,payload,validator):
        return validator(s(branch_path=target,evidence=evidence,reason='invalid'))
    monkeypatch.setattr(routing,'ask',ask)
    with pytest.raises(ValueError): plan(cfg,'Some note.','学习日志')


def test_explicit_log_target_cannot_fall_back_outside_learning_log(cfg,monkeypatch):
    def ask(c,s,instruction,payload,validator):
        if s is routing.LearningTarget:
            return validator(s(branch_path=['每日总结','学习日志'],evidence='学习日志',reason='explicit log'))
        return validator(s(branch_path=['其他碎片'],cross_category=False,reason='unsure'))
    monkeypatch.setattr(routing,'ask',ask)
    with pytest.raises(ValueError,match='requested branch'): plan(cfg,'Some note.','学习日志')
