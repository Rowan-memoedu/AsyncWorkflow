import json
from pathlib import Path
import pytest
from async_workflow.core import Attention, Settings, atomic, file_hash
from async_workflow import hotwords, asr, conversation_asr


def test_filetrans_uses_the_same_verified_release_and_detects_stale_metadata(tmp_path):
    path = tmp_path/'current.json'
    atomic(path, {'schema_version':2,'hotwords':[{'word':'ExampleProduct','weight':4}]})
    cfg = Settings(ffprobe=Path('ffprobe'), codex=Path('codex'), asr_preference_profile=path)
    atomic(tmp_path/'current-release.json', {'profile_sha256':file_hash(path),
        'tables':[{'model':'fun-asr','verified':True,'count':1,'vocabulary_id':'vocab-fixture'}]})
    params, receipt = hotwords.parameters(cfg, 'fun-asr')
    assert params == {'vocabulary_id':'vocab-fixture'}
    assert receipt['profile_sha256'] == file_hash(path)
    atomic(path, {'schema_version':2,'hotwords':[{'word':'DifferentProduct','weight':4}]})
    with pytest.raises(Attention, match='release_not_verified'):
        hotwords.parameters(cfg, 'fun-asr')


def test_fun_file_model_retains_server_timestamps_and_profile_identity():
    raw = {'route':'funasr','source_sha256':'fixture', 'status':{'model':'fun-asr'},
           'text':'Example.', 'utterances':[{'text':'Example.','start_ms':10,'end_ms':900}],
           'preference_profile':{'profile_sha256':'profile-fixture'}}
    result = asr.normalize_funasr(raw, 'fixture')
    assert result['model'] == 'fun-asr'
    assert result['sentences'] == raw['utterances']
    assert result['preference_profile'] == raw['preference_profile']


def test_conversation_keeps_real_word_times_and_reuses_success(tmp_path, monkeypatch):
    audio = tmp_path/'audio.mp3'
    audio.write_bytes(b'fixture')
    cfg = Settings(ffprobe=Path('ffprobe'), codex=Path('codex'), asr_preference_profile=tmp_path/'current.json')
    folder = tmp_path/'channel'
    raw = {'source_sha256':file_hash(audio), 'status':{'model':'fun-asr','hotwords':{'verified':True,'count':1}},
        'service_response':{'transcription':{'transcripts':[{'sentences':[{'text':'Example.',
            'begin_time':10,'end_time':900,'words':[{'text':'Example','punctuation':'.','begin_time':10,'end_time':900}]}]}]}}}
    atomic(folder/'shared-asr/raw.json', raw)
    monkeypatch.setattr(conversation_asr.subprocess, 'run', lambda *a, **kw: pytest.fail('replayed completed recognition'))
    receipt = conversation_asr.shared_chunk_receipt(cfg, audio, folder, file_hash(audio))
    assert conversation_asr.words_from_receipt(receipt) == [{'text':'Example.','start_ms':10,'end_ms':900}]
    assert conversation_asr.chunk_receipt(cfg, audio, folder) == receipt


def test_conversation_rejects_missing_word_timestamps(tmp_path):
    audio = tmp_path/'audio.mp3'
    audio.write_bytes(b'fixture')
    cfg = Settings(ffprobe=Path('ffprobe'), codex=Path('codex'), asr_preference_profile=tmp_path/'current.json')
    folder = tmp_path/'channel'
    atomic(folder/'shared-asr/raw.json', {'source_sha256':file_hash(audio),
        'status':{'model':'fun-asr','hotwords':{'verified':True}},
        'service_response':{'transcription':{'transcripts':[{'sentences':[{'text':'Example.'}]}]}}})
    with pytest.raises(Attention, match='word_coverage'):
        conversation_asr.shared_chunk_receipt(cfg, audio, folder, file_hash(audio))
