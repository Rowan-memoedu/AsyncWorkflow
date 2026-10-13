"""Mixed-room audio uses native FUN-ASR diarization, never channel identity."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from . import asr, routing
from .core import Attention, Manifest, atomic, digest, file_hash, read_json

REVISION = 'fun-asr-room-1'


class SpeakerName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speaker_id: str
    name: str
    evidence: str


class Names(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speakers: list[SpeakerName]


def resolve_names(cfg, store, turns, profile):
    """Resolve anonymous acoustic IDs from dialogue evidence, not arrival order."""
    path = store.folder / 'speaker-names.json'
    saved = read_json(path)
    if saved:
        return saved['mapping']
    ids = {t['speaker_id'] for t in turns}
    candidates = [profile['self_name'], profile['other_name']]
    text = '\n'.join(t['text'] for t in turns)
    def validate(value):
        if len(value.speakers) != len(ids) or {s.speaker_id for s in value.speakers} != ids:
            raise ValueError('必须完整列出输入 speaker_id，每个一次')
        for speaker in value.speakers:
            if speaker.name not in [*candidates, '说话人'+speaker.speaker_id]:
                raise ValueError('名称只能是给定姓名，确实不明时保留说话人编号')
            if speaker.name in candidates and (not speaker.evidence or speaker.evidence not in text):
                raise ValueError('姓名对应必须附一段输入原文中的连续证据')
        return value
    result = routing.ask(cfg, Names,
        '输入是现场面对面录音，FUN-ASR 已按真实声音给出 speaker_id。这不是声道编号。'
        '仅根据完整对话中的称呼、自我介绍、问答指代和人物经历语境，将声学编号对应到给定姓名。'
        '被称呼者通常是另一位，不能把称呼误认成自称。禁止按编号、发言顺序、左右声道猜名字。'
        '不同编号可能是同一个人的误分段；背景插话和确实无法确定的编号保留为“说话人”加编号。'
        'evidence 使用能支持归属的原文连续片段；不改写转写，不审核会议内容。',
        {'participants': candidates, 'turns': turns}, validate)
    mapping = {s.speaker_id: s.name for s in result.speakers}
    atomic(path, {'mapping': mapping, 'evidence': result.model_dump()})
    return mapping


def prepare(cfg, members, profile):
    """One continuous mono input gives consistent acoustic IDs across file breaks."""
    identity = digest(json.dumps([REVISION, [m['id'] for m in members], profile['self_name'],
                                  profile['other_name']], ensure_ascii=False))
    store = Manifest(cfg, identity)
    path = store.folder / 'room-transcript.json'
    saved = read_json(path)
    if saved:
        return saved
    store.update(recording_type='meeting_asr_batch', sources=[m['path'] for m in members])
    ffmpeg = cfg.ffprobe.with_name('ffmpeg.exe')
    files, boundaries, offset = [], [], 0
    for member in members:
        source = Path(member['path'])
        if file_hash(source) != member['id']:
            raise Attention('meeting_source_changed')
        audio = store.folder / (member['id']+'.wav')
        if not audio.exists():
            temporary = audio.with_suffix('.preparing.wav')
            subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-i', str(source),
                            '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(temporary)], check=True)
            temporary.replace(audio)
        # Duration after resampling is exact; original containers may include padding.
        import wave
        with wave.open(str(audio), 'rb') as stream:
            length = round(stream.getnframes()*1000/stream.getframerate())
        boundaries.append({'id': member['id'], 'start_ms': offset, 'end_ms': offset+length})
        offset += length
        files.append(audio)
    combined = store.folder / 'combined.wav'
    if not combined.exists():
        # Inputs are owned hash-named files; no user-controlled concat directives.
        listing = store.folder / 'concat.txt'
        atomic(listing, '\n'.join("file '"+p.name+"'" for p in files), text=True)
        temporary = combined.with_suffix('.preparing.wav')
        subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '1',
                        '-i', str(listing), '-c', 'copy', str(temporary)], check=True)
        temporary.replace(combined)
    asr.recognize_filetrans(cfg, store, combined, model='fun-asr',
                           parameters={'diarization_enabled': True, 'speaker_count': 2})
    result = read_json(store.folder/'asr.json')
    turns = []
    for sentence in result['sentences']:
        if 'speaker_id' not in sentence or sentence['start_ms'] is None or sentence['end_ms'] is None:
            raise Attention('fun_asr_diarization_missing')
        turns.append({**sentence, 'speaker_id': str(sentence['speaker_id']), 'id': len(turns)})
    mapping = resolve_names(cfg, store, turns, profile)
    for turn in turns:
        turn['speaker'] = mapping[turn['speaker_id']]
    saved = {'turns': turns, 'speakers': mapping, 'boundaries': boundaries,
             'batch_id': identity, 'source_ids': [m['id'] for m in members],
             'text': '\n'.join(t['speaker']+'：'+t['text'] for t in turns),
             'identity_basis': 'native-acoustic-diarization-and-dialogue-context'}
    atomic(path, saved)
    atomic(store.folder/'named-transcript.txt', saved['text'], text=True)
    return saved
