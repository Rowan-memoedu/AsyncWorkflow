from __future__ import annotations
import time
from datetime import datetime
from pathlib import Path

from watchfiles import watch as watchfiles
from prefect.deployments import run_deployment

from .core import Settings, Attention, Manifest, EXTENSIONS, safe_source, source_metadata, file_hash, recorded_time, read_json, atomic, now
from .recording_types import fully_processed


def submission_receipt(manifest, result):
    data = manifest.read()
    history = data.get('submission_log', [])
    manifest.update(submission_log=[*history, {'at': now(), **result}])
    return result


def continue_runs(data):
    """Retry existing native failures; never create replacement jobs or effects."""
    from uuid import UUID
    from prefect.client.orchestration import get_client
    from prefect.states import Scheduled
    ids = ([data['analysis_run']] if data.get('analysis_run') else []) + list(data.get('segment_runs', {}).values())
    if data.get('workflow_feedback_run'):
        ids.append(data['workflow_feedback_run'])
    retried = []
    with get_client(sync_client=True) as client:
        for run_id in dict.fromkeys(ids):
            run = client.read_flow_run(UUID(run_id))
            if run.state and (run.state.is_failed() or run.state.is_crashed()):
                result = client.set_flow_run_state(run.id, Scheduled(name='AwaitingRetry'), force=True)
                if result.status.value != 'ACCEPT':
                    raise Attention('native_retry_not_accepted')
                retried.append(run_id)
    return retried


def audio_files(root: Path):
    # pathlib.walk does not follow directory symlinks; reparse checks below are mandatory.
    import os
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink() and not Path(directory, d).is_junction()]
        for name in files:
            path = Path(directory, name)
            if path.suffix.lower() in EXTENSIONS:
                yield safe_source(path, root)


def baseline(cfg: Settings):
    target = cfg.root / "baseline.json"
    if target.exists():
        return
    atomic(target, {"created_at": now(), "hashes": [file_hash(p) for p in audio_files(cfg.inbox)]})


def submit_file(cfg: Settings, path: Path, *, canary=False):
    source = safe_source(path, cfg.inbox)
    first = source.stat()
    time.sleep(cfg.stability_seconds)
    second = source.stat()
    if first.st_size == 0 or (first.st_size, first.st_mtime_ns) != (second.st_size, second.st_mtime_ns):
        raise Attention("source_not_stable")
    identity = file_hash(source)
    manifest = Manifest(cfg, identity)
    with manifest.lock:
        data = manifest.read()
        paths = data["sources"]
        if str(source) not in paths:
            manifest.update(sources=[*paths, str(source)])
        if fully_processed(data):
            return submission_receipt(manifest, {'recording_id': identity, 'run_id': data.get('analysis_run'),
                                      'duplicate': True, 'skipped': 'already_processed'})
        if data.get("analysis_run"):
            retried = continue_runs(data)
            return submission_receipt(manifest, {'recording_id': identity, 'run_id': data['analysis_run'],
                                      'reused': True, 'retry_runs': retried})
        if not canary:
            if not cfg.enabled or not cfg.enabled_at:
                raise Attention("ingestion_disabled")
            initial = read_json(cfg.root / "baseline.json", {})
            if identity in initial.get("hashes", []):
                manifest.update(recording_id=identity, eligibility="historical_baseline")
                return {"retained_only": "historical_baseline"}
            provenance = source_metadata(source, cfg)
            cloud = read_json(cfg.root / "xiaomi-baseline.json", {})
            if provenance.get("source") == "xiaomi" and provenance["asset_id"] in cloud.get("asset_ids", []):
                manifest.update(recording_id=identity, eligibility="historical_baseline", provenance=provenance)
                return {"retained_only": "historical_baseline"}
            timestamp = datetime.fromisoformat(provenance["recorded_at"]) if provenance.get("recorded_at") else None
            if timestamp and timestamp < datetime.fromisoformat(cfg.enabled_at):
                manifest.update(recording_id=identity, eligibility="historical_baseline", sources=[str(source)])
                return {"retained_only": "historical_baseline"}
        run = run_deployment("analyze-recording/local", parameters={"path": str(source), "canary": canary}, timeout=0, idempotency_key="audio:" + identity)
        manifest.update(recording_id=identity, analysis_run=str(run.id))
        return submission_receipt(manifest, {"run_id": str(run.id), "recording_id": identity,
                                  'reused_asr': (manifest.folder/'asr.json').exists()})


def watch(cfg: Settings):
    def sweep():
        current = Settings.load()
        if not current.enabled:
            atomic(current.root/'watcher-status.json', {'checked_at':now(),'enabled':False,'submitted':0})
            return
        historical_ids = {str(value) for value in read_json(current.root / "xiaomi-baseline.json", {}).get("asset_ids", [])}
        historical_hashes = set(read_json(current.root / "baseline.json", {}).get("hashes", []))
        attempted = 0
        submitted = 0
        for path in audio_files(current.inbox):
            if attempted >= current.max_batch:
                break
            provenance = source_metadata(path, current)
            if provenance.get("source") == "xiaomi" and provenance["asset_id"] in historical_ids:
                continue
            timestamp = datetime.fromisoformat(provenance['recorded_at']) if provenance.get('recorded_at') else None
            if current.enabled_at and timestamp and timestamp < datetime.fromisoformat(current.enabled_at):
                continue
            # An existing manifest is cheap to inspect; failures remain visible in Prefect.
            identity = file_hash(path)
            if identity in historical_hashes:
                continue
            existing = Manifest(current, identity).read()
            if existing.get("analysis_run") or existing.get("eligibility") == "historical_baseline":
                continue
            attempted += 1
            try:
                result=submit_file(current, path)
                submitted += bool(result.get('run_id'))
            except Attention as exc:
                atomic(current.root / "last-ingest-error.json", {"time": now(), "reason": str(exc)})
        atomic(current.root/'watcher-status.json', {'checked_at':now(),'enabled':True,
               'enabled_at':current.enabled_at,'attempted':attempted,'submitted':submitted})
    try:
        recover(cfg)
        sweep()
    except Exception as exc:
        atomic(cfg.root / "last-ingest-error.json", {"time":now(), "error_type":type(exc).__name__})
    for _ in watchfiles(cfg.inbox, recursive=True, yield_on_timeout=True, rust_timeout=60000):
        try:
            recover(cfg)
            sweep()
        except Exception as exc:
            atomic(cfg.root / "last-ingest-error.json", {"time": now(), "error_type": type(exc).__name__})


def recover(cfg):
    """Consult Prefect run state, never maintain another scheduling queue."""
    from uuid import UUID
    from prefect.client.orchestration import get_client
    from prefect.states import Scheduled
    from prefect.flow_runs import resume_flow_run
    import asyncio
    from . import remnote
    remnote_ready = None
    with get_client(sync_client=True) as client:
        for file in cfg.artifacts.glob("*/manifest.json"):
            data = read_json(file)
            ids = ([data["analysis_run"]] if data.get("analysis_run") else []) + list(data.get("segment_runs", {}).values())
            if data.get('workflow_feedback_run'):
                ids.append(data['workflow_feedback_run'])
            for run_id in ids:
                run = client.read_flow_run(UUID(run_id))
                if run.state.is_crashed() and run.run_count < 3:
                    # Every external action reconciles its manifest before doing any work.
                    client.set_flow_run_state(run.id, Scheduled(name="AwaitingRetry"), force=True)
                if run.state.is_paused() and run.state.state_details.pause_key == "dependency":
                    segment = next((s for s in data.get("segments", []) if data.get("segment_runs", {}).get(s["key"]) == run_id), None)
                    if segment and all(data["effects"].get(data["segments"][i]["key"], {}).get("status") == "verified" for i in segment["depends_on"]):
                        resume_flow_run(run.id)
                if run.state.is_paused() and run.state.state_details.pause_key == 'feedback-document-dependency':
                    from .feedback import pending_documents
                    if not pending_documents(data):
                        resume_flow_run(run.id)
                if run.state.is_paused() and (run.state.state_details.pause_key or '').startswith('remnote-connection:'):
                    if remnote_ready is None:
                        remnote_ready=asyncio.run(remnote.is_ready(cfg))
                    if remnote_ready:
                        resume_flow_run(run.id)
