from pathlib import Path

import pytest

from async_workflow import ingest
from async_workflow.core import Settings, atomic, file_hash


@pytest.mark.parametrize('source_kind', ['xiaomi', 'local'])
def test_historical_baseline_does_not_consume_new_recording_batch(tmp_path, monkeypatch, source_kind):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'), enabled=True, max_batch=2)
    cfg.prepare()
    def create(index):
        relative = (Path('xiaomi')/'0'/str(index)/'1700000000000'/'20231115_061320.m4a'
                    if source_kind == 'xiaomi' else Path(f'20261002_{index:06d}.wav'))
        path = cfg.inbox/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f'fixture-{index}'.encode())
        return path
    historical = [create(i) for i in range(12)]
    new = [create(i) for i in range(20, 23)]
    if source_kind == 'xiaomi':
        atomic(cfg.root/'xiaomi-baseline.json', {'asset_ids': [str(i) for i in range(12)]})
    else:
        atomic(cfg.root/'baseline.json', {'hashes': [file_hash(p) for p in historical]})
    dispatched, hashed = [], []
    def submit(current, path):
        if path in historical:
            return {'retained_only': 'historical_baseline'}
        dispatched.append(path)
        return {'run_id': 'submitted'}
    def hash_observed(path):
        hashed.append(path)
        return file_hash(path)
    monkeypatch.setattr(Settings, 'load', lambda: cfg)
    monkeypatch.setattr(ingest, 'recover', lambda _: None)
    monkeypatch.setattr(ingest, 'watchfiles', lambda *a, **kw: iter(()))
    monkeypatch.setattr(ingest, 'audio_files', lambda _: iter([*historical, *new]))
    monkeypatch.setattr(ingest, 'submit_file', submit)
    monkeypatch.setattr(ingest, 'file_hash', hash_observed)
    ingest.watch(cfg)
    assert dispatched == new[:2]
    if source_kind == 'xiaomi':
        assert not any(path in historical for path in hashed)
