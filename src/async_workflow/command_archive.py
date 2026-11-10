"""Explicit retrospective archival repair; original execution receipts stay intact."""
from __future__ import annotations

from pathlib import Path

from . import dispatch_policy, recording_types, splitter
from .core import Attention, atomic, digest, now, read_json


def active_parts(data):
    repair = data.get('command_archive', {})
    superseded = set(repair.get('superseded_keys', [])) if repair.get('completed') else set()
    parts = [p for p in data.get('segments', []) if p['key'] not in superseded]
    feedback = data.get('workflow_feedback', {})
    if feedback.get('key') and feedback.get('source_spans') and feedback['key'] not in superseded:
        parts.append({**feedback, 'kind': 'workflow_feedback', 'markdown': '',
                      'dispatch_policy': data.get('feedback_dispatch_policy', {})})
    if repair.get('completed'):
        parts.extend(repair['segments'])
    return parts


def replacement(data, key):
    repair = data.get('command_archive', {})
    if repair.get('completed') and key in repair.get('superseded_keys', []):
        return {'status': 'verified', 'kind': 'archival_correction',
                'operation': repair['operation'], 'original_effect_key': key,
                'replacement_keys': [p['key'] for p in repair['segments']]}
    return None


def positions(part, source):
    result = set()
    for start, stop in part.get('source_spans', []):
        if not 0 <= start < stop <= len(source):
            raise Attention('archival_repair_source_ranges_invalid')
        result.update(i for i in range(start, stop) if not source[i].isspace())
    if not result and part.get('raw_text', '').strip():
        text = part['raw_text']
        if source.count(text) != 1:
            raise Attention('archival_repair_source_not_unique')
        start = source.index(text)
        result.update(i for i in range(start, start+len(text)) if not source[i].isspace())
    return result


def affected(parts, old, mistaken, source):
    """Include complete existing blocks when a repaired control span governs them."""
    new_spans = [positions(p, source) for p in parts]
    old_spans = {p['key']: positions(p, source) for p in old}
    covered = set().union(*(old_spans[k] for k in mistaken))
    selected, superseded = set(), set(mistaken)
    while True:
        new_selected = {i for i, span in enumerate(new_spans) if span & covered}
        new_covered = covered | set().union(*(new_spans[i] for i in new_selected))
        new_superseded = {k for k, span in old_spans.items() if span & new_covered} | superseded
        new_covered |= set().union(*(old_spans[k] for k in new_superseded))
        if new_selected == selected and new_superseded == superseded:
            return [parts[i] for i in sorted(selected)], new_superseded
        selected, superseded, covered = new_selected, new_superseded, new_covered


def prepare(cfg, store, operation):
    folder = store.folder/'repairs'/operation
    revision = digest(Path(__file__).read_bytes()+splitter.SYSTEM.encode()+cfg.production_identity.encode())
    before = read_json(folder/'before.json')
    if not before:
        if store.read().get('command_archive', {}).get('completed'):
            raise Attention('command_archive_already_completed')
        for other in folder.parent.glob('*/before.json'):
            if other.parent != folder and not read_json(other.parent/'plan.json', {}).get('completed'):
                raise Attention('unfinished_repair_requires_same_operation:'+other.parent.name)
        before = store.read()
        atomic(folder/'before.json', before)
    if plan := read_json(folder/'plan.json'):
        if plan.get('revision') == revision or plan.get('completed'):
            return folder, before, plan
        if plan.get('corrections') or plan.get('archived'):
            raise Attention('applied_archival_repair_cannot_change_revision')
        atomic(folder/'rejected'/(plan.get('revision', 'initial')+'.json'), plan)
    transcript = read_json(store.folder/'asr.json', {})
    source = transcript.get('text', '')
    if not source or (transcript.get('source_sha256') and transcript['source_sha256'] != store.folder.name):
        raise Attention('successful_source_transcript_required')
    old = list(before.get('segments', []))
    if feedback := before.get('workflow_feedback'):
        if feedback.get('key') and feedback.get('source_spans'):
            old.append({**feedback, 'kind': 'workflow_feedback'})
    decisions = {}
    for part in old:
        if part.get('kind') not in ('command', 'workflow_feedback') or part.get('cancelled'):
            continue
        decision = dispatch_policy.decide(cfg, store, part['raw_text'])
        if decision['route'] == 'record':
            decisions[part['key']] = decision
    if not decisions:
        raise Attention('no_misclassified_commands')
    split_path = folder/'split.json'
    saved_split = read_json(split_path)
    parts = saved_split.get('parts') if isinstance(saved_split, dict) and saved_split.get('revision') == revision else None
    if parts is None:
        outline = (store.folder/'outline.md').read_text(encoding='utf-8') if (store.folder/'outline.md').exists() else ''
        parts = splitter.split(cfg, source, outline)
        atomic(split_path, {'revision': revision, 'parts': parts})
    chosen, superseded = affected(parts, old, decisions, source)
    if any(p['kind'] == 'command' for p in chosen):
        raise Attention('archival_repair_will_not_execute_commands')
    selected, controls, routes, used = [], [], {}, set()
    normalize = lambda value: ''.join(value.split())
    for part in chosen:
        part = dict(part)
        part['key'] = digest(store.folder.name+operation+str(part['source_spans']))[:24]
        part['archive_operation'] = operation
        body = part.get('content_text', part['raw_text'])
        if not body.strip() and part.get('document_action') != 'create':
            controls.append(part)
            continue
        matching = next((p for p in old if p['key'] in superseded and p['key'] not in used
            and p.get('kind') not in ('command', 'workflow_feedback')
            and normalize(p.get('content_text', p['raw_text'])) == normalize(body)), None)
        if matching:
            used.add(matching['key'])
            part.update(markdown=matching['markdown'], reuses_effect=matching['key'], source_effect=matching['key'])
            saved = before.get('effects', {}).get(matching['key'], {})
            if (saved.get('parent') and not part.get('focus_kind') and
                    (not part.get('destination') or part.get('destination') == matching.get('destination'))):
                routes[part['key']] = {**saved['routing'], 'type': 'document', 'parent_id': saved['parent']}
        else:
            part['markdown'] = recording_types.cached_outline(cfg, store, body,
                lambda text: splitter.refine(cfg, text)) if body.strip() else ''
        # A spoken container already created for this very source is reused by ID.
        if part.get('document_action') == 'create':
            containers = {e['created_document_id'] for p in old
                if positions(p, source) & positions(part, source)
                for e in [before.get('effects', {}).get(p['key'], {})] if e.get('created_document_id')}
            if len(containers) > 1:
                raise Attention('archival_repair_document_ambiguous')
            if containers:
                routes[part['key']] = {'type': 'document', 'parent_id': containers.pop(),
                    'recorded_day': before['provenance']['recorded_at'][:10], 'reason': 'reuse_original_created_document'}
        selected.append(part)
    plan = {'operation': operation, 'stage': 'archive_commands', 'at': now(), 'revision': revision,
            'production_model': cfg.production_model,
            'source_transcript_hash': digest(source), 'reused_asr': True,
            'new_outline_calls': sum('reuses_effect' not in p for p in selected),
            'decisions': decisions, 'superseded_keys': sorted(superseded), 'segments': selected,
            'control_only': controls, 'routes': routes, 'corrections': {}, 'archived': {}}
    atomic(folder/'plan.json', plan)
    return folder, before, plan


def correction_scope(before, plan):
    # Only overlapping old notes may be relocated/archived by the existing repair path.
    return {**before, 'segments': [p for p in before.get('segments', [])
                                  if p['key'] in plan['superseded_keys']]}


def complete(store, folder, plan, run_id):
    for part in plan['segments']:
        if (store.effect(part['key']) or {}).get('status') not in ('verified', 'cancelled'):
            raise Attention('archival_repair_unverified')
    plan.update(completed=True, completed_at=now(), flow_run_id=run_id)
    store.update(command_archive={k: plan[k] for k in ('operation', 'completed', 'completed_at',
        'superseded_keys', 'segments', 'decisions', 'control_only')},
        latest_repair_run=run_id, latest_repair_operation=plan['operation'])
    atomic(folder/'plan.json', plan)
