import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import pytest
from async_workflow import repairs, splitter, asr
from async_workflow.core import Settings, Manifest, atomic


@pytest.fixture
def setup(tmp_path,monkeypatch):
    cfg=Settings(root=tmp_path/'state',artifacts=tmp_path/'out',inbox=tmp_path/'audio',codex=Path('codex'),ffprobe=Path('ffprobe'),trash_rem_id='trash')
    cfg.prepare()
    store=Manifest(cfg,'a'*64)
    part={'index':0,'key':'old','kind':'record','route':'remnote','raw_text':'Existing accepted words.',
          'markdown':'# Accepted title\n- Accepted words','source_spans':[[0,24]],'destination':'','cancelled':False}
    store.update(segments=[part],provenance={'recorded_at':'2026-10-03T00:00:00+08:00'},
                 effects={'old':{'backend':'independent-remnote-plugin','status':'verified','root_id':'owned-root','parent':'old-parent',
                                 'routing':{'type':'document','parent_id':'old-parent','recorded_day':'2026-10-03'}}})
    atomic(store.folder/'asr.json',{'text':'Existing accepted words. New separate thought.'})
    atomic(store.folder/'outline.md','# Previously accepted whole outline',text=True)
    monkeypatch.setattr(asr,'recognize',lambda *a,**k:pytest.fail('a repair must not call ASR'))
    return cfg,store,part


def test_routing_repair_reuses_accepted_text_without_split_or_refine(setup,monkeypatch):
    cfg,store,part=setup
    monkeypatch.setattr(splitter,'split',lambda *a,**k:pytest.fail('routing repair must not split'))
    monkeypatch.setattr(splitter,'refine',lambda *a,**k:pytest.fail('routing repair must not refine'))
    _,_,plan=repairs.prepare(cfg,store,'routing','revision-a')
    assert plan['segments'][0]['markdown']==part['markdown']
    assert plan['segments'][0]['reuses_effect']=='old'
    assert plan['reused_asr'] and plan['new_outline_calls']==0


def test_split_repair_refines_only_newly_extracted_content(setup,monkeypatch):
    cfg,store,part=setup
    calls=[]
    def split(c,text,outline):
        assert text=='Existing accepted words. New separate thought.'
        assert outline=='# Previously accepted whole outline'
        return [dict(part),{**part,'index':1,'raw_text':'New separate thought.','markdown':'','source_spans':[[25,46]]}]
    monkeypatch.setattr(splitter,'split',split)
    monkeypatch.setattr(splitter,'refine',lambda c,text:calls.append(text) or '# New thought')
    _,_,plan=repairs.prepare(cfg,store,'split','revision-b')
    assert calls==['New separate thought.']
    assert plan['segments'][0]['markdown']==part['markdown'] and plan['new_outline_calls']==1
    _,_,reused=repairs.prepare(cfg,store,'split','revision-b')
    assert reused==plan and calls==['New separate thought.']


def test_correction_reuses_owned_root_and_does_not_repeat_verified_steps(setup,monkeypatch):
    cfg,store,_=setup
    folder,before,plan=repairs.prepare(cfg,store,'routing','revision-c')
    calls=[]
    @asynccontextmanager
    async def connect(c): yield object()
    async def route(*a,**k): return {'type':'document','parent_id':'correct-parent','recorded_day':'2026-10-03'}
    async def rpc(session,method,**args):
        calls.append((method,args))
        assert method=='relocate_outline' and args['effect_key']=='a'*64+'/old'
        return {'status':'verified','root_id':'owned-root','metadata_archived_id':'old-metadata'}
    monkeypatch.setattr(repairs.remnote,'connection',connect)
    monkeypatch.setattr(repairs.remnote,'call',rpc)
    monkeypatch.setattr(repairs.routing,'plan',route)
    asyncio.run(repairs.apply(cfg,store,folder,before,plan))
    asyncio.run(repairs.apply(cfg,store,folder,before,plan))
    assert len(calls)==1
    corrected=store.effect(plan['segments'][0]['key'])
    assert corrected['root_id']=='owned-root' and corrected['parent']=='correct-parent'
    assert store.effect('old')['parent']=='old-parent'


def test_repair_never_replays_a_command(setup):
    cfg,store,part=setup
    store.update(segments=[{**part,'kind':'command','route':'codex'}])
    with pytest.raises(Exception,match='command_repair_requires_existing_thread'):
        repairs.prepare(cfg,store,'split','unsafe-replay')


def test_unfinished_repair_blocks_another_operation_but_allows_same_resume(setup):
    cfg,store,_=setup
    folder,_,first=repairs.prepare(cfg,store,'routing','in-progress')
    with pytest.raises(Exception,match='unfinished_repair_requires_same_operation:in-progress'):
        repairs.prepare(cfg,store,'publish','different-version')
    _,_,resumed=repairs.prepare(cfg,store,'routing','in-progress')
    assert resumed == first
    atomic(folder/'plan.json',{**first,'completed':True})
    repairs.prepare(cfg,store,'publish','next-repair')


def test_split_repair_does_not_reintroduce_separated_feedback(setup,monkeypatch):
    from async_workflow import feedback
    from async_workflow.core import digest
    cfg,store,part=setup
    text='Existing accepted words. Secretary, update recording routing.'
    atomic(store.folder/'asr.json', {'text':text})
    value=feedback.FeedbackPartition(spans=[{'unit':0,'quote':'Secretary, update recording routing.'}],request='Update recording routing.',reason='fixture')
    extracted=feedback.partition(value,text)
    atomic(store.folder/'feedback.json',{**extracted,'source_hash':digest(text)})
    def split(c,body,outline):
        assert body==extracted['remaining_text']
        assert 'update recording routing' not in body
        return [dict(part)]
    monkeypatch.setattr(splitter,'split',split)
    _,_,plan=repairs.prepare(cfg,store,'split','feedback-aware')
    assert plan['segments'][0]['reuses_effect']=='old'
    assert plan['new_outline_calls']==0
