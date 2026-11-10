"""One native Prefect run and one archival result per contact and recording day."""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from filelock import FileLock
from prefect import flow
from prefect.deployments import run_deployment
from . import conversation, conversation_asr, meeting_audio, remnote, routing
from .core import Attention, UnknownOutcome, Settings, Manifest, atomic, digest, file_hash, now, probe, read_json, safe_source, source_metadata
from .recording_types import meeting_contact, recording_provenance

REVISION = 'contact-day-1'


def remember_document(cfg, contact, document_id):
    # A user may rename the auto-created document before the next recording day.
    with FileLock(str(cfg.root/'locks/meeting-targets.lock')):
        path = cfg.root/'meeting-targets.json'
        values = read_json(path, {})
        if values.get(contact) != document_id:
            values[contact] = document_id
            atomic(path, values)


def discover(cfg, *, refresh=True):
    from .ingest import audio_files
    if refresh:
        from .xiaomi import refresh_recording_names
        refresh_recording_names(cfg)
    groups = {}
    for path in audio_files(cfg.inbox):
        provenance = recording_provenance(path.name, source_metadata(path, cfg))
        contact = meeting_contact(cfg, path.name, provenance)
        if not contact:
            continue
        day = routing.recorded_day(provenance)
        key = (contact, day)
        identity = file_hash(path)
        store = Manifest(cfg, identity)
        group = groups.setdefault(key, {'contact': contact, 'day': day, 'members': {}})
        member = {'id': identity, 'path': str(path), 'name': provenance.get('cloud_file_name', path.name),
                  'provenance': provenance, 'duration': store.read().get('duration') or probe(path, cfg.ffprobe)}
        existing = group['members'].get(identity)
        if not existing or datetime.fromisoformat(member['provenance']['recorded_at']) < datetime.fromisoformat(existing['provenance']['recorded_at']):
            group['members'][identity] = member
    result = []
    for group in groups.values():
        group['members'] = sorted(group['members'].values(), key=lambda m:(datetime.fromisoformat(m['provenance']['recorded_at']), m['name']))
        group['id'] = digest('meeting-day:'+group['contact']+':'+group['day'])
        group['revision'] = digest(json.dumps([REVISION, [m['id'] for m in group['members']], cfg.conversation_profiles[group['contact']],
                                              conversation.POLICY], ensure_ascii=False, sort_keys=True))[:24]
        result.append(group)
    return sorted(result, key=lambda g:(g['day'], g['contact']))


def automatic_allowed(cfg, group):
    if not cfg.enabled or not cfg.enabled_at:
        return False
    old_ids = set(read_json(cfg.root/'xiaomi-baseline.json', {}).get('asset_ids', []))
    old_hashes = set(read_json(cfg.root/'baseline.json', {}).get('hashes', []))
    for member in group['members']:
        provenance = member['provenance']
        if (member['id'] not in old_hashes and provenance.get('asset_id') not in old_ids
                and datetime.fromisoformat(provenance['recorded_at']) >= datetime.fromisoformat(cfg.enabled_at)):
            return True
    return False


def submit(cfg, group, *, manual=False):
    store = Manifest(cfg, group['id'])
    with store.lock:
        previous = store.read()
        if previous.get('completed_revision') == group['revision']:
            effect = previous.get('effects', {}).get(previous.get('latest_effect'), {})
            if effect.get('status') == 'verified':
                remember_document(cfg, group['contact'], effect['routing']['document_id'])
            return {'group_id': group['id'], 'skipped': 'already_processed'}
        if not manual and not automatic_allowed(cfg, group):
            return {'group_id': group['id'], 'skipped': 'historical_baseline'}
        if previous.get('submitted_revision') == group['revision'] and previous.get('analysis_run'):
            from .ingest import continue_runs
            retried = continue_runs(previous) if manual else []
            return {'group_id': group['id'], 'run_id': previous['analysis_run'], 'reused': True, 'retry_runs': retried}
        stats = [(Path(m['path']).stat().st_size, Path(m['path']).stat().st_mtime_ns) for m in group['members']]
        time.sleep(cfg.stability_seconds)
        if any(s[0] == 0 or s != (Path(m['path']).stat().st_size, Path(m['path']).stat().st_mtime_ns)
               for s, m in zip(stats, group['members'])):
            raise Attention('source_not_stable')
        run = run_deployment('process-meeting-day/local', parameters={'group': group}, timeout=0,
                             idempotency_key='meeting:'+group['id']+':'+group['revision'], as_subflow=False)
        store.update(recording_id=group['id'], recording_type='meeting_day',
                     analysis_run=str(run.id), submitted_revision=group['revision'])
        return {'group_id': group['id'], 'run_id': str(run.id), 'files': len(group['members'])}


def transcript(cfg, store, group, profile):
    path = store.folder/'days'/group['revision']/'transcript.json'
    saved = read_json(path)
    if saved:
        return saved
    turns, speakers, offset, references = [], {}, 0, []
    if profile['mode'] == 'diarized':
        # Successful batches survive later same-day arrivals. Only missing sources
        # are concatenated for a new native diarization call.
        batches = store.read().get('speech_batches', [])
        wanted = {m['id'] for m in group['members']}
        used = {i for batch in batches for i in batch['source_ids']}
        missing = [m for m in group['members'] if m['id'] not in used]
        if missing:
            batch = meeting_audio.prepare(cfg, missing, profile)
            batches.append({'batch_id': batch['batch_id'], 'source_ids': batch['source_ids']})
            store.update(speech_batches=batches)
        by_member = {}
        for batch in batches:
            data = read_json(Manifest(cfg, batch['batch_id']).folder/'room-transcript.json')
            for boundary in data['boundaries']:
                if boundary['id'] not in wanted:
                    continue
                start, end = boundary['start_ms'], boundary['end_ms']
                local = [{**t, 'start_ms': max(0, t['start_ms']-start), 'end_ms': t['end_ms']-start}
                         for t in data['turns'] if start <= t['start_ms'] < end]
                by_member[boundary['id']] = {'turns': local, 'speakers': data['speakers']}
    elif profile['mode'] != 'stereo':
        raise Attention('meeting_audio_mode_invalid')
    for member in group['members']:
        member_store = Manifest(cfg, member['id'])
        data = (conversation_asr.recognize(cfg, member_store, Path(member['path']))
                if profile['mode'] == 'stereo' else by_member[member['id']])
        for turn in data['turns']:
            turns.append({**turn, 'id': len(turns), 'source_id': member['id'],
                          'start_ms': turn['start_ms']+offset, 'end_ms': turn['end_ms']+offset})
        for name in data['speakers'].values():
            speakers[name] = name
        offset += round(member['duration']*1000)
        reference = read_json(member_store.folder/'asr.json', {}).get('text', '')
        if reference:
            references.append(reference)
    saved = {'turns': turns, 'speakers': speakers, 'text': '\n'.join(t['speaker']+'：'+t['text'] for t in turns),
             'accepted_text_reference': '\n'.join(references), 'identity_basis': profile['mode']}
    atomic(path, saved)
    return saved


def obsolete_effects(cfg, store, group):
    previous = store.read()
    values = []
    if previous.get('latest_effect'):
        effect = previous['effects'][previous['latest_effect']]
        values.append(effect)
    for member in group['members']:
        data = Manifest(cfg, member['id']).read()
        if data.get('meeting_group_id') == group['id']:
            continue
        for part in data.get('segments', []):
            effect = data.get('effects', {}).get(part['key'], {})
            if effect.get('root_id') and effect.get('status') == 'verified':
                if effect.get('backend') != 'independent-remnote-plugin':
                    raise Attention('old_meeting_effect_requires_reconciliation')
                values.append(effect)
    return list({v['root_id']: v for v in values}.values())


async def publish(cfg, store, group, profile, markdown, plan, plan_path):
    key = 'day-'+group['revision']
    async with remnote.connection(cfg) as session:
        document_id = profile.get('document_id') or read_json(cfg.root/'meeting-targets.json', {}).get(group['contact'])
        if not document_id:
            target = await remnote.call(session, 'create_document', effect_key='meeting-contact/'+digest(group['contact']),
                                        title=profile['document_title'], parent_id=cfg.meeting_document_id)
            if not target.get('is_document'):
                raise Attention('meeting_document_readback_missing')
            document_id = target['document_id']
        remember_document(cfg, group['contact'], document_id)
        if not store.effect(key):
            route = {'type': 'meeting', 'document_id': document_id, 'recorded_day': group['day'],
                     'requested': '', 'review_eligible': False}
            target = await remnote.resolve_target(cfg, session, route, group['id'])
            store.save_effect(key, {'backend': 'independent-remnote-plugin', 'status': 'not_started', 'routing': route, **target})
        effect = await remnote.publish(cfg, store, key, markdown, '', json.dumps(group, ensure_ascii=False))
        for old in plan['obsolete']:
            if old['root_id'] == effect['root_id'] or old['root_id'] in plan['archived']:
                continue
            result = await remnote.call(session, 'relocate_outline', effect_key=old['plugin_effect_key'],
                operation_id=group['id']+'/'+group['revision']+'/archive/'+old['root_id'],
                parent_id=cfg.trash_rem_id, trash_id=cfg.trash_rem_id, strip_metadata=False)
            if result.get('status') != 'verified':
                raise Attention('meeting_archive_readback_missing')
            plan['archived'][old['root_id']] = result
            atomic(plan_path, plan)
    return key, effect


def process(cfg, group):
    store = Manifest(cfg, group['id'])
    from .recovery import worker_started
    worker_started(store)
    with FileLock(str(cfg.root/'locks'/('meeting-'+group['id']+'.lock'))):
        if store.read().get('completed_revision') == group['revision']:
            return {'skipped': 'already_processed', 'group_id': group['id']}
        profile = cfg.conversation_profiles[group['contact']]
        for member in group['members']:
            source = safe_source(Path(member['path']), cfg.inbox)
            if file_hash(source) != member['id']:
                raise Attention('meeting_source_changed')
        path = store.folder/'days'/group['revision']/'plan.json'
        plan = read_json(path)
        if plan is None:
            plan = {'group': group, 'obsolete': obsolete_effects(cfg, store, group), 'archived': {}}
            atomic(path, plan)
        store.update(group=group, phase='transcription')
        speech = transcript(cfg, store, group, profile)
        store.update(phase='outline')
        markdown = conversation.outline(cfg, store, speech)
        atomic(path.parent/'outline.md', markdown, text=True)
        store.update(phase='publish')
        with FileLock(str(cfg.root/'locks/remnote-write.lock')):
            key, effect = asyncio.run(publish(cfg, store, group, profile, markdown, plan, path))
        store.update(latest_effect=key, completed_revision=group['revision'], phase='completed', completed_at=now())
        for member in group['members']:
            Manifest(cfg, member['id']).update(meeting_group_id=group['id'], meeting_revision=group['revision'])
        return {'group_id': group['id'], 'day': group['day'], 'files': len(group['members']),
                'root_id': effect['root_id'], 'archived': list(plan['archived'])}


@flow(name='process-meeting-day', log_prints=False, persist_result=True)
def process_meeting_day(group: dict):
    cfg = Settings.load()
    from prefect.flow_runs import suspend_flow_run
    from uuid import uuid4
    while True:
        try:
            return process(cfg, group)
        except UnknownOutcome as exc:
            if str(exc) not in remnote.RECONNECT_REASONS:
                raise
            suspend_flow_run(key='remnote-connection:'+str(uuid4()))
