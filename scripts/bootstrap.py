"""Migrate only local connection settings; never print credentials."""
from pathlib import Path
import base64
import json
import re
import shutil
import win32crypt

from async_workflow.auth import user_env, protect
from async_workflow.core import Settings, PROMPT_HASH, file_hash

root = Path("D:/AppData/AsyncWorkflow")
if (root / "config.json").exists():
    raise SystemExit("Already initialized; inspect config before changing it")
cfg = Settings(ffprobe=Path("D:/Applications/P47/FFmpeg/ffmpeg-8.1.2-full_build/bin/ffprobe.exe"), codex=Path("C:/Users/fy/AppData/Local/OpenAI/Codex/bin/be3fd7e5c1969ff6/codex.exe"))
cfg.prepare()
legacy = Path.home() / ".codex/global-transcription"
legacy_config = json.loads((legacy / "config.json").read_text(encoding="utf-8-sig"))
cfg.oss_bucket = legacy_config.get("tingwu_oss_bucket", "")
cfg.oss_endpoint = legacy_config.get("tingwu_oss_endpoint", cfg.oss_endpoint)
cfg.oss_prefix = legacy_config.get("tingwu_oss_prefix", "codex/global-transcription/tingwu").strip("/") + "/async-workflow"
values = {}
envelope = json.loads((legacy / "credentials.dpapi.json").read_text(encoding="utf-8-sig"))
decoded = json.loads(win32crypt.CryptUnprotectData(base64.b64decode(envelope["ciphertext"]), b"openai-codex-global-transcription-v1", None, None, 1)[1])
for name in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_SECURITY_TOKEN", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
    if value := user_env(name) or decoded.get(name):
        values[name] = value
if not values.get("DEEPSEEK_API_KEY"):
    source = Path.home() / "Documents/New project/DeepSeek API 配置.md"
    if source.exists():
        match = re.search(r"sk-[A-Za-z0-9_-]{20,}", source.read_text(encoding="utf-8-sig"))
        if match:
            values["DEEPSEEK_API_KEY"] = match.group()
protect(cfg.credential_file, values)
cfg.prompt.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile("D:/Projects/AudioWorkflow/prompts/refine.md", cfg.prompt)
assert file_hash(cfg.prompt) == PROMPT_HASH
cfg.save()
print(json.dumps({"initialized": True, "credentials_available": {k: bool(values.get(k)) for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")}, "oss_configured": bool(cfg.oss_bucket), "prompt_hash_match": True, "enabled": cfg.enabled}))
