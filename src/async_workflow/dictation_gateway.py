"""Loopback-only OpenAI protocol adapter for short-press dictation.

Uses the same content policies and decision validator as recording refinement.
No ASR, desktop input, task dispatch or archival is performed here.
"""
from __future__ import annotations
import asyncio
import hmac
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .core import Settings, Attention, atomic, digest, now, read_json
from . import refinement as policy
from .markdown import parse_outline

VERSION = 'dictation-openai-v3'
MODEL = 'writing-router'
PORT = 43143
DEADLINE_SECONDS = 27


def instructions(route):
    if route == 'writing':
        return policy.writing_instructions(dictation=True)
    else:
        rules = policy.prompt('refine-dictation-non-writing.md')
    return rules + '\n\n' + policy.prompt('refine-dictation-format.md')


def identity(cfg):
    models = json.dumps({route: policy.generation_settings(cfg, route)
                         for route in ('writing', 'non_writing')}, sort_keys=True)
    return digest(VERSION + policy.decision_identity() + models
                  + instructions('writing') + instructions('non_writing'))


def extract_source(body):
    """Accept only explicit supported envelopes; never guess from arbitrary chat."""
    messages = body.get('messages')
    if not isinstance(messages, list):
        raise ValueError('messages_required')
    if any(not isinstance(m, dict) or m.get('role') not in ('system', 'developer', 'user') for m in messages):
        raise ValueError('conversation_not_dictation')
    users = [m for m in messages if isinstance(m, dict) and m.get('role') == 'user']
    if len(users) != 1 or not isinstance(users[0].get('content'), str):
        raise ValueError('unsupported_source_envelope')
    text = users[0]['content']
    # Shandianshuo 0.8.2's actual connection probe/VoiceInput envelope.
    # Strip only the protocol prefix, never a matching phrase in ordinary input.
    native = any(m.get('role') == 'system' and isinstance(m.get('content'), str)
                 and m['content'].startswith('你是语音输入文本整理器，不是对话助手。') for m in messages)
    if native:
        prefix = 'ASR 原文：\n'
        if not text.startswith(prefix):
            raise ValueError('unsupported_native_source_envelope')
        text = text[len(prefix):]
    if not text.strip() or len(text) > 120000:
        raise ValueError('source_empty_or_too_large')
    return text


def compile_bullets(value):
    lines = [line.rstrip() for line in value.splitlines() if line.strip()]
    if lines and lines[-1].strip() == '-':
        lines.pop()
    if not lines or any(not re.fullmatch(r'(?:  )*- \S.*', line) for line in lines):
        raise ValueError('bullet_format_invalid')
    if lines[0].startswith(' '):
        raise ValueError('root_indent_invalid')
    previous = 0
    for line in lines:
        indent = len(line)-len(line.lstrip(' '))
        if indent > previous + 2:
            raise ValueError('indent_jump')
        previous = indent
    text = '\n'.join(lines)
    parse_outline('# dictation\n' + text)
    # Only check delimited formulas, leaving literal currency outside math alone.
    for line in lines:
        if len(re.findall(r'(?<!\\)\$', line)) % 2:
            raise ValueError('unpaired_math_delimiter')
    return text + '\n- '


async def api_post(cfg, endpoint, payload):
    from openai import AsyncOpenAI, OpenAIError
    try:
        async with AsyncOpenAI(api_key=policy.credential(cfg), base_url='https://api.openai.com/v1',
                               timeout=DEADLINE_SECONDS, max_retries=0) as api:
            return await api.post(endpoint, body=payload, cast_to=dict)
    except OpenAIError as exc:
        status = getattr(exc, 'status_code', None)
        raise Attention('dictation_openai_' + (str(status) if isinstance(status, int) else 'transport')) from None


async def refine(cfg, text, post=api_post):
    raw = await post(cfg, '/decisions', {'model': policy.DECISION_MODEL,
        'input': json.dumps({'recording_transcript': text}, ensure_ascii=False),
        'questions': [policy.decision_question()]})
    choice = policy.validate_decision(raw)
    model, reasoning = policy.generation_settings(cfg, choice['route'])
    generated = await post(cfg, '/responses', {'model': model,
        'instructions': instructions(choice['route']),
        'input': [{'role': 'user', 'content': json.dumps({'recording_transcript': text}, ensure_ascii=False)}],
        'store': False, 'truncation': 'disabled', 'max_output_tokens': policy.MAX_OUTPUT_TOKENS,
        'reasoning': {'effort': reasoning}})
    if generated.get('model') != model:
        raise Attention('dictation_generation_model_mismatch')
    output = compile_bullets(policy.output_text(generated))
    return output, {'decision': choice, 'decision_response': raw,
                    'generation_model': generated['model'], 'generation_usage': generated.get('usage', {}),
                    'generation_reasoning': reasoning,
                    'prompt_sha256': digest(instructions(choice['route'])),
                    'generation_response_id': generated.get('id')}


async def until_disconnected(request, coroutine, deadline=DEADLINE_SECONDS):
    work = asyncio.create_task(coroutine)
    async def disconnected():
        while True:
            message = await request.receive()
            if message['type'] == 'http.disconnect':
                return
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({work, watcher}, timeout=deadline, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        if watcher in done:
            raise HTTPException(499, 'dictation_cancelled')
        raise HTTPException(504, 'dictation_deadline_exceeded')
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


def create_app(settings_loader=Settings.load, post=api_post):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def runtime(cfg):
        return cfg.root / 'dictation'

    async def authorize(request, cfg):
        # Host + loopback binding prevent DNS rebinding; no CORS is enabled.
        if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
            raise HTTPException(403, 'loopback_host_required')
        secret = runtime(cfg) / 'client-token.txt'
        expected = secret.read_text(encoding='utf-8').strip() if secret.exists() else ''
        supplied = request.headers.get('authorization', '')
        if not expected or not hmac.compare_digest(supplied, 'Bearer ' + expected):
            raise HTTPException(401, 'local_client_token_required')

    @app.get('/health')
    async def health():
        cfg = settings_loader()
        return {'ready': (runtime(cfg)/'client-token.txt').exists(), 'version': VERSION,
                'model': MODEL, 'policy_identity': identity(cfg)}

    @app.get('/v1/models')
    async def models(request: Request):
        cfg = settings_loader(); await authorize(request, cfg)
        return {'object': 'list', 'data': [{'id': MODEL, 'object': 'model', 'owned_by': 'local'}]}

    @app.post('/v1/chat/completions')
    async def complete(request: Request):
        cfg = settings_loader(); await authorize(request, cfg)
        if int(request.headers.get('content-length', '0')) > 1000000:
            raise HTTPException(413, 'request_too_large')
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('model') != MODEL:
                raise ValueError('unsupported_model')
            source = extract_source(body)
        except (ValueError, TypeError):
            # Native protocol fixture capture is explicitly enabled only during setup.
            if read_json(runtime(cfg)/'options.json', {}).get('capture_protocol') and isinstance(locals().get('body'), dict):
                atomic(runtime(cfg)/'protocol-capture.json', body)
            raise HTTPException(400, 'unsupported_dictation_request') from None
        if read_json(runtime(cfg)/'options.json', {}).get('capture_protocol'):
            atomic(runtime(cfg)/'protocol-capture.json', body)
        cfg.prepare()
        signature = digest(identity(cfg) + source)
        receipt = runtime(cfg)/'receipts'/(signature+'.json')
        saved = read_json(receipt)
        started = time.monotonic()
        if saved:
            if (saved.get('signature') != signature or saved.get('policy_identity') != identity(cfg)
                    or saved.get('output_sha256') != digest(saved.get('output', ''))):
                raise HTTPException(502, 'dictation_cache_integrity_failed')
            try:
                output = compile_bullets(saved['output'])
                evidence = saved['evidence']
            except (KeyError, ValueError):
                raise HTTPException(502, 'dictation_cache_integrity_failed') from None
        else:
            try:
                output, evidence = await until_disconnected(request, refine(cfg, source, post))
            except (Attention, ValueError) as exc:
                atomic(runtime(cfg)/'failures'/(uuid4().hex+'.json'), {'at': now(), 'source': source,
                    'error': str(exc) if isinstance(exc, Attention) else 'dictation_format_invalid',
                    'policy_identity': identity(cfg)})
                raise HTTPException(502, 'dictation_refinement_failed') from None
            except HTTPException as exc:
                atomic(runtime(cfg)/'failures'/(uuid4().hex+'.json'), {'at': now(), 'source': source,
                    'error': exc.detail, 'policy_identity': identity(cfg)})
                raise
            atomic(receipt, {'at': now(), 'source_sha256': digest(source), 'signature': signature,
                'output': output, 'output_sha256': digest(output), 'evidence': evidence,
                'seconds': time.monotonic()-started, 'policy_identity': identity(cfg)})
        identifier = 'chatcmpl-' + uuid4().hex
        if body.get('stream'):
            async def chunks():
                for delta, finish in [({'role': 'assistant', 'content': output}, None), ({}, 'stop')]:
                    yield 'data: ' + json.dumps({'id': identifier, 'object': 'chat.completion.chunk',
                        'created': int(time.time()), 'model': MODEL,
                        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}, ensure_ascii=False) + '\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(chunks(), media_type='text/event-stream')
        return JSONResponse({'id': identifier, 'object': 'chat.completion', 'created': int(time.time()),
            'model': MODEL, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': output},
                                        'finish_reason': 'stop'}]})

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=PORT, access_log=False)


if __name__ == '__main__':
    main()
