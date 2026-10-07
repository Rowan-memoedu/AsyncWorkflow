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


def test_local_launch_failure_is_logged_as_not_submitted(setup,monkeypatch):
    cfg,store,source,_=setup
    cfg.transcription_cli=store.folder/'public-cli.py';cfg.transcription_cli.write_text('print(1)')
    def fail(*a,**k): raise FileNotFoundError('local launch failed')
    monkeypatch.setattr(asr.subprocess,'Popen',fail)
    with pytest.raises(Attention,match='local_process_not_started'):
        asr.recognize(cfg,store,source)
    assert store.read()['asr']['submission']=='not_started'
    assert (store.folder/'asr-cli.err.log').exists()


def test_unknown_provider_failure_keeps_pid_exit_and_logs_without_replay(setup,monkeypatch):
    cfg,store,source,_=setup
    cfg.transcription_cli=store.folder/'public-cli.py';cfg.transcription_cli.write_text('print(1)')
    class Process:
        pid=42
        def wait(self,timeout): return 1
    calls=[]
    def launch(*a,**k):
        calls.append(a)
        k['stderr'].write(b'public failure evidence\n')
        return Process()
    monkeypatch.setattr(asr.subprocess,'Popen',launch)
    with pytest.raises(Attention,match='cli_failed'): asr.recognize(cfg,store,source)
    with pytest.raises(UnknownOutcome): asr.recognize(cfg,store,source)
    stage=store.read()['asr']
    assert stage['pid']==42 and stage['exit_code']==1 and len(calls)==1
    assert 'public failure' in (store.folder/'asr-cli.err.log').read_text()


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


def test_shorter_cumulative_prefix_with_later_timestamp_keeps_complete_text(setup):
    _,store,_,raw=setup
    raw['utterances']=[{'start_ms':100,'end_ms':500,'text':'First. Second.'},
                      {'start_ms':100,'end_ms':900,'text':'First.'},
                      {'start_ms':100,'end_ms':1800,'text':'First. Second. Third.'}]
    raw['text']='\n'.join(s['text'] for s in raw['utterances'])
    result=asr.normalize_funasr(raw,store.folder.name)
    assert result['text']=='First. Second. Third.' and result['collapsed_snapshots']==2


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
