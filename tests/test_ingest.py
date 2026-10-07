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
    monkeypatch.setattr(ingest, 'scan_ticks', lambda: iter(()))
    monkeypatch.setattr(ingest, 'audio_files', lambda _: iter([*historical, *new]))
    monkeypatch.setattr(ingest, 'submit_file', submit)
    monkeypatch.setattr(ingest, 'file_hash', hash_observed)
    ingest.watch(cfg)
    assert dispatched == new[:2]
    if source_kind == 'xiaomi':
        assert not any(path in historical for path in hashed)


def test_pre_enable_recordings_do_not_starve_new_audio_or_get_hashed(tmp_path,monkeypatch):
    from async_workflow.core import read_json
    cfg=Settings(root=tmp_path/'state',inbox=tmp_path/'audio',artifacts=tmp_path/'out',
                 ffprobe=Path('ffprobe'),codex=Path('codex'),enabled=True,max_batch=1,
                 enabled_at='2026-10-07T06:00:00+00:00')
    cfg.prepare()
    old=[cfg.inbox/f'old-{i}.wav' for i in range(20)]
    fresh=cfg.inbox/'10-07-2026 14.01.wav'
    monkeypatch.setattr(Settings,'load',lambda:cfg)
    monkeypatch.setattr(ingest,'recover',lambda _:None)
    monkeypatch.setattr(ingest,'scan_ticks',lambda:iter(()))
    monkeypatch.setattr(ingest,'audio_files',lambda _:iter([*old,fresh]))
    monkeypatch.setattr(ingest,'source_metadata',lambda path,cfg:{'recorded_at':
                        '2026-10-07T06:01:00+00:00' if path==fresh else '2026-10-06T00:00:00+00:00'})
    def hashed(path):
        assert path==fresh
        return 'a'*64
    monkeypatch.setattr(ingest,'file_hash',hashed)
    calls=[]
    monkeypatch.setattr(ingest,'submit_file',lambda current,path:calls.append(path) or {'run_id':'native'})
    ingest.watch(cfg)
    assert calls==[fresh]
    status=read_json(cfg.root/'watcher-status.json')
    assert status['enabled'] and status['submitted']==1 and status['attempted']==1


def test_disabled_watcher_writes_health_without_scanning(tmp_path,monkeypatch):
    from async_workflow.core import read_json
    cfg=Settings(root=tmp_path/'state',inbox=tmp_path/'audio',artifacts=tmp_path/'out',
                 ffprobe=Path('ffprobe'),codex=Path('codex'))
    cfg.prepare()
    monkeypatch.setattr(Settings,'load',lambda:cfg)
    monkeypatch.setattr(ingest,'recover',lambda _:None)
    monkeypatch.setattr(ingest,'scan_ticks',lambda:iter(()))
    monkeypatch.setattr(ingest,'audio_files',lambda _:pytest.fail('disabled ingestion'))
    ingest.watch(cfg)
    assert read_json(cfg.root/'watcher-status.json')['enabled'] is False


def test_monitor_scans_only_on_half_hour_ticks(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'), enabled=True)
    cfg.prepare()
    elapsed, scans = [0], []
    class StopMonitor(Exception):
        pass
    def sleep(seconds):
        assert seconds == 1800
        if elapsed[0] == 3600:
            raise StopMonitor
        elapsed[0] += seconds
    monkeypatch.setattr(Settings, 'load', lambda: cfg)
    monkeypatch.setattr(ingest, 'recover', lambda _: None)
    monkeypatch.setattr(ingest, 'audio_files', lambda _: scans.append(elapsed[0]) or iter(()))
    monkeypatch.setattr(ingest.time, 'sleep', sleep)
    with pytest.raises(StopMonitor):
        ingest.watch(cfg)
    assert scans == [0, 1800, 3600]
