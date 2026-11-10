"""Writing-aware refinement. Decisions and generation use official OpenAI APIs.

No execution, archival, audio upload, or provider fallback occurs in this module.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

from filelock import FileLock

from .core import Attention, atomic, digest, now, read_json
from .markdown import parse_outline

VERSION = 'writing-aware-openai-v1'
DECISION_MODEL = 'gpt-6-luna'
MIN_NON_WRITING_CONFIDENCE = 0.75
MAX_OUTPUT_TOKENS = 32768
PROMPTS = Path(__file__).with_name('prompts')
CHOICES = [
    {'value': 'writing', 'description': '已有表达性正文，须保留自然表述与风格；含实质写作的混合输入也选此项。'},
    {'value': 'non_writing', 'description': '纯任务、需求、步骤、约束、清单或事实条目，可做结构化整理。'},
    {'value': 'uncertain', 'description': '无法可靠区分用途，采用保留表达的写作整理方式。'},
]


def prompt(name):
    return (PROMPTS / name).read_text(encoding='utf-8')


def decision_question():
    return {'type': 'choice', 'name': 'refinement_style',
            'instructions': prompt('decide-writing.md'), 'choices': CHOICES}


def decision_identity():
    return digest(json.dumps({'version': VERSION, 'model': DECISION_MODEL,
                             'question': decision_question(),
                             'non_writing_confidence': MIN_NON_WRITING_CONFIDENCE},
                            ensure_ascii=False, sort_keys=True))


def identity(cfg):
    return digest(json.dumps({'version': VERSION, 'decision': decision_identity(),
                             'model': cfg.refinement_model, 'reasoning': cfg.refinement_reasoning,
                             'max_output_tokens': MAX_OUTPUT_TOKENS,
                             'prompts': {name: prompt(name) for name in
                                 ('refine-common.md', 'refine-writing.md', 'refine-non-writing.md')}},
                            ensure_ascii=False, sort_keys=True))


def credential(cfg):
    value = os.environ.get('OPENAI_API_KEY', '').strip()
    if not value:
        path = Path(os.environ.get('OPENAI_ENV_FILE') or cfg.openai_env_file)
        try:
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                key, separator, candidate = line.strip().partition('=')
                if separator and key == 'OPENAI_API_KEY':
                    value = candidate.strip().strip('\"\'')
                    break
        except OSError:
            raise Attention('openai_refinement_credential_missing') from None
    if not value:
        raise Attention('openai_refinement_credential_missing')
    return value


def request(cfg, endpoint, payload):
    from openai import OpenAI, OpenAIError
    try:
        # The pinned SDK predates Decisions; its public generic POST surface
        # sends the documented REST request without replacing the SDK/runtime.
        with OpenAI(api_key=credential(cfg), base_url='https://api.openai.com/v1',
                    timeout=180, max_retries=2) as api:
            return api.post(endpoint, body=payload, cast_to=dict)
    except OpenAIError as exc:
        status = getattr(exc, 'status_code', None)
        suffix = str(status) if isinstance(status, int) else 'transport'
        raise Attention('openai_refinement_' + endpoint.strip('/') + '_' + suffix) from None


def _probability(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def validate_decision(raw):
    if not isinstance(raw, dict) or raw.get('model') != DECISION_MODEL:
        raise Attention('refinement_decision_model_mismatch')
    answers = raw.get('answers')
    if not isinstance(answers, list) or len(answers) != 1:
        raise Attention('refinement_decision_invalid')
    answer = answers[0]
    if not isinstance(answer, dict):
        raise Attention('refinement_decision_invalid')
    if answer.get('type') == 'refusal':
        raise Attention('refinement_decision_refused')
    choices = {c['value'] for c in CHOICES}
    if (answer.get('type') != 'choice' or answer.get('name') != 'refinement_style'
            or answer.get('choice') not in choices or not _probability(answer.get('confidence'))):
        raise Attention('refinement_decision_invalid')
    probabilities = answer.get('probabilities')
    if not isinstance(probabilities, list) or len(probabilities) != len(choices):
        raise Attention('refinement_decision_invalid_probabilities')
    distribution = {}
    for item in probabilities:
        if (not isinstance(item, dict) or item.get('value') not in choices
                or item['value'] in distribution or not _probability(item.get('probability'))):
            raise Attention('refinement_decision_invalid_probabilities')
        distribution[item['value']] = item['probability']
    if abs(sum(distribution.values()) - 1) > 0.01:
        raise Attention('refinement_decision_invalid_probabilities')
    selected = answer['choice']
    # A low-confidence non-writing decision must not erase expressive prose.
    route = ('non_writing' if selected == 'non_writing'
             and answer['confidence'] >= MIN_NON_WRITING_CONFIDENCE else 'writing')
    return {'choice': selected, 'route': route, 'confidence': answer['confidence'],
            'probabilities': distribution, 'conservative_route': selected != route}


def usage(cfg, operation, raw, seconds):
    with FileLock(str(cfg.root / 'locks/model-usage.lock')):
        with (cfg.root / 'model-usage.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps({'at': now(), 'operation': operation, 'provider': 'openai_api',
                'model': raw.get('model'), 'usage': raw.get('usage', {}), 'seconds': seconds}) + '\n')


def decide(cfg, text):
    if not text.strip():
        raise Attention('refinement_source_empty')
    cfg.prepare()
    signature = digest(decision_identity() + text)
    path = cfg.root / 'refinement-decisions' / (signature + '.json')
    with FileLock(str(cfg.root / 'locks' / ('refinement-' + signature + '.lock'))):
        saved = read_json(path)
        if saved:
            raw = saved.get('response')
            if (saved.get('signature') != signature or saved.get('source_sha256') != digest(text)
                    or saved.get('response_sha256') != digest(json.dumps(raw, sort_keys=True))):
                raise Attention('refinement_decision_cache_mismatch')
            return {**validate_decision(raw), 'receipt': str(path), 'cached': True}
        started = time.monotonic()
        raw = request(cfg, '/decisions', {'model': DECISION_MODEL,
            'input': json.dumps({'recording_transcript': text}, ensure_ascii=False),
            'questions': [decision_question()]})
        seconds = time.monotonic() - started
        # Keep the actual response, including invalid/refused answers, for diagnosis.
        atomic(cfg.root / 'model-attempts' / ('decision-' + signature + '-' +
               digest(json.dumps(raw, sort_keys=True)) + '.json'),
               {'operation': 'refinement_decision', 'source_sha256': digest(text),
                'response': raw, 'seconds': seconds, 'at': now()})
        decision = validate_decision(raw)
        usage(cfg, 'refinement_decision', raw, seconds)
        atomic(path, {'signature': signature, 'source_sha256': digest(text),
                      'decision_identity': decision_identity(), 'response': raw,
                      'response_sha256': digest(json.dumps(raw, sort_keys=True)),
                      **decision, 'seconds': seconds, 'at': now()})
        return {**decision, 'receipt': str(path), 'cached': False}


def output_text(raw):
    if not isinstance(raw, dict) or raw.get('status') != 'completed':
        raise Attention('refinement_response_incomplete')
    parts = []
    for item in raw.get('output', []):
        if item.get('type') != 'message':
            continue
        for content in item.get('content', []):
            if content.get('type') == 'refusal':
                raise Attention('refinement_response_refused')
            if content.get('type') == 'output_text':
                parts.append(content.get('text', ''))
    value = '\n'.join(parts).strip()
    if not value:
        raise Attention('refinement_response_empty')
    return value


def generate(cfg, text, instructions, *, route, decision_receipt=None):
    cfg.prepare()
    inputs = [{'role': 'user', 'content': json.dumps({'recording_transcript': text}, ensure_ascii=False)}]
    for attempt in range(3):
        started = time.monotonic()
        raw = request(cfg, '/responses', {'model': cfg.refinement_model,
            'instructions': instructions, 'input': inputs, 'store': False,
            'reasoning': {'effort': cfg.refinement_reasoning},
            'max_output_tokens': MAX_OUTPUT_TOKENS, 'truncation': 'disabled'})
        seconds = time.monotonic() - started
        atomic(cfg.root / 'model-attempts' / ('refine-' + digest(text + json.dumps(raw, sort_keys=True)) + '.json'),
            {'operation': 'refine', 'provider': 'openai_api', 'route': route,
             'decision_receipt': decision_receipt, 'policy_identity': identity(cfg),
             'source': text, 'prompt_sha256': digest(instructions), 'response': raw,
             'seconds': seconds, 'at': now()})
        usage(cfg, 'refine', raw, seconds)
        if raw.get('model') != cfg.refinement_model:
            raise Attention('refinement_generation_model_mismatch')
        value = output_text(raw)
        try:
            parse_outline(value)
            return value
        except ValueError:
            if attempt == 2:
                raise Attention('outline_format_failed') from None
            # Retry from the original source; never use a lossy candidate as source.
            inputs = [inputs[0], {'role': 'user', 'content':
                '上次输出未通过格式校验。请重新从原始素材整理，严格输出一个 # 标题及 - 层级列表；保留原信息和语气。'}]


def refine(cfg, text):
    decision = decide(cfg, text)
    name = 'refine-writing.md' if decision['route'] == 'writing' else 'refine-non-writing.md'
    instructions = prompt('refine-common.md') + '\n\n' + prompt(name)
    return generate(cfg, text, instructions, route=decision['route'], decision_receipt=decision['receipt'])
