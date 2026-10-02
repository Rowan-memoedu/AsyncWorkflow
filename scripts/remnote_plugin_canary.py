"""Real plugin write/readback canary in an explicitly supplied test document."""
import argparse
import asyncio
import json
from async_workflow.core import Settings, atomic
from async_workflow.remnote import connection, call
from async_workflow.markdown import parse_outline

p=argparse.ArgumentParser()
p.add_argument('--parent-id', required=True)
args=p.parse_args()
cfg=Settings.load()

async def main():
    async with connection(cfg) as session:
        results=[]
        for key,text in [('symbols','# 公式与符号\n- $E=mc^2$\n  - 字面 :: 以及 →\n- 第二条'),('title','# 只有标题的观点')]:
            body={'effect_key':'public-plugin-canary-20261002/'+key,'parent_id':args.parent_id,
                  'tree':parse_outline(text),'metadata':'独立 RemNote 插件公开验收样例；不含私人录音。'}
            first=await call(session,'publish_outline',**body)
            second=await call(session,'publish_outline',**body)
            assert first['root_id']==second['root_id'] and first['status']==second['status']=='verified'
            results.append({'case':key,'root_id':first['root_id'],'repeat_reused':second.get('reused')})
        atomic(cfg.root/'remnote-plugin-canary.json',results)
        print(json.dumps(results,ensure_ascii=False,indent=2))

asyncio.run(main())
