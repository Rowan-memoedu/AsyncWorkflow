from pathlib import Path
import json
import pytest
from async_workflow import conversation as meeting, conversation_asr as audio, repairs
from async_workflow.core import Settings, Manifest, Attention, UnknownOutcome, atomic, file_hash


@pytest.fixture
def cfg(tmp_path):
    value=Settings(root=tmp_path/'state',inbox=tmp_path/'in',artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'),codex=Path('codex'),
                   conversation_channel_profiles={'Call Test_*':{'0':'Lee','1':'Sam'}})
    value.prepare()
    return value


def receipt(*sentences):
    return {'events':[{'output':{'sentence':s}} for s in sentences]}


def final(identity,text,words,start=0):
    return {'sentence_id':identity,'sentence_end':True,'text':text,'words':words,'begin_time':start}


def word(text,begin,end):
    return {'text':text,'begin_time':begin,'end_time':end,'punctuation':''}


def test_cumulative_word_snapshots_keep_full_text_without_dropping_real_repetition():
    a=word('Yes',100,200); b=word(' today',300,500); repeat=word('Yes',800,1000)
    result=audio.words_from_receipt(receipt(final(1,'Yes',[a]),final(2,'Yes today',[a,b]),
                                          final(3,'Yes',[repeat],start=800)))
    assert [w['text'] for w in result]==['Yes',' today','Yes']
    assert [w['start_ms'] for w in result]==[100,300,800]


@pytest.mark.parametrize('sentence',[
    final(1,'Lost words',[word('Lost',0,100)]),
    final(1,'Bad',[word('Bad',0,0)]),
])
def test_missing_words_or_invalid_timing_stop_instead_of_inventing_alignment(sentence):
    with pytest.raises(Attention): audio.words_from_receipt(receipt(sentence))


def test_unfinished_stream_does_not_become_completed_transcript():
    with pytest.raises(Attention,match='unfinished'):
        audio.words_from_receipt(receipt({'sentence_id':2,'text':'pending','sentence_end':False}))
    with pytest.raises(Attention,match='incomplete_sse'):
        list(audio.events(['data: {"output": {}}']))


def test_channel_mapping_is_bound_to_verified_source_pattern(cfg):
    assert audio.profile(cfg,'Call Test_2026.aac')=={'0':'Lee','1':'Sam'}
    with pytest.raises(Attention,match='unconfirmed'): audio.profile(cfg,'Other call.aac')
    cfg.conversation_channel_profiles['Call *']={'0':'Pat','1':'Kim'}
    with pytest.raises(Attention,match='unconfirmed'): audio.profile(cfg,'Call Test_2026.aac')


def test_turns_merge_by_real_time_and_keep_overlapping_speakers():
    values=[{'text':'Answer.','start_ms':1000,'end_ms':2000,'channel':1,'speaker':'Sam'},
            {'text':'Question?','start_ms':10,'end_ms':1600,'channel':0,'speaker':'Lee'},
            {'text':'Next','start_ms':4000,'end_ms':5000,'channel':0,'speaker':'Lee'}]
    turns=audio.turns_from_words(values)
    assert [t['speaker'] for t in turns]==['Lee','Sam','Lee']
    assert turns[0]['end_ms']>turns[1]['start_ms']
    assert [t['id'] for t in turns]==[0,1,2]


def test_unknown_submission_never_replays_and_successful_chunk_reuses_receipt(cfg,monkeypatch):
    folder=cfg.root/'chunk';folder.mkdir()
    source=folder/'audio.mp3';source.write_bytes(b'fixture')
    atomic(folder/'state.json',{'submission':'unknown'})
    monkeypatch.setattr(audio.httpx,'Client',lambda *a,**k:pytest.fail('network replay'))
    with pytest.raises(UnknownOutcome): audio.chunk_receipt(cfg,source,folder)
    atomic(folder/'receipt.json',{'complete':True,'audio_sha256':file_hash(source),'events':[]})
    assert audio.chunk_receipt(cfg,source,folder)['complete']


def draft():
    return meeting.Draft(title='Public discussion',sections=[
        meeting.Section(name='共识',markdown='- Smaller batches simplify validation.',source_turns=[0]),
        meeting.Section(name='继续讨论',markdown='- Sam and Lee still need to choose a budget.',source_turns=[1]),
        meeting.Section(name='无用的闲聊',markdown='- Lee had lunch.',source_turns=[2])])


def test_three_sections_require_complete_source_coverage_and_anonymous_consensus():
    source={'turns':[{'id':i} for i in range(3)],'speakers':{'0':'Lee','1':'Sam'}}
    value=draft()
    body=meeting.validate(value,source)
    assert [c['text'] for c in meeting.parse_outline(body)['children']]==meeting.SECTIONS
    value.sections[2].source_turns=[]
    with pytest.raises(ValueError,match='source_turns'): meeting.validate(value,source)
    value=draft();value.sections[0].markdown='- Lee proposes smaller batches.'
    with pytest.raises(ValueError,match='说话人'): meeting.validate(value,source)
    value=draft();value.title='# Extra title marker'
    with pytest.raises(ValueError,match='title'): meeting.validate(value,source)
    value=draft();value.sections[1].markdown=''
    with pytest.raises(ValueError,match='源发言'): meeting.validate(value,source)


def test_outline_uses_accepted_text_for_terms_and_caches_result(cfg,monkeypatch):
    from async_workflow.core import digest
    cfg.prompt=cfg.root/'prompt.md';cfg.prompt.write_text('Structure faithfully.',encoding='utf-8')
    monkeypatch.setattr(meeting,'PROMPT_HASH',digest(cfg.prompt.read_bytes()))
    store=Manifest(cfg,'b'*64)
    atomic(store.folder/'asr.json',{'text':'Accepted spelling of a project term.'})
    original=(store.folder/'asr.json').read_bytes()
    source={'turns':[{'id':i} for i in range(3)],'speakers':{'0':'Lee','1':'Sam'},'text':'channel-derived turns'}
    calls=[]
    def ask(config,schema,instruction,payload,validator):
        calls.append(payload)
        assert payload['accepted_text_reference']=='Accepted spelling of a project term.'
        assert payload['speakers']==source['speakers']
        return validator(draft())
    monkeypatch.setattr(meeting.routing,'ask',ask)
    first=meeting.outline(cfg,store,source)
    assert meeting.outline(cfg,store,source)==first and len(calls)==1
    assert (store.folder/'asr.json').read_bytes()==original


def test_explicit_meeting_repair_preserves_old_transcript_and_forces_new_effect(cfg,monkeypatch):
    store=Manifest(cfg,'a'*64)
    part={'index':0,'key':'old','kind':'record','raw_text':'old text','source_spans':[[0,8]],'markdown':'# Old\n- Original'}
    store.update(recording_type='conversation',sources=['Call Test_2026.aac'],segments=[part])
    atomic(store.folder/'asr.json',{'text':'old text'})
    monkeypatch.setattr(meeting,'prepare',lambda *a:({'text':'Sam: New text'},'# New\n- 共识\n- 继续讨论\n- 无用的闲聊'))
    monkeypatch.setattr(repairs.splitter,'refine',lambda *a:pytest.fail('ordinary refinement'))
    folder,before,plan=repairs.prepare(cfg,store,'meeting','repair')
    assert before['segments']==[part]
    assert 'reuses_effect' not in plan['segments'][0]
    assert plan['segments'][0]['key']!='old'
    assert json.loads((store.folder/'asr.json').read_text())['text']=='old text'
    monkeypatch.setattr(meeting,'prepare',lambda *a:pytest.fail('completed preparation repeated'))
    assert repairs.prepare(cfg,store,'meeting','repair')[2]==plan
