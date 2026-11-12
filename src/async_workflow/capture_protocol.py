"""Small, bounded recording-app requests. No arbitrary commands, paths, or audio."""
import json
import os
from pathlib import Path
import re
import threading

REQUEST_ID = re.compile(r'[a-f0-9]{32}')
RECORDING_ID = re.compile(r'[a-f0-9]{64}')
ROUTE_KEY = re.compile(r'[dn][a-f0-9]{8}')


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_request(value):
    if not isinstance(value, dict) or not matches(REQUEST_ID, value.get('id')):
        raise ValueError('Invalid request id')
    kind = value.get('kind')
    fields = {'id', 'kind'}
    if kind == 'create_document':
        fields |= {'key', 'parent', 'title'}
        if not matches(ROUTE_KEY, value.get('key')):
            raise ValueError('Invalid route key')
        if value.get('parent') != '' and not matches(ROUTE_KEY, value.get('parent')):
            raise ValueError('Invalid parent')
        title = value.get('title')
        if (not isinstance(title, str) or not title.strip() or len(title) > 120
                or any(ord(c) < 32 for c in title)):
            raise ValueError('Invalid title')
    elif kind in ('hold', 'commit', 'release'):
        fields |= {'recording_id'}
        if not matches(RECORDING_ID, value.get('recording_id')):
            raise ValueError('Invalid recording id')
        if kind != 'hold':
            fields |= {'hold_id'}
            if not matches(REQUEST_ID, value.get('hold_id')):
                raise ValueError('Invalid hold id')
        if kind == 'commit':
            fields |= {'merged_id'}
            if (not matches(RECORDING_ID, value.get('merged_id'))
                    or value['merged_id'] == value['recording_id']):
                raise ValueError('Invalid merged recording id')
    else:
        raise ValueError('Invalid request kind')
    if set(value) != fields:
        raise ValueError('Unexpected request fields')
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class Mailbox:
    def __init__(self, directory):
        self.directory = Path(directory) / 'capture-requests'
        self.lock = threading.RLock()

    def path(self, identity):
        if not isinstance(identity, str) or not REQUEST_ID.fullmatch(identity):
            raise ValueError('Invalid request id')
        return self.directory / (identity + '.json')

    def get(self, identity):
        path = self.path(identity)
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def submit(self, request):
        validate_request(request)
        with self.lock:
            previous = self.get(request['id'])
            if previous:
                if previous['request'] != request:
                    raise ValueError('Request id reused with different content')
                return previous
            if len(list(self.directory.glob('*.json'))) >= 10000:
                raise ValueError('Request capacity reached')
            row = {'request': request, 'result': None}
            save(self.path(request['id']), row)
            return row

    def pending(self):
        with self.lock:
            rows = [json.loads(p.read_text(encoding='utf-8')) for p in self.directory.glob('*.json')]
            return [r['request'] for r in rows if r['result'] is None][:100]

    def complete(self, identity, result):
        if not isinstance(result, dict) or len(json.dumps(result)) > 262144 or not isinstance(result.get('status'), str):
            raise ValueError('Invalid result')
        with self.lock:
            row = self.get(identity)
            if row is None:
                raise ValueError('Unknown request')
            if row['result'] is not None and row['result'] != result:
                raise ValueError('Result already committed')
            row['result'] = result
            save(self.path(identity), row)
            return row
