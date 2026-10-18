import asyncio
from pathlib import Path

import pytest

from async_workflow import focus_routing, routing, splitter
from async_workflow.core import Attention, Settings, atomic


@pytest.fixture
def cfg(tmp_path):
    cfg=Settings(root=tmp_path/'state',ffprobe=Path('ffprobe'),codex=Path('codex'),template_id='template')
    categories={key:{'rem_id':'global-'+key,'title':key,
        'daily_path':[] if key=='habit' else ['日程',key]} for key in ('goal','plan','todo','habit')}
    atomic(cfg.root/'focus-targets.json',{'schema_version':1,'document_id':'focus-document',
        'document_title':'Focus items','categories':categories})
    atomic(cfg.root/'daily-template.json',{'template_id':'template','version':'focus-v1',
        'tree':{'children':[{'title':'日程','children':[
            {'title':key,'children':[]} for key in ('goal','plan','todo')]},
            {'title':'其他碎片','children':[]}]}})
    return cfg


def item(category='goal',scope='today',**overrides):
    return {'kind':'record','focus_kind':category,'focus_scope':scope,
        'raw_text':f'{category}: Today do one useful thing. Tomorrow do another.',
        'focus_evidence':category+':','focus_time_evidence':'' if scope=='unspecified' else 'Today',
        'focus_date':'','index':0,**overrides}


def plan(cfg,part,day='2026-10-17'):
    async def forbidden(*a,**kw): pytest.fail('bound focus routing must not call model or RemNote')
    return asyncio.run(routing.plan(cfg,None,forbidden,'',part['raw_text'],
        {'recorded_at':day+'T10:00:00+08:00'},item=part))


@pytest.mark.parametrize('category',['goal','plan','todo'])
def test_today_categories_use_corresponding_daily_branch(cfg,monkeypatch,category):
    monkeypatch.setattr(routing,'ask',lambda *a,**kw:pytest.fail('typed route is already known'))
    route=plan(cfg,item(category))
    assert route['type']=='daily' and route['branch_path']==['日程',category]
    assert route['recorded_day']=='2026-10-17'


@pytest.mark.parametrize('category',['goal','plan','todo','habit'])
@pytest.mark.parametrize('scope',['unspecified','other'])
def test_non_today_and_undated_categories_use_global_bound_nodes(cfg,category,scope):
    route=plan(cfg,item(category,scope))
    assert route['type']=='document' and route['parent_id']=='global-'+category
    assert route['focus_document_id']=='focus-document'


def test_habit_never_uses_daily_even_when_today_is_explicit(cfg):
    route=plan(cfg,item('habit','today'))
    assert route['parent_id']=='global-habit' and route['reason']=='habit_always_global'


def test_habit_stays_global_even_if_daily_destination_is_present(cfg,monkeypatch):
    monkeypatch.setattr(routing,'ask',lambda *a,**kw:pytest.fail('Habit cannot enter Daily Note'))
    part=item('habit','today')
    route=asyncio.run(routing.plan(cfg,None,None,'Daily Note / habit',part['raw_text'],
        {'recorded_at':'2026-10-17T10:00:00+08:00'},item=part))
    assert route['parent_id']=='global-habit'


def test_explicit_date_compares_with_recording_day_instead_of_processing_day(cfg):
    part=item('plan','date',raw_text='plan: On October 2, 2020 do one useful thing.',
        focus_time_evidence='October 2, 2020',focus_date='2020-10-02')
    assert plan(cfg,part,'2020-10-02')['type']=='daily'
    assert plan(cfg,part,'2020-10-03')['parent_id']=='global-plan'


@pytest.mark.parametrize('changes',[
    {'focus_evidence':'not in source'},
    {'focus_time_evidence':''},
    {'focus_scope':'made-up'},
    {'focus_scope':'date','focus_date':'2026-02-30'},
])
def test_invalid_source_or_time_scope_fails_before_write(cfg,changes):
    with pytest.raises(Attention): plan(cfg,item(**changes))


def test_missing_targets_and_missing_daily_branch_are_visible(cfg):
    alternate=cfg.model_copy(update={'root':cfg.root/'absent'})
    with pytest.raises(Attention,match='targets_not_synced'):
        focus_routing.plan(alternate,item(),'2026-10-17',[['日程','goal']])
    with pytest.raises(Attention,match='daily_branch_not_synced'):
        focus_routing.plan(cfg,item(),'2026-10-17',[])


def test_same_declared_list_can_split_today_and_other_days_without_losing_content(cfg):
    text='To do list\nToday replace the bulb.\nTomorrow book a ticket.'
    parsed=splitter.VoiceDumpSplitResult(items=[
        splitter.SplitVoiceItem(kind='record',spans=[{'unit':0,'role':'control'},{'unit':1}],
            reason='today part of one list',focus_kind='todo',focus_scope='today',
            focus_evidence='To do list',focus_time_evidence='Today'),
        splitter.SplitVoiceItem(kind='record',spans=[{'unit':2}],
            reason='tomorrow part of the same list',focus_kind='todo',focus_scope='other',
            focus_from=0,focus_evidence='To do list',focus_time_evidence='Tomorrow'),
    ])
    parts=splitter.validate_split(parsed,text)
    assert parts[0]['content_text']=='Today replace the bulb.\n'
    assert parts[1]['content_text']=='Tomorrow book a ticket.'
    assert plan(cfg,parts[0])['branch_path']==['日程','todo']
    assert plan(cfg,parts[1])['parent_id']=='global-todo'


@pytest.mark.parametrize('changes',[
    {'kind':'command','route':'codex','intent_evidence':'Plan'},
    {'focus_time_evidence':'invented time'},
    {'focus_from':0},
    {'focus_scope':'date','focus_date':'2020-15-99'},
])
def test_splitter_rejects_fabricated_focus_metadata(changes):
    values={'kind':'record','spans':[{'unit':0}],'reason':'test','focus_kind':'plan',
        'focus_scope':'today','focus_evidence':'Plan','focus_time_evidence':'today',**changes}
    value=splitter.VoiceDumpSplitResult(items=[values])
    with pytest.raises(ValueError): splitter.validate_split(value,'Plan today, write one paragraph.')


def test_old_untyped_segments_keep_no_focus_route(cfg):
    assert focus_routing.plan(cfg,{'kind':'todo'},'2026-10-17',[]) is None


def test_explicit_document_still_overrides_declared_focus_default(cfg,monkeypatch):
    async def choose(*a): return {'type':'document','parent_id':'specified-document'}
    monkeypatch.setattr(routing,'choose_document',choose)
    route=asyncio.run(routing.plan(cfg,None,None,'My project','Goal today: useful work.',
        {'recorded_at':'2026-10-17T10:00:00+08:00'},item=item()))
    assert route['parent_id']=='specified-document'
