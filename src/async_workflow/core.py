from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

PROMPT_HASH = "c996ee91a7e23aa3b98a1685cf90df7ff51e5655943416da434cea3fd26c9dc8"
EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".amr"}


class Attention(RuntimeError):
    """A concrete unresolved fact; safe to display without raw exception details."""


class UnknownOutcome(Attention):
    """An external call may have happened; retry must reconcile first."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve_codex_path(configured: Path) -> Path:
    """Follow a replaced Desktop runtime only when one installed binary is unambiguous."""
    if configured.is_file():
        return configured
    managed = Path.home() / 'AppData/Local/OpenAI/Codex/bin'
    if configured.parent.parent != managed:
        return configured
    candidates = [p for p in managed.glob('*/codex.exe') if p.is_file()]
    return candidates[0] if len(candidates) == 1 else configured


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def atomic(path: Path, value, *, text=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = value if text else json.dumps(value, ensure_ascii=False, indent=2)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path = Path("D:/AppData/AsyncWorkflow")
    artifacts: Path = Path("D:/Artifacts/AsyncWorkflow/recordings")
    inbox: Path = Path("D:/AppData/AsyncWorkflow/audio")
    prompt: Path = Path("D:/AppData/AsyncWorkflow/prompts/refine.md")
    ffprobe: Path
    codex: Path
    remnote_url: str = "http://127.0.0.1:43141"
    template_id: str = ""
    todo_document_id: str = ""
    trash_rem_id: str = ""
    new_document_parent_id: str = ""
    prefect_url: str = "http://127.0.0.1:4200/api"
    deepseek_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    asr_provider: Literal["funasr", "bailian"] = "funasr"
    asr_model: str = "fun-asr-realtime"
    transcription_cli: Path = Path.home() / ".codex/skills/global-transcription/scripts/transcribe.py"
    oss_bucket: str = ""
    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_region: str = "cn-beijing"
    oss_prefix: str = "codex/global-transcription/tingwu/async-workflow"
    credential_file: Path = Path("D:/AppData/AsyncWorkflow/access.dpapi.json")
    default_rem_id: str = ""
    project_aliases: dict[str, str] = Field(default_factory=dict)
    command_workspace: Path = Path('D:/Workspaces/新建文件夹')
    command_project_id: str = ''
    feedback_thread_id: str = ''
    meeting_document_id: str = ''
    conversation_contacts: list[str] = Field(default_factory=list)
    conversation_channel_profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    conversation_profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    recording_types: Path = Path('D:/Projects/AsyncWorkflow/docs/recording-types.md')
    script_actions: dict[str, dict] = Field(default_factory=dict)
    enabled: bool = False
    enabled_at: str | None = None
    xiaomi_url: str = "http://127.0.0.1:43140"
    stability_seconds: int = 10
    max_batch: int = 10

    @classmethod
    def load(cls):
        return cls.model_validate(read_json(Path(os.environ.get("ASYNC_WORKFLOW_CONFIG", "D:/AppData/AsyncWorkflow/config.json")), {}))

    def save(self):
        atomic(Path(os.environ.get("ASYNC_WORKFLOW_CONFIG", "D:/AppData/AsyncWorkflow/config.json")), self.model_dump(mode="json"))

    def prepare(self):
        for path in (self.root, self.inbox, self.artifacts, self.root / "locks", self.root / "logs"):
            path.mkdir(parents=True, exist_ok=True)


def safe_source(path: Path, root: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root))
    if not absolute.is_relative_to(base):
        raise Attention("source_outside_inbox")
    for item in (absolute, *absolute.parents):
        if item.exists() and (item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise Attention("reparse_source_rejected")
        if item == base:
            break
    if not absolute.is_file():
        raise Attention("source_not_regular_file")
    return absolute


def recorded_time(name: str) -> datetime | None:
    stem = Path(name).stem
    formats = ("%Y%m%d%H%M%S", "%Y%m%d_%H%M%S", "%Y%m%d %H%M%S", "%Y-%m-%d %H-%M-%S", "%Y-%m-%d_%H-%M-%S", "%Y-%m-%d %H.%M.%S", "%Y%m%d%H%M", "%Y%m%d_%H%M", "%Y-%m-%d %H-%M", "%Y年%m月%d日 %H时%M分%S秒", "%m-%d-%Y %H.%M", "%m-%d-%Y %H.%M.%S")
    for fmt in formats:
        try:
            value = datetime.strptime(stem, fmt)
            # Parsing is strict and the whole basename is consumed.
            from zoneinfo import ZoneInfo
            return value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        except ValueError:
            continue
    return None


def eligibility(name: str, duration: float, source_type: str | None = None) -> str:
    if source_type and source_type != "RECORDER":
        return "source_not_recorder"
    if recorded_time(name) is None:
        return "name_not_datetime"
    if not 0 < duration < float("inf"):
        raise Attention("duration_unknown")
    return "eligible" if duration < 1200 else "over_duration"


def source_metadata(path: Path, cfg: Settings) -> dict:
    parts = path.relative_to(cfg.inbox).parts
    if parts[0] != "xiaomi":
        return {"source": "local", "recorded_at": recorded_time(path.name).isoformat() if recorded_time(path.name) else None}
    # Native downloader targetPath: xiaomi/type/assetId/takenEpochMillis/fileName.
    if len(parts) != 5 or not parts[2].isdigit() or not parts[3].isdigit():
        raise Attention("xiaomi_source_metadata_missing")
    kind = {"0":"RECORDER", "1":"PHONE_CALL", "2":"FM", "3":"APP"}.get(parts[1], "UNKNOWN")
    taken = datetime.fromtimestamp(int(parts[3]) / 1000, timezone.utc)
    if int(parts[3]) <= 0 or taken > datetime.now(timezone.utc):
        raise Attention("xiaomi_source_time_invalid")
    names = read_json(cfg.root / 'xiaomi-recording-names.json', {}).get('names', {})
    return {"source":"xiaomi", "source_type":kind, "asset_id":parts[2], "recorded_at":taken.isoformat(),
            "cloud_file_name": names.get(parts[2], path.name)}


def probe(path: Path, executable: Path) -> float:
    result = subprocess.run([str(executable), "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)], capture_output=True, timeout=45)
    if result.returncode:
        raise Attention("media_probe_failed")
    obj = json.loads(result.stdout)
    if not any(s.get("codec_type") == "audio" for s in obj.get("streams", [])):
        raise Attention("no_audio_stream")
    try:
        return float(obj["format"]["duration"])
    except (ValueError, KeyError):
        raise Attention("duration_unknown") from None


class Manifest:
    def __init__(self, settings: Settings, recording_id: str):
        if not re.fullmatch(r"[a-f0-9]{64}", recording_id):
            raise ValueError("invalid recording id")
        self.folder = settings.artifacts / recording_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "manifest.json"
        self.lock = FileLock(str(settings.root / "locks" / (recording_id + ".lock")))

    def read(self):
        return read_json(self.path, {"schema_version": 1, "effects": {}, "sources": []})

    def update(self, **fields):
        with self.lock:
            data = self.read()
            data.update(fields)
            atomic(self.path, data)
        return data

    def effect(self, key: str):
        return self.read()["effects"].get(key)

    def save_effect(self, key: str, result: dict):
        with self.lock:
            data = self.read()
            data["effects"][key] = {**result, "updated_at": now()}
            atomic(self.path, data)
