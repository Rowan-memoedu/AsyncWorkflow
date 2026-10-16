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


@pytest.mark.parametrize('error', [
    'Fun-ASR service error: CLIENT_ERROR',
    'Fun-ASR HTTP stream failure: URLError',
    'Fun-ASR final sentence has invalid timestamps',
])
def test_finished_explicit_cli_failure_can_resume_same_recording(setup,monkeypatch,error):
    import json
    cfg,store,source,raw=setup
    cfg.transcription_cli=store.folder/'cli.py';cfg.transcription_cli.write_text('fixture')
    output=store.folder/'asr-cli.out.log'
    output.write_text(json.dumps({'ok':False,'error':error})+'\n')
    store.update(asr={'submission':'unknown','exit_code':2,'finished_at':'earlier',
                      'started_at':'earlier','stdout_log':str(output),'pid':123})
    class Process:
        pid=456
        def wait(self,timeout):
            atomic(store.folder/'funasr/raw.json',raw)
            return 0
    def launch(*a,**kw):
        # The global CLI creates its result directory with exist_ok=False.
        # Its durable request cache must therefore live outside that directory.
        assert not Path(kw['env']['FUNASR_RECEIPT_DIR']).is_relative_to(store.folder/'funasr')
        return Process()
    monkeypatch.setattr(asr.subprocess,'Popen',launch)
    assert asr.recognize(cfg,store,source)=='Accepted words.'
    stage=store.read()['asr']
    assert stage['attempt']==2 and stage['attempts'][0]['pid']==123


@pytest.mark.parametrize('error,code',[
    ('Fun-ASR HTTP 401',2),('Fun-ASR HTTP stream failure: URLError',1),
    ('local runtime failure: FileNotFoundError',2),
])
def test_other_cli_errors_do_not_authorize_provider_replay(setup,error,code):
    import json
    _,store,_,_=setup
    output=store.folder/'asr-cli.out.log';output.write_text(json.dumps({'ok':False,'error':error}))
    store.update(asr={'submission':'unknown','exit_code':code,'finished_at':'earlier','stdout_log':str(output)})
    assert asr.retryable_cli_failure(store) is None


def test_cli_recovery_does_not_use_a_previous_attempt_error(setup):
    _,store,_,_=setup
    output=store.folder/'asr-cli.out.log';output.write_text('{"ok":false,"error":"Fun-ASR HTTP 503"}\n')
    store.update(asr={'submission':'unknown','exit_code':2,'finished_at':'now',
                      'stdout_log':str(output),'stdout_offset':output.stat().st_size})
    assert asr.retryable_cli_failure(store) is None


def test_explicit_cli_retry_limit_is_bounded(setup):
    _,store,source,_=setup
    output=store.folder/'asr-cli.out.log';output.write_text('{"ok":false,"error":"Fun-ASR HTTP 503"}\n')
    store.update(asr={'submission':'unknown','attempt':3,'exit_code':2,'finished_at':'earlier','stdout_log':str(output)})
    with pytest.raises(Attention,match='retry_limit_exceeded'):
        asr.recognize(setup[0],store,source)


def chunk_fixture(store):
    store.update(duration=2.5,asr={'submission':'unknown','attempt':3})
    folder=store.folder/'funasr/chunks'
    atomic(folder/'0000.json',{'source_sha256':store.folder.name,'offset_ms':0,'length_ms':2000,'complete':True,
        'events':[{'output':{'sentence':{'sentence_id':1,'sentence_end':True,'begin_time':100,'end_time':1900,'text':'Cached words.'}}}]})
    atomic(folder/'0001.json',{'source_sha256':store.folder.name,'offset_ms':2000,'length_ms':500,'complete':False,
        'events':[{'code':'CLIENT_ERROR','message':'ASR_RESPONSE_HAVE_NO_WORDS'}]})
    return folder


def test_complete_chunk_receipts_restore_failed_asr_without_any_process_or_request(setup,monkeypatch):
    cfg,store,source,_=setup
    chunk_fixture(store)
    monkeypatch.setattr(asr.subprocess,'Popen',lambda *a,**k:pytest.fail('No provider replay needed'))
    assert asr.recognize(cfg,store,source)=='Cached words.'
    from async_workflow.core import read_json
    result=read_json(store.folder/'asr.json')
    assert result['provider_status']['no_words_chunk_offsets_ms']==[2000]
    assert store.read()['asr']['attempt']==3


@pytest.mark.parametrize('corruption',['wrong_source','missing_chunk','other_error','partial_text'])
def test_chunk_recovery_requires_complete_source_bound_evidence(setup,corruption):
    from async_workflow.core import read_json
    _,store,_,_=setup
    folder=chunk_fixture(store)
    d=read_json(folder/'0001.json')
    if corruption=='wrong_source':d['source_sha256']='f'*64
    if corruption=='missing_chunk':d['offset_ms']=3000
    if corruption=='other_error':d['events']=[{'code':'CLIENT_ERROR','message':'Other failure'}]
    if corruption=='partial_text':d['events'].insert(0,{'output':{'sentence':{'text':'Unfinished words'}}})
    atomic(folder/'0001.json',d)
    assert asr.restore_funasr_chunks(store) is False
    assert not (store.folder/'asr.json').exists()


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
