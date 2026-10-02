import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import pytest
from async_workflow import routing, remnote
from async_workflow.core import Settings, Manifest, UnknownOutcome, atomic

@pytest.fixture
def cfg(tmp_path):
    cfg=Settings(root=tmp_path/'state',artifacts=tmp_path/'artifacts',inbox=tmp_path/'audio',
                 ffprobe=Path('ffprobe'),codex=Path('codex'),template_id='template')
    cfg.prepare()
    tree={'title':'日记模板','children':[{'title':'每日复盘','children':[{'title':'经验','children':[]}]},
                                      {'title':'其他碎片','children':[]},{'title':'','children':[]}]}
    atomic(cfg.root/'daily-template.json',{'template_id':'template','version':'v1','tree':tree})
    return cfg

def test_calendar_day_uses_recording_time_not_processing_time():
    assert routing.recorded_day({'recorded_at':'2026-09-30T16:01:00+00:00'})=='2026-10-01'
    assert routing.recorded_day({'recorded_at':'2026-09-30T15:59:00+00:00'})=='2026-09-30'

def test_template_paths_skip_empty_nodes(cfg):
    _,paths=routing.snapshot(cfg)
    assert paths==[['每日复盘'],['每日复盘','经验'],['其他碎片']]

def test_no_target_and_indivisible_categories_go_to_other_without_io(cfg,monkeypatch):
    def ask(c,s,i,p,validator):
        return validator(routing.DailyChoice(branch_path=['每日复盘'],cross_category=True,reason='whole thought'))
    monkeypatch.setattr(routing,'ask',ask)
    async def forbidden(*a,**k): pytest.fail('daily/template must not be read during classification')
    r=asyncio.run(routing.plan(cfg,None,forbidden,'','one integrated thought',{'recorded_at':'2026-10-01T21:00:00+08:00'}))
    assert r['branch_path']==['其他碎片'] and r['recorded_day']=='2026-10-01'

def test_no_candidates_go_to_other_and_keep_spoken_target(cfg,monkeypatch):
    monkeypatch.setattr(routing,'ask',lambda *a,**k:routing.Queries(queries=['approximate name']))
    async def call(session,method,**args):
        assert method=='search_documents'
        return {'candidates':[]}
    r=asyncio.run(routing.plan(cfg,None,call,'approximate document','thought',{'recorded_at':'2026-10-01T00:00:00+08:00'}))
    assert r['branch_path']==['其他碎片'] and r['requested']=='approximate document'

def test_ambiguous_documents_use_first_ranked_immediately(cfg,monkeypatch):
    def ask(c,s,i,p,validator=lambda x:x):
        if s is routing.Queries: return routing.Queries(queries=['name'])
        return validator(routing.DocumentChoice(ranked_ids=['b','a'],uncertainty='high',reason='close titles'))
    monkeypatch.setattr(routing,'ask',ask)
    async def call(session,method,**args): return {'candidates':[{'id':'a','title':'A'},{'id':'b','title':'B'}]}
    r=asyncio.run(routing.plan(cfg,None,call,'name','thought',{'recorded_at':'2026-10-01T00:00:00+08:00'}))
    assert r['parent_id']=='b' and r['review_eligible']

def test_publish_lost_reply_reuses_same_plugin_effect_without_rerouting(cfg,monkeypatch):
    store=Manifest(cfg,'c'*64)
    store.update(provenance={'recorded_at':'2026-10-01T00:00:00+08:00'})
    calls=[]
    @asynccontextmanager
    async def connect(cfg): yield object()
    async def plan(*a,**k): return {'type':'document','parent_id':'parent','recorded_day':'2026-10-01'}
    async def call(session,method,**args):
        assert method=='publish_outline'
        calls.append(args)
        if len(calls)==1: raise UnknownOutcome('reply lost after plugin completed')
        return {'status':'verified','root_id':'same-owned-root'}
    monkeypatch.setattr(remnote,'connection',connect)
    monkeypatch.setattr(remnote,'call',call)
    monkeypatch.setattr(routing,'plan',plan)
    with pytest.raises(UnknownOutcome): asyncio.run(remnote.publish(cfg,store,'part','# One\n- Child','doc','source'))
    monkeypatch.setattr(routing,'plan',lambda *a,**k:pytest.fail('must keep original target'))
    result=asyncio.run(remnote.publish(cfg,store,'part','# One\n- Child','doc','source'))
    assert result['status']=='verified' and calls[0]==calls[1]

def test_partial_legacy_mcp_effect_is_never_recreated(cfg):
    store=Manifest(cfg,'d'*64)
    store.save_effect('part',{'status':'submitted','root_id':'old-root'})
    with pytest.raises(UnknownOutcome,match='legacy_remnote'):
        asyncio.run(remnote.publish(cfg,store,'part','# One','','source'))
