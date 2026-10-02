"""Windows credential adapter. No secret values are logged or sent to Prefect."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path

from .core import Attention, atomic, read_json


def user_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            pass
    return "" if value == "PENDING_LOCAL_ENTRY" else value


def protect(path: Path, values: dict):
    import win32crypt
    encrypted = win32crypt.CryptProtectData(json.dumps(values).encode(), "AsyncWorkflow", b"async-workflow-v1", None, None, 1)
    atomic(path, {"ciphertext": base64.b64encode(encrypted).decode()})


def access(path: Path) -> dict:
    values = {}
    envelope = read_json(path, {})
    if envelope:
        import win32crypt
        values = json.loads(win32crypt.CryptUnprotectData(base64.b64decode(envelope["ciphertext"]), b"async-workflow-v1", None, None, 1)[1])
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_SECURITY_TOKEN"):
        if value := user_env(name):
            values[name] = value
    return values


def require(values: dict, key: str) -> str:
    if not values.get(key):
        raise Attention("credential_missing:" + key)
    return values[key]
