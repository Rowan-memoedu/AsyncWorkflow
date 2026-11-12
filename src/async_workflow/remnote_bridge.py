"""Loopback-only transport for the independent, scope-limited RemNote plugin."""
from __future__ import annotations
import asyncio
import hmac
import json
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from .auth import access, protect
from .core import Settings, atomic, read_json

METHODS = {'status', 'template_snapshot', 'search_documents', 'resolve_daily', 'publish_outline',
           'targets_snapshot', 'create_document', 'resolve_todo', 'relocate_outline', 'retire_outline',
           'command_inbox_read', 'command_inbox_archive', 'resolve_meeting', 'inspect_daily',
           'capture_nodes', 'capture_catalog', 'capture_enroll'}
ORIGINS = {'http://localhost:43141', 'http://127.0.0.1:43141', 'http://[::1]:43141'}


class LoopbackHostMiddleware:
    """Validate bracketed IPv6 authorities without splitting the address at ':'."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            authority = dict(scope.get('headers', [])).get(b'host', b'').decode('latin-1')
            valid = authority.lower() in {
                'localhost', 'localhost:43141', '127.0.0.1', '127.0.0.1:43141',
                '[::1]', '[::1]:43141',
            }
            if not valid:
                if scope['type'] == 'websocket':
                    await send({'type':'websocket.close', 'code':1008})
                else:
                    await PlainTextResponse('Invalid host header', status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def token_for(cfg: Settings, *, create=False):
    path = cfg.root / 'remnote-plugin.dpapi.json'
    if not path.exists() and create:
        protect(path, {'token': secrets.token_urlsafe(40)})
    return access(path).get('token', '')


class RPC(BaseModel):
    model_config = ConfigDict(extra='forbid')
    method: str
    args: dict = {}


def create_app(cfg: Settings, token: str | None = None, static_dir: Path | None = None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LoopbackHostMiddleware)
    secret = token or token_for(cfg, create=True)
    socket: WebSocket | None = None
    pending: dict[str, asyncio.Future] = {}
    network_diagnostics: dict = {}
    kb_file = cfg.root / 'remnote-plugin-kb.json'

    @app.middleware('http')
    async def limits(request: Request, next_call):
        if int(request.headers.get('content-length', '0')) > 1024*1024:
            return JSONResponse({'error':'request_too_large'}, status_code=413)
        response = await next_call(request)
        response.headers['Cache-Control'] = 'no-store'
        # Static plugin assets contain no credentials. RemNote must fetch its
        # manifest across origins; the secret and RPC routes do not allow CORS.
        if request.url.path.startswith('/plugin/') and request.url.path not in ('/plugin/', '/plugin/index.html'):
            response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    @app.get('/status')
    async def status():
        return {'connected': socket is not None, 'protocol':1, 'network':network_diagnostics}

    @app.get('/bootstrap')
    async def bootstrap(request: Request):
        # Browser-controlled Fetch Metadata cannot be set by a remote webpage.
        # Non-browser local clients instead read the DPAPI envelope directly.
        if request.headers.get('sec-fetch-site') != 'same-origin':
            raise HTTPException(403, 'same_origin_plugin_required')
        if request.headers.get('origin') not in (None, *ORIGINS):
            raise HTTPException(403, 'origin_rejected')
        return {'token':secret}

    @app.post('/rpc')
    async def rpc(body: RPC, request: Request):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not supplied or not hmac.compare_digest(supplied, secret):
            raise HTTPException(401, 'authentication_required')
        if body.method not in METHODS:
            raise HTTPException(400, 'unsupported_method')
        if socket is None:
            raise HTTPException(503, 'plugin_not_connected')
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        pending[request_id] = future
        try:
            await socket.send_json({'id':request_id,'method':body.method,'args':body.args})
            result = await asyncio.wait_for(future, timeout=180)
            if 'error' in result:
                return JSONResponse({'error':result['error']}, status_code=409)
            return result['result']
        except (TimeoutError, WebSocketDisconnect, RuntimeError, ConnectionError):
            raise HTTPException(503, 'plugin_result_unknown') from None
        finally:
            pending.pop(request_id, None)

    @app.websocket('/plugin-socket')
    async def plugin_socket(ws: WebSocket):
        nonlocal socket
        network_diagnostics['socket_origin']=ws.headers.get('origin')
        if ws.headers.get('origin') not in (*ORIGINS, 'null', 'app://js'):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if not hmac.compare_digest(str(hello.get('token','')), secret) or hello.get('protocol')!=1:
                await ws.close(code=1008)
                return
            kb_id=hello.get('kb_id')
            known=read_json(kb_file, {})
            if not isinstance(kb_id,str) or not kb_id or (known and known.get('kb_id')!=kb_id) or socket is not None:
                await ws.close(code=1008)
                return
            if not known: atomic(kb_file, {'kb_id':kb_id})
            socket=ws
            await ws.send_json({'kind':'ready','protocol':1})
            while True:
                response=await ws.receive_json()
                if future:=pending.get(response.get('id')):
                    if not future.done(): future.set_result(response)
        except (WebSocketDisconnect, TimeoutError):
            pass
        finally:
            if socket is ws:
                socket=None
                for future in tuple(pending.values()):
                    if not future.done(): future.set_exception(ConnectionError('plugin_disconnected'))

    folder=static_dir or Path('D:/Artifacts/AsyncWorkflow/remnote-plugin')
    @app.get('/plugin/')
    @app.get('/plugin/index.html')
    async def plugin_html(request: Request):
        if not (folder/'index.html').is_file():
            raise HTTPException(404, 'plugin_not_built')
        text=(folder/'index.html').read_text('utf-8')
        framed=(request.headers.get('sec-fetch-dest')=='iframe'
                and request.headers.get('sec-fetch-mode')=='navigate')
        if framed:
            value=json.dumps(secret).replace('<', '\\u003c')
            seed='<script>globalThis.__ASYNC_BRIDGE_TOKEN='+value+';</script>'
            position=text.find('<script')
            text=text[:position]+seed+text[position:] if position>=0 else text+seed
        network_diagnostics['html_credential_seeded']=framed
        return HTMLResponse(text,headers={
            'Content-Security-Policy':'frame-ancestors app://js https://www.remnote.com https://remnote.com',
            'Cache-Control':'no-store',
        })
    if folder.is_dir(): app.mount('/plugin', StaticFiles(directory=folder, html=True), name='plugin')
    return app


def serve():
    import uvicorn
    uvicorn.run(create_app(Settings.load()), host='localhost', port=43141, access_log=False, ws_max_size=1024*1024)


if __name__ == '__main__':
    serve()
