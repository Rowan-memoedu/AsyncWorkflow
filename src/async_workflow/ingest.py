from __future__ import annotations
import time
from datetime import datetime
from pathlib import Path

from watchfiles import watch as watchfiles
from prefect.deployments import run_deployment

from .core import Settings, Attention, Manifest, EXTENSIONS, safe_source, source_metadata, file_hash, recorded_time, read_json, atomic, now


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
        if data.get("analysis_run"):
            return {"run_id": data["analysis_run"], "duplicate": True}
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
        return {"run_id": str(run.id), "recording_id": identity}


def watch(cfg: Settings):
    def sweep():
        current = Settings.load()
        if not current.enabled:
            return
        attempted = 0
        for path in audio_files(current.inbox):
            if attempted >= current.max_batch:
                break
            # An existing manifest is cheap to inspect; failures remain visible in Prefect.
            identity = file_hash(path)
            existing = Manifest(current, identity).read()
            if existing.get("analysis_run") or existing.get("eligibility") == "historical_baseline":
                continue
            attempted += 1
            try:
                submit_file(current, path)
            except Attention as exc:
                atomic(current.root / "last-ingest-error.json", {"time": now(), "reason": str(exc)})
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
    with get_client(sync_client=True) as client:
        for file in cfg.artifacts.glob("*/manifest.json"):
            data = read_json(file)
            ids = ([data["analysis_run"]] if data.get("analysis_run") else []) + list(data.get("segment_runs", {}).values())
            for run_id in ids:
                run = client.read_flow_run(UUID(run_id))
                if run.state.is_crashed() and run.run_count < 3:
                    # Every external action reconciles its manifest before doing any work.
                    client.set_flow_run_state(run.id, Scheduled(name="AwaitingRetry"), force=True)
                if run.state.is_paused() and run.state.state_details.pause_key == "dependency":
                    segment = next((s for s in data.get("segments", []) if data.get("segment_runs", {}).get(s["key"]) == run_id), None)
                    if segment and all(data["effects"].get(data["segments"][i]["key"], {}).get("status") == "verified" for i in segment["depends_on"]):
                        resume_flow_run(run.id)
