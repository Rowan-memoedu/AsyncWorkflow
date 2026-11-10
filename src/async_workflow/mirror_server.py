"""Private HTTPS-facing read mirror; no recording or workflow execution endpoints."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .mirror import IDENTITY, MAX_BYTES, listing, validate


def create_app(directory, config):
    directory = Path(directory)
    snapshot = directory / 'snapshot.json'
    lock = threading.RLock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    cached = None
    if snapshot.exists():
        cached = validate(json.loads(snapshot.read_text(encoding='utf-8')))

    def matches(secret, expected):
        return isinstance(expected, str) and hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), expected)

    def reader(request):
        auth = request.headers.get('authorization', '')
        try:
            scheme, value = auth.split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            user, password = base64.b64decode(value, validate=True).decode('utf-8').split(':', 1)
            return matches(password, config['readers'].get(user, ''))
        except (ValueError, UnicodeError):
            return False

    @app.middleware('http')
    async def private_response(request, call_next):
        if request.url.path == '/api/sync':
            token = request.headers.get('authorization', '')
            accepted = token.startswith('Bearer ') and matches(token[7:], config['publisher_hash'])
        else:
            accepted = reader(request)
        if not accepted:
            response = JSONResponse({'detail': '需要登录'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Basic realm="recordings", charset="UTF-8"'})
        else:
            response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def current():
        with lock:
            if cached is None:
                raise HTTPException(503, '尚未收到电脑同步的记录')
            return cached

    @app.post('/api/sync')
    async def sync(request: Request):
        nonlocal cached
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise HTTPException(413, 'Snapshot too large')
        try:
            incoming = validate(json.loads(body))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid snapshot') from None
        with lock:
            if cached and datetime.fromisoformat(incoming['updated_at']) < datetime.fromisoformat(cached['updated_at']):
                raise HTTPException(409, 'Older snapshot')
            incoming['synced_at'] = datetime.now(timezone.utc).isoformat()
            directory.mkdir(parents=True, exist_ok=True)
            temporary = snapshot.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(incoming, output, ensure_ascii=False, separators=(',', ':'))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, snapshot)
            cached = incoming
        return {'total': len(incoming['recordings']), 'updated_at': incoming['updated_at']}

    @app.get('/api/recordings')
    def recordings(q: str = ''):
        if len(q) > 500:
            raise HTTPException(400, '搜索词过长')
        return listing(current(), q)

    @app.get('/api/recordings/{identity}')
    def detail(identity: str):
        if not IDENTITY.fullmatch(identity):
            raise HTTPException(404, '录音不存在')
        row = next((r for r in current()['recordings'] if r['id'] == identity), None)
        if row is None:
            raise HTTPException(404, '录音不存在')
        return row

    static = Path(__file__).parent / 'web'

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    app.mount('/assets', StaticFiles(directory=static), name='assets')
    return app


if __name__ == '__main__':
    import uvicorn
    root = Path(os.environ.get('RECORDINGS_STATE', '/var/lib/recording-view'))
    config = json.loads((root / 'credentials.json').read_text(encoding='utf-8'))
    uvicorn.run(create_app(root, config), host='127.0.0.1', port=43143, access_log=False)
