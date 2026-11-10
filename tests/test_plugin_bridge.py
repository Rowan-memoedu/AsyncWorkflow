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
    static=tmp_path/'plugin';static.mkdir()
    (static/'index.html').write_text('<body><script type="module" src="./index-sandbox.js"></script></body>','utf-8')
    (static/'index-sandbox.js').write_text('console.log("synthetic plugin");','utf-8')
    with TestClient(create_app(cfg, token='test-secret',static_dir=static),base_url='http://localhost:43141') as client:
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

def test_opaque_sandbox_cannot_fetch_secret_and_requires_socket_secret(client):
    headers={'origin':'null','sec-fetch-site':'cross-site'}
    assert client.get('/bootstrap',headers=headers).status_code==403
    assert client.get('/bootstrap',headers={**headers,'referer':'https://evil.example/plugin/index.html'}).status_code==403
    assert client.get('/bootstrap',headers={**headers,'referer':'http://[invalid/plugin/index.html'}).status_code==403
    response=client.get('/bootstrap',headers={**headers,'referer':'http://localhost:43141/plugin/index.html?widgetName=index'})
    assert response.status_code==403
    assert 'access-control-allow-origin' not in response.headers
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'null'}) as ws:
        ws.send_json({'token':'wrong','kb_id':'kb','protocol':1})
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'null'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'kb','protocol':1})
        assert ws.receive_json()['kind']=='ready'

def test_native_host_still_requires_websocket_secret(client):
    response=client.get('/bootstrap',headers={'origin':'app://js','sec-fetch-site':'cross-site'})
    assert response.status_code==403
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'app://js'}) as ws:
        ws.send_json({'token':'wrong','kb_id':'kb','protocol':1})
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    with client.websocket_connect('ws://localhost:43141/plugin-socket',headers={'origin':'app://js'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'kb','protocol':1})
        assert ws.receive_json()['kind']=='ready'

def test_secret_html_is_navigation_only_not_cors_readable(client):
    for headers in ({},{'origin':'null'},{'origin':'https://evil.example','sec-fetch-dest':'empty','sec-fetch-mode':'cors'}):
        reply=client.get('/plugin/index.html?widgetName=index',headers=headers)
        assert reply.status_code==200 and 'test-secret' not in reply.text
        assert 'access-control-allow-origin' not in reply.headers
    framed=client.get('/plugin/index.html?widgetName=index',headers={'sec-fetch-dest':'iframe','sec-fetch-mode':'navigate'})
    assert 'globalThis.__ASYNC_BRIDGE_TOKEN="test-secret"' in framed.text
    assert 'access-control-allow-origin' not in framed.headers
    assert framed.headers['content-security-policy']=='frame-ancestors app://js https://www.remnote.com https://remnote.com'
    assert framed.headers['cache-control']=='no-store'
    asset=client.get('/plugin/index-sandbox.js',headers={'origin':'null'})
    assert asset.headers['access-control-allow-origin']=='*' and 'test-secret' not in asset.text

def test_html_secret_cannot_break_out_of_script(tmp_path):
    folder=tmp_path/'assets';folder.mkdir();(folder/'index.html').write_text('<body>synthetic</body>')
    cfg=Settings(root=tmp_path,ffprobe=Path('ffprobe'),codex=Path('codex'))
    with TestClient(create_app(cfg,token='</script><script>evil()</script>',static_dir=folder),base_url='http://localhost:43141') as local:
        reply=local.get('/plugin/',headers={'sec-fetch-dest':'iframe','sec-fetch-mode':'navigate'})
        assert reply.text.count('<script>')==1
        assert '\\u003c/script>' in reply.text

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

def test_ipv6_authority_and_origin_preserve_authentication(client):
    assert client.get('http://[::1]:43141/status').status_code == 200
    assert client.get('http://[::1]:43141/bootstrap').status_code == 403
    with client.websocket_connect('ws://[::1]:43141/plugin-socket', headers={'origin':'http://[::1]:43141'}) as ws:
        ws.send_json({'token':'test-secret','kb_id':'kb','protocol':1})
        assert ws.receive_json()['kind'] == 'ready'

@pytest.mark.parametrize('host', ['evil.example','localhost.evil.example','[::2]:43141',
                                  'localhost@evil.example','evil@localhost','[::1]:9999',
                                  '[::1','[::1]evil','localhost/path'])
def test_rejects_non_loopback_or_malformed_authority(client, host):
    assert client.get('/status', headers={'host':host}).status_code == 400
