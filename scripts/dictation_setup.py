"""Prepare, temporarily stage, enable or restore this adapter's exact settings.

Never prints or persists the OpenAI key. App-local client token stays outside Git.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import secrets

from async_workflow.core import Settings, atomic, read_json, now, file_hash
from async_workflow.dictation_gateway import MODEL, PORT, identity

APP = Path.home()/'AppData/Roaming/Shandianshuo/config.json'
CLIENT = Path.home()/'AppData/Local/Shandianshuo/shandianshuo.exe'
REQUIRED_CHECKS = ('engineering', 'live_api', 'native_request_envelope', 'native_short_press',
                   'native_final_bullets', 'native_single_insert', 'timeout', 'cancellation')


def accepted(verdict, cfg):
    return (verdict.get('passed') is True and verdict.get('policy_identity') == identity(cfg)
            and verdict.get('client_sha256') == file_hash(CLIENT)
            and all(verdict.get('checks', {}).get(name) is True for name in REQUIRED_CHECKS))


def selected(config):
    return {'correction': config['ai']['correction'],
            'compatible': config['ai']['providers']['openai_compatible'],
            'organize_level': config['voice_input_organize_level'],
            'short_action': config['short_press_action'], 'long_action': config['long_press_action']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'stage', 'activate-test', 'enable', 'restore', 'status'])
    args = parser.parse_args()
    cfg = Settings.load(); root = cfg.root/'dictation'; root.mkdir(parents=True, exist_ok=True)
    token_file = root/'client-token.txt'
    if not token_file.exists():
        atomic(token_file, secrets.token_urlsafe(32), text=True)
    config = read_json(APP)
    before = root/'settings-before.json'
    if args.action in ('stage', 'activate-test', 'enable'):
        if args.action == 'enable':
            verdict = read_json(root/'acceptance.json', {})
            if not accepted(verdict, cfg):
                raise SystemExit('Matching acceptance evidence is required before final enable')
        previous = read_json(before)
        if not previous or previous.get('closed'):
            atomic(before, {'at': now(), **deepcopy(selected(config))})
        if args.action != 'stage':
            config['ai']['correction']['provider'] = 'openai_compatible'
            config['voice_input_organize_level'] = 'raw'
        config['ai']['providers']['openai_compatible'].update(
            enabled=True, endpoint=f'http://127.0.0.1:{PORT}/v1', model=MODEL,
            api_key=token_file.read_text(encoding='utf-8').strip())
        atomic(APP, config)
        atomic(root/'options.json', {'capture_protocol': args.action != 'enable'})
    if args.action == 'restore':
        original = read_json(before)
        if not original or original.get('closed'):
            raise SystemExit('No scoped settings snapshot')
        if config['ai']['correction']['provider'] not in ('openai_compatible', original['correction']['provider']):
            raise SystemExit('Fast provider changed externally; preserve the current selection')
        current_compatible = config['ai']['providers']['openai_compatible']
        if current_compatible != original['compatible'] and current_compatible.get('endpoint') != f'http://127.0.0.1:{PORT}/v1':
            raise SystemExit('Compatible provider changed externally; preserve its current configuration')
        config['ai']['correction']['provider'] = original['correction']['provider']
        config['ai']['providers']['openai_compatible'] = original['compatible']
        config['voice_input_organize_level'] = original['organize_level']
        atomic(APP, config)
        atomic(root/'options.json', {'capture_protocol': False})
        atomic(before, {**original, 'closed': True, 'restored_at': now()})
    print({'action': args.action, 'provider': config['ai']['correction']['provider'],
           'organize_level': config['voice_input_organize_level'],
           'short_action': config['short_press_action'], 'long_action': config['long_press_action'],
           'policy_identity': identity(cfg)})


if __name__ == '__main__':
    main()
