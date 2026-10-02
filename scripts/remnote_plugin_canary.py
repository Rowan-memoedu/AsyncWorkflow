"""Real plugin write/readback canary in an explicitly supplied test document."""
import argparse
import asyncio
import json
from async_workflow.core import Settings, atomic
from async_workflow.remnote import connection, call
from async_workflow.markdown import parse_outline

p=argparse.ArgumentParser()
p.add_argument('--parent-id', required=True)
p.add_argument('--run-id', default='public-plugin-canary-20261002')
args=p.parse_args()
cfg=Settings.load()

async def main():
    async with connection(cfg) as session:
        status=await call(session,'status')
        assert tuple(map(int,status.get('plugin_version','0.0.0').split('.')))>=(0,1,2), 'Reload the updated plugin before this canary'
        results=[]
        for key,text in [('symbols','# 公式与符号\n- $E=mc^2$\n  - 字面 :: 以及 →\n- 第二条'),('title','# 只有标题的观点'),('independent','# 独立符号样例\n- **保持原样** A >> B；C ;; D；x==y\n- 数学 $a \\ll b$ 与 $x^2+y^2$')]:
            body={'effect_key':args.run_id+'/'+key,'parent_id':args.parent_id,
                  'tree':parse_outline(text),'metadata':'独立 RemNote 插件公开验收样例；不含私人录音。'}
            first=await call(session,'publish_outline',**body)
            second=await call(session,'publish_outline',**body)
            assert first['root_id']==second['root_id'] and first['status']==second['status']=='verified'
            readback=first.get('readback',[])
            assert readback, 'Updated plugin readback required'
            elements=[part for node in readback for part in node['text']]
            assert not any(isinstance(part,dict) and part.get('i')=='s' for part in elements)
            assert all(not node['back_text'] for node in readback)
            if key=='symbols':
                assert any('字面 :: 以及 →'==''.join(x if isinstance(x,str) else x.get('text','') for x in node['text']) for node in readback)
                assert any(isinstance(part,dict) and part.get('i')=='x' and part.get('text')=='E=mc^2' for part in elements)
            results.append({'case':key,'root_id':first['root_id'],'repeat_reused':second.get('reused'),'readback':readback})
        atomic(cfg.root/'remnote-plugin-canary.json',results)
        print(json.dumps(results,ensure_ascii=False,indent=2))

asyncio.run(main())
