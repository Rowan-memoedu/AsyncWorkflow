"""Prepare, temporarily stage, enable or restore this adapter's exact settings.

Never prints or persists the OpenAI key. App-local client token stays outside Git.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import secrets
import re

import httpx
import psutil

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
            'short_action': config['short_press_action'], 'long_action': config['long_press_action'],
            'voice_input_override': deepcopy(config.get('features', {}).get('model-overrides', {}).get('voice_input', {}))}


def product_gateway():
    """Use the installed host's supported loopback API; never expose its token."""
    gateways = {}
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() != 'shandianshuo.exe':
            continue
        for child in process.children(recursive=True):
            try:
                env = child.environ()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            url = env.get('SHANDIANSHUO_DSH_PRODUCT_SETTINGS_URL', '').rstrip('/')
            token = env.get('SHANDIANSHUO_DSH_PRODUCT_SETTINGS_TOKEN', '')
            if token and re.fullmatch(r'http://127\.0\.0\.1:\d+/v1', url):
                gateways[url] = token
    if not gateways:
        return None
    if len(gateways) != 1:
        raise SystemExit('Multiple native settings gateways; no settings changed')
    url, token = next(iter(gateways.items()))
    def request(route, body):
        response = httpx.post(url+'/'+route, json=body,
            headers={'Authorization': 'Bearer '+token}, timeout=5)
        if response.status_code != 200:
            raise SystemExit('Native settings request failed; verify live settings before continuing')
        return response.json()
    return request


def live_models(gateway):
    return gateway('read', {'area': 'models'})['settings'] if gateway else None


def restored_in_running_app(models, original):
    return models is None or models['fast_model']['configured_provider'] == original['correction']['provider']


def restore_config(config, original, models):
    """Never remove adapter credentials/timeout while the running app uses it."""
    config['ai']['correction']['provider'] = original['correction']['provider']
    if not restored_in_running_app(models, original):
        return False
    config['ai']['providers']['openai_compatible'] = original['compatible']
    config['voice_input_organize_level'] = original['organize_level']
    if 'voice_input_override' in original:
        config.setdefault('features', {}).setdefault('model-overrides', {})['voice_input'] = deepcopy(original['voice_input_override'])
    return True


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
    gateway = product_gateway()
    models = live_models(gateway)
    if args.action in ('stage', 'activate-test', 'enable'):
        health = httpx.get(f'http://127.0.0.1:{PORT}/health', timeout=5)
        if health.status_code != 200 or health.json().get('policy_identity') != identity(cfg) or not health.json().get('ready'):
            raise SystemExit('Matching adapter service must be ready before native configuration')
        if args.action == 'enable':
            verdict = read_json(root/'acceptance.json', {})
            if not accepted(verdict, cfg):
                raise SystemExit('Matching acceptance evidence is required before final enable')
        previous = read_json(before)
        if previous and previous.get('closed') and not restored_in_running_app(models, previous):
            raise SystemExit('Previous restore is not effective in the running app; preserve its original snapshot')
        if not previous or previous.get('closed'):
            atomic(before, {'at': now(), **deepcopy(selected(config))})
        if args.action != 'stage':
            config['ai']['correction']['provider'] = 'openai_compatible'
            # Keep the native correction path active; only the adapter formats text.
            config['voice_input_organize_level'] = 'strong'
            config.setdefault('features', {}).setdefault('model-overrides', {}).setdefault('voice_input', {})['timeout_secs'] = 30
        config['ai']['providers']['openai_compatible'].update(
            enabled=True, endpoint=f'http://127.0.0.1:{PORT}/v1', model=MODEL,
            api_key=token_file.read_text(encoding='utf-8').strip())
        atomic(APP, config)
        atomic(root/'options.json', {'capture_protocol': args.action != 'enable'})
        if args.action != 'stage' and gateway:
            gateway('write', {'action': 'model_override', 'scenario': 'voice_input', 'timeout_secs': 30})
            gateway('write', {'action': 'voice_input_style', 'level': 'strong'})
            models = live_models(gateway)
            if (models['fast_model']['effective_provider'] != 'openai_compatible'
                    or models['fast_model']['effective_model'] != MODEL
                    or models['overrides']['voice_input']['values'].get('timeout_secs') != 30
                    or gateway('read', {'area': 'voice_input'})['settings']['style_level'] != 'strong'):
                raise SystemExit('Prepared settings are not active in the running app; native selection is required')
        if args.action != 'stage' and not gateway:
            raise SystemExit('Prepared settings require running-app verification before activation can be certified')
    if args.action == 'restore':
        original = read_json(before)
        if not original or (original.get('closed') and restored_in_running_app(models, original)):
            raise SystemExit('No scoped settings snapshot')
        if config['ai']['correction']['provider'] not in ('openai_compatible', original['correction']['provider']):
            raise SystemExit('Fast provider changed externally; preserve the current selection')
        current_compatible = config['ai']['providers']['openai_compatible']
        if current_compatible != original['compatible'] and current_compatible.get('endpoint') != f'http://127.0.0.1:{PORT}/v1':
            raise SystemExit('Compatible provider changed externally; preserve its current configuration')
        complete = restore_config(config, original, models)
        atomic(APP, config)
        atomic(root/'options.json', {'capture_protocol': False})
        atomic(before, {**original, 'closed': False, 'restore_pending_native_selection': not complete,
                       'restore_checked_at': now()})
        if not complete:
            raise SystemExit('Restore pending: select the original fast provider in the running app, then rerun restore. Adapter credentials and timeout were preserved.')
        if gateway:
            gateway('write', {'action': 'voice_input_style', 'level': original['organize_level']})
            if 'voice_input_override' in original:
                values = original['voice_input_override']
                gateway('write', {'action': 'model_override', 'scenario': 'voice_input',
                    **{k: v for k, v in values.items() if k in ('timeout_secs', 'temperature')},
                    'reset': [k for k in ('timeout_secs', 'temperature') if k not in values]})
            models = live_models(gateway)
            if (not restored_in_running_app(models, original)
                    or gateway('read', {'area': 'voice_input'})['settings']['style_level'] != original['organize_level']
                    or ('voice_input_override' in original
                        and models['overrides']['voice_input']['values'] != original['voice_input_override'])):
                raise SystemExit('Restore has not been verified in the running app; original snapshot remains open')
        atomic(before, {**original, 'closed': True, 'restore_pending_native_selection': False,
                       'restore_checked_at': now()})
    print({'action': args.action, 'provider': config['ai']['correction']['provider'],
           'organize_level': config['voice_input_organize_level'],
           'short_action': config['short_press_action'], 'long_action': config['long_press_action'],
           'live_fast_model': models['fast_model'] if models else None,
           'live_voice_input_override': models['overrides']['voice_input']['values'] if models else None,
           'policy_identity': identity(cfg)})


if __name__ == '__main__':
    main()
