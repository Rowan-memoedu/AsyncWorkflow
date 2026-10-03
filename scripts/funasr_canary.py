"""Exercise the production FUN-ASR adapter on an explicitly supplied public audio fixture."""
import argparse,json
from pathlib import Path
from async_workflow import asr
from async_workflow.core import Settings,Manifest,file_hash,atomic,now
parser=argparse.ArgumentParser()
parser.add_argument('public_audio',type=Path)
parser.add_argument('--artifacts',type=Path,required=True)
args=parser.parse_args()
cfg=Settings.load().model_copy(update={'artifacts':args.artifacts})
source=args.public_audio.resolve();store=Manifest(cfg,file_hash(source))
text=asr.recognize(cfg,store,source)
result={'at':now(),'model':store.read()['asr']['model'],'provider':store.read()['asr']['provider'],
        'source_sha256':file_hash(source),'transcript':text,'default_provider':cfg.asr_provider}
atomic(args.artifacts/'verification.json',result)
print(json.dumps(result,ensure_ascii=False))
