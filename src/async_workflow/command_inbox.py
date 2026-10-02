"""Mechanical RemNote inbox -> fresh Codex Thread, on the existing Prefect server.

No LLM preprocessing, parent-thread context, prompt wrapper or output schema.
Local manifests are source delivery receipts; native App Server owns Threads.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from uuid import UUID

from filelock import FileLock
from openai_codex import ApprovalMode, Codex, Sandbox
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import ReasoningEffort
from prefect import flow
from prefect.client.orchestration import get_client
from prefect.deployments import run_deployment
from pydantic import BaseModel, ConfigDict, Field

from . import remnote
from .codex_appserver import sdk_config
from .core import Attention, Manifest, Settings, UnknownOutcome, atomic, digest, now, read_json

MODEL_RULE = re.compile(r'^一次性命令当前要求使用的模型[：:]\s*(\S+)\s+(\S+)\s*$')
TERMINAL = {'completed', 'failed', 'interrupted', 'legacy_submitted'}


class CommandSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: str
    inbox_id: str
    archive_id: str
    cwd: Path
    approved_rules_hash: str
    enabled: bool = False
    interval_seconds: int = Field(default=1800, ge=60)

    @classmethod
    def load(cls, cfg):
        return cls.model_validate(read_json(cfg.root / 'command-inbox.json', {}))

    def scope(self):
        return self.model_dump(include={'document_id', 'inbox_id', 'archive_id'})


def item_settings(cfg):
    return cfg.model_copy(update={'artifacts': cfg.root / 'command-inbox/items'})


def receipt(cfg, command_id):
    return Manifest(item_settings(cfg), command_id)


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get('children', []))


def rules(snapshot, settings):
    texts = [node['text'] for node in flatten(snapshot['rules']) if node['text'].strip()]
    matches = [MODEL_RULE.fullmatch(text) for text in texts]
    models = [match for match in matches if match]
    if len(models) != 1:
        raise Attention('command_model_rule_missing_or_ambiguous')
    fixed = [text for text, match in zip(texts, matches) if not match]
    if digest(json.dumps(fixed, ensure_ascii=False)) != settings.approved_rules_hash:
        raise Attention('command_rules_changed_requires_review')
    model, effort = models[0].groups()
    model = model if model.startswith('gpt-') else 'gpt-' + model
    try:
        ReasoningEffort(effort)
    except ValueError:
        raise Attention('command_reasoning_effort_invalid') from None
    return {'model': model, 'effort': effort}


def is_blank(node):
    return not node['text'].strip() and not node['prompt_text'].strip() and not node.get('children')


def blocks(nodes):
    """Only empty direct siblings delimit commands; nested nodes stay together."""
    current = []
    for node in nodes:
        if is_blank(node):
            if current:
                yield current, [*current, node]
                current = []
        else:
            current.append(node)
    if current:
        yield current, current


def plain_prompt(nodes, depth=0):
    """Lossless text serialization; indentation is the only structural addition.

    Keep the readiness '%' and original text, including intra-node newlines.
    RemNote rich text is exported by its SDK, never paraphrased by a model.
    """
    lines = []
    for node in nodes:
        lines.append('  ' * depth + node['prompt_text'])
        if node.get('children'):
            lines.append(plain_prompt(node['children'], depth + 1))
    return '\n'.join(lines)


def candidates(snapshot, settings):
    if snapshot.get('protocol') != 'command-inbox-1' or snapshot.get('document_id') != settings.document_id:
        raise Attention('command_plugin_protocol_or_scope_mismatch')
    model = rules(snapshot, settings)
    result = []
    for content, archived in blocks(snapshot['inbox']['children']):
        if not content[0]['text'].lstrip().startswith('%'):
            continue
        command_id = digest('remnote-command:' + settings.document_id + ':' + content[0]['id'])
        prompt = plain_prompt(content)
        source = {**settings.scope(), 'nodes': archived, 'prompt': prompt, 'prompt_sha256': digest(prompt),
                  'command_id': command_id, 'cwd': str(settings.cwd), **model}
        source['revision'] = digest(json.dumps(source, sort_keys=True, ensure_ascii=False))
        result.append(source)
    return result


async def snapshot_async(cfg, settings):
    async with remnote.connection(cfg) as client:
        return await remnote.call(client, 'command_inbox_read', **settings.scope())


def snapshot(cfg, settings):
    return asyncio.run(snapshot_async(cfg, settings))


def archive(cfg, manifest):
    data = manifest.read()
    if data.get('archive', {}).get('status') == 'verified':
        return data['archive']
    sent = data['effects'].get('dispatch', {})
    if not sent.get('thread_id') or not sent.get('turn_id'):
        raise UnknownOutcome('command_not_confirmed_submitted_no_archive')
    source = data['command_source']

    async def move():
        async with remnote.connection(cfg) as client:
            return await remnote.call(client, 'command_inbox_archive',
                **{key: source[key] for key in ('document_id', 'inbox_id', 'archive_id', 'nodes')},
                effect_key=source['command_id'], thread_id=sent['thread_id'], turn_id=sent['turn_id'])

    result = asyncio.run(move())
    if (result.get('status') != 'verified' or result.get('ids') != [node['id'] for node in source['nodes']]
            or result.get('thread_id') != sent['thread_id'] or result.get('turn_id') != sent['turn_id']):
        raise UnknownOutcome('command_archive_readback_mismatch')
    manifest.update(archive={**result, 'at': now()})
    return result


def try_archive(cfg, manifest):
    from filelock import Timeout
    try:
        with FileLock(str(cfg.root / 'locks' / ('command-archive-' + manifest.folder.name + '.lock')), timeout=10):
            archive(cfg, manifest)
    except Timeout:
        return  # Another caller is already finishing the same idempotent move.
    except Exception as exc:
        # Delivery already happened. A failed move must never replay the command
        # or interrupt its running native turn; the next scan retries this move.
        manifest.update(archive={'status': 'pending', 'error_type': type(exc).__name__, 'at': now()})


def model_available(codex, source):
    model = next((m for m in codex.models().data if m.model == source['model']), None)
    if model is None or source['effort'] not in {x.reasoning_effort.value for x in model.supported_reasoning_efforts}:
        raise Attention('requested_command_model_or_effort_unavailable')


def deliver(cfg, manifest, source, after_submit):
    """Exactly one new native Thread, with exactly the source text as its turn."""
    prior = manifest.effect('dispatch')
    if prior:
        if prior.get('turn_id'):
            after_submit()
            return prior
        raise UnknownOutcome('command_submission_unknown_no_replay')
    if digest(source['prompt']) != source['prompt_sha256']:
        raise Attention('command_prompt_integrity_failed')
    with Codex(sdk_config(cfg, Path(source['cwd']))) as codex:
        model_available(codex, source)
        state = {'phase': 'thread_start_pending', 'status': 'outcome_unknown',
                 'model': source['model'], 'effort': source['effort'],
                 'prompt_sha256': source['prompt_sha256'], 'at': now()}
        manifest.save_effect('dispatch', state)
        try:
            # Deliberately omit base/developer instructions, parent history,
            # thread fork/resume, agent-generated title and result schema.
            thread = codex.thread_start(cwd=source['cwd'], model=source['model'],
                config={'model_reasoning_effort': source['effort']}, ephemeral=False,
                sandbox=Sandbox.workspace_write, approval_mode=ApprovalMode.deny_all,
                service_name='async_workflow')
            state.update(thread_id=thread.id, phase='thread_created')
            manifest.save_effect('dispatch', state)
            thread.set_name(source['prompt'].splitlines()[0][:60])
            state['phase'] = 'turn_start_pending'
            manifest.save_effect('dispatch', state)
            handle = thread.turn(source['prompt'], model=source['model'], effort=ReasoningEffort(source['effort']))
            state.update(turn_id=handle.id, status='submitted', phase='turn_started', submitted_at=now())
            manifest.save_effect('dispatch', state)
            after_submit()
            completed = handle.run()
            state.update(status=completed.status.value, phase='turn_terminal', finished_at=now())
            atomic(manifest.folder / 'native-result.json', {'status': state['status'], 'text': completed.final_response})
            manifest.save_effect('dispatch', state)
            return state
        except Exception as exc:
            state['error_type'] = type(exc).__name__
            manifest.save_effect('dispatch', state)
            raise UnknownOutcome('command_native_outcome_unknown_ids_preserved') from None


def reconcile(cfg, manifest):
    saved = manifest.effect('dispatch')
    if not saved or saved.get('status') in TERMINAL:
        return
    if not saved.get('turn_id'):
        raise UnknownOutcome('command_submission_unknown_no_replay')
    source = manifest.read()['command_source']
    with CodexClient(sdk_config(cfg, Path(source['cwd']))) as client:
        client.initialize()
        data = client.thread_read(saved['thread_id'], include_turns=True).model_dump(mode='json', by_alias=True)['thread']
    turn = next((t for t in data.get('turns', []) if t['id'] == saved['turn_id']), None)
    if not turn:
        raise UnknownOutcome('command_known_turn_not_found')
    if turn['status'] != 'inProgress':
        manifest.save_effect('dispatch', {**saved, 'status': turn['status'], 'phase': 'turn_terminal', 'checked_at': now()})


def queue_command(cfg, source):
    manifest = receipt(cfg, source['command_id'])
    with FileLock(str(cfg.root / 'locks' / ('command-' + source['command_id'] + '.lock')), timeout=0):
        data = manifest.read()
        if data['effects'].get('dispatch'):
            return 'already_started'
        previous = data.get('prefect_delivery', {})
        attempt = previous.get('attempt', 0)
        if previous.get('run_id'):
            with get_client(sync_client=True) as client:
                run = client.read_flow_run(UUID(previous['run_id']))
            if not run.state or not run.state.is_final():
                return 'already_queued'
            if run.state.is_cancelled() and data.get('command_source', {}).get('revision') == source['revision']:
                return 'cancelled'
            attempt += 1  # No native effect exists: safe to retry preflight failures.
        pending = {'attempt': attempt}
        manifest.update(command_source=source, prefect_delivery=pending)
        run = run_deployment('deliver-remnote-command/local', parameters={'command_id': source['command_id']},
            timeout=0, idempotency_key=source['command_id'] + ':' + str(attempt), as_subflow=False)
        manifest.update(prefect_delivery={**pending, 'run_id': str(run.id)})
        return 'queued'


@flow(name='deliver-remnote-command', log_prints=False, retries=0)
def deliver_command(command_id: str):
    cfg = Settings.load()
    cfg.prepare()
    settings = CommandSettings.load(cfg)
    if not settings.enabled:
        raise Attention('command_inbox_disabled')
    manifest = receipt(cfg, command_id)
    with FileLock(str(cfg.root / 'locks' / ('command-' + command_id + '.lock'))):
        if manifest.effect('dispatch'):
            try_archive(cfg, manifest)
            reconcile(cfg, manifest)
            return {'command_id': command_id, 'status': 'previous_delivery_preserved'}
        source = manifest.read()['command_source']
        current = next((s for s in candidates(snapshot(cfg, settings), settings) if s['command_id'] == command_id), None)
        if current != source:
            return {'command_id': command_id, 'status': 'source_changed_before_delivery'}
        result = deliver(cfg, manifest, source, lambda: try_archive(cfg, manifest))
        return {key: result.get(key) for key in ('thread_id', 'turn_id', 'status', 'model', 'effort')}


@flow(name='scan-remnote-commands', log_prints=False, retries=0)
def scan_commands():
    cfg = Settings.load()
    cfg.prepare()
    settings = CommandSettings.load(cfg)
    if not settings.enabled:
        return {'status': 'disabled'}
    with FileLock(str(cfg.root / 'locks/command-inbox-scan.lock'), timeout=0):
        return scan_once(cfg, settings)


def scan_once(cfg, settings):
    from filelock import Timeout
    report = {'at': now(), 'status': 'completed', 'ready': 0, 'queued': 0, 'issues': []}
    try:
        # Finish partial moves before snapshotting: their remnants are never
        # interpreted as new, independently ready commands.
        for path in item_settings(cfg).artifacts.glob('*/manifest.json'):
            manifest = receipt(cfg, path.parent.name)
            saved = manifest.effect('dispatch')
            if not saved:
                continue
            if saved.get('turn_id'):
                try_archive(cfg, manifest)
                if manifest.read().get('archive', {}).get('status') != 'verified':
                    report['issues'].append({'command_id': path.parent.name, 'reason': 'archive_pending'})
            try:
                # Nonblocking: the native execution worker may hold this lease.
                with FileLock(str(cfg.root / 'locks' / ('command-' + path.parent.name + '.lock')), timeout=0):
                    reconcile(cfg, manifest)
            except Timeout:
                continue
            except Exception as exc:
                report['issues'].append({'command_id': path.parent.name, 'error_type': type(exc).__name__})
        ready = candidates(snapshot(cfg, settings), settings)
        report['ready'] = len(ready)
        # Any Rem belonging to a prior delivery is claimed even after a partial
        # move or a manual reorder. Never create a second task from a remainder.
        claimed = set()
        for path in item_settings(cfg).artifacts.glob('*/manifest.json'):
            data = read_json(path)
            if data['effects'].get('dispatch'):
                claimed.update(n['id'] for n in data['command_source']['nodes'])
        for source in ready:
            if any(n['id'] in claimed for n in source['nodes']):
                continue
            try:
                if queue_command(cfg, source) == 'queued':
                    report['queued'] += 1
            except Exception as exc:
                report['issues'].append({'command_id': source['command_id'], 'error_type': type(exc).__name__})
        if report['issues']:
            report['status'] = 'needs_attention'
    except Exception as exc:
        report.update(status='blocked', error_type=type(exc).__name__)
        if isinstance(exc, Attention):
            report['reason'] = str(exc)
        raise
    finally:
        atomic(cfg.root / 'command-inbox/last-scan.json', report)
    return report


def deployments():
    cfg = Settings.load()
    if not (cfg.root / 'command-inbox.json').is_file():
        return []
    settings = CommandSettings.load(cfg)
    return [scan_commands.to_deployment(name='local', interval=settings.interval_seconds,
                                       paused=not settings.enabled, concurrency_limit=1),
            deliver_command.to_deployment(name='local', concurrency_limit=4)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'inspect', 'status'])
    args = parser.parse_args()
    cfg = Settings.load()
    cfg.prepare()
    settings = CommandSettings.load(cfg)
    if args.action == 'scan':
        from prefect.settings import PREFECT_API_URL, PREFECT_HOME, PREFECT_LOCAL_STORAGE_PATH, temporary_settings
        with temporary_settings({PREFECT_API_URL: cfg.prefect_url, PREFECT_HOME: cfg.root / 'prefect',
                                 PREFECT_LOCAL_STORAGE_PATH: cfg.root / 'prefect-results'}):
            print(json.dumps(scan_commands(), ensure_ascii=False))
    elif args.action == 'inspect':
        data = snapshot(cfg, settings)
        ready = candidates(data, settings)
        # No private command body is printed to terminal/Prefect logs.
        print(json.dumps({'protocol': data['protocol'], 'model': rules(data, settings), 'ready': len(ready),
            'items': [{'id': s['command_id'], 'prompt_sha256': s['prompt_sha256']} for s in ready]}))
    else:
        print(json.dumps({'enabled': settings.enabled, 'interval_seconds': settings.interval_seconds,
                         'last_scan': read_json(cfg.root / 'command-inbox/last-scan.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
