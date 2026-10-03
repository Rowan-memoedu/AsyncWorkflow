"""Use Codex's own durable submission queue; never emulate a chat or take its writer."""
from pathlib import Path

from filelock import FileLock
from openai_codex.client import CodexClient

from .codex_appserver import ProtocolObject, sdk_config
from .core import Attention, UnknownOutcome, now


def rpc(client, method, params):
    return client.request(method,params,response_model=ProtocolObject).model_dump()


def pages(client, method, thread_id, **options):
    cursor=None
    seen=set()
    while True:
        result=rpc(client,method,{'threadId':thread_id,'limit':100,**options,
                                **({'cursor':cursor} if cursor else {})})
        yield from result['data']
        cursor=result.get('nextCursor')
        if not cursor: return
        if cursor in seen: raise Attention('native_feedback_pagination_repeated')
        seen.add(cursor)


def text_of(inputs):
    return ''.join(item.get('text','') for item in inputs if item.get('type')=='text')


def observed(client, thread_id, message_id, body):
    for queued in pages(client,'thread/queue/list',thread_id):
        if queued['clientUserMessageId']==message_id:
            if text_of(queued['input'])!=body: raise Attention('native_feedback_body_mismatch')
            return {'status':'queued','queued_submission_id':queued['id']}
    for turn in pages(client,'thread/turns/list',thread_id,itemsView='full'):
        for item in turn.get('items',[]):
            if item.get('type')=='userMessage' and item.get('clientId')==message_id:
                if text_of(item['content'])!=body: raise Attention('native_feedback_body_mismatch')
                # A persisted turn proves receipt, not the owning Desktop's live state.
                return {'status':'delivered','turn_id':turn['id']}
    return None


def deliver(cfg, store, key, body):
    if not cfg.feedback_thread_id: raise Attention('feedback_maintenance_thread_not_configured')
    thread_id=cfg.feedback_thread_id
    message_id=store.folder.name+':'+key
    with FileLock(str(cfg.root/'locks'/(key+'.lock'))):
        saved=store.effect(key)
        if saved and saved.get('thread_id')!=thread_id:
            raise Attention('feedback_target_changed_requires_reconciliation')
        with CodexClient(sdk_config(cfg,cfg.command_workspace)) as client:
            client.initialize()
            thread=rpc(client,'thread/read',{'threadId':thread_id,'includeTurns':False})['thread']
            if (thread.get('projectId')!=cfg.command_project_id or
                    Path(thread['cwd']).resolve()!=cfg.command_workspace.resolve()):
                raise Attention('feedback_native_project_mismatch')
            found=observed(client,thread_id,message_id,body)
            state={'backend':'app-server-native-queue','thread_id':thread_id,
                   'client_user_message_id':message_id,'at':now()}
            if found:
                result={**state,**found}
                store.save_effect(key,result)
                return result
            if saved:
                raise UnknownOutcome('feedback_submission_unknown_no_replay')
            store.save_effect(key,{**state,'status':'outcome_unknown'})
            try:
                result=rpc(client,'thread/queue/add',{'threadId':thread_id,
                    'clientUserMessageId':message_id,'input':[{'type':'text','text':body}]})
                queued=result['queuedSubmission']
                if queued['clientUserMessageId']!=message_id or text_of(queued['input'])!=body:
                    raise Attention('native_feedback_body_mismatch')
                result={**state,'status':'queued','queued_submission_id':queued['id']}
                store.save_effect(key,result)
                return result
            except Exception:
                found=observed(client,thread_id,message_id,body)
                if found:
                    result={**state,**found}
                    store.save_effect(key,result)
                    return result
                raise UnknownOutcome('feedback_submission_unknown_no_replay') from None
