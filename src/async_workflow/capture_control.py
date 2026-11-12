"""Coordinate continuation with the existing recording worker's side-effect boundary."""
from contextlib import contextmanager
import asyncio
from filelock import FileLock

from .core import Attention, atomic, read_json
from .capture_protocol import validate_request, RECORDING_ID


def lock(cfg, identity):
    if not RECORDING_ID.fullmatch(identity):
        raise ValueError('Invalid recording identity')
    return FileLock(str(cfg.root / 'locks' / ('capture-effect-' + identity + '.lock')))


def state_path(cfg, identity):
    if not RECORDING_ID.fullmatch(identity):
        raise ValueError('Invalid recording identity')
    return cfg.root / 'capture-holds' / (identity + '.json')


def check(cfg, identity):
    state = read_json(state_path(cfg, identity), {})
    if state.get('status') in ('held', 'superseded'):
        raise Attention('capture_recording_' + state['status'])


@contextmanager
def effect_guard(cfg, identity):
    with lock(cfg, identity):
        check(cfg, identity)
        yield


def can_hold(cfg, identity):
    data = read_json(cfg.artifacts / identity / 'manifest.json', {})
    # A resolved route with no target writes is safe. Unknown/submitted effects are not.
    if any(effect.get('status') != 'not_started' or effect.get('parent')
           for effect in data.get('effects', {}).values()):
        return False
    if data.get('analysis_version') and data.get('segments') == []:
        return False
    return not any(data.get(key) for key in ('command_repair', 'command_archive', 'meeting'))


def execute(cfg, request):
    validate_request(request)
    receipt = cfg.root / 'capture-request-results' / (request['id'] + '.json')
    with FileLock(str(cfg.root / 'locks' / ('capture-request-' + request['id'] + '.lock'))):
        previous = read_json(receipt)
        if previous:
            if previous['request'] != request:
                raise Attention('capture_request_identity_changed')
            return previous['result']
        if request['kind'] == 'create_document':
            from .capture_catalog_sync import create_document
            result = asyncio.run(create_document(cfg, request))
        else:
            identity = request['recording_id']
            with lock(cfg, identity):
                path = state_path(cfg, identity)
                state = read_json(path, {})
                if request['kind'] == 'hold':
                    if state.get('status') == 'superseded' or not can_hold(cfg, identity):
                        result = {'status': 'processed'}
                    elif state.get('status') == 'held' and state.get('hold_id') != request['id']:
                        result = {'status': 'busy'}
                    else:
                        atomic(path, {'status': 'held', 'hold_id': request['id']})
                        result = {'status': 'held'}
                elif state.get('hold_id') != request['hold_id']:
                    initial = read_json(cfg.root / 'capture-request-results' / (request['hold_id'] + '.json'))
                    if not initial:
                        raise Attention('capture_hold_not_received_yet')
                    result = {'status': 'released' if request['kind'] == 'release'
                              and initial['result']['status'] in ('processed', 'busy') else 'hold_mismatch'}
                elif request['kind'] == 'commit':
                    if state.get('status') == 'superseded' and state.get('merged_id') == request['merged_id']:
                        result = {'status': 'committed'}
                    elif state.get('status') != 'held' or not can_hold(cfg, identity):
                        result = {'status': 'processed'}
                    else:
                        atomic(path, {**state, 'status': 'superseded', 'merged_id': request['merged_id']})
                        result = {'status': 'committed'}
                elif state.get('status') == 'superseded':
                    result = {'status': 'committed'}
                else:
                    atomic(path, {**state, 'status': 'released'})
                    result = {'status': 'released'}
        atomic(receipt, {'request': request, 'result': result})
        return result


def exchange(cfg, client, connection):
    url = connection['url'].rstrip('/') + '/api/capture/worker'
    headers = {'Authorization': 'Bearer ' + connection['publisher_token']}
    response = client.get(url, headers=headers)
    response.raise_for_status()
    for request in sorted(response.json()['requests'], key=lambda r: r['kind'] == 'create_document'):
        try:
            result = execute(cfg, request)
        except Exception:
            # Preserve pending requests and exact request IDs across temporary plugin outages.
            continue
        response = client.post(url + '/' + request['id'], json=result, headers=headers)
        response.raise_for_status()
