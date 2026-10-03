import asyncio
from pathlib import Path

import pytest

from async_workflow import routing, routing_rules
from async_workflow.core import Attention, Settings, atomic


@pytest.fixture
def cfg(tmp_path):
    cfg=Settings(root=tmp_path/'state',ffprobe=Path('ffprobe'),codex=Path('codex'),template_id='template')
    atomic(cfg.root/'daily-template.json',{'template_id':'template','version':'v1',
        'tree':{'children':[{'title':'其他碎片','children':[]}]}})
    return cfg


def rule(name='exercise', target='training-book', **changes):
    return {'id':name,'condition':'Records of physical exercise go in the training book.',
            'target_id':target,'target_title':'Training book','source':'An explicit user request.',
            'accepted_at':'2026-10-03','enabled':True,**changes}


def save(cfg, *rules):
    atomic(cfg.root/'routing-rules.json',{'schema_version':1,'rules':list(rules)})


def plan(cfg, content='I rode a bicycle for half an hour.', requested='', **item):
    async def call(session,method,**args):
        assert method=='search_documents'
        return {'candidates':[{'id':'training-book','title':'Training book','ancestors':[]}]}
    return asyncio.run(routing.plan(cfg,None,call,requested,content,
        {'recorded_at':'2026-10-03T12:00:00+08:00'},item=item))


def test_persistent_rule_survives_new_settings_and_keeps_evidence(cfg,monkeypatch):
    save(cfg,rule())
    seen=[]
    def ask(c,s,instruction,payload,validator):
        seen.append(payload)
        return validator(routing_rules.RuleChoice(matches=[routing_rules.RuleMatch(rule_id='exercise',evidence='rode a bicycle')],reason='exercise'))
    monkeypatch.setattr(routing,'ask',ask)
    first=plan(cfg)
    second=plan(Settings.model_validate(cfg.model_dump()))
    assert first==second
    assert first['parent_id']=='training-book' and first['persistent_rule_ids']==['exercise']
    assert first['rule_evidence'][0]['evidence']=='rode a bicycle'
    assert all(set(p['rules'][0])=={'id','condition'} for p in seen)


def test_no_match_keeps_daily_routing(cfg,monkeypatch):
    save(cfg,rule())
    def ask(c,s,instruction,payload,validator):
        if s is routing_rules.RuleChoice:
            return validator(s(matches=[],reason='unrelated'))
        return validator(s(branch_path=['其他碎片'],cross_category=False,reason='general note'))
    monkeypatch.setattr(routing,'ask',ask)
    assert plan(cfg,'Clouds look like ships today.')['branch_path']==['其他碎片']


def test_explicit_destination_overrides_persistent_defaults(cfg,monkeypatch):
    save(cfg,rule())
    async def choose(*a): return {'type':'document','parent_id':'explicit-book'}
    monkeypatch.setattr(routing,'choose_document',choose)
    monkeypatch.setattr(routing,'ask',lambda *a,**kw:pytest.fail('explicit destination must skip rule matching'))
    assert plan(cfg,requested='My daily diary')['parent_id']=='explicit-book'
    assert plan(cfg,requested='New book',document_action='create')['type']=='create_document'


@pytest.mark.parametrize('kind',['shopping','information','todo','command'])
def test_specialized_routes_do_not_enter_default_rules(cfg,monkeypatch,kind):
    save(cfg,rule())
    monkeypatch.setattr(routing,'ask',lambda *a,**kw:pytest.fail('specialized item entered rule matching'))
    assert routing_rules.choose(cfg,'ride a bicycle',{'kind':kind},routing.ask) is None


@pytest.mark.parametrize('match',[
    {'rule_id':'fabricated','evidence':'rode a bicycle'},
    {'rule_id':'exercise','evidence':'text not in source'},
    {'rule_id':'exercise','evidence':' '},
])
def test_unknown_rule_or_invented_evidence_is_rejected(cfg,monkeypatch,match):
    save(cfg,rule())
    def ask(c,s,instruction,payload,validator): return validator(s(matches=[match],reason='x'))
    monkeypatch.setattr(routing,'ask',ask)
    with pytest.raises(ValueError): plan(cfg)


def test_conflicting_targets_fail_before_any_write(cfg,monkeypatch):
    save(cfg,rule(),rule('another','another-book'))
    def ask(c,s,instruction,payload,validator):
        return validator(s(matches=[{'rule_id':id,'evidence':'rode a bicycle'}
            for id in ('exercise','another')],reason='both'))
    monkeypatch.setattr(routing,'ask',ask)
    with pytest.raises(Attention,match='rule_conflict'): plan(cfg)


def test_missing_or_disabled_rules_need_no_model_call(cfg,monkeypatch):
    monkeypatch.setattr(routing,'ask',lambda *a,**kw:pytest.fail('inactive rules need no matching'))
    assert routing_rules.choose(cfg,'some note',{},routing.ask) is None
    save(cfg,rule(enabled=False))
    assert routing_rules.choose(cfg,'some note',{},routing.ask) is None


def test_invalid_rule_configuration_is_not_silently_ignored(cfg):
    save(cfg,rule(),rule())
    with pytest.raises(Attention,match='rules_invalid'): routing_rules.snapshot(cfg)


@pytest.mark.parametrize('candidate',[
    {'id':'another-book','title':'Training book','ancestors':[]},
    {'id':'training-book','title':'Training book','ancestors':['trash']},
])
def test_bound_target_must_exist_outside_trash(cfg,monkeypatch,candidate):
    save(cfg,rule())
    def ask(c,s,instruction,payload,validator):
        return validator(s(matches=[{'rule_id':'exercise','evidence':'bicycle'}],reason='exercise'))
    monkeypatch.setattr(routing,'ask',ask)
    async def call(*a,**kw): return {'candidates':[candidate]}
    with pytest.raises(Attention,match='target_unavailable'):
        asyncio.run(routing.plan(cfg,None,call,'','I rode a bicycle.',
            {'recorded_at':'2026-10-03T12:00:00+08:00'}))
