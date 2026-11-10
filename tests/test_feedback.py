from pathlib import Path
from types import SimpleNamespace
import pytest

from async_workflow import feedback,feedback_delivery as delivery
from async_workflow.core import Attention,Manifest,Settings,UnknownOutcome


def test_feedback_removal_preserves_interleaved_notes_and_offsets():
    text='今天散步。秘书，更新录音路由，以后运动都进训练册。午饭很香。'
    parsed=feedback.FeedbackPartition(spans=[{'unit':1}],request='以后运动都进训练册。',reason='future rule')
    result=feedback.partition(parsed,text)
    assert result['raw_text']=='秘书，更新录音路由，以后运动都进训练册。'
    assert len(result['remaining_text'])==len(text)
    assert result['remaining_text'][:5]=='今天散步。'
    assert result['remaining_text'].endswith('午饭很香。')
    assert ''.join(text[a:b] for a,b in result['source_spans'])==result['raw_text']


def test_context_is_not_removed_from_normal_content():
    text='新建训练册。秘书，更新录音归档，以后都放这本。'
    value=feedback.FeedbackPartition(spans=[{'unit':1}],context_spans=[{'unit':0}],request='以后放训练册。',reason='reference')
    result=feedback.partition(value,text)
    assert result['remaining_text'].startswith('新建训练册。')
    assert result['context_text']=='新建训练册。'


def test_feedback_cannot_fabricate_source_or_overlap():
    with pytest.raises(ValueError,match='source evidence'):
        feedback.partition(feedback.FeedbackPartition(request='invented',reason='x'),'A note.')
    with pytest.raises(ValueError,match='overlap'):
        feedback.partition(feedback.FeedbackPartition(spans=[{'unit':0},{'unit':0}],request='x',reason='x'),'A note.')
    with pytest.raises(ValueError,match='exact and unique'):
        feedback.partition(feedback.FeedbackPartition(spans=[{'unit':0,'quote':'made up'}],request='x',reason='x'),'A note.')


@pytest.fixture
def setup(tmp_path,monkeypatch):
    cfg=Settings(root=tmp_path/'state',inbox=tmp_path/'audio',artifacts=tmp_path/'out',
        ffprobe=Path('ffprobe'),codex=Path('codex'),command_workspace=tmp_path,
        command_project_id='project',feedback_thread_id='fixed-native-thread')
    cfg.prepare();store=Manifest(cfg,'3'*64)
    class Client:
        queue=[]
        turns=[]
        calls=[]
        lose_response=False
        absent_response=False
        wrong_project=False
        def __init__(self,*a): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def initialize(self): pass
        def request(self,method,params,**kwargs):
            self.calls.append((method,params))
            if method=='thread/read':
                result={'thread':{'cwd':str(cfg.command_workspace),'projectId':'wrong' if self.wrong_project else 'project'}}
            elif method=='thread/queue/list': result={'data':self.queue,'nextCursor':None}
            elif method=='thread/turns/list': result={'data':self.turns,'nextCursor':None}
            elif method=='thread/queue/add':
                if self.absent_response: raise OSError('unknown before response')
                item={'id':'native-submission',**params}
                self.queue.append(item)
                if self.lose_response: raise OSError('response lost after accepted')
                result={'queuedSubmission':item}
            else: pytest.fail('Unexpected native mutation: '+method)
            return SimpleNamespace(model_dump=lambda:result)
    monkeypatch.setattr(delivery,'CodexClient',Client)
    # These tests exercise native delivery; policy classification has separate tests.
    from async_workflow import dispatch_policy
    monkeypatch.setattr(dispatch_policy,'decide',lambda *a:{'route':'workflow','reason':'fixture','evidence':'Secretary, update recording workflow.'})
    return cfg,store,Client


def test_fixed_thread_gets_only_body_and_retry_reuses_native_submission(setup):
    cfg,store,client=setup
    body='# 一个要求\n- 保留限制。'
    first=delivery.deliver(cfg,store,'feedback-one',body)
    again=delivery.deliver(cfg,store,'feedback-one',body)
    assert first['queued_submission_id']==again['queued_submission_id']=='native-submission'
    sent=[p for method,p in client.calls if method=='thread/queue/add']
    assert len(sent)==1
    assert sent[0]['threadId']=='fixed-native-thread'
    assert sent[0]['input']==[{'type':'text','text':body}]
    assert 'outputSchema' not in sent[0]


def test_consumed_message_reconciles_with_native_turn_without_resend(setup):
    cfg,store,client=setup
    delivery.deliver(cfg,store,'feedback-one','Body')
    item=client.queue.pop()
    client.turns.append({'id':'actual-turn','status':'completed','items':[
        {'type':'userMessage','clientId':item['clientUserMessageId'],'content':item['input']}]})
    observed=delivery.deliver(cfg,store,'feedback-one','Body')
    assert observed['status']=='delivered' and observed['turn_id']=='actual-turn'
    assert sum(method=='thread/queue/add' for method,_ in client.calls)==1


def test_lost_response_recovers_native_queue_receipt(setup):
    cfg,store,client=setup;client.lose_response=True
    assert delivery.deliver(cfg,store,'feedback-one','Body')['status']=='queued'
    assert delivery.deliver(cfg,store,'feedback-one','Body')['status']=='queued'
    assert sum(method=='thread/queue/add' for method,_ in client.calls)==1


def test_unknown_outcome_is_not_replayed(setup):
    cfg,store,client=setup;client.absent_response=True
    with pytest.raises(UnknownOutcome): delivery.deliver(cfg,store,'feedback-one','Body')
    with pytest.raises(UnknownOutcome): delivery.deliver(cfg,store,'feedback-one','Body')
    assert sum(method=='thread/queue/add' for method,_ in client.calls)==1


def test_wrong_project_and_changed_body_do_not_get_sent(setup):
    cfg,store,client=setup;client.wrong_project=True
    with pytest.raises(Attention,match='project_mismatch'): delivery.deliver(cfg,store,'feedback-one','Body')
    client.wrong_project=False
    delivery.deliver(cfg,store,'feedback-one','Body')
    with pytest.raises(Attention,match='body_mismatch'): delivery.deliver(cfg,store,'feedback-one','Changed')
    assert sum(method=='thread/queue/add' for method,_ in client.calls)==1


def test_feedback_only_repair_never_reprocesses_asr_or_notes(setup,monkeypatch):
    from async_workflow import flows,asr,remnote
    from async_workflow.core import atomic
    cfg,store,_=setup
    atomic(store.folder/'asr.json',{'text':'A workflow request.'})
    original=[{'key':'existing-note','document_action':'append'}]
    store.update(segments=original,effects={'existing-note':{'status':'verified','root_id':'keep-this-id'}})
    monkeypatch.setattr(flows.Settings,'load',staticmethod(lambda:cfg))
    monkeypatch.setattr(asr,'recognize',lambda *a:pytest.fail('ASR must not run'))
    monkeypatch.setattr(remnote,'publish',lambda *a,**k:pytest.fail('Notes must not be written'))
    monkeypatch.setattr(feedback,'prepare',lambda *a:{'source_spans':[[0,5]],'key':'feedback-one','cancelled':False,'raw_text':'Secretary, update recording workflow.'})
    monkeypatch.setattr(feedback,'message',lambda *a:'Only feedback body')
    result=flows.process_workflow_feedback.fn(store.folder.name)
    assert result['status']=='queued'
    assert store.read()['segments']==original
    assert store.effect('existing-note')['root_id']=='keep-this-id'


def test_new_document_must_verify_before_feedback_is_sent(setup,monkeypatch):
    from async_workflow import flows
    from async_workflow.core import atomic
    cfg,store,_=setup
    atomic(store.folder/'asr.json',{'text':'A workflow request.'})
    store.update(segments=[{'key':'new-document','document_action':'create','source_spans':[[0,5]]}])
    monkeypatch.setattr(flows.Settings,'load',staticmethod(lambda:cfg))
    monkeypatch.setattr(feedback,'prepare',lambda *a:{'source_spans':[[10,15]],'context_spans':[[0,5]],'key':'feedback-one','cancelled':False,'raw_text':'Secretary, update recording workflow.'})
    monkeypatch.setattr(feedback,'message',lambda *a:pytest.fail('Cannot ground a not-yet-created document'))
    pauses=[]
    monkeypatch.setattr(flows,'suspend_flow_run',lambda **kw:pauses.append(kw))
    with pytest.raises(Attention,match='dependency_not_verified'):
        flows.process_workflow_feedback.fn(store.folder.name)
    assert pauses==[{'key':'feedback-document-dependency'}]


def test_feedback_waits_only_for_documents_in_its_reference_context():
    data={'segments':[
        {'key':'referenced','document_action':'create','source_spans':[[0,15]]},
        {'key':'unrelated','document_action':'create','source_spans':[[30,50]]}],
        'workflow_feedback':{'context_spans':[[5,10]]},'effects':{}}
    assert feedback.pending_documents(data)==['referenced']
    data['effects']['referenced']={'status':'verified'}
    assert feedback.pending_documents(data)==[]
    assert feedback.pending_documents(data,{'context_spans':[]})==[]


def test_cancelled_feedback_is_not_dispatched(setup,monkeypatch):
    from async_workflow import flows
    from async_workflow.core import atomic
    cfg,store,client=setup
    atomic(store.folder/'asr.json',{'text':'Actually do not change it.'})
    monkeypatch.setattr(flows.Settings,'load',staticmethod(lambda:cfg))
    monkeypatch.setattr(feedback,'prepare',lambda *a:{'source_spans':[[0,5]],'key':'feedback-one','cancelled':True})
    assert flows.process_workflow_feedback.fn(store.folder.name)['status']=='cancelled'
    assert client.calls==[]


@pytest.mark.parametrize('notes',['今天练习了吉他。',''])
def test_new_analysis_routes_feedback_separately_from_ordinary_notes(setup,monkeypatch,notes):
    from async_workflow import flows
    from async_workflow.core import atomic,digest
    cfg,store,_=setup
    source=cfg.inbox/'10-07-2026 12.00.mp3'
    source.write_bytes(b'fixture recording')
    text=notes+'秘书，更新录音工作流，以后转写保留英文术语。'
    extracted=feedback.partition(feedback.FeedbackPartition(
        spans=[{'unit':1 if notes else 0}],request='以后转写保留英文术语。',reason='format'),text)
    extracted.update(extractor_revision=digest(feedback.RULES),source_hash=digest(text))
    monkeypatch.setattr(flows.Settings,'load',staticmethod(lambda:cfg))
    monkeypatch.setattr(flows,'file_hash',lambda path:store.folder.name)
    monkeypatch.setattr(flows,'probe',lambda *a:30)
    monkeypatch.setattr(flows,'source_metadata',lambda *a:{})
    monkeypatch.setattr(flows,'eligibility',lambda *a:'eligible')
    monkeypatch.setattr(flows.recording_types,'confirm_self',lambda *a:'single_self')
    monkeypatch.setattr(flows.asr,'recognize',lambda *a:text)
    def extract(*a):
        atomic(store.folder/'feedback.json',extracted)
        return extracted
    monkeypatch.setattr(feedback,'prepare',extract)
    calls=[]
    def outline(body,*args):
        assert body.strip()==notes and notes
        calls.append('outline')
        return '# '+notes
    def split(body,*args):
        assert body==extracted['remaining_text'] and notes
        calls.append('split')
        return [{'index':0,'source_spans':[[0,len(notes)]],'raw_text':notes,'content_text':notes}]
    monkeypatch.setattr(flows,'outline_task',outline)
    monkeypatch.setattr(flows,'split_task',split)
    monkeypatch.setattr(flows,'run_deployment',lambda *a,**k:calls.append(a[0]) or SimpleNamespace(id='note-run'))
    monkeypatch.setattr(feedback,'submit',lambda *a:calls.append('feedback-run'))
    flows.analyze_recording.fn(str(source),canary=True)
    assert calls.count('process-segment/local')==bool(notes)
    assert calls.count('feedback-run')==1
    if not notes: assert calls==['feedback-run']
    assert len(store.read()['segments'])==bool(notes)


def test_submitted_feedback_body_is_frozen_across_prompt_revisions(setup,monkeypatch):
    from async_workflow import routing
    from async_workflow.core import atomic
    cfg,store,_=setup
    item={'key':'feedback-one','source_hash':'source','raw_text':'request','context_text':'','request':'request'}
    atomic(store.folder/'feedback-message.json',{'key':'feedback-one','markdown':'Delivered request','signature':'older'})
    store.save_effect('feedback-one',{'status':'delivered','thread_id':cfg.feedback_thread_id})
    monkeypatch.setattr(routing,'ask',lambda *a,**k:pytest.fail('A sent message must stay unchanged'))
    assert feedback.message(cfg,store,item)=='Delivered request'


def test_new_feedback_uses_replacement_binding_and_preserves_previous_receipts(setup):
    cfg,store,client=setup
    previous={'status':'delivered','thread_id':cfg.feedback_thread_id,'turn_id':'old-turn'}
    store.save_effect('previous-feedback',previous)
    previous=store.effect('previous-feedback')
    cfg.feedback_thread_id='replacement-native-thread'
    result=delivery.deliver(cfg,store,'new-feedback','A new routing request.')
    assert result['thread_id']=='replacement-native-thread'
    sent=[p for method,p in client.calls if method=='thread/queue/add']
    assert len(sent)==1 and sent[0]['threadId']=='replacement-native-thread'
    assert store.effect('previous-feedback')==previous


@pytest.mark.parametrize('status',['queued','delivered','outcome_unknown'])
def test_rebinding_does_not_replay_a_previously_submitted_feedback(setup,status):
    cfg,store,client=setup
    previous={'status':status,'thread_id':cfg.feedback_thread_id,'client_user_message_id':'old-message'}
    store.save_effect('feedback-one',previous)
    previous=store.effect('feedback-one')
    cfg.feedback_thread_id='replacement-native-thread'
    with pytest.raises(Attention,match='target_changed_requires_reconciliation'):
        delivery.deliver(cfg,store,'feedback-one','Previously submitted body.')
    assert store.effect('feedback-one')==previous
    assert not client.calls
