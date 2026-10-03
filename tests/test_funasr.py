from pathlib import Path
import pytest
from async_workflow import asr,transcript_revision
from async_workflow.core import Settings,Manifest,atomic,file_hash,UnknownOutcome,Attention


@pytest.fixture
def setup(tmp_path):
    cfg=Settings(root=tmp_path/'state',artifacts=tmp_path/'out',inbox=tmp_path/'audio',ffprobe=Path('ffprobe'),codex=Path('codex'),trash_rem_id='trash')
    cfg.prepare();source=cfg.inbox/'audio.wav';source.write_bytes(b'public-fixture')
    rid=file_hash(source);store=Manifest(cfg,rid)
    raw={'route':'funasr','source_sha256':rid,'status':{'model':'fun-asr-realtime'},'text':'Accepted words.',
         'utterances':[{'text':'Accepted words.','start_ms':100,'end_ms':2000}]}
    store.update(sources=[str(source)])
    return cfg,store,source,raw


def test_default_fun_asr_reuses_maintained_receipt_without_provider_call(setup,monkeypatch):
    cfg,store,source,raw=setup
    assert cfg.asr_provider=='funasr'
    atomic(store.folder/'funasr/raw.json',raw)
    monkeypatch.setattr(asr.subprocess,'run',lambda *a,**k:pytest.fail('ASR repeated'))
    assert asr.recognize(cfg,store,source)=='Accepted words.'
    assert store.read()['asr']['model']=='fun-asr-realtime'


def test_successful_legacy_transcript_survives_default_change(setup,monkeypatch):
    cfg,store,source,_=setup
    atomic(store.folder/'asr.json',{'text':'Earlier accepted source.'})
    monkeypatch.setattr(asr,'access',lambda *a:pytest.fail('credentials unnecessary'))
    assert asr.recognize(cfg,store,source)=='Earlier accepted source.'


def test_unknown_submission_is_not_blindly_repeated(setup):
    cfg,store,source,_=setup
    store.update(asr={'submission':'unknown'})
    with pytest.raises(UnknownOutcome): asr.recognize(cfg,store,source)


def test_wrong_source_receipt_cannot_replace_transcript(setup):
    cfg,store,source,raw=setup
    raw['source_sha256']='b'*64
    with pytest.raises(Attention,match='source_or_provider'): asr.normalize_funasr(raw,store.folder.name)


def test_cumulative_http_snapshots_use_the_last_containing_span(setup):
    _,store,_,raw=setup
    raw['utterances']=[{'start_ms':100,'end_ms':300,'text':'First.'},
        {'start_ms':100,'end_ms':900,'text':'First. Second.'},
        {'start_ms':100,'end_ms':1500,'text':'First. Second. Third.'},
        {'start_ms':300000,'end_ms':301000,'text':'Next chunk.'}]
    raw['text']='\n'.join(s['text'] for s in raw['utterances'])
    result=asr.normalize_funasr(raw,store.folder.name)
    assert result['text']=='First. Second. Third.\nNext chunk.'
    assert result['collapsed_snapshots']==2
    assert result['sentences']==[raw['utterances'][2],raw['utterances'][3]]


def test_real_repeated_speech_at_different_times_is_preserved(setup):
    _,store,_,raw=setup
    raw['utterances']=[{'start_ms':100,'end_ms':300,'text':'Again.'},
        {'start_ms':500,'end_ms':900,'text':'Again.'}]
    raw['text']='Again.\nAgain.'
    result=asr.normalize_funasr(raw,store.folder.name)
    assert result['text']==raw['text'] and result['collapsed_snapshots']==0


def test_conflicting_overlapping_speech_is_not_silently_rewritten(setup):
    _,store,_,raw=setup
    raw['utterances']=[{'start_ms':100,'end_ms':300,'text':'First interpretation.'},
        {'start_ms':100,'end_ms':900,'text':'Different interpretation.'}]
    raw['text']='\n'.join(s['text'] for s in raw['utterances'])
    with pytest.raises(Attention,match='overlapping_revisions_require_review'):
        asr.normalize_funasr(raw,store.folder.name)


def test_accepted_revision_retains_before_and_never_erases_later_work(setup,monkeypatch):
    cfg,store,source,raw=setup
    atomic(store.folder/'asr.json',{'text':'Incorrect old source.'})
    receipt=store.folder/'review/raw.json';atomic(receipt,raw)
    monkeypatch.setattr(transcript_revision,'verify_quiescent',lambda *a:None)
    result=transcript_revision.accept(cfg,store.folder.name,receipt,'User accepted this reviewed transcript.')
    store.update(segments=[{'key':'later'}])
    assert transcript_revision.accept(cfg,store.folder.name,receipt,'Already accepted')['reused']
    assert store.read()['segments']==[{'key':'later'}]
    assert (store.folder/'source-revisions'/result['revision']/'asr.json').read_text().find('Incorrect old source.')>=0


def test_asr_revision_does_not_replay_existing_command(setup):
    cfg,_,_,_=setup
    with pytest.raises(Attention,match='existing_thread_reconciliation'):
        transcript_revision.verify_quiescent(cfg,{'effects':{'x':{'session_id':'native-thread','status':'completed_unverified'}}})
