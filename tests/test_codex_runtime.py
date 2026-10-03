from pathlib import Path
from async_workflow.core import resolve_codex_path


def install(root, name):
    binary=root/name/'codex.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fixture')
    return binary


def test_desktop_update_resolves_only_unique_replacement(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    managed=tmp_path/'AppData/Local/OpenAI/Codex/bin'
    old=managed/'removed'/'codex.exe'
    current=install(managed,'current')
    assert resolve_codex_path(old)==current
    install(managed,'another')
    assert resolve_codex_path(old)==old


def test_existing_and_custom_runtime_paths_are_preserved(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    managed=tmp_path/'AppData/Local/OpenAI/Codex/bin'
    existing=install(managed,'existing')
    install(managed,'newer')
    assert resolve_codex_path(existing)==existing
    custom=tmp_path/'custom/codex.exe'
    assert resolve_codex_path(custom)==custom
