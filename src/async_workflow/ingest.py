from __future__ import annotations
import time
from datetime import datetime
from pathlib import Path

from prefect.deployments import run_deployment

from .core import Settings, Attention, Manifest, EXTENSIONS, safe_source, source_metadata, file_hash, recorded_time, read_json, atomic, now
from .recording_types import fully_processed

SCAN_INTERVAL_SECONDS = 1800


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
    from .recovery import run_ids
    ids = run_ids(data)
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
    if cfg.conversation_profiles:
        from . import meetings, xiaomi
        from .recording_types import meeting_contact
        xiaomi.refresh_recording_names(cfg)
        if meeting_contact(cfg, source.name, source_metadata(source, cfg)):
            identity = file_hash(source)
            group = next(g for g in meetings.discover(cfg, refresh=False)
                         if any(m['id'] == identity for m in g['members']))
            return meetings.submit(cfg, group, manual=canary)
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


def scan_ticks():
    while True:
        time.sleep(SCAN_INTERVAL_SECONDS)
        yield


def watch(cfg: Settings):
    def sweep():
        current = Settings.load()
        if not current.enabled:
            atomic(current.root/'watcher-status.json', {'checked_at':now(),'enabled':False,'submitted':0,
                   'scan_interval_seconds':SCAN_INTERVAL_SECONDS})
            return
        historical_ids = {str(value) for value in read_json(current.root / "xiaomi-baseline.json", {}).get("asset_ids", [])}
        historical_hashes = set(read_json(current.root / "baseline.json", {}).get("hashes", []))
        attempted = 0
        submitted = 0
        meeting_paths = set()
        if current.conversation_profiles:
            from . import meetings
            for group in meetings.discover(current):
                meeting_paths.update(m['path'] for m in group['members'])
                if not meetings.automatic_allowed(current, group):
                    continue
                result = meetings.submit(current, group)
                submitted += bool(result.get('run_id') and not result.get('reused'))
        for path in audio_files(current.inbox):
            if str(path) in meeting_paths:
                continue
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
               'enabled_at':current.enabled_at,'attempted':attempted,'submitted':submitted,
               'scan_interval_seconds':SCAN_INTERVAL_SECONDS})
    def cycle():
        # A recovery service outage must not prevent ingestion, or vice versa.
        for operation in (lambda: recover(Settings.load()), sweep):
            try:
                operation()
            except Exception as exc:
                atomic(cfg.root / "last-ingest-error.json", {"time": now(), "error_type": type(exc).__name__})
    cycle()
    for _ in scan_ticks():
        cycle()


def recover(cfg):
    """Consult Prefect run state, never maintain another scheduling queue."""
    from uuid import UUID
    from prefect.client.orchestration import get_client
    from prefect.states import Scheduled
    from prefect.flow_runs import resume_flow_run
    import asyncio
    from . import remnote
    from .recovery import run_ids, local_workers, orphaned, unresolved_completed
    remnote_ready = None
    report = {'checked_at': now(), 'interval_seconds': SCAN_INTERVAL_SECONDS, 'retried': [], 'resumed': [], 'errors': []}
    processes = None
    seen = set()
    with get_client(sync_client=True) as client:
        for file in cfg.artifacts.glob("*/manifest.json"):
            try:
                data = read_json(file)
            except (OSError, ValueError):
                report['errors'].append({'recording_id': file.parent.name, 'error': 'manifest_unreadable'})
                continue
            for run_id in run_ids(data):
                if run_id in seen:
                    continue
                seen.add(run_id)
                try:
                    run = client.read_flow_run(UUID(run_id))
                    if run.state is None:
                        continue
                    if run.state.is_running() or run.state.is_pending():
                        if processes is None:
                            processes = local_workers()
                    if (run.state.is_failed() or run.state.is_crashed()
                            or orphaned(run, data, processes or (set(), False))
                            or unresolved_completed(run, data)):
                        result = client.set_flow_run_state(run.id, Scheduled(name="AwaitingRetry"), force=True)
                        if result is not None and result.status.value != 'ACCEPT':
                            raise Attention('native_retry_not_accepted')
                        report['retried'].append(run_id)
                    if not run.state.is_paused():
                        continue
                    pause = run.state.state_details.pause_key or ''
                    ready = False
                    if pause == 'native-thread':
                        from .dispatch_policy import addressed, has_submission
                        segment = next((s for s in data.get('segments', []) if data.get('segment_runs', {}).get(s['key']) == run_id), None)
                        if segment and not addressed(segment.get('raw_text', '')) and not has_submission(data.get('effects', {}).get(segment['key'])):
                            # Re-evaluate a legacy false command; never manufacture a human answer.
                            client.set_flow_run_state(run.id, Scheduled(name='AwaitingRetry'), force=True)
                            report['retried'].append(run_id)
                    if pause == 'dependency':
                        segment = next((s for s in data.get('segments', []) if data.get('segment_runs', {}).get(s['key']) == run_id), None)
                        ready = bool(segment and all(data.get('effects', {}).get(data['segments'][i]['key'], {}).get('status') == 'verified' for i in segment['depends_on']))
                    elif pause == 'feedback-document-dependency':
                        from .feedback import pending_documents
                        ready = not pending_documents(data)
                    elif pause.startswith('remnote-connection:'):
                        if remnote_ready is None:
                            remnote_ready = asyncio.run(remnote.is_ready(cfg))
                        ready = remnote_ready
                    # Human input stays pending. It is never answered automatically.
                    if ready:
                        resume_flow_run(run.id)
                        report['resumed'].append(run_id)
                except Exception as exc:
                    report['errors'].append({'run_id': run_id, 'error': type(exc).__name__})
    report['checked_runs'] = len(seen)
    atomic(cfg.root / 'recovery-status.json', report)
    return report
