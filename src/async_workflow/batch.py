"""Finite, sequential manual batches, using existing Prefect jobs and manifests."""
from __future__ import annotations
import json
import time
from datetime import datetime
from uuid import UUID, uuid4
from .core import Attention, Manifest, atomic, file_hash, now, source_metadata
from .ingest import audio_files, submit_file
from .recording_types import recording_provenance


def recent(cfg, count, cutoff=None):
    values=[]
    for path in audio_files(cfg.inbox):
        metadata=recording_provenance(path.name,source_metadata(path,cfg))
        stamp=metadata.get('recorded_at')
        if not stamp:
            continue
        parsed=datetime.fromisoformat(stamp)
        if cutoff is None or parsed<=cutoff:
            values.append((parsed,path))
    return [p for _,p in sorted(sorted(values, key=lambda v:(v[0],str(v[1])), reverse=True)[:count],key=lambda v:v[0])]


def wait_recording(client, store, timeout=3600):
    deadline=time.monotonic()+timeout
    previous=None
    while time.monotonic()<deadline:
        data=store.read()
        ids=([data['analysis_run']] if data.get('analysis_run') else [])+list(data.get('segment_runs',{}).values())
        if data.get('workflow_feedback_run'):
            ids.append(data['workflow_feedback_run'])
        states={run_id:client.read_flow_run(UUID(run_id)).state for run_id in ids}
        snapshot={key:value.name if value else 'Pending' for key,value in states.items()}
        if snapshot!=previous:
            print(json.dumps({'recording_id':store.folder.name,'states':snapshot},ensure_ascii=False),flush=True)
            previous=snapshot
        if states and all(state and (state.is_final() or state.is_paused()) for state in states.values()):
            return snapshot
        time.sleep(3)
    raise Attention('batch_wait_timeout_existing_runs_retained')


def run(cfg, paths, *, manual=False):
    from prefect.client.orchestration import get_client
    receipt=cfg.artifacts.parent/'batches'/(datetime.now().strftime('%Y-%m-%d')+'-'+uuid4().hex+'.json')
    data={'started_at':now(),'sources':[str(p) for p in paths],'records':[]}
    atomic(receipt,data)
    print(json.dumps({'batch_receipt':str(receipt),'count':len(paths)},ensure_ascii=False),flush=True)
    with get_client(sync_client=True) as client:
        for path in paths:
            print(json.dumps({'processing':path.name},ensure_ascii=False),flush=True)
            result=submit_file(cfg,path,canary=manual)
            store=Manifest(cfg,result.get('group_id') or file_hash(path))
            states={} if result.get('skipped') else wait_recording(client,store)
            current=store.read()
            row={'name':path.name,**result,'states':states,'eligibility':current.get('eligibility'),
                 'effects':{key:value.get('status') for key,value in current.get('effects',{}).items()}}
            data['records'].append(row)
            atomic(receipt,data)
            print(json.dumps(row,ensure_ascii=False),flush=True)
    data['finished_at']=now()
    atomic(receipt,data)
    return receipt
