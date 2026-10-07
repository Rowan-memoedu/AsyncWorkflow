"""Explicit stage repairs reuse successful ASR and preserve completed effects.

Pure plans and prior receipts are retained before corrections. No command is replayed.
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from filelock import FileLock
from prefect import flow
from prefect.runtime import flow_run
from . import remnote, routing, splitter
from .core import Settings, Manifest, Attention, atomic, digest, read_json, now


def identity(cfg, recording_id, stage):
    if stage not in ('split','content','routing','publish'): raise Attention('unknown_repair_stage')
    versions={
        'recording':recording_id,'stage':stage,
        'repair':digest(Path(__file__).read_bytes()),
        'routing':digest(Path(routing.__file__).read_bytes()),
        'split':digest(splitter.SYSTEM) if stage in ('split','content') else None,
        'template':read_json(cfg.root/'daily-template.json',{}).get('version'),
        'targets':read_json(cfg.root/'todo-targets.json',{}).get('version'),
        'format':'content-with-separators',
    }
    return digest(json.dumps(versions,sort_keys=True))[:24]


def prepare(cfg,store,stage,operation):
    if stage not in ('split','content','routing','publish'): raise Attention('unknown_repair_stage')
    folder=store.folder/'repairs'/operation
    for prior in folder.parent.glob('*/before.json'):
        if prior.parent != folder and not read_json(prior.parent/'plan.json',{}).get('completed'):
            raise Attention('unfinished_repair_requires_same_operation:'+prior.parent.name)
    before=read_json(folder/'before.json')
    if not before:
        before=store.read()
        if not before.get('segments'): raise Attention('successful_analysis_required')
        if any(p['kind']=='command' for p in before['segments']): raise Attention('command_repair_requires_existing_thread_reconciliation')
        atomic(folder/'before.json',before)
    existing=read_json(folder/'plan.json')
    if existing: return folder,before,existing
    transcript=read_json(store.folder/'asr.json')
    if not transcript or not transcript.get('text'): raise Attention('successful_source_transcript_required')
    old=before['segments']
    if before.get('recording_type') == 'conversation':
        # Meeting speech is always complete archival data, even on a repair.
        # Container-command extraction and the normal command splitter do not apply.
        parts=[dict(p) for p in old]
    elif stage=='split':
        outline=(store.folder/'outline.md').read_text(encoding='utf-8')
        feedback_plan=read_json(store.folder/'feedback.json')
        text=transcript['text']
        if feedback_plan:
            if feedback_plan['source_hash']!=digest(text):
                raise Attention('feedback_source_changed_requires_review')
            text=feedback_plan['remaining_text']
        parts=splitter.split(cfg,text,outline) if text.strip() else []
    elif stage=='content': parts=[splitter.extract_content(cfg,p) for p in old]
    else: parts=[dict(p) for p in old]
    if any(p['kind']=='command' for p in parts): raise Attention('repair_will_not_create_or_replay_commands')
    used=set()
    refinements=0
    for part in parts:
        origin=next((p for p in old if p['raw_text']==part['raw_text']),None)
        if origin: part['source_effect']=origin['key']
        same=next((p for p in old if p['index'] not in used and p['raw_text']==part['raw_text']
                   and p.get('content_text',p['raw_text'])==part.get('content_text',part['raw_text'])),None)
        if same:
            used.add(same['index'])
            part['markdown']=same['markdown']
            part['reuses_effect']=same['key']
        else:
            part.pop('reuses_effect',None)
            body=part.get('content_text',part['raw_text'])
            part['markdown']=splitter.refine(cfg,body) if body.strip() else ''
            refinements+=1
        part['key']=digest(store.folder.name+operation+str(part['source_spans']))[:24]
    plan={'operation':operation,'stage':stage,'at':now(),'source_transcript_hash':digest(transcript['text']),
          'reused_asr':True,'new_outline_calls':refinements,'segments':parts,'routes':{},'corrections':{},'archived':{}}
    atomic(folder/'plan.json',plan)
    return folder,before,plan


async def apply(cfg,store,folder,before,plan):
    if not cfg.trash_rem_id: raise Attention('repair_trash_not_bound')
    async with remnote.connection(cfg) as session:
        # Decide every destination before any correction. Freeze the decisions on disk.
        for part in plan['segments']:
            key=part['key']
            if part.get('cancelled'): continue
            if key in plan['routes']: continue
            old=before['effects'].get(part.get('source_effect',part.get('reuses_effect','')))
            if plan['stage'] in ('publish','content') and old:
                route=old['routing']
            else:
                route=await routing.plan(cfg,session,remnote.call,part['destination'],part['raw_text'],before['provenance'],item=part)
            plan['routes'][key]=route
            atomic(folder/'plan.json',plan)
        retained=set()
        for part in plan['segments']:
            key=part['key']
            old=before['effects'].get(part.get('reuses_effect',''))
            source_effect=before['effects'].get(part.get('source_effect',''),old)
            removal=store.read().get('external_removals',{}).get((source_effect or {}).get('root_id',''))
            if removal:
                part['cancelled']=True
                effect={'status':'cancelled','reason':'user_kept_removed_output','root_id':source_effect['root_id'],
                        'decision':removal,'reuses_effect':part.get('source_effect')}
                store.save_effect(key,effect)
                plan['corrections'][key]=effect
                retained.add(source_effect['root_id'])
                atomic(folder/'plan.json',plan)
                continue
            if part.get('cancelled'):
                store.save_effect(key,{'status':'cancelled'})
                continue
            if key in plan['corrections']:
                if plan['corrections'][key].get('root_id'): retained.add(plan['corrections'][key]['root_id'])
                continue
            route=plan['routes'][key]
            target=({k:source_effect[k] for k in ('parent','daily_id','created_document_id') if k in source_effect}
                    if plan['stage'] in ('publish','content') and source_effect
                    else await remnote.resolve_target(cfg,session,route,store.folder.name))
            log={'recording':before.get('original_name'),'provenance':before.get('provenance'),
                 'sources':before.get('sources'),'repair_operation':plan['operation'],'stage':plan['stage']}
            if old and old.get('root_id') and old.get('status')=='verified' and old.get('backend')=='independent-remnote-plugin':
                plugin_effect=old.get('plugin_effect_key',store.folder.name+'/'+part['reuses_effect'])
                result=await remnote.call(session,'relocate_outline',effect_key=plugin_effect,
                    operation_id=store.folder.name+'/'+plan['operation']+'/'+key,
                    parent_id=target['parent'],trash_id=cfg.trash_rem_id,strip_metadata=True)
                if result.get('status')!='verified' or result.get('root_id')!=old['root_id']:
                    raise Attention('repair_readback_mismatch')
                effect={'backend':'independent-remnote-plugin','status':'verified','root_id':result['root_id'],
                        **target,'routing':route,'plugin_effect_key':plugin_effect,'content_format':'content-with-separators',
                        'separators':result.get('separators'),
                        'metadata_log':log,'repair_operation':plan['operation'],'reuses_effect':part['reuses_effect'],
                        'metadata_archived_id':result.get('metadata_archived_id'),'verified_at':now()}
                store.save_effect(key,effect)
            else:
                if not store.effect(key):
                    store.save_effect(key,{'backend':'independent-remnote-plugin','status':'not_started',**target,'routing':route})
                effect=await remnote.publish(cfg,store,key,part['markdown'],part['destination'],json.dumps(log,ensure_ascii=False),raw_text=part['raw_text'],item=part)
            if effect.get('root_id'): retained.add(effect['root_id'])
            plan['corrections'][key]=effect
            atomic(folder/'plan.json',plan)
        # An obsolete owned output is archived only after all replacement results verify.
        for old_part in before['segments']:
            old=before['effects'].get(old_part['key'],{})
            if not old.get('root_id') or old['root_id'] in retained or old_part['key'] in plan['archived']: continue
            if old.get('backend')!='independent-remnote-plugin' or old.get('status')!='verified':
                raise Attention('old_effect_requires_reconciliation')
            result=await remnote.call(session,'relocate_outline',
                effect_key=old.get('plugin_effect_key',store.folder.name+'/'+old_part['key']),
                operation_id=store.folder.name+'/'+plan['operation']+'/archive-'+old_part['key'],
                parent_id=cfg.trash_rem_id,trash_id=cfg.trash_rem_id,strip_metadata=False)
            if result.get('status')!='verified': raise Attention('old_effect_archive_unverified')
            plan['archived'][old_part['key']]=result
            atomic(folder/'plan.json',plan)
    return plan


@flow(name='repair-recording',log_prints=False,persist_result=True)
def repair_recording(recording_id:str,stage:str,operation_id:str):
    cfg=Settings.load()
    store=Manifest(cfg,recording_id)
    with FileLock(str(cfg.root/'locks'/('repair-'+recording_id+'.lock'))):
        folder,before,plan=prepare(cfg,store,stage,operation_id)
        if plan.get('completed'): return {'operation':operation_id,'completed':True,'reused':True}
        with FileLock(str(cfg.root/'locks/remnote-write.lock')):
            plan=asyncio.run(apply(cfg,store,folder,before,plan))
        store.update(segments=plan['segments'],segment_runs={},latest_repair_run=str(flow_run.id),
                     latest_repair_operation=operation_id)
        plan['completed']=True
        plan['completed_at']=now()
        plan['flow_run_id']=str(flow_run.id)
        atomic(folder/'plan.json',plan)
        return {'operation':operation_id,'completed':True,'reused_asr':True,'new_outline_calls':plan['new_outline_calls'],
                'roots':[v['root_id'] for v in plan['corrections'].values() if v.get('status')=='verified' and v.get('root_id')],
                'kept_removed':[v['root_id'] for v in plan['corrections'].values() if v.get('reason')=='user_kept_removed_output']}
