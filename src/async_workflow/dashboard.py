"""Loopback-only, read-only view of all local audio and durable processing receipts."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import threading
import time
import unicodedata
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import Settings, digest, now, read_json, source_metadata, recorded_time
from .ingest import audio_files, SCAN_INTERVAL_SECONDS
from .recovery import run_ids
from .command_archive import active_parts

LABELS = {'verified': '已正确归档', 'blocked': '堵塞 · 自动恢复中', 'running': '处理中',
          'no_content': '仅开场说明 · 无正文',
          'pending': '待处理', 'retained': '保留未纳入', 'needs_input': '等待用户输入',
          'command_done': '命令回合完成 · 待验收', 'submitted': '已提交 Thread',
          'cancelled': '已撤回', 'unknown': '状态暂不可核对'}
EFFECT_LABELS = {'verified': '写入已回读', 'cancelled': '已撤回', 'completed_unverified': '回合完成，待验收',
                 'needs_input': '等待用户输入', 'failed': '执行失败', 'outcome_unknown': '提交结果待核对',
                 'submitted': '已提交，等待结果', 'queued': '已入队', 'delivered': '已送达', 'not_started': '待提交'}


def thread_link(value):
    try:
        return 'codex://threads/' + str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def read_safe(path, default=None):
    try:
        return read_json(path, default)
    except (OSError, ValueError):
        return default


def effect_threads(effects, corrected=()):
    found = {}
    for key, effect in effects.items():
        for field in ('thread_id', 'session_id', 'repair_thread_id'):
            value = effect.get(field)
            if link := thread_link(value):
                found[value] = {'id': value, 'url': link, 'effect_key': key,
                                'role': '脚本修复' if field == 'repair_thread_id' else
                                '工作流维护' if effect.get('backend') == 'app-server-native-queue' else '任务执行'}
                if key in corrected:
                    found[value]['role'] = '历史误投 · 已补归档 · '+found[value]['role']
    return list(found.values())


def active_effects(data):
    keys = [item['key'] for item in active_parts(data)]
    if key := data.get('latest_effect'):
        keys.append(key)
    return {key: data.get('effects', {}).get(key, {}) for key in keys}


def classify(data, runs, available):
    effects = active_effects(data)
    statuses = [effect.get('status', 'pending') for effect in effects.values()]
    repair = runs.get(data.get('latest_repair_run'), {}).get('state', {}).get('type')
    if repair in ('FAILED', 'CRASHED', 'PAUSED'):
        return 'blocked'
    if repair in ('RUNNING', 'PENDING', 'SCHEDULED'):
        return 'running'
    if not statuses and data.get('command_archive', {}).get('completed'):
        return 'no_content'
    if statuses and all(s in ('verified', 'cancelled') for s in statuses):
        return 'cancelled' if all(s == 'cancelled' for s in statuses) else 'verified'
    if any(s == 'needs_input' for s in statuses):
        return 'needs_input'
    if statuses and all(s in ('verified', 'cancelled', 'completed_unverified') for s in statuses):
        return 'command_done'
    states = [runs[r].get('state', {}) for r in run_ids(data) if r in runs]
    if any(s.get('type') == 'PAUSED' and (s.get('state_details', {}).get('pause_key') or '') in
           ('native-thread', 'approval', 'decision') for s in states):
        return 'needs_input'
    if any(s.get('type') == 'RUNNING' for s in states):
        return 'running'
    if (any(s in ('failed', 'outcome_unknown') for s in statuses)
            or any(s.get('type') in ('FAILED', 'CRASHED', 'PAUSED') for s in states)):
        return 'blocked'
    if statuses and all(s in ('verified', 'cancelled', 'completed_unverified', 'queued', 'delivered', 'submitted') for s in statuses):
        return 'submitted'
    if data.get('eligibility') and data['eligibility'] != 'eligible':
        return 'retained'
    if run_ids(data) and (not available or any(r not in runs for r in run_ids(data))):
        return 'unknown'
    return 'pending'


def recording_order(row):
    try:
        stamp = datetime.fromisoformat(row.get('recorded_at') or '')
        epoch = stamp.timestamp() if stamp.tzinfo is not None else float('-inf')
    except (ValueError, TypeError):
        epoch = float('-inf')
    return epoch, row['name'], row['id']


def latest_processing_at(manifests, runs):
    """Actual processing events, excluding inventory reads and recovery scans."""
    stamps = []
    for data in manifests.values():
        stamps.extend(data.get('asr', {}).get(k) for k in ('started_at', 'finished_at'))
        stamps.extend(effect.get('updated_at') for effect in data.get('effects', {}).values())
    for run in runs.values():
        stamps.extend(run.get(k) for k in ('start_time', 'end_time'))
    valid = []
    for stamp in stamps:
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is not None:
                valid.append(parsed)
        except (ValueError, TypeError):
            pass
    return max(valid).isoformat() if valid else None


class Inventory:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0

    def snapshot(self):
        with self.lock:
            if self.cached and time.monotonic() - self.cached_at < 10:
                return self.cached
            cfg = self.cfg
            manifests, errors = {}, []
            for path in cfg.artifacts.glob('*/manifest.json'):
                value = read_safe(path)
                if value is None:
                    errors.append({'recording_id': path.parent.name, 'reason': '处理记录无法读取'})
                elif re.fullmatch('[a-f0-9]{64}', path.parent.name):
                    manifests[path.parent.name] = value
            groups = {}
            for identity, value in manifests.items():
                if value.get('recording_type') == 'meeting_day':
                    for member in value.get('group', {}).get('members', []):
                        groups[member['id']] = (identity, member)
            ids = list(dict.fromkeys(r for data in manifests.values() for r in run_ids(data)))
            runs, available = {}, True
            try:
                with httpx.Client(timeout=5) as client:
                    for offset in range(0, len(ids), 200):
                        response = client.post(cfg.prefect_url + '/flow_runs/filter', json={
                            'flow_runs': {'id': {'any_': ids[offset:offset+200]}}, 'limit': 200})
                        response.raise_for_status()
                        runs.update({r['id']: r for r in response.json()})
            except (httpx.HTTPError, ValueError):
                available = False
            rows, paths = {}, {}
            for identity, original in manifests.items():
                if original.get('recording_type') in ('meeting_day', 'meeting_asr_batch'):
                    continue
                group_id, member = groups.get(identity, (original.get('meeting_group_id'), {}))
                data = manifests.get(group_id, original)
                sources = list(original.get('sources', []))
                if member.get('path') and member['path'] not in sources:
                    sources.append(member['path'])
                if not sources and not original.get('original_name'):
                    continue
                provenance = member.get('provenance') or original.get('provenance', {})
                row = {'id': identity, 'name': provenance.get('cloud_file_name') or member.get('name') or original.get('original_name') or Path(sources[0]).name,
                       'recorded_at': provenance.get('recorded_at'), 'sources': sources,
                       'duration': original.get('duration') or member.get('duration'),
                       'type': '交流录音' if group_id or original.get('recording_type') == 'conversation' else '个人录音',
                       'status': classify(data, runs, available), 'eligibility': original.get('eligibility', ''),
                       'threads': effect_threads(data.get('effects', {}),
                           data.get('command_archive', {}).get('decisions', {})
                           if data.get('command_archive', {}).get('completed') else ()), 'group_id': group_id,
                       'data': data, 'original': original}
                rows[identity] = row
                for path in sources:
                    paths[str(Path(path)).casefold()] = identity
            baseline = set(str(x) for x in read_safe(cfg.root/'xiaomi-baseline.json', {}).get('asset_ids', []))
            baseline_hashes = set(read_safe(cfg.root/'baseline.json', {}).get('hashes', []))
            try:
                for path in audio_files(cfg.inbox):
                    if str(path).casefold() in paths:
                        continue
                    identity = 'file-' + digest(str(path).casefold())
                    try:
                        provenance = source_metadata(path, cfg)
                    except Exception:
                        provenance = {}
                    stamp = provenance.get('recorded_at')
                    historical = str(provenance.get('asset_id')) in baseline or (
                        stamp and cfg.enabled_at and datetime.fromisoformat(stamp) < datetime.fromisoformat(cfg.enabled_at))
                    rows[identity] = {'id': identity, 'name': provenance.get('cloud_file_name') or path.name,
                        'recorded_at': stamp, 'sources': [str(path)], 'duration': None, 'type': '未分类',
                        'status': 'retained' if historical else 'pending', 'eligibility': 'historical_baseline' if historical else '',
                        'threads': [], 'group_id': None, 'data': {}, 'original': {}}
            except (OSError, ValueError):
                errors.append({'reason': '部分录音目录无法读取'})
            for row in rows.values():
                # Typed recorder names embed the original local recording time.
                if '__QVR1_' in row['name'] and (stamp := recorded_time(row['name'])):
                    row['recorded_at'] = stamp.isoformat()
                if row['id'] in baseline_hashes and not run_ids(row['data']):
                    row.update(status='retained', eligibility='historical_baseline')
                row['status_label'] = LABELS[row['status']]
                transcript = read_safe(cfg.artifacts/row['id']/'asr.json', {}).get('text', '')
                parts = active_parts(row['data'])
                row['search_text'] = '\n'.join([row['name'], transcript, *[p.get('raw_text', '')+'\n'+p.get('markdown', '') for p in parts],
                    row['data'].get('workflow_feedback', {}).get('raw_text', '')])
                row['has_transcript'] = bool(transcript or any(p.get('raw_text') for p in parts))
            recovery = read_safe(cfg.root/'recovery-status.json', {})
            watcher = read_safe(cfg.root/'watcher-status.json', {})
            next_check = None
            if recovery.get('checked_at'):
                next_check = (datetime.fromisoformat(recovery['checked_at']) + timedelta(seconds=SCAN_INTERVAL_SECONDS)).isoformat()
            ordered = sorted(rows.values(), key=recording_order, reverse=True)
            self.cached = {'rows': ordered, 'runs': runs, 'summary': dict(Counter(r['status'] for r in ordered)),
                'total': len(ordered), 'updated_at': now(), 'prefect_available': available, 'errors': errors,
                'last_processed_at': latest_processing_at(manifests, runs),
                'recovery': {k: recovery.get(k) for k in ('checked_at', 'checked_runs', 'retried', 'resumed', 'errors')},
                'next_check': next_check, 'watcher': watcher, 'interval_seconds': SCAN_INTERVAL_SECONDS}
            self.cached_at = time.monotonic()
            return self.cached

    def listing(self, query='', snapshot=None):
        snapshot = snapshot if snapshot is not None else self.snapshot()
        terms = unicodedata.normalize('NFKC', query).casefold().split()
        rows = []
        for row in snapshot['rows']:
            haystack = unicodedata.normalize('NFKC', row['search_text']).casefold()
            if not all(term in haystack for term in terms):
                continue
            public = {k: v for k,v in row.items() if k not in ('data', 'original', 'sources', 'search_text')}
            if terms:
                start = max(0, haystack.find(terms[0])-40)
                public['match_excerpt'] = row['search_text'][start:start+180]
            rows.append(public)
        return {**{k: v for k,v in snapshot.items() if k not in ('rows', 'runs')},
                'recordings': rows, 'matched': len(rows), 'query': query,
                'without_transcript': sum(not r['has_transcript'] for r in snapshot['rows'])}

    def detail(self, identity, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.snapshot()
        row = next((r for r in snapshot['rows'] if r['id'] == identity), None)
        if row is None:
            raise HTTPException(404, '录音不存在')
        data = row['data']
        effects = data.get('effects', {})
        parts = []
        candidates = active_parts(data)
        if data.get('latest_effect'):
            candidates.append({'key': data['latest_effect'], 'kind': 'record', 'raw_text': '', 'markdown': ''})
        for item in candidates:
            key = item.get('key')
            effect = effects.get(key, {})
            route = item.get('dispatch_policy', {}).get('route')
            kind = ('todo' if route == 'record' else 'command' if route == 'command' else 'workflow_feedback' if route == 'workflow' else item.get('kind'))
            routing = dict(effect.get('routing', {}))
            if effect.get('parent'):
                routing['parent_id'] = effect['parent']
            if routing.get('branch_path') and not routing.get('path'):
                prefix = ['Daily Note', routing['recorded_day']] if routing.get('type') == 'daily' else []
                routing['path'] = ' / '.join(prefix + routing['branch_path'])
            parts.append({'key': key, 'kind': kind, 'original_kind': item.get('original_kind'),
                'raw_text': item.get('raw_text', ''), 'markdown': item.get('markdown', ''),
                'destination': item.get('destination', ''), 'routing': {k: routing.get(k) for k in
                    ('type', 'title', 'path', 'document_id', 'parent_id', 'recorded_day', 'branch', 'focus_document_title', 'focus_branch_title', 'category', 'software') if routing.get(k)},
                'status': effect.get('status', 'pending'), 'status_label': EFFECT_LABELS.get(effect.get('status'), '等待处理'),
                'reason': item.get('dispatch_policy', {}).get('reason') or item.get('reason', ''),
                'threads': effect_threads({key: effect}), 'updated_at': effect.get('updated_at'),
                'note_id': effect.get('root_id'), 'error': effect.get('error') or effect.get('error_type')})
        asr = row['original'].get('asr', {})
        transcript = read_safe(self.cfg.artifacts/identity/'asr.json', {}).get('text', '') if not identity.startswith('file-') else ''
        return {**{k: v for k,v in row.items() if k not in ('data', 'original', 'search_text')}, 'parts': parts,
                'transcript': transcript, 'asr': {k: asr.get(k) for k in ('submission', 'attempt', 'started_at', 'finished_at', 'failure_code', 'exit_code')},
                'runs': [{'id': r, 'name': snapshot['runs'].get(r, {}).get('name'),
                          'state': snapshot['runs'].get(r, {}).get('state', {}).get('name', '不可读取'),
                          'message': snapshot['runs'].get(r, {}).get('state', {}).get('message'),
                          'run_count': snapshot['runs'].get(r, {}).get('run_count')} for r in run_ids(data)]}


def create_app(cfg=None):
    inventory = Inventory(cfg or Settings.load())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get('/api/recordings')
    def recordings(q: str = ''):
        if len(q) > 500:
            raise HTTPException(400, '搜索词过长')
        return inventory.listing(q)

    @app.get('/api/recordings/{identity}')
    def detail(identity: str):
        if not re.fullmatch(r'(?:file-)?[a-f0-9]{64}', identity):
            raise HTTPException(404, '录音不存在')
        return inventory.detail(identity)

    static = Path(__file__).parent/'web'

    @app.get('/')
    def index():
        return FileResponse(static/'index.html')

    app.mount('/assets', StaticFiles(directory=static), name='assets')
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=43142, access_log=False)
