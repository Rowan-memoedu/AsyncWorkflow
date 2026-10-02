from __future__ import annotations
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import alibabacloud_oss_v2 as oss
import httpx

from .auth import access, require
from .core import Attention, UnknownOutcome, Manifest, Settings, atomic, read_json

BASE = "https://dashscope.aliyuncs.com/api/v1"


def cleanup(bucket, cfg, manifest, stage):
    """Delete only the exact owned request object; verify its unique prefix is empty."""
    if not stage.get("object_key") or stage.get("cleaned"):
        return
    key = stage["object_key"]
    if not key.startswith(cfg.oss_prefix.strip("/") + "/"):
        raise Attention("unexpected_staging_prefix")
    try:
        bucket.delete_object(oss.DeleteObjectRequest(bucket=cfg.oss_bucket, key=key))
        result = bucket.list_objects_v2(oss.ListObjectsV2Request(bucket=cfg.oss_bucket, prefix=key.rsplit("/", 1)[0] + "/", max_keys=2))
        if result.contents:
            raise Attention("oss_object_still_present")
    except Attention:
        raise
    except Exception:
        raise Attention("oss_cleanup_unverified") from None
    stage["cleaned"] = True
    manifest.update(asr=stage)


def normalize(value: dict) -> tuple[str, list[dict]]:
    sentences = []
    for transcript in value.get("transcripts", []):
        for sentence in transcript.get("sentences", []):
            if text := sentence.get("text", "").strip():
                sentences.append({"text": text, "start_ms": sentence.get("begin_time"), "end_ms": sentence.get("end_time")})
    if not sentences:
        raise Attention("asr_empty_or_missing_sentences")
    return "\n".join(s["text"] for s in sentences), sentences


def recognize(cfg: Settings, manifest: Manifest, source: Path) -> str:
    result_path = manifest.folder / "asr.json"
    if result_path.exists():
        # A previous successful result still requires staged-object cleanup.
        saved = read_json(result_path)
    else:
        saved = None
    credentials = access(cfg.credential_file)
    settings = oss.config.load_default()
    settings.region = cfg.oss_region
    settings.endpoint = cfg.oss_endpoint
    settings.credentials_provider = oss.credentials.StaticCredentialsProvider(require(credentials, "ALIBABA_CLOUD_ACCESS_KEY_ID"), require(credentials, "ALIBABA_CLOUD_ACCESS_KEY_SECRET"), credentials.get("ALIBABA_CLOUD_SECURITY_TOKEN"))
    bucket = oss.Client(settings)
    stage = manifest.read().get("asr", {})
    if saved and stage.get("cleaned"):
        return saved["text"]
    if stage.get("submission") == "unknown" and not stage.get("task_id"):
        raise UnknownOutcome("asr_submission_unknown")
    if stage.get("submission") == "rejected" or stage.get("provider_status") in ("FAILED", "CANCELED", "UNKNOWN", "SUBTASK_FAILED"):
        cleanup(bucket, cfg, manifest, stage)
        raise Attention("asr_terminal_failure_requires_review")
    key = stage.get("object_key") or f"{cfg.oss_prefix.strip('/')}/{uuid.uuid4().hex}/source{source.suffix.lower()}"
    if not key.startswith(cfg.oss_prefix.strip("/") + "/"):
        raise Attention("unexpected_staging_prefix")
    with httpx.Client(timeout=120, follow_redirects=False) as http:
        if not saved and not stage.get("task_id"):
            stage.update(object_key=key, submission="not_submitted", cleaned=False)
            manifest.update(asr=stage)
            try:
                bucket.put_object_from_file(oss.PutObjectRequest(bucket=cfg.oss_bucket, key=key), str(source))
            except Exception:
                raise Attention("oss_upload_failed") from None
            stage.update(object_key=key, submission="unknown", cleaned=False)
            manifest.update(asr=stage)
            signed = bucket.presign(oss.GetObjectRequest(bucket=cfg.oss_bucket, key=key), expires=timedelta(hours=3))
            headers = {"Authorization": "Bearer " + require(credentials, "DASHSCOPE_API_KEY"), "X-DashScope-Async": "enable"}
            try:
                reply = http.post(BASE + "/services/audio/asr/transcription", headers=headers, json={"model": cfg.asr_model, "input": {"file_urls": [signed.url]}, "parameters": {}})
            except httpx.HTTPError:
                raise UnknownOutcome("asr_submission_unknown") from None
            if reply.status_code >= 400:
                if reply.status_code < 500:
                    stage["submission"] = "rejected"
                    manifest.update(asr=stage)
                    cleanup(bucket, cfg, manifest, stage)
                raise Attention("asr_submit_http_" + str(reply.status_code))
            task_id = reply.json().get("output", {}).get("task_id")
            if not task_id:
                raise UnknownOutcome("asr_submission_without_id")
            stage.update(task_id=task_id, submission="submitted")
            manifest.update(asr=stage)
        if not saved:
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                reply = http.get(BASE + "/tasks/" + stage["task_id"], headers={"Authorization": "Bearer " + require(credentials, "DASHSCOPE_API_KEY")})
                if reply.status_code != 200:
                    raise Attention("asr_poll_http_" + str(reply.status_code))
                output = reply.json().get("output", {})
                status = output.get("task_status")
                if status == "SUCCEEDED":
                    break
                if status not in ("PENDING", "RUNNING"):
                    stage["provider_status"] = status
                    manifest.update(asr=stage)
                    cleanup(bucket, cfg, manifest, stage)
                    raise Attention("asr_provider_terminal_failure")
                time.sleep(5)
            else:
                raise Attention("asr_poll_timeout_existing_task_retained")
            results = output.get("results", [])
            if len(results) != 1 or results[0].get("subtask_status") != "SUCCEEDED":
                stage["provider_status"] = "SUBTASK_FAILED"
                manifest.update(asr=stage)
                cleanup(bucket, cfg, manifest, stage)
                raise Attention("asr_subtask_failed")
            url = results[0].get("transcription_url", "")
            host = urlsplit(url).hostname or ""
            if urlsplit(url).scheme != "https" or not host.endswith(".oss-cn-beijing.aliyuncs.com"):
                raise Attention("unexpected_asr_result_host")
            response = http.get(url)
            if response.status_code != 200:
                raise Attention("asr_result_download_failed")
            text, sentences = normalize(response.json())
            saved = {"text": text, "sentences": sentences, "model": cfg.asr_model, "task_id": stage["task_id"]}
            atomic(result_path, saved)
            atomic(manifest.folder / "transcript.txt", text, text=True)
        cleanup(bucket, cfg, manifest, stage)
        return saved["text"]
