import copy
import hashlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from async_workflow import mirror, mirror_server


def snapshot():
    return {'schema_version': 1, 'updated_at': datetime.now(timezone.utc).isoformat(),
        'prefect_available': True, 'errors': [], 'recovery': {}, 'interval_seconds': 1800,
        'recordings': [{'id': 'a' * 64, 'name': '独立验证.m4a', 'status': 'verified',
          'status_label': '已正确归档', 'has_transcript': True, 'transcript': '周末整理课程笔记',
          'sources': [], 'threads': [], 'asr': {}, 'runs': [],
          'parts': [{'kind': 'record', 'markdown': '复习线性代数', 'raw_text': '做课程复习',
                     'routing': {'title': '学习日志'}, 'threads': []}]}]}


@pytest.fixture
def client(tmp_path):
    config = {'readers': {'reader': hashlib.sha256(b'read-secret').hexdigest()},
              'publisher_hash': hashlib.sha256(b'write-secret').hexdigest()}
    return TestClient(mirror_server.create_app(tmp_path, config)), tmp_path, config


def upload(client, data=None):
    return client.post('/api/sync', json=data or snapshot(), headers={'Authorization': 'Bearer write-secret'})


def test_all_read_surfaces_require_auth_and_roles_are_separate(client):
    app, _, _ = client
    for path in ['/', '/assets/app.js', '/api/recordings', '/api/recordings/' + 'a'*64]:
        assert app.get(path).status_code == 401
        assert app.get(path, auth=('reader', 'bad')).status_code == 401
        assert app.get(path, headers={'Authorization': 'Bearer write-secret'}).status_code == 401
    assert app.post('/api/sync', json=snapshot(), auth=('reader', 'read-secret')).status_code == 401
    assert upload(app).status_code == 200
    assert app.get('/api/recordings', auth=('reader', 'read-secret')).json()['total'] == 1
    assert app.post('/api/recordings', auth=('reader', 'read-secret')).status_code == 405


def test_search_and_detail_survive_server_restart_without_computer(client):
    app, root, config = client
    assert upload(app).status_code == 200
    restarted = TestClient(mirror_server.create_app(root, config))
    for query in ['课程', '复习', '线性代数', '课程 笔记']:
        assert restarted.get('/api/recordings', params={'q': query}, auth=('reader', 'read-secret')).json()['matched'] == 1
    response = restarted.get('/api/recordings/' + 'a'*64, auth=('reader', 'read-secret'))
    assert response.json()['parts'][0]['routing']['title'] == '学习日志'
    assert response.headers['cache-control'] == 'no-store'


def test_bad_partial_duplicate_and_old_updates_keep_last_valid_snapshot(client, monkeypatch):
    app, root, _ = client
    original = snapshot()
    assert upload(app, original).status_code == 200
    saved = (root/'snapshot.json').read_bytes()
    for mutate in [lambda x: x['recordings'].append(copy.deepcopy(x['recordings'][0])),
                   lambda x: x['recordings'][0].pop('parts'),
                   lambda x: x['recordings'][0].update(sources=['D:/private/source.wav'])]:
        invalid = copy.deepcopy(original)
        mutate(invalid)
        assert upload(app, invalid).status_code == 400
        assert (root/'snapshot.json').read_bytes() == saved
    original['updated_at'] = '2020-01-01T00:00:00+00:00'
    assert upload(app, original).status_code == 409
    monkeypatch.setattr(mirror_server, 'MAX_BYTES', 20)
    assert upload(app).status_code == 413
    assert (root/'snapshot.json').read_bytes() == saved


def test_projection_does_not_ship_manifests_sources_or_credentials():
    data = snapshot()
    data['recordings'][0].update(sources=['D:/private/recording.wav'], config={'token': 'secret'},
                                original={'private': True}, data={'credentials': 'secret'})
    class Inventory:
        def snapshot(self): return object()
        def listing(self, **kwargs): return {**data, 'recordings': data['recordings']}
        def detail(self, identity, **kwargs): return data['recordings'][0]
    result = mirror.project(Inventory())
    assert result['recordings'][0]['sources'] == []
    assert 'secret' not in str(result) and 'D:/private' not in str(result)
    assert mirror.validate(result) is result


def test_incomplete_source_cannot_replace_the_server_inventory():
    class Inventory:
        def snapshot(self): return {}
        def listing(self, **kwargs): return {'errors': [{'reason': 'cannot read one manifest'}]}
    with pytest.raises(ValueError, match='Incomplete local inventory'):
        mirror.project(Inventory())
