from __future__ import annotations
import time
import uuid
import os
import json
import re
import subprocess
import sys
import psutil
from filelock import FileLock
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import alibabacloud_oss_v2 as oss
import httpx

from .auth import access, require
from .core import Attention, UnknownOutcome, Manifest, Settings, atomic, read_json, file_hash, now

BASE = "https://dashscope.aliyuncs.com/api/v1"


def retryable_cli_failure(manifest: Manifest) -> str | None:
    """FUN-ASR is read-only: every terminated attempt may be retried, without a cap."""
    stage = manifest.read().get('asr', {})
    if stage.get('exit_code') is None or not stage.get('finished_at'):
        return None
    if (manifest.folder/'asr.json').exists() or (manifest.folder/'funasr/raw.json').exists():
        return None
    path = Path(stage.get('stdout_log') or manifest.folder/'asr-cli.out.log')
    fallback = 'funasr_cli_terminated:' + str(stage['exit_code'])
    if not path.is_file():
        return fallback
    with path.open('rb') as stream:
        stream.seek(max(stage.get('stdout_offset', 0), max(0, path.stat().st_size-65536)))
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    try:
        receipt = json.loads(next(line for line in reversed(lines) if line.strip()))
    except (ValueError, StopIteration):
        return fallback
    if not isinstance(receipt, dict):
        return fallback
    error = receipt.get('error', '') if receipt.get('ok') is False else ''
    return error if isinstance(error, str) and error else fallback


def reconcile_cli(cfg, manifest, source, stage):
    """Do not run two transcribers. Expire only this recording's proven local child."""
    if not stage.get('pid'):
        raise UnknownOutcome('funasr_submission_unknown_no_process_evidence')
    try:
        process = psutil.Process(stage['pid'])
        command = process.cmdline()
        owned = str(cfg.transcription_cli) in command and str(source) in command
        if stage.get('process_created'):
            owned = owned and process.create_time() == stage['process_created']
        if owned:
            if time.time() - process.create_time() < 3600:
                raise UnknownOutcome('funasr_existing_process_still_running')
            children = process.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            process.terminate()
            _, alive = psutil.wait_procs([*children, process], timeout=5)
            for child in alive:
                child.kill()
            _, alive = psutil.wait_procs(alive, timeout=5)
            if alive:
                raise UnknownOutcome('funasr_previous_process_not_stopped')
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, OSError):
        raise UnknownOutcome('funasr_process_state_unavailable') from None
    stage.update(submission='failed', exit_code=-1, finished_at=now(), failure_code='local_process_ended_without_result')
    manifest.update(asr=stage)


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
                sentences.append({"text": text, "start_ms": sentence.get("begin_time"), "end_ms": sentence.get("end_time"),
                                  **({'speaker_id': sentence['speaker_id']} if 'speaker_id' in sentence else {})})
    if not sentences:
        raise Attention("asr_empty_or_missing_sentences")
    return "\n".join(s["text"] for s in sentences), sentences


def normalize_funasr(value: dict, recording_id: str) -> dict:
    if value.get('route') != 'funasr' or value.get('source_sha256') != recording_id:
        raise Attention('funasr_source_or_provider_mismatch')
    model = value.get('status', {}).get('model')
    if model not in ('fun-asr-realtime', 'fun-asr'):
        raise Attention('funasr_model_mismatch')
    text, sentences = value.get('text', ''), value.get('utterances', [])
    if not isinstance(text, str) or not text.strip() or not sentences:
        raise Attention('funasr_empty_result')
    for sentence in sentences:
        start, end = sentence.get('start_ms'), sentence.get('end_ms')
        if type(start) is not int or type(end) is not int or not 0 <= start < end or not sentence.get('text', '').strip():
            raise Attention('funasr_invalid_final_sentence')
    if ''.join(text.split()) != ''.join(''.join(s['text'] for s in sentences).split()):
        raise Attention('funasr_text_sentence_mismatch')
    # Some HTTP streams emit cumulative finals with fresh IDs but the same
    # start time. A later containing span supersedes its exact text prefix.
    # Never deduplicate equal words spoken at different positions in the audio.
    stable = []
    for sentence in sorted(sentences, key=lambda s:(s['start_ms'],s['end_ms'])):
        if stable and sentence['start_ms'] == stable[-1]['start_ms']:
            previous = stable[-1]
            if sentence['text'].startswith(previous['text']):
                stable[-1] = sentence
                continue
            if previous['text'].startswith(sentence['text']):
                # Cumulative SSE snapshots can arrive with a shorter prefix
                # carrying a later end time. Keep the containing source text.
                continue
            raise Attention('funasr_overlapping_revisions_require_review')
        if stable and sentence['start_ms'] < stable[-1]['end_ms']:
            raise Attention('funasr_overlapping_revisions_require_review')
        stable.append(sentence)
    return {'text':'\n'.join(s['text'] for s in stable), 'sentences':stable,
            'model':model, 'provider':'funasr', 'normalization_revision':'timed-prefix-v2',
            'collapsed_snapshots':len(sentences)-len(stable),
            'source_sha256':recording_id, 'provider_status':value['status'],
            'preference_profile':value.get('preference_profile')}


def restore_funasr_chunks(manifest: Manifest) -> bool:
    """Rebuild a missing final receipt from complete, source-bound HTTP chunks."""
    duration = manifest.read().get('duration')
    if not duration:
        return False
    folder = manifest.folder/'funasr-chunks'
    if not folder.exists():
        folder = manifest.folder/'funasr/chunks'
    files = sorted(p for p in folder.glob('*.json')
                   if re.fullmatch(r'\d{4}\.json',p.name))
    utterances, empty, offset = [], [], 0
    for index,path in enumerate(files):
        d = read_json(path)
        if (path.stem != f'{index:04d}' or d.get('source_sha256') != manifest.folder.name
                or d.get('offset_ms') != offset or not isinstance(d.get('length_ms'),int)
                or d['length_ms'] <= 0):
            return False
        events = d.get('events', [])
        no_words = bool(events and events[-1].get('code') == 'CLIENT_ERROR'
            and events[-1].get('message') == 'ASR_RESPONSE_HAVE_NO_WORDS'
            and not any(e.get('output', {}).get('sentence', {}).get('text', '').strip() for e in events))
        if no_words:
            empty.append(offset)
        elif d.get('complete'):
            finals, pending = {}, set()
            for event in events:
                if event.get('code'):
                    return False
                sentence = event.get('output', {}).get('sentence', {})
                if not sentence or sentence.get('heartbeat'):
                    continue
                identity=(sentence.get('channel_id',0),sentence.get('sentence_id'))
                if identity[1] is None:
                    return False
                if sentence.get('sentence_end') is True:
                    pending.discard(identity)
                    if sentence.get('text','').strip():
                        start,end=sentence.get('begin_time'),sentence.get('end_time')
                        if type(start) is not int or type(end) is not int or not 0 <= start < end:
                            return False
                        finals[identity]={'text':sentence['text'].strip(),'start_ms':start+offset,'end_ms':end+offset}
                elif sentence.get('text','').strip() and identity not in finals:
                    pending.add(identity)
            if pending or not finals:
                return False
            utterances.extend(sorted(finals.values(),key=lambda s:(s['start_ms'],s['end_ms'])))
        else:
            return False
        offset += d['length_ms']
    if not utterances or offset != round(duration*1000):
        return False
    raw={'route':'funasr','source_sha256':manifest.folder.name,
         'text':'\n'.join(s['text'] for s in utterances),'utterances':utterances,
         'status':{'model':'fun-asr-realtime','transport':'http-sse','chunks':len(files),
                   'restored_from_chunks':True,'no_words_chunk_offsets_ms':empty}}
    normalize_funasr(raw,manifest.folder.name)
    atomic(manifest.folder/'funasr/raw.json',raw)
    return True


def recognize(cfg: Settings, manifest: Manifest, source: Path) -> str:
    with FileLock(str(cfg.root/'locks'/('asr-'+manifest.folder.name+'.lock')), timeout=3):
        return _recognize(cfg, manifest, source)


def _recognize(cfg: Settings, manifest: Manifest, source: Path) -> str:
    saved = read_json(manifest.folder / 'asr.json')
    stage = manifest.read().get('asr', {})
    # A provider change never discards an accepted transcript or a pending OSS task.
    if stage.get('object_key') and not stage.get('cleaned'):
        return recognize_filetrans(cfg, manifest, source)
    if saved:
        return saved['text']
    if cfg.asr_provider == 'bailian':
        return recognize_filetrans(cfg, manifest, source)
    if file_hash(source) != manifest.folder.name:
        raise Attention('funasr_source_changed')
    output = Path(stage.get('output_dir') or manifest.folder / 'funasr')
    if not output.resolve().is_relative_to(manifest.folder.resolve()):
        raise Attention('funasr_receipt_outside_recording')
    raw_path = output / 'raw.json'
    invalid = stage.get('invalid_raw_receipts', [])
    if not raw_path.exists() and restore_funasr_chunks(manifest):
        raw_path = manifest.folder / 'funasr/raw.json'
    if not raw_path.exists() or str(raw_path) in invalid:
        failure = retryable_cli_failure(manifest)
        if stage.get('submission') == 'unknown' and not failure:
            reconcile_cli(cfg, manifest, source, stage)
        previous_attempt = stage.get('attempt', 1 if stage.get('started_at') else 0)
        if not cfg.transcription_cli.is_file():
            raise Attention('global_transcription_cli_missing')
        history = stage.get('attempts', [])
        if stage.get('started_at'):
            history = [*history, {k:v for k,v in stage.items() if k != 'attempts'}]
        if output.exists():
            output = manifest.folder / 'funasr-attempts' / f'{previous_attempt+1:06d}'
        raw_path = output / 'raw.json'
        stage={'provider':'funasr','model':'fun-asr-realtime',
               'submission':'unknown','output_dir':str(output),'cleaned':True,
               'attempt':previous_attempt+1, 'attempts':history,
               'started_at':now(), 'stdout_log':str(manifest.folder/'asr-cli.out.log'),
               'stderr_log':str(manifest.folder/'asr-cli.err.log')}
        stage['invalid_raw_receipts'] = invalid
        stdout_path = Path(stage['stdout_log'])
        stage['stdout_offset'] = stdout_path.stat().st_size if stdout_path.exists() else 0
        manifest.update(asr=stage)
        temp = cfg.root / 'temp'
        temp.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, 'TEMP':str(temp), 'TMP':str(temp), 'PYTHONIOENCODING':'utf-8',
               'FUNASR_RECEIPT_DIR':str(manifest.folder/'funasr-chunks')}
        with open(stage['stdout_log'],'ab') as stdout, open(stage['stderr_log'],'ab') as stderr:
            try:
                process = subprocess.Popen([sys.executable, str(cfg.transcription_cli), 'transcribe', str(source),
                                            '--provider', 'funasr', '--output-dir', str(output)] +
                                           (['--preference-profile', str(cfg.asr_preference_profile)] if cfg.asr_preference_profile else []),
                                           stdout=stdout, stderr=stderr, env=env)
            except OSError as exc:
                stage.update(submission='not_started',startup_error_type=type(exc).__name__)
                manifest.update(asr=stage)
                raise Attention('funasr_local_process_not_started') from None
            stage['pid']=process.pid
            try:
                stage['process_created'] = psutil.Process(process.pid).create_time()
            except psutil.Error:
                pass
            manifest.update(asr=stage)
            try:
                code=process.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                reconcile_cli(cfg, manifest, source, stage)
                raise Attention('funasr_timeout_recovered_retry_next_cycle') from None
            stage.update(exit_code=code,finished_at=now())
            manifest.update(asr=stage)
            failure = retryable_cli_failure(manifest)
            if failure:
                stage.update(submission='failed', failure_code=failure)
                manifest.update(asr=stage)
        if code and not raw_path.exists():
            raise Attention('funasr_cli_failed_receipt_retained')
    try:
        result = normalize_funasr(read_json(raw_path), manifest.folder.name)
    except (Attention, ValueError, AttributeError):
        stage.update(submission='failed', invalid_raw_receipts=[*invalid, str(raw_path)])
        manifest.update(asr=stage)
        raise
    if file_hash(source) != manifest.folder.name:
        raise Attention('funasr_source_changed')
    atomic(manifest.folder / 'asr.json', result)
    atomic(manifest.folder / 'transcript.txt', result['text'], text=True)
    manifest.update(asr={**stage,'provider':'funasr','model':result['model'],'submission':'completed',
                         'output_dir':str(output),'raw_receipt':str(raw_path),'cleaned':True})
    return result['text']


def recognize_filetrans(cfg: Settings, manifest: Manifest, source: Path, *, model=None, parameters=None) -> str:
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
    model = stage.get('model') or model or (cfg.asr_model if cfg.asr_provider == 'bailian' else 'qwen-audio-3.1-asr-flash-filetrans')
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
            from .hotwords import parameters as shared_parameters
            hotword_parameters, hotword_receipt = shared_parameters(cfg, model)
            request_parameters = {**hotword_parameters, **(parameters or {})}
            if hotword_receipt:
                stage['hotwords'] = hotword_receipt
            stage.update(object_key=key, submission="not_submitted", cleaned=False, model=model)
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
                reply = http.post(BASE + "/services/audio/asr/transcription", headers=headers, json={"model": model, "input": {"file_urls": [signed.url]}, "parameters": request_parameters})
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
            saved = {"text": text, "sentences": sentences, "model": stage.get('model', 'qwen-audio-3.1-asr-flash-filetrans'), "task_id": stage["task_id"]}
            atomic(result_path, saved)
            atomic(manifest.folder / "transcript.txt", text, text=True)
        cleanup(bucket, cfg, manifest, stage)
        return saved["text"]
