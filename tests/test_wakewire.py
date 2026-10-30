from pathlib import Path
from types import SimpleNamespace
from async_workflow.codex_appserver import sdk_config


def test_recording_command_uses_wakewire_and_keeps_native_runtime_and_cwd(monkeypatch):
    monkeypatch.setenv("WAKEWIRE_CLI", "D:/fixture/wakewire/cli.js")
    monkeypatch.setenv("WAKEWIRE_HOME", "D:/fixture/wakewire-state")
    cfg = SimpleNamespace(codex=Path("D:/fixture/codex.exe"))
    result = sdk_config(cfg, Path("D:/fixture/workspace"), via_wakewire=True)
    assert result.codex_bin == str(cfg.codex)
    assert result.cwd == str(Path("D:/fixture/workspace"))
    assert result.launch_args_override[1:5] == (
        "D:/fixture/wakewire/cli.js", "codex-session", "--client", "async_workflow")
    assert result.env == {"WAKEWIRE_HOME": "D:/fixture/wakewire-state"}


def test_other_sdk_consumers_keep_the_existing_native_connection():
    result = sdk_config(SimpleNamespace(codex=Path("codex")), Path("."))
    assert result.launch_args_override is None
    assert result.env is None
