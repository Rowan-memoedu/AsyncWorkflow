"""Official App Server SDK adapter; Codex owns Thread history and execution.

The local receipt only associates a recording effect with real Thread/Turn IDs.
It is not a second Thread store, scheduler, or transport fallback.
"""
from __future__ import annotations

from pathlib import Path

from filelock import FileLock
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from openai_codex.client import CodexClient
from pydantic import BaseModel, ConfigDict

from .core import Attention, UnknownOutcome, atomic, digest, now, read_json


def sdk_config(cfg, cwd: Path) -> CodexConfig:
    # Keep the desktop's CODEX_HOME, login and normal user configuration. The
    # explicit binary is the runtime whose Desktop interoperability was tested.
    return CodexConfig(codex_bin=str(cfg.codex), cwd=str(cwd),
                       client_name="async_workflow", client_title="Async Workflow",
                       experimental_api=True)


class ProtocolObject(BaseModel):
    model_config = ConfigDict(extra='allow')


def check_project(cfg,cwd):
    if not cfg.command_project_id: raise Attention('command_project_not_bound')
    with CodexClient(sdk_config(cfg,cwd)) as client:
        client.initialize()
        project=client.request('project/read',{'projectId':cfg.command_project_id},response_model=ProtocolObject).model_dump()['project']
    if not any(Path(root['path']).resolve()==cwd.resolve() for root in project['roots']):
        raise Attention('command_project_workspace_mismatch')


def bind_project(cfg, thread_id, cwd):
    """Use the current official protocol absent from the older SDK's convenience methods."""
    if not cfg.command_project_id:
        raise Attention('command_project_not_bound')
    with CodexClient(sdk_config(cfg,cwd)) as client:
        client.initialize()
        project=client.request('project/read',{'projectId':cfg.command_project_id},response_model=ProtocolObject).model_dump()['project']
        if not any(Path(root['path']).resolve()==cwd.resolve() for root in project['roots']):
            raise Attention('command_project_workspace_mismatch')
        client.request('thread/metadata/update',
            {'threadId':thread_id,'projectId':cfg.command_project_id},response_model=ProtocolObject).model_dump()
        observed=client.thread_read(thread_id,include_turns=False).model_dump(mode='json',by_alias=True)['thread']
    if observed.get('projectId')!=cfg.command_project_id or Path(observed['cwd']).resolve()!=cwd.resolve():
        raise Attention('native_thread_project_readback_mismatch')
    return {'project_id':observed['projectId'],'cwd':observed['cwd'],'verified_at':now()}


def prompt_for(item: dict) -> str:
    return item.get("followup") or item["markdown"]


def prepare_input(cfg, item, folder):
    from . import splitter
    from .markdown import parse_outline
    signature=digest(item['raw_text'])
    path=folder/'input.json'
    saved=read_json(path)
    if saved:
        if saved['source_hash']!=signature: raise Attention('command_input_changed')
        return {**item,**saved['body']}
    body=item if 'content_text' in item else splitter.extract_content(cfg,{**item,'kind':'command'})
    if not body['content_text'].strip(): raise Attention('thread_task_body_missing')
    reusable=body.get('markdown') if ('content_text' in item or body['content_text']==item['raw_text']) else None
    markdown=reusable or splitter.refine(cfg,body['content_text'])
    parse_outline(markdown)
    prepared={'content_text':body['content_text'],'control_spans':body.get('control_spans',[]),'markdown':markdown}
    atomic(path,{'source_hash':signature,'at':now(),'raw_text':item['raw_text'],'body':prepared})
    return {**item,**prepared}


def _complete(saved, text, native_status="completed"):
    # A completed turn may answer a question or ask the user one. Record native
    # execution status without imposing a machine-readable answer on the chat.
    status = {"completed": "completed_unverified", "interrupted": "cancelled"}.get(native_status, "failed")
    return {**saved, "native_status": native_status, "finished_at": now(),
            "status": status, "result": {"summary": text or ""}}


def reconcile(cfg, manifest, key, saved):
    """Read a known native turn after a lost response without resuming/replaying it."""
    if not saved.get("turn_id") or not saved.get("session_id"):
        raise UnknownOutcome("appserver_submission_unknown_no_replay")
    with CodexClient(sdk_config(cfg, Path(saved["cwd"]))) as client:
        client.initialize()
        thread = client.thread_read(saved["session_id"], include_turns=True)
        data = thread.model_dump(mode="json", by_alias=True)["thread"]
    turns = [t for t in data.get("turns", []) if t["id"] == saved["turn_id"]]
    if len(turns) != 1 or turns[0]["status"] == "inProgress":
        raise UnknownOutcome("appserver_turn_not_terminal_no_replay")
    turn = turns[0]
    messages = [i for i in turn.get("items", []) if i.get("type") == "agentMessage"]
    finals = [i for i in messages if i.get("phase") == "final_answer"]
    text = (finals or messages or [{}])[-1].get("text", "")
    result = _complete(saved, text, turn["status"])
    atomic(Path(saved["output_dir"]) / "completion.json", result)
    manifest.save_effect(key, result)
    return result


def execute(cfg, manifest, key: str, item: dict, cwd: Path):
    # Caller holds the per-effect lock. This additional lock serializes writers
    # from this workflow targeting the same project.
    with FileLock(str(cfg.root / "locks" / ("project-" + digest(str(cwd)) + ".lock"))):
        saved = manifest.effect(key)
        terminal = ("verified", "completed_unverified", "needs_input", "failed", "cancelled")
        if item.get("followup") and (not saved or not saved.get("session_id")):
            raise Attention("followup_requires_existing_thread")
        following = bool(saved and saved.get("status") in terminal and item.get("followup"))
        if saved and not following:
            if saved["status"] in terminal:
                return saved
            receipt = read_json(Path(saved["output_dir"]) / "completion.json")
            if receipt and receipt.get("effect_key") == key:
                manifest.save_effect(key, receipt)
                return receipt
            if saved.get("phase") != "thread_created":
                return reconcile(cfg, manifest, key, saved)

        folder = manifest.folder / "commands" / key
        if following:
            folder = folder / ("followup-" + digest(saved["output_dir"] + item["followup"])[:16])
        folder.mkdir(parents=True, exist_ok=True)
        if not following:
            item=prepare_input(cfg,item,folder)
        session_id = saved.get("session_id") if saved else None
        if not session_id:
            check_project(cfg,cwd)
        state = {"status": "outcome_unknown", "phase": "thread_start_pending", "backend": "app-server",
                 "kind": "command", "effect_key": key, "cwd": str(cwd), "output_dir": str(folder),
                 "prepared_at": now(), **({"session_id": session_id} if session_id else {})}
        if following and saved.get('project_assignment'):
            state['project_assignment'] = saved['project_assignment']
        manifest.save_effect(key, state)
        try:
            with Codex(sdk_config(cfg, cwd)) as codex:
                if session_id:
                    thread = codex.thread_resume(session_id)
                else:
                    thread = codex.thread_start(cwd=str(cwd), ephemeral=False, sandbox=Sandbox.workspace_write,
                                                approval_mode=ApprovalMode.deny_all, service_name="async_workflow")
                state.update(session_id=thread.id, thread_id=thread.id, phase="thread_created")
                manifest.save_effect(key, state)  # ID is durable BEFORE submission.
                if not following:
                    state['project_assignment']=bind_project(cfg,thread.id,cwd)
                    manifest.save_effect(key,state)
                if not session_id:
                    title = item.get("title") or item['markdown'].splitlines()[0].lstrip('# ').strip()[:60]
                    thread.set_name(title)
                state.update(phase="turn_start_pending")
                manifest.save_effect(key, state)
                handle = thread.turn(prompt_for(item))
                state.update(status="submitted", phase="turn_started", turn_id=handle.id, started_at=now())
                manifest.save_effect(key, state)
                completed = handle.run()
                result = _complete(state, completed.final_response, completed.status.value)
                # Independent completion receipt permits manifest-crash recovery.
                atomic(folder / "completion.json", result)
                manifest.save_effect(key, result)
                return result
        except Exception as exc:
            # Preserve native IDs even on transport/schema/turn failure. Never
            # start a second repair Thread for a task that already owns one.
            atomic(folder / "failure.json", {"at": now(), "error_type": type(exc).__name__,
                                               "message": str(exc), "phase": state["phase"]})
            if state.get("session_id") and "already has an active writer" in str(exc):
                result = {**state, "status": "needs_input", "reason": "thread_owned_elsewhere"}
                manifest.save_effect(key, result)
                return result
            if state.get("turn_id"):
                return reconcile(cfg, manifest, key, state)
            # An explicit pre-turn failure can safely retain the already-created
            # thread. A lost turn/start response cannot be assumed unsubmitted.
            manifest.save_effect(key, state)
            raise UnknownOutcome("appserver_outcome_unknown_preserved_native_ids") from None
