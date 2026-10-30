"""Official App Server SDK adapter; Codex owns Thread history and execution.

The local receipt only associates a recording effect with real Thread/Turn IDs.
It is not a second Thread store, scheduler, or transport fallback.
"""
from __future__ import annotations

from pathlib import Path
from time import sleep
import os

from filelock import FileLock
from openai_codex import CodexConfig, InvalidRequestError, JsonRpcError, Thread
from openai_codex.client import CodexClient
from pydantic import BaseModel, ConfigDict

from .core import Attention, UnknownOutcome, atomic, digest, now, read_json, resolve_codex_path


def sdk_config(cfg, cwd: Path, *, via_wakewire: bool = False) -> CodexConfig:
    # Keep the desktop's CODEX_HOME, login and normal user configuration. The
    # Desktop updates replace the managed binary directory. Resolve only an
    # unambiguous replacement; preserve existing/custom runtime configuration.
    runtime = str(resolve_codex_path(cfg.codex))
    launch = ((os.environ.get("WAKEWIRE_NODE", "C:/Program Files/nodejs/node.exe"),
               os.environ.get("WAKEWIRE_CLI", "D:/Applications/WakeWire/node_modules/wakewire/dist/cli.js"),
               "codex-session", "--client", "async_workflow", "--codex", runtime, "--cwd", str(cwd))
              if via_wakewire else None)
    return CodexConfig(codex_bin=runtime, launch_args_override=launch, cwd=str(cwd),
                       env={"WAKEWIRE_HOME": os.environ.get("WAKEWIRE_HOME", "D:/AppData/WakeWire")} if via_wakewire else None,
                       client_name="async_workflow", client_title="Async Workflow",
                       experimental_api=True)


class ProtocolObject(BaseModel):
    model_config = ConfigDict(extra='allow')


def check_project(cfg, cwd, client):
    if not cfg.command_project_id: raise Attention('command_project_not_bound')
    project=client.request('project/read',{'projectId':cfg.command_project_id},response_model=ProtocolObject).model_dump()['project']
    if not any(Path(root['path']).resolve()==cwd.resolve() for root in project['roots']):
        raise Attention('command_project_workspace_mismatch')


def project_assignment(cfg, observed, cwd):
    if observed.get('projectId')!=cfg.command_project_id or Path(observed['cwd']).resolve()!=cwd.resolve():
        raise Attention('native_thread_project_readback_mismatch')
    return {'project_id':observed['projectId'],'cwd':observed['cwd'],'verified_at':now()}


def recoverable_empty_thread(saved, exc):
    """Only an explicit missing rollout BEFORE turn/start permits replacement."""
    return (saved.get('phase') == 'thread_created' and not saved.get('turn_id') and not saved.get('is_followup')
            and isinstance(exc, InvalidRequestError) and exc.code == -32600
            and exc.message == f"no rollout found for thread id {saved.get('session_id')}")


def prompt_for(item: dict) -> str:
    return item.get("followup") or item["markdown"]


def name_thread(thread, item, folder):
    title = item.get('title') or item['markdown'].splitlines()[0].lstrip('# ').strip()[:60]
    for delay in (0, .5, 1, 2, 4):
        if delay:
            sleep(delay)
        try:
            thread.set_name(title)
            atomic(folder/'title.json', {'at':now(), 'thread_id':thread.id, 'title':title})
            return True
        except Exception as exc:
            atomic(folder/'title-failure.json', {'at':now(), 'message':str(exc)})
            # The first turn is accepted before its rollout is necessarily
            # flushed. Retrying this identical metadata write is idempotent.
            if not (isinstance(exc, JsonRpcError) and exc.code == -32603
                    and thread.id in exc.message
                    and ('no rollout found' in exc.message or 'is empty' in exc.message)):
                break
    return False


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
    with CodexClient(sdk_config(cfg, Path(saved["cwd"]), via_wakewire=True)) as client:
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
        state = {"status": "outcome_unknown", "phase": "thread_start_pending", "backend": "app-server",
                 "kind": "command", "effect_key": key, "cwd": str(cwd), "output_dir": str(folder),
                 "prepared_at": now(), **({"session_id": session_id} if session_id else {})}
        if saved and not following:
            # Keep the last known phase until resume succeeds. A transport error
            # during resume must not erase proof that no turn was submitted.
            state = dict(saved)
        if following and saved.get('project_assignment'):
            state['project_assignment'] = saved['project_assignment']
        if following:
            state['is_followup'] = True
        action_started = False
        try:
            with CodexClient(sdk_config(cfg, cwd, via_wakewire=True)) as client:
                client.initialize()
                check_project(cfg, cwd, client)
                if session_id:
                    action_started = True
                    try:
                        observed = client.thread_resume(session_id).model_dump(mode='json',by_alias=True)['thread']
                    except InvalidRequestError as exc:
                        if following or not recoverable_empty_thread(saved, exc):
                            raise
                        recovery_path = folder / 'pre-turn-recoveries.json'
                        recoveries = read_json(recovery_path) or []
                        recoveries.append({'at':now(), 'previous_receipt':saved,
                            'previous_failure':read_json(folder/'failure.json'),
                            'resume_error':str(exc), 'reason':'missing_rollout_before_submission'})
                        atomic(recovery_path, recoveries)
                        session_id = None
                        state = {"status":"outcome_unknown", "phase":"thread_start_pending",
                            "backend":"app-server", "kind":"command", "effect_key":key,
                            "cwd":str(cwd), "output_dir":str(folder), "prepared_at":now(),
                            "replaced_empty_threads":[r['previous_receipt']['session_id'] for r in recoveries]}
                if not session_id:
                    manifest.save_effect(key, state)
                    action_started = True
                    # The SDK's convenience method does not expose projectId yet.
                    # Its public protocol client accepts current official fields.
                    # Bind at creation: metadata/update requires stored history,
                    # while an empty new thread may exist only in this connection.
                    observed = client.thread_start({'cwd':str(cwd), 'ephemeral':False,
                        'sandbox':'workspace-write', 'approvalPolicy':'never',
                        'serviceName':'async_workflow', 'projectId':cfg.command_project_id}
                        ).model_dump(mode='json',by_alias=True)['thread']
                thread = Thread(client, observed['id'])
                state.update(session_id=thread.id, thread_id=thread.id, phase="thread_created")
                manifest.save_effect(key, state)  # Exact ID saved BEFORE submission.
                if session_id and not following and observed.get('projectId') != cfg.command_project_id:
                    # An old resumable empty thread already has stored history.
                    client.request('thread/metadata/update',
                        {'threadId':thread.id,'projectId':cfg.command_project_id},response_model=ProtocolObject)
                    observed = client.thread_read(thread.id).model_dump(mode='json',by_alias=True)['thread']
                state['project_assignment'] = project_assignment(cfg, observed, cwd)
                manifest.save_effect(key, state)
                if session_id and not following and observed.get('turns'):
                    # The desktop may have continued an earlier empty thread.
                    # Its persisted conversation wins over our pre-turn receipt.
                    result = {**state, 'status':'needs_input', 'reason':'thread_continued_elsewhere'}
                    manifest.save_effect(key, result)
                    return result
                state.update(phase="turn_start_pending")
                manifest.save_effect(key, state)
                handle = thread.turn(prompt_for(item))
                state.update(status="submitted", phase="turn_started", turn_id=handle.id, started_at=now())
                manifest.save_effect(key, state)
                named = following or name_thread(thread, item, folder)
                completed = handle.run()
                if not named:
                    name_thread(thread, item, folder)
                result = _complete(state, completed.final_response, completed.status.value)
                # Independent completion receipt permits manifest-crash recovery.
                atomic(folder / "completion.json", result)
                manifest.save_effect(key, result)
                return result
        except Exception as exc:
            if not action_started:
                raise
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
