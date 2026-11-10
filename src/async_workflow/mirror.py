"""An explicit, read-only projection of the local recording inventory."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re
import unicodedata

MAX_BYTES = 32 * 1024 * 1024
IDENTITY = re.compile(r'(?:file-)?[a-f0-9]{64}')
ROW_FIELDS = ('id', 'name', 'recorded_at', 'duration', 'type', 'status', 'status_label',
              'eligibility', 'group_id', 'has_transcript')
PART_FIELDS = ('key', 'kind', 'original_kind', 'raw_text', 'markdown', 'destination',
               'status', 'status_label', 'reason', 'updated_at', 'note_id', 'error')
ROUTE_FIELDS = ('type', 'title', 'path', 'document_id', 'parent_id', 'recorded_day',
                'branch', 'focus_document_title', 'focus_branch_title', 'category', 'software')


def pick(value, names):
    return {key: value[key] for key in names if key in value}


def threads(values):
    return [pick(t, ('id', 'url', 'role', 'effect_key')) for t in values
            if re.fullmatch(r'codex://threads/[a-f0-9-]{36}', t.get('url', ''))]


def project(inventory):
    """Copy display fields only. Never ship source paths, manifests or configuration."""
    snapshot = inventory.snapshot()
    listing = inventory.listing(snapshot=snapshot)
    if listing.get('errors'):
        raise ValueError('Incomplete local inventory; preserve the last server snapshot')
    records = []
    for row in listing['recordings']:
        detail = inventory.detail(row['id'], snapshot=snapshot)
        record = pick(row, ROW_FIELDS)
        record.update(threads=threads(detail.get('threads', [])),
                      transcript=detail.get('transcript', ''), sources=[],
                      asr=pick(detail.get('asr', {}), ('attempt', 'started_at', 'finished_at', 'failure_code', 'exit_code')),
                      runs=[pick(r, ('id', 'name', 'state', 'run_count', 'message')) for r in detail.get('runs', [])],
                      parts=[])
        for part in detail.get('parts', []):
            record['parts'].append({**pick(part, PART_FIELDS),
                                    'threads': threads(part.get('threads', [])),
                                    'routing': pick(part.get('routing', {}), ROUTE_FIELDS)})
        records.append(record)
    return {'schema_version': 1, 'updated_at': listing['updated_at'], 'recordings': records,
            'last_processed_at': listing.get('last_processed_at'),
            'prefect_available': listing['prefect_available'],
            'recovery': pick(listing.get('recovery', {}), ('checked_at', 'checked_runs', 'retried', 'resumed')),
            'errors': [{'reason': '本机部分记录暂不可读取'} for _ in listing.get('errors', [])],
            'next_check': listing.get('next_check'), 'interval_seconds': listing.get('interval_seconds', 1800)}


def validate(data):
    """Reject partial, duplicate or structurally invalid updates before replacing data."""
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Unsupported snapshot')
    stamp = datetime.fromisoformat(data['updated_at'])
    if stamp.tzinfo is None or stamp.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise ValueError('Invalid timestamp')
    records = data['recordings']
    if (not isinstance(data.get('prefect_available'), bool) or not isinstance(data.get('errors'), list)
            or not isinstance(data.get('recovery'), dict) or not isinstance(data.get('interval_seconds'), int)):
        raise ValueError('Invalid service state')
    if not isinstance(records, list) or len(records) > 100000:
        raise ValueError('Invalid inventory')
    seen = set()
    for row in records:
        if not isinstance(row, dict) or not IDENTITY.fullmatch(row.get('id', '')) or row['id'] in seen:
            raise ValueError('Invalid identity')
        seen.add(row['id'])
        if not all(isinstance(row.get(k), str) for k in ('name', 'status', 'status_label', 'transcript')):
            raise ValueError('Invalid recording')
        if (not isinstance(row.get('parts'), list) or not isinstance(row.get('threads'), list)
                or not isinstance(row.get('asr'), dict) or not isinstance(row.get('runs'), list)
                or row.get('sources') != []):
            raise ValueError('Invalid detail')
        for part in row['parts']:
            if not isinstance(part, dict) or not isinstance(part.get('routing'), dict) or not isinstance(part.get('threads'), list):
                raise ValueError('Invalid part')
    return data


def listing(data, query=''):
    terms = unicodedata.normalize('NFKC', query).casefold().split()
    rows = []
    for row in data['recordings']:
        text = '\n'.join([row['name'], row['transcript'], *[
            str(p.get('raw_text', '')) + '\n' + str(p.get('markdown', '')) for p in row['parts']]])
        normalized = unicodedata.normalize('NFKC', text).casefold()
        if not all(term in normalized for term in terms):
            continue
        public = {**pick(row, ROW_FIELDS), 'threads': row['threads']}
        if terms:
            start = max(0, normalized.find(terms[0]) - 40)
            public['match_excerpt'] = text[start:start + 180]
        rows.append(public)
    return {**pick(data, ('updated_at', 'synced_at', 'last_processed_at', 'prefect_available', 'errors', 'recovery', 'next_check', 'interval_seconds')),
            'remote': True, 'total': len(data['recordings']), 'matched': len(rows), 'query': query,
            'summary': dict(Counter(row['status'] for row in data['recordings'])),
            'without_transcript': sum(not r.get('has_transcript') for r in data['recordings']),
            'recordings': rows}
