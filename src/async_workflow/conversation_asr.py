"""FUN-ASR HTTP word timestamps for independently recorded call channels.

Reuses FFmpeg and the existing model, credential store and 299-second request
format. Unlike the legacy text-only CLI adapter, keeps provider word timestamps.
"""
from __future__ import annotations
import base64
import fnmatch
import json
import re
import subprocess
from pathlib import Path
import httpx
from .auth import access, require
from .core import Attention, UnknownOutcome, atomic, digest, file_hash, read_json, now

REVISION = 'channel-words-1'
URL = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'


def profile(cfg, name):
    matches = [speakers for pattern, speakers in cfg.conversation_channel_profiles.items()
               if fnmatch.fnmatchcase(name, pattern)]
    if len(matches) != 1:
        raise Attention('conversation_channel_mapping_unconfirmed')
    speakers = matches[0]
    if set(speakers) != {'0', '1'} or len(set(speakers.values())) != 2 or any(not x.strip() for x in speakers.values()):
        raise Attention('conversation_channel_mapping_invalid')
    return speakers


def events(lines):
    data = []
    for line in lines:
        if not line:
            if data:
                body = '\n'.join(data)
                if body != '[DONE]':
                    value = json.loads(body)
                    if value.get('code'):
                        raise Attention('conversation_asr_provider_error')
                    yield value
                data = []
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        raise Attention('conversation_asr_incomplete_sse')


def chunk_receipt(cfg, audio, folder):
    receipt_path, state_path = folder/'receipt.json', folder/'state.json'
    audio_hash = file_hash(audio)
    saved = read_json(receipt_path)
    if saved:
        if saved['audio_sha256'] != audio_hash or not saved['complete']:
            raise Attention('conversation_asr_receipt_mismatch')
        return saved
    state = read_json(state_path, {})
    if state.get('submission') == 'unknown':
        raise UnknownOutcome('conversation_asr_submission_unknown_no_replay')
    key = require(access(cfg.credential_file), 'DASHSCOPE_API_KEY')
    payload = {'model':'fun-asr-realtime', 'parameters':{'format':'mp3'}, 'resources':[],
               'input':{'messages':[{'role':'user','content':[{'audio':
                    'data:audio/mp3;base64,'+base64.b64encode(audio.read_bytes()).decode('ascii')}]}]}}
    atomic(state_path, {'submission':'unknown','audio_sha256':audio_hash,'started_at':now()})
    received = []
    with (folder/'events.jsonl').open('a', encoding='utf-8') as log:
        try:
            with httpx.Client(timeout=180) as client:
                with client.stream('POST', URL, headers={'Authorization':'Bearer '+key,'X-DashScope-SSE':'enable'}, json=payload) as response:
                    if response.is_error:
                        atomic(state_path, {'submission':'rejected','status_code':response.status_code,'at':now()})
                        raise Attention('conversation_asr_http_'+str(response.status_code))
                    for event in events(response.iter_lines()):
                        log.write(json.dumps(event,ensure_ascii=False)+'\n')
                        log.flush()
                        received.append(event)
        except httpx.HTTPError:
            raise UnknownOutcome('conversation_asr_submission_unknown_no_replay') from None
    saved = {'complete':True,'audio_sha256':audio_hash,'events':received,'finished_at':now()}
    atomic(receipt_path, saved)
    atomic(state_path, {'submission':'completed','audio_sha256':audio_hash,'finished_at':now()})
    return saved


def clean_text(text):
    return ''.join(c for c in text if c.isalnum()).casefold()


def words_from_receipt(receipt):
    finals, pending = {}, set()
    for event in receipt['events']:
        sentence = event.get('output', {}).get('sentence', {})
        if not sentence or sentence.get('heartbeat'):
            continue
        identity = sentence.get('sentence_id')
        if sentence.get('sentence_end') is True:
            pending.discard(identity)
            if sentence.get('text','').strip():
                finals[identity] = sentence
        elif sentence.get('text','').strip() and identity not in finals:
            pending.add(identity)
    if pending:
        raise Attention('conversation_asr_unfinished_sentences')
    stable = []
    for sentence in sorted(finals.values(), key=lambda s:(s.get('begin_time',0), len(s['text']))):
        if stable and sentence.get('begin_time') == stable[-1].get('begin_time'):
            if clean_text(sentence['text']).startswith(clean_text(stable[-1]['text'])):
                stable[-1] = sentence
                continue
            if clean_text(stable[-1]['text']).startswith(clean_text(sentence['text'])):
                continue
            raise Attention('conversation_asr_conflicting_snapshots')
        stable.append(sentence)
    words = []
    for sentence in stable:
        items = sentence.get('words', [])
        if clean_text(''.join(w.get('text','') for w in items)) != clean_text(sentence['text']):
            raise Attention('conversation_asr_word_coverage_mismatch')
        for item in items:
            start, end = item.get('begin_time'), item.get('end_time')
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise Attention('conversation_asr_invalid_word_timestamps')
            words.append({'start_ms':start,'end_ms':end,'text':item['text']+item.get('punctuation','')})
    return words


def turns_from_words(words):
    turns = []
    for channel in sorted({w['channel'] for w in words}):
        current = None
        for word in sorted((w for w in words if w['channel']==channel),key=lambda w:w['start_ms']):
            if current and (word['start_ms']-current['end_ms'] > 900 or
                            current['end_ms']-current['start_ms'] > 12000 or
                            re.search(r'[。！？!?]$',current['text'])):
                turns.append(current)
                current = None
            if current is None:
                current = dict(word)
            else:
                current['text'] += word['text']
                current['end_ms'] = max(current['end_ms'],word['end_ms'])
        if current:
            turns.append(current)
    return [{'id':i,**t} for i,t in enumerate(sorted(turns,key=lambda t:(t['start_ms'],t['channel'])))]


def recognize(cfg, store, source):
    speakers = profile(cfg, source.name)
    identity = file_hash(source)
    if identity != store.folder.name:
        raise Attention('conversation_source_changed')
    signature = digest(json.dumps([REVISION,identity,speakers],sort_keys=True))[:24]
    folder = store.folder/'conversation'/signature
    result_path = folder/'transcript.json'
    saved = read_json(result_path)
    if saved:
        return saved
    probe = subprocess.run([str(cfg.ffprobe),'-v','error','-show_streams','-show_format','-of','json',str(source)],
                           capture_output=True,text=True,check=True)
    media = json.loads(probe.stdout)
    streams = [s for s in media['streams'] if s['codec_type']=='audio']
    if len(streams)!=1 or streams[0]['channels']!=2:
        raise Attention('conversation_requires_verified_stereo_source')
    duration = float(media['format']['duration'])
    ffmpeg = cfg.ffprobe.with_name('ffmpeg.exe')
    words, receipts = [], []
    # Chunk caches are independent of nickname/output revisions.
    for channel in (0,1):
        for offset in range(0, int(duration)+1, 299):
            length = min(299., duration-offset)
            if length <= 0:
                continue
            chunk = store.folder/'conversation-channels'/f'{channel}-{offset:06d}'
            chunk.mkdir(parents=True, exist_ok=True)
            audio = chunk/'audio.mp3'
            if not audio.exists():
                prepared = chunk/'preparing.mp3'
                subprocess.run([str(ffmpeg),'-v','error','-y','-ss',str(offset),'-i',str(source),'-t',str(length),
                                '-af',f'pan=mono|c0=c{channel}','-ar','16000','-b:a','64k',str(prepared)],check=True)
                prepared.replace(audio)
            receipt = chunk_receipt(cfg,audio,chunk)
            for word in words_from_receipt(receipt):
                words.append({**word,'start_ms':word['start_ms']+offset*1000,'end_ms':word['end_ms']+offset*1000,
                              'channel':channel,'speaker':speakers[str(channel)]})
            receipts.append(str(chunk/'receipt.json'))
    if not words:
        raise Attention('conversation_empty_transcript')
    if file_hash(source)!=identity:
        raise Attention('conversation_source_changed')
    turns = turns_from_words(words)
    text = '\n'.join(f"[{t['start_ms']/1000:.2f}-{t['end_ms']/1000:.2f}] {t['speaker']}：{t['text']}" for t in turns)
    result = {'revision':REVISION,'source_sha256':identity,'signature':signature,'speakers':speakers,
              'turns':turns,'text':text,'receipts':receipts,'words':words,'created_at':now()}
    atomic(result_path,result)
    atomic(folder/'transcript.txt',text,text=True)
    store.update(conversation_transcript=str(result_path))
    return result
