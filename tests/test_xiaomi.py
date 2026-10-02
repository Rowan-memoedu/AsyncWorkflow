from contextlib import contextmanager
from pathlib import Path

import pytest

from async_workflow import xiaomi
from async_workflow.core import Settings, Attention, atomic, read_json


def setup(tmp_path, monkeypatch):
    cfg = Settings(root=tmp_path, inbox=tmp_path / "audio", ffprobe=Path("ffprobe"), codex=Path("codex"))
    accounts = [{"id": 10}, {"id": 20}]
    jobs, calls = [], []

    @contextmanager
    def connection(_cfg):
        yield object()

    def request(_client, method, path, **kwargs):
        calls.append((method, path))
        if path == "/api/account":
            return accounts
        if path == "/api/album/latest/20":
            return [{"id": 30, "remoteId": "-1", "shadow": False}]
        if path == "/api/asset/30/latest":
            return [{"id": 40, "type": "AUDIO"}]
        if path == "/api/crontab":
            if method == "POST":
                job = {"id": 50, **kwargs["json"]}
                job["config"] = {k:v for k,v in job["config"].items() if v is not None}
                jobs.append(job)
                return job
            return jobs
        raise AssertionError((method, path))

    monkeypatch.setattr(xiaomi, "connection", connection)
    monkeypatch.setattr(xiaomi, "request", request)
    return cfg, jobs, calls


def test_explicit_verified_account_survives_stale_account_and_repeat(tmp_path, monkeypatch):
    cfg, jobs, calls = setup(tmp_path, monkeypatch)
    assert xiaomi.configure(cfg, account_id=20)["configured"]
    assert xiaomi.configure(cfg)["configured"]
    assert len(jobs) == 1 and jobs[0]["accountId"] == 20
    assert read_json(cfg.root / "xiaomi-baseline.json")["asset_ids"] == ["40"]
    assert calls.count(("GET", "/api/asset/30/latest")) == 1
    assert not cfg.enabled


def test_multiple_accounts_are_never_selected_by_list_order(tmp_path, monkeypatch):
    cfg, jobs, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(Attention, match="account_missing_or_ambiguous"):
        xiaomi.configure(cfg)
    assert jobs == [] and calls == [("GET", "/api/account")]


@pytest.mark.parametrize("file,error", [
    ("xiaomi-binding.json", "bound_account_changed"),
    ("xiaomi-baseline.json", "baseline_account_changed"),
])
def test_existing_account_ownership_cannot_be_reassigned(tmp_path, monkeypatch, file, error):
    cfg, jobs, _ = setup(tmp_path, monkeypatch)
    atomic(cfg.root / file, {"account_id": 10, "asset_ids": ["41"]})
    with pytest.raises(Attention, match=error):
        xiaomi.configure(cfg, account_id=20)
    assert jobs == [] and read_json(cfg.root / file)["account_id"] == 10


def test_named_job_with_changed_account_is_not_silently_reused(tmp_path, monkeypatch):
    cfg, jobs, _ = setup(tmp_path, monkeypatch)
    xiaomi.configure(cfg, account_id=20)
    jobs[0]["accountId"] = 10
    with pytest.raises(Attention, match="existing_job_differs"):
        xiaomi.configure(cfg)
    assert len(jobs) == 1 and jobs[0]["accountId"] == 10
