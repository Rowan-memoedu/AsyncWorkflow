"""Public ASR capability probe plus a fictional, source-neutral identity fixture."""
from pathlib import Path
from async_workflow import asr, meeting_audio, conversation
from async_workflow.core import Settings, Manifest, atomic, digest, read_json

cfg = Settings.load().model_copy(update={'artifacts':Path('D:/Artifacts/AsyncWorkflow/2026-10-13/meeting-day-canary')})
public = Path('C:/Users/fy/.codex/global-transcription/canary/english.wav')
import hashlib
data = public.read_bytes()
assert hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest() == '40d7eb5c19f9b0d7a83456b13adc70beec8fc29f'
store = Manifest(cfg, digest('public-english-native-fun-asr-diarization-1'))
text = asr.recognize_filetrans(cfg, store, public, model='fun-asr',
                             parameters={'diarization_enabled':True,'speaker_count':2})
result = read_json(store.folder/'asr.json')
assert all('speaker_id' in s for s in result['sentences'])
assert store.read()['asr']['cleaned']
assert all(word in text.lower() for word in ('one','two','three'))
print('public_fun_asr_diarization_and_cleanup_passed', flush=True)
turns = [
    {'id':0,'speaker_id':'7','text':'我是林岚，我们每周五发布一个小版本。'},
    {'id':1,'speaker_id':'4','text':'我是周明。好，发布前跑回归测试，保留回滚版本。'},
    {'id':2,'speaker_id':'7','text':'预算我建议一百元。'},
    {'id':3,'speaker_id':'4','text':'我不同意，至少两百元，预算下次继续讨论。'},
    {'id':4,'speaker_id':'7','text':'今天中午吃了面。'},
]
names_store=Manifest(cfg,digest('fictional-room-names-fixture-1'))
mapping=meeting_audio.resolve_names(cfg,names_store,turns,{'self_name':'周明','other_name':'林岚'})
assert mapping=={'7':'林岚','4':'周明'}
for turn in turns: turn['speaker']=mapping[turn['speaker_id']]
transcript={'turns':turns,'speakers':mapping,'text':'\n'.join(t['speaker']+'：'+t['text'] for t in turns)}
outline=conversation.outline(cfg,names_store,transcript)
sections=conversation.parse_outline(outline)['children']
assert [s['text'] for s in sections]==conversation.SECTIONS
atomic(cfg.artifacts/'result.json',{'asr_speaker_ids':True,'oss_cleanup':True,'context_names':mapping,
                                   'three_sections':True,'outline':outline})
print('fictional_identity_and_three_section_canary_passed',flush=True)
