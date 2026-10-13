"""Small filename gate and cached semantic self-recording check; no diarization."""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal
from pydantic import BaseModel, ConfigDict
from .core import Attention, atomic, digest, eligibility, recorded_time, read_json, now
from .routing import ask


class SelfRecording(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verdict: Literal['single_self', 'conversation', 'uncertain']
    reason: str


def meeting_contact(cfg, name, provenance):
    names = [name, provenance.get('cloud_file_name', '')]
    matches = [contact for contact in cfg.conversation_profiles
               if contact and any(contact.casefold() in value.casefold() for value in names)]
    if len(matches) > 1:
        raise Attention('conversation_contact_ambiguous')
    return matches[0] if matches else None


def kind(cfg, name, duration, provenance):
    if meeting_contact(cfg, name, provenance):
        if not 0 < duration < float('inf'):
            raise Attention('duration_unknown')
        return 'conversation', 'eligible'
    # Ordinary names never become meeting recordings, regardless of their text.
    if recorded_time(name):
        reason = eligibility(name, duration)
        return ('ordinary' if reason == 'eligible' else 'retained', reason)
    call = provenance.get('source_type') == 'PHONE_CALL' or bool(
        re.search(r'WeChatRecording|微信.*(?:通话|录音)|电话|通话', name, re.I))
    if call:
        if not any(contact and contact.casefold() in name.casefold() for contact in cfg.conversation_contacts):
            return 'retained', 'conversation_contact_unconfirmed'
        if not 0 < duration < float('inf'):
            raise Attention('duration_unknown')
        return 'conversation', 'eligible'
    return 'retained', 'name_not_datetime'


def recording_provenance(name, provenance):
    result = dict(provenance)
    stamp = recorded_time(name)
    if stamp is None:
        match = re.search(r'(?:^|_)(\d{14})(?=\D|$)', name)
        if match:
            try:
                stamp = datetime.strptime(match[1], '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            except ValueError:
                pass
    if stamp:
        result['source_recorded_at'] = result.get('recorded_at')
        result['recorded_at'] = stamp.isoformat()
        result['time_basis'] = 'filename'
    return result


def confirm_self(cfg, manifest, text):
    rules = cfg.recording_types.read_text(encoding='utf-8')
    signature = digest(text + rules)
    path = manifest.folder / 'recording-type.json'
    saved = read_json(path)
    if saved and saved.get('signature') == signature:
        return saved['verdict']
    choice = ask(cfg, SelfRecording,
        '仅判断这份转写是否可以确认是单人自述。不要验证是否属于特定人的声纹，'
        '不要要求对话人标签。向助手口述命令、引用他人原话和自问自答仍可以是单人自述；'
        '明确多人轮流交流为 conversation；确实无法确认则 uncertain。适用标准：\n' + rules,
        {'transcript': text})
    atomic(path, {**choice.model_dump(), 'signature': signature, 'at': now()})
    return choice.verdict


def conversation_part(recording_id, text, outline):
    return {'index': 0, 'key': digest(recording_id + ':meeting')[:24],
            'kind': 'record', 'route': 'remnote', 'recording_type': 'conversation',
            'raw_text': text, 'content_text': text, 'markdown': outline,
            'source_spans': [[0, len(text)]], 'destination': '',
            'depends_on': [], 'cancelled': False}


def cached_outline(cfg, manifest, text, refine):
    from .splitter import PROMPT_HASH, REFINE_REVISION
    signature = digest(text + PROMPT_HASH + REFINE_REVISION + cfg.deepseek_model)
    path = manifest.folder / ('outline-' + signature + '.json')
    saved = read_json(path)
    if saved:
        return saved['markdown']
    value = refine(text)
    atomic(path, {'signature': signature, 'markdown': value})
    return value


def fully_processed(data):
    parts = data.get('segments')
    if parts is None or (not parts and not data.get('workflow_feedback_run')):
        return False
    effects = data.get('effects', {})
    if not all(effects.get(part['key'], {}).get('status') in
               ('verified', 'cancelled', 'completed_unverified') for part in parts):
        return False
    if data.get('workflow_feedback_run'):
        key = data.get('workflow_feedback', {}).get('key')
        if not key or effects.get(key, {}).get('status') not in ('queued', 'delivered', 'cancelled'):
            return False
    return True
