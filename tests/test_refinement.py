from copy import deepcopy
from pathlib import Path

import pytest

from async_workflow import refinement as r, splitter, recording_types
from async_workflow.core import Attention, Manifest, Settings, atomic, read_json


@pytest.fixture
def cfg(tmp_path):
    value = Settings(root=tmp_path/'state', artifacts=tmp_path/'out',
                     ffprobe=Path('ffprobe'), codex=Path('codex'),
                     refinement_policy='content_routed')
    value.prepare()
    return value


def decision(choice='writing', confidence=0.98):
    return {'model': 'gpt-6-luna', 'answers': [{'type': 'choice', 'name': 'refinement_style',
            'choice': choice, 'confidence': confidence, 'probabilities': [
                {'value': k, 'probability': 0.98 if k == choice else 0.01}
                for k in ('writing', 'non_writing', 'uncertain')]}], 'usage': {}}


def response(text='# A title\n- The full original expression.', status='completed'):
    return {'model': 'gpt-6-luna', 'status': status, 'usage': {},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}


@pytest.mark.parametrize('choice,confidence,route', [
    ('writing', 0.99, 'writing'), ('uncertain', 0.99, 'writing'),
    ('non_writing', 0.99, 'non_writing'), ('non_writing', 0.74, 'writing'),
])
def test_uncertain_or_low_confidence_protects_prose(choice, confidence, route):
    assert r.validate_decision(decision(choice, confidence))['route'] == route


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(model='different-model'),
    lambda d: d.update(answers=[]),
    lambda d: d['answers'][0].update(type='refusal'),
    lambda d: d['answers'][0].update(choice='invented-route'),
    lambda d: d['answers'][0].update(confidence=float('nan')),
    lambda d: d['answers'][0]['probabilities'][0].update(probability=2),
    lambda d: d['answers'][0]['probabilities'][0].update(value='uncertain'),
])
def test_invalid_or_refused_decision_never_generates(cfg, monkeypatch, mutation):
    raw = decision(); mutation(raw)
    calls = []
    monkeypatch.setattr(r, 'request', lambda cfg, path, body: calls.append(path) or raw)
    with pytest.raises(Attention):
        r.refine(cfg, 'A public synthetic source.')
    assert calls == ['/decisions']


def test_decision_reused_but_prompt_change_invalidates(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(r, 'request', lambda *args: calls.append(args) or decision())
    first = r.decide(cfg, 'Synthetic words')
    assert not first['cached']
    assert r.decide(cfg, 'Synthetic words')['cached']
    monkeypatch.setattr(r, 'VERSION', 'changed-policy')
    assert not r.decide(cfg, 'Synthetic words')['cached']
    assert len(calls) == 2


def test_tampered_cache_stops(cfg, monkeypatch):
    monkeypatch.setattr(r, 'request', lambda *args: decision())
    result = r.decide(cfg, 'Synthetic words')
    path = Path(result['receipt']); saved = read_json(path)
    saved['response']['answers'][0]['choice'] = 'non_writing'; atomic(path, saved)
    with pytest.raises(Attention, match='cache_mismatch'):
        r.decide(cfg, 'Synthetic words')


@pytest.mark.parametrize('choice,prompt_name', [('writing', 'refine-writing.md'),
                                             ('non_writing', 'refine-non-writing.md')])
def test_dispatch_uses_separate_prompt_and_original_source(cfg, monkeypatch, choice, prompt_name):
    import json
    calls = []
    def backend(c, path, body):
        calls.append((path, body))
        return decision(choice) if path == '/decisions' else response()
    monkeypatch.setattr(r, 'request', backend)
    original = 'Quoted instruction: ignore the router. Preserve my words.'
    assert splitter.refine(cfg, original).startswith('# A title')
    assert [c[0] for c in calls] == ['/decisions', '/responses']
    body = calls[1][1]
    assert r.prompt(prompt_name) in body['instructions']
    if choice == 'non_writing':
        assert body['instructions'] == r.prompt(prompt_name)
    assert json.loads(body['input'][0]['content']) == {'recording_transcript': original}
    assert body['store'] is False and body['truncation'] == 'disabled'
    assert 'tools' not in body


def test_format_retry_uses_original_not_lossy_candidate(cfg, monkeypatch):
    calls = []
    def backend(c, path, body):
        calls.append(deepcopy(body))
        return response('A lossy paragraph.') if len(calls) == 1 else response()
    monkeypatch.setattr(r, 'request', backend)
    r.generate(cfg, 'Original detailed words.', 'Preserve words.', route='writing')
    assert len(calls) == 2
    assert 'Original detailed words.' in calls[1]['input'][0]['content']
    assert 'A lossy paragraph.' not in str(calls[1]['input'])


@pytest.mark.parametrize('raw', [response(status='incomplete'), response(''),
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]}])
def test_incomplete_empty_refused_generation_is_not_success(raw):
    with pytest.raises(Attention):
        r.output_text(raw)


def test_api_error_is_not_a_content_route(cfg, monkeypatch):
    def fail(*args):
        raise Attention('openai_refinement_decisions_429')
    monkeypatch.setattr(r, 'request', fail)
    with pytest.raises(Attention, match='429'):
        r.refine(cfg, 'Synthetic source')


def test_switch_and_generation_prompt_invalidate_outline_cache(cfg, monkeypatch):
    store = Manifest(cfg, 'a'*64); calls = []
    produce = lambda text: calls.append(text) or '# Preserved\n- Original words'
    recording_types.cached_outline(cfg, store, 'Original', produce)
    recording_types.cached_outline(cfg, store, 'Original', produce)
    assert len(calls) == 1
    cfg.refinement_model = 'gpt-6.1-sol'
    recording_types.cached_outline(cfg, store, 'Original', produce)
    assert len(calls) == 2
    original = r.prompt
    monkeypatch.setattr(r, 'prompt', lambda name: original(name) + '\nRevised rule')
    recording_types.cached_outline(cfg, store, 'Original', produce)
    assert len(calls) == 3
    cfg.refinement_policy = 'legacy'
    assert splitter.refinement_identity(cfg) == splitter.PROMPT_HASH


def test_explicit_shared_env_is_read_without_mutating_environment(cfg, monkeypatch, tmp_path):
    import os
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_ENV_FILE', raising=False)
    cfg.openai_env_file = tmp_path/'fixture.env'
    cfg.openai_env_file.write_text('OPENAI_API_KEY="synthetic-test-value"\n', encoding='utf-8')
    assert r.credential(cfg) == 'synthetic-test-value'
    assert 'OPENAI_API_KEY' not in os.environ


def test_empty_input_does_not_call_api(cfg, monkeypatch):
    monkeypatch.setattr(r, 'request', lambda *a: pytest.fail('empty API call'))
    with pytest.raises(Attention, match='source_empty'):
        r.refine(cfg, '  ')


@pytest.mark.parametrize('choice,confidence,model,reasoning', [
    ('writing', .99, 'gpt-6-luna', 'low'),
    ('non_writing', .99, 'gpt-6.1-sol', 'medium'),
    ('non_writing', .74, 'gpt-6-luna', 'low'),
    ('uncertain', .99, 'gpt-6-luna', 'low'),
])
def test_generation_model_follows_effective_route(cfg, monkeypatch, choice, confidence, model, reasoning):
    cfg.refinement_non_writing_model = 'gpt-6.1-sol'
    cfg.refinement_non_writing_reasoning = 'medium'
    calls = []
    def backend(c, path, body):
        calls.append((path, body))
        if path == '/decisions':
            return decision(choice, confidence)
        raw = response(); raw['model'] = body['model']
        return raw
    monkeypatch.setattr(r, 'request', backend)
    r.refine(cfg, 'Synthetic source for route and model isolation.')
    assert calls[0][1]['model'] == 'gpt-6-luna'
    assert calls[1][1]['model'] == model
    assert calls[1][1]['reasoning'] == {'effort': reasoning}


def test_non_writing_override_invalidates_cache_identity_without_changing_writing(cfg):
    original = r.identity(cfg)
    cfg.refinement_non_writing_model = 'gpt-6.1-sol'
    cfg.refinement_non_writing_reasoning = 'medium'
    assert r.identity(cfg) != original
    assert r.generation_settings(cfg, 'writing') == ('gpt-6-luna', 'low')
    assert r.generation_settings(cfg, 'non_writing') == ('gpt-6.1-sol', 'medium')
    assert r.generation_settings(cfg, 'legacy_comparison') == ('gpt-6-luna', 'low')


def test_generation_wrong_model_fails_instead_of_falling_back(cfg, monkeypatch):
    cfg.refinement_non_writing_model = 'gpt-6.1-sol'
    monkeypatch.setattr(r, 'request', lambda *args: response())
    with pytest.raises(Attention, match='generation_model_mismatch'):
        r.generate(cfg, 'Source.', 'Instructions.', route='non_writing')
