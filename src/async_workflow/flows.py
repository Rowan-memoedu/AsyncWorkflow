from __future__ import annotations
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from prefect import flow, task, serve
from prefect.cache_policies import INPUTS, TASK_SOURCE
from prefect.deployments import run_deployment
from prefect.flow_runs import suspend_flow_run
from prefect.input import RunInput
from pydantic import Field

from . import asr, executor, remnote, splitter, feedback
from . import recording_types, conversation
from .core import Settings, Manifest, Attention, UnknownOutcome, atomic, digest, file_hash, safe_source, source_metadata, probe, eligibility, read_json


class Decision(RunInput):
    answer: str = Field(description="明确回答当前问题；留空不会批准")
    destination: str = Field(default="", description="需要消歧时填写准确目标名称或路径")
    approved: bool = Field(default=False, description="只有明确批准本次所列具体动作才勾选")
    cancel: bool = False


@task(persist_result=True, cache_policy=INPUTS + TASK_SOURCE, retries=2, retry_delay_seconds=[10, 30])
def outline_task(text: str, model: str, prompt_hash: str, adapter_revision: str):
    return splitter.refine(Settings.load(), text)


@task(persist_result=True, cache_policy=INPUTS + TASK_SOURCE, retries=0)
def split_task(text: str, outline: str, model: str, rule_revision: str):
    return splitter.split(Settings.load(), text, outline)


@flow(name="analyze-recording", log_prints=False, persist_result=True)
def analyze_recording(path: str, canary: bool = False):
    cfg = Settings.load()
    cfg.prepare()
    source = safe_source(Path(path), cfg.inbox)
    recording_id = file_hash(source)
    manifest = Manifest(cfg, recording_id)
    with manifest.lock:
        data = manifest.read()
        paths = data["sources"]
        if str(source) not in paths:
            paths.append(str(source))
        manifest.update(recording_id=recording_id, sources=paths)
    if recording_types.fully_processed(data):
        return {'recording_id': recording_id, 'skipped': 'already_processed'}
    duration = probe(source, cfg.ffprobe)
    provenance = recording_types.recording_provenance(source.name, source_metadata(source, cfg))
    recording_kind, reason = recording_types.kind(cfg, source.name, duration, provenance)
    manifest.update(duration=duration, eligibility=reason, original_name=source.name, provenance=provenance,
                    recording_type=recording_kind, type_standard=digest(cfg.recording_types.read_bytes()))
    if reason != "eligible":
        return {"recording_id": recording_id, "retained_only": reason}
    if not canary and not cfg.enabled:
        raise Attention("ingestion_not_enabled")
    if data.get("segments"):
        parts = data["segments"]
    else:
        if recording_kind == 'conversation':
            transcript, outline = conversation.prepare(cfg, manifest, source)
            text = transcript['text']
            feedback_plan = {'extractor_revision': 'meeting-no-execution'}
        else:
            text = asr.recognize(cfg, manifest, source)
            verdict = recording_types.confirm_self(cfg, manifest, text)
            if verdict != 'single_self':
                manifest.update(eligibility='not_single_self:' + verdict)
                return {'recording_id': recording_id, 'retained_only': 'not_single_self:' + verdict}
            feedback_plan = feedback.prepare(cfg, manifest, text)
            text = feedback_plan['remaining_text']
        refine = lambda body: recording_types.cached_outline(cfg, manifest, body,
            lambda value: outline_task(value, cfg.deepseek_model, splitter.PROMPT_HASH, splitter.REFINE_REVISION))
        if recording_kind != 'conversation':
            outline = refine(text) if text.strip() else ''
        atomic(manifest.folder / "outline.md", outline, text=True)
        parts = ([recording_types.conversation_part(recording_id, text, outline)] if recording_kind == 'conversation'
                 else split_task(text, outline, cfg.deepseek_model, digest(splitter.SYSTEM)) if text.strip() else [])
        version = {"audio": recording_id, "asr":read_json(manifest.folder/'asr.json',{}).get('model',cfg.asr_model), "model":cfg.deepseek_model, "prompt":splitter.PROMPT_HASH, "refine_adapter":splitter.REFINE_REVISION, "splitter":digest(splitter.SYSTEM), "feedback":feedback_plan['extractor_revision'], "schema":1}
        version_id = digest(json.dumps(version, sort_keys=True))
        for item in parts:
            item.setdefault("key", digest(recording_id + ":" + data.get('source_revision','') + str(item["source_spans"]))[:24])
            body = item.get('content_text', item['raw_text'])
            item["markdown"] = (outline if len(parts) == 1 and body == text else
                                refine(body) if body.strip() else '')
            atomic(manifest.folder / (item["key"] + ".md"), item["markdown"], text=True)
            atomic(manifest.folder / (item["key"] + ".json"), {"recording":{"id":recording_id, "audio":str(source), "duration":duration, **provenance}, "analysis_version":version_id, "segment":{k:v for k,v in item.items() if k != "markdown"}})
        atomic(manifest.folder / "analysis-version.json", version)
        manifest.update(segments=parts, prompt_hash=splitter.PROMPT_HASH, model=cfg.deepseek_model, analysis_version=version_id)
    if file_hash(source) != recording_id:
        raise Attention("source_changed_during_analysis")
    runs = manifest.read().get("segment_runs", {})
    for item in parts:
        if item["key"] not in runs:
            run = run_deployment("process-segment/local", parameters={"recording_id": recording_id, "index": item["index"]}, timeout=0, idempotency_key=recording_id + ":" + item["key"], as_subflow=False)
            runs[item["key"]] = str(run.id)
            manifest.update(segment_runs=runs)
    if read_json(manifest.folder/'feedback.json',{}).get('source_spans'):
        feedback.submit(manifest)
    return {"recording_id": recording_id, "analysis": "completed", "segment_runs": runs}


@flow(name='process-workflow-feedback',log_prints=False,persist_result=True)
def process_workflow_feedback(recording_id: str):
    cfg=Settings.load()
    store=Manifest(cfg,recording_id)
    source=read_json(store.folder/'asr.json')
    if not source or not source.get('text'): raise Attention('successful_source_transcript_required')
    item=feedback.prepare(cfg,store,source['text'])
    if not item['source_spans']: return {'status':'no_feedback'}
    if item['cancelled']:
        result={'status':'cancelled','kind':'workflow_feedback'}
        store.save_effect(item['key'],result)
        return result
    if feedback.pending_documents(store.read(),item):
        suspend_flow_run(key='feedback-document-dependency')
        if feedback.pending_documents(store.read(),item): raise Attention('feedback_document_dependency_not_verified')
    from .feedback_delivery import deliver
    return deliver(cfg,store,item['key'],feedback.message(cfg,store,item))


@flow(name="process-segment", log_prints=False, persist_result=True)
def process_segment(recording_id: str, index: int):
    cfg = Settings.load()
    manifest = Manifest(cfg, recording_id)
    data = manifest.read()
    item = dict(data["segments"][index])
    key = item["key"]
    existing = manifest.effect(key)
    if existing and existing["status"] in ("verified", "cancelled"):
        return existing
    if item["cancelled"]:
        manifest.save_effect(key, {"status": "cancelled"})
        return {"status": "cancelled"}
    # Waiting in a separate child deployment leaves other independent segments runnable.
    for dependency in item["depends_on"]:
        previous = manifest.effect(data["segments"][dependency]["key"])
        if not previous or previous["status"] != "verified":
            # Machine dependency, not a human permission question. The watcher resumes
            # this native suspension only after reading verified prerequisite receipts.
            suspend_flow_run(key="dependency")
            previous = manifest.effect(data["segments"][dependency]["key"])
            if not previous or previous["status"] != "verified":
                raise Attention("dependency_not_verified")
    if item['kind']=='command' and item['route']!='thread':
        if item['route']=='script' and item['risk']!='normal':
            # A configured script cannot hold a conversation or interpret an
            # approval. Hand this command to a real Thread before any script
            # process starts; its agent can resolve the specific uncertainty.
            item={**item,'route':'codex'}
        result=perform(cfg,manifest,key,item)
        if item['route']=='script' and result['status']=='failed' and not result.get('repair_thread_id'):
            # The failing script had no native Thread. Create exactly one repair
            # task through the same official SDK and keep its exact native ID.
            repair={**item,'route':'codex','title':'修复脚本：'+item.get('action',''),
                    'raw_text':'用户配置脚本执行失败，请分析并修复该脚本。原始任务：'+item['raw_text']
                      +'\n脚本 ID：'+item.get('action','')+'\n本次执行日志目录：'+result['output_dir']
                      +'\n退出码：'+str(result.get('exit_code'))
                      +'\n修复工作流并验证；不要盲目重放可能已产生副作用的原动作。'}
            for field in ('markdown','content_text','control_spans'):
                repair.pop(field,None)
            repaired=executor.execute(cfg,manifest,key+'-repair',repair)
            result={**result,'repair_thread_id':repaired.get('session_id'),'repair_status':repaired['status']}
            manifest.save_effect(key,result)
        return result
    if item['kind']!='command':
        # Unclear knowledge classification is still archival content. Routing
        # chooses a static Daily branch or 其他碎片; no separate question form.
        while True:
            try:
                return perform(cfg,manifest,key,item)
            except UnknownOutcome as exc:
                if str(exc) not in remnote.RECONNECT_REASONS:
                    raise
                # A machine outage releases the worker. The existing effect key
                # reconciles a possibly completed write after reconnecting.
                # A fresh pause key handles a second disconnect after resuming.
                suspend_flow_run(key='remnote-connection:'+str(uuid4()))
    if item["route"] == "thread":
        suspend_flow_run(wait_for_input=Decision.with_initial_data(description="此命令明确指定桌面端 Thread，请由有原生工具的 Codex 回合投递；外部服务不会代替。"), key="native-thread")
        raise Attention("native_thread_result_requires_readback")
    raise Attention("unsupported_command_route")


def perform(cfg, manifest, key, item):
    if item["kind"] == "command":
        return executor.execute(cfg, manifest, key, item)
    metadata = f"类型：{item['kind']}\n录音：{manifest.read()['original_name']}\n时长：{manifest.read()['duration']} 秒\n来源：{manifest.read()['sources'][0]}"
    # Serialize document writes, but do not hold this lock while waiting for input.
    from filelock import FileLock
    with FileLock(str(cfg.root / "locks/remnote-write.lock")):
        return asyncio.run(remnote.publish(cfg, manifest, key, item["markdown"], item["destination"], metadata, raw_text=item['raw_text'],item=item))


def serve_local():
    from .command_inbox import deployments
    from .repairs import repair_recording
    serve(analyze_recording.to_deployment(name="local", concurrency_limit=2),
          process_segment.to_deployment(name="local", concurrency_limit=2),
          process_workflow_feedback.to_deployment(name='local',concurrency_limit=1),
          repair_recording.to_deployment(name="local", concurrency_limit=1),
          *deployments(), pause_on_shutdown=False, limit=9)
