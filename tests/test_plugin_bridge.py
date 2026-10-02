from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from async_workflow.core import Settings
from async_workflow.remnote_bridge import create_app

@pytest.fixture
def client(tmp_path):
    cfg=Settings(root=tmp_path,ffprobe=Path('ffprobe'),codex=Path('codex'))
    with TestClient(create_app(cfg, token='test-secret'),base_url='http://localhost:43141') as client:
        yield client

def test_private_methods_need_auth_and_have_no_generic_write_api(client):
    assert client.post('/rpc',json={'method':'status'}).status_code==401
    assert client.post('/rpc',json={'method':'delete_note'},headers={'Authorization':'Bearer test-secret'}).status_code==400
    assert client.post('/rpc',json={'method':'status'},headers={'Authorization':'Bearer test-secret'}).status_code==503

def test_remote_webpage_cannot_read_bootstrap(client):
    assert client.get('/bootstrap').status_code==403
    assert client.get('/bootstrap',headers={'sec-fetch-site':'cross-site','origin':'https://example.com'}).status_code==403
    assert client.get('/bootstrap',headers={'sec-fetch-site':'same-origin','origin':'https://example.com'}).status_code==403
    reply=client.get('/bootstrap',headers={'sec-fetch-site':'same-origin'})
    assert reply.json()=={'token':'test-secret'} and 'access-control-allow-origin' not in reply.headers

def test_origin_and_knowledge_base_are_bound(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'https://example.com'}): pass
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'http://localhost:43141'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'kb','protocol':1})
        assert ws.receive_json()['kind']=='ready'
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'http://localhost:43141'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'different-kb','protocol':1})
        with pytest.raises(WebSocketDisconnect): ws.receive_json()

def test_transport_correlates_reply_and_clears_connection(client):
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'http://localhost:43141'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'kb','protocol':1})
        ws.receive_json()
        assert client.get('/status').json()['connected']
        with ThreadPoolExecutor() as pool:
            result=pool.submit(client.post,'/rpc',json={'method':'status'},headers={'Authorization':'Bearer test-secret'})
            request=ws.receive_json()
            ws.send_json({'id':request['id'],'result':{'connected':True,'protocol':1}})
            assert result.result(timeout=5).json()['connected']
    assert not client.get('/status').json()['connected']
