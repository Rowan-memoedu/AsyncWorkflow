"""Loopback-only transport for the independent, five-operation RemNote plugin."""
from __future__ import annotations
import asyncio
import hmac
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict

from .auth import access, protect
from .core import Settings, atomic, read_json

METHODS = {'status', 'template_snapshot', 'search_documents', 'resolve_daily', 'publish_outline'}
ORIGINS = {'http://localhost:43141', 'http://127.0.0.1:43141'}


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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1'])
    secret = token or token_for(cfg, create=True)
    socket: WebSocket | None = None
    pending: dict[str, asyncio.Future] = {}
    kb_file = cfg.root / 'remnote-plugin-kb.json'

    @app.middleware('http')
    async def limits(request: Request, next_call):
        if int(request.headers.get('content-length', '0')) > 1024*1024:
            return JSONResponse({'error':'request_too_large'}, status_code=413)
        response = await next_call(request)
        response.headers['Cache-Control'] = 'no-store'
        # Static plugin assets contain no credentials. RemNote must fetch its
        # manifest across origins; the secret and RPC routes do not allow CORS.
        if request.url.path.startswith('/plugin/'):
            response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    @app.get('/status')
    async def status():
        return {'connected': socket is not None, 'protocol':1}

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
        if ws.headers.get('origin') not in ORIGINS:
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
    if folder.is_dir(): app.mount('/plugin', StaticFiles(directory=folder, html=True), name='plugin')
    return app


def serve():
    import uvicorn
    uvicorn.run(create_app(Settings.load()), host='127.0.0.1', port=43141, access_log=False, ws_max_size=1024*1024)


if __name__ == '__main__':
    serve()
