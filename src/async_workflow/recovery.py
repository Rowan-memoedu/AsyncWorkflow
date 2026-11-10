"""Read native run/process evidence; scheduling remains entirely in Prefect."""
from __future__ import annotations

import os
import time
import psutil

from .core import now


def run_ids(data):
    values = [data.get(k) for k in ('analysis_run', 'workflow_feedback_run', 'latest_repair_run', 'meeting_run')]
    values.extend(data.get('segment_runs', {}).values())
    return list(dict.fromkeys(str(value) for value in values if value))


def worker_started(store):
    from prefect.runtime import flow_run
    if not flow_run.id:
        return
    process = psutil.Process()
    with store.lock:
        workers = store.read().get('workers', {})
        workers[flow_run.id] = {'pid': os.getpid(), 'created': process.create_time(), 'started_at': now()}
        store.update(workers=workers)


def local_workers():
    """Never infer a dead worker from age alone or from an inaccessible process."""
    ids, complete = set(), True
    for process in psutil.process_iter(['name']):
        if 'python' not in (process.info['name'] or '').lower():
            continue
        try:
            command = process.cmdline()
            if any('prefect' in arg for arg in command):
                if value := process.environ().get('PREFECT__FLOW_RUN_ID'):
                    ids.add(value)
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError):
            complete = False
    return ids, complete


def orphaned(run, data, snapshot):
    if not (run.state.is_running() or run.state.is_pending()):
        return False
    stamp = getattr(run.state, 'timestamp', None)
    if not stamp or time.time() - stamp.timestamp() < 1800:
        return False
    ids, complete = snapshot
    if not complete or str(run.id) in ids:
        return False
    worker = data.get('workers', {}).get(str(run.id))
    if worker:
        try:
            if psutil.Process(worker['pid']).create_time() == worker['created']:
                return False
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, OSError):
            return False
    return True


def unresolved_completed(run, data):
    """Completed orchestration can still contain an unobserved external receipt."""
    if not run.state.is_completed():
        return False
    key = next((k for k,v in data.get('segment_runs', {}).items() if v == str(run.id)), None)
    if str(run.id) == data.get('workflow_feedback_run'):
        key = data.get('workflow_feedback', {}).get('key')
    from .command_archive import replacement
    if replacement(data, key):
        return False
    return bool(key and data.get('effects', {}).get(key, {}).get('status') in
                ('not_started', 'submitted', 'outcome_unknown', 'failed'))
