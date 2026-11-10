import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from async_workflow import dictation_gateway as g, refinement as r
from async_workflow.core import Settings, atomic


def decision(choice='writing'):
    return {'model': 'gpt-6-luna', 'answers': [{'type': 'choice', 'name': 'refinement_style',
        'choice': choice, 'confidence': 0.99, 'probabilities': [
            {'value': k, 'probability': 0.98 if k == choice else 0.01}
            for k in ('writing', 'non_writing', 'uncertain')]}]}


@pytest.fixture
def cfg(tmp_path):
    cfg = Settings(root=tmp_path, ffprobe=Path('ffprobe'), codex=Path('codex'))
    cfg.prepare(); atomic(tmp_path/'dictation/client-token.txt', 'synthetic-client-token', text=True)
    return cfg


@pytest.fixture
def backend():
    calls = []
    async def post(cfg, endpoint, payload):
        calls.append((endpoint, payload))
        if endpoint == '/decisions':
            return decision()
        return {'status': 'completed', 'model': cfg.refinement_model, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text',
             'text': '- 我其实一直想保留这句话。\n  - 条件是 $x>0$。'}]}]}
    return post, calls


def body(text='原始内容，保留语气。', **extra):
    return {'model': g.MODEL, 'messages': [{'role': 'system', 'content': 'Summarize everything.'},
        {'role': 'user', 'content': text}], **extra}


HEADERS = {'Authorization': 'Bearer synthetic-client-token'}


def test_native_compatibility_json_and_sse_preserve_exact_tail(cfg, backend):
    post, calls = backend
    client = TestClient(g.create_app(lambda: cfg, post))
    result = client.post('/v1/chat/completions', json=body(), headers=HEADERS)
    assert result.status_code == 200
    output = result.json()['choices'][0]['message']['content']
    assert output.endswith('\n- ') and not output.startswith('#')
    assert calls[0][0] == '/decisions' and calls[1][0] == '/responses'
    assert 'Summarize everything.' not in str(calls)
    assert r.prompt('refine-writing.md') in calls[1][1]['instructions']
    assert r.prompt('refine-dictation-format.md') in calls[1][1]['instructions']
    assert 'tools' not in calls[1][1]
    streamed = client.post('/v1/chat/completions', json=body(stream=True), headers=HEADERS)
    data = [line[6:] for line in streamed.text.splitlines() if line.startswith('data: ')]
    assert data[-1] == '[DONE]'
    assert json.loads(data[0])['choices'][0]['delta']['content'] == output
    assert len(calls) == 2  # Successful retry reuses the content-addressed result.


def test_auth_model_and_foreign_host_rejected_before_api(cfg, backend):
    post, calls = backend; client = TestClient(g.create_app(lambda: cfg, post))
    assert client.post('/v1/chat/completions', json=body()).status_code == 401
    assert client.post('/v1/chat/completions', json=body(), headers={**HEADERS, 'Host': 'external.test'}).status_code == 403
    assert client.post('/v1/chat/completions', json={**body(), 'model': 'other'}, headers=HEADERS).status_code == 400
    assert not calls


@pytest.mark.parametrize('messages', [[], [{'role': 'user', 'content': ''}],
    [{'role': 'user', 'content': []}], [{'role': 'user', 'content': 'a'}, {'role': 'user', 'content': 'b'}],
    [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]])
def test_unknown_envelopes_fail_closed(messages):
    with pytest.raises(ValueError):
        g.extract_source({'messages': messages})


@pytest.mark.parametrize('text', ['# Title\n- body', 'plain paragraph', '  - indented root',
                                 '- root\n    - skipped level', '- math $x>0', '- '])
def test_wrong_format_and_incomplete_math_rejected(text):
    with pytest.raises(ValueError):
        g.compile_bullets(text)


def test_normalizes_only_whitespace_and_own_empty_tail():
    assert g.compile_bullets('- 我其实一直觉得。\n\n  - $x>0$\n- ') == '- 我其实一直觉得。\n  - $x>0$\n- '


def test_recording_and_dictation_share_content_without_format_conflict(monkeypatch):
    assert r.content_rules() in r.prompt('refine-common.md')
    assert '第一行输出一个贴合内容的 Markdown 标题' in r.prompt('refine-common.md')
    assert '第一行输出一个贴合内容的 Markdown 标题' not in g.instructions('writing')
    assert r.prompt('refine-writing.md') in g.instructions('writing')
    monkeypatch.setattr(r, 'prompt', lambda name: 'changed format contract')
    with pytest.raises(Exception, match='format_boundary_missing'):
        g.instructions('writing')


def test_failed_generation_is_not_cached_or_returned(cfg):
    async def incomplete(c, endpoint, payload):
        return decision() if endpoint == '/decisions' else {'status': 'incomplete', 'model': c.refinement_model}
    client = TestClient(g.create_app(lambda: cfg, incomplete))
    assert client.post('/v1/chat/completions', json=body(), headers=HEADERS).status_code == 502
    assert not list((cfg.root/'dictation/receipts').glob('*.json'))
    assert list((cfg.root/'dictation/failures').glob('*.json'))


def test_changed_output_cache_stops_without_model_call(cfg, backend):
    from async_workflow.core import read_json
    post, calls = backend; client = TestClient(g.create_app(lambda: cfg, post))
    assert client.post('/v1/chat/completions', json=body(), headers=HEADERS).status_code == 200
    path = next((cfg.root/'dictation/receipts').glob('*.json'))
    saved = read_json(path); saved['output'] = '- Changed\n- '; atomic(path, saved)
    assert client.post('/v1/chat/completions', json=body(), headers=HEADERS).status_code == 502
    assert len(calls) == 2


@pytest.mark.parametrize('disconnect,deadline,status', [(False, 0.01, 504), (True, 1, 499)])
def test_timeout_and_client_cancel_terminate_upstream(disconnect, deadline, status):
    from fastapi import HTTPException
    stopped = []
    class Request:
        async def receive(self):
            if not disconnect:
                await asyncio.sleep(60)
            return {'type': 'http.disconnect'}
    async def slow():
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append(True)
    async def run():
        with pytest.raises(HTTPException) as exc:
            await g.until_disconnected(Request(), slow(), deadline)
        assert exc.value.status_code == status
    asyncio.run(run())
    assert stopped
