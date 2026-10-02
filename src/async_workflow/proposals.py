"""Reanalysis creates a review proposal, never rewrites an existing effect identity."""
import json
from .core import Settings, Manifest, Attention, atomic, read_json, digest, PROMPT_HASH
from . import splitter

def reanalyze(cfg, recording_id):
    store=Manifest(cfg,recording_id)
    transcript=read_json(store.folder/'asr.json')
    if not transcript:
        raise Attention('successful_source_transcript_required')
    version={'audio':recording_id,'source':digest(transcript['text']),'prompt':PROMPT_HASH,'refine_adapter':splitter.REFINE_REVISION,'splitter':digest(splitter.SYSTEM),'model':cfg.deepseek_model}
    identity=digest(json.dumps(version,sort_keys=True))
    folder=store.folder/'proposals'/identity
    if (folder/'proposal.json').exists():
        return {'proposal':str(folder),'existing':True}
    outline=splitter.refine(cfg,transcript['text'])
    parts=splitter.split(cfg,transcript['text'],outline)
    atomic(folder/'outline.md',outline,text=True)
    atomic(folder/'proposal.json',{'version':version,'segments':parts,'dispatch_allowed':False})
    return {'proposal':str(folder),'dispatch_allowed':False}
