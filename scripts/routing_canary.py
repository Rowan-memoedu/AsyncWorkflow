"""Live, source-independent text canaries; no note writes or command execution."""
import argparse,asyncio,json
from async_workflow import routing,splitter
from async_workflow.core import Settings,atomic,now

cfg=Settings.load()
parser=argparse.ArgumentParser()
parser.add_argument('--split-only',action='store_true')
args=parser.parse_args()
results=[]
async def check_daily():
    cases=[
        ('product-reflection','设备管理软件把所有按钮放在一页，开发者觉得熟悉，但新用户的工作记忆负担很大。', '其他碎片'),
        ('personal-investment','以前我为长途通勤花了很多钱，现在我愿意把省下来的预算用来买专业绘图工具，先用小额订阅试试。我对提升自己效率的投入态度变了。','私密 日记'),
        ('general-investment','按月订阅绘图软件可以降低客户的首次购买门槛，但订阅过多会使工作流程碎片化。','其他碎片'),
        ('concrete-study','今天做微积分换元积分的练习，用了四十分钟，总算学会了这类题。','每日碎碎念'),
        ('study-cognition','我在做矩阵乘法练习时发现，题目里的颜色提示让我依赖识别而不是主动回忆，这影响了记忆提取。','认知科学相关'),
        ('subject-thought','今天学习图论的树时，我又推了一遍：树中顶点数比边数多一，删去叶子可做归纳证明。','一些注定重复的思考'),
    ]
    async def forbidden(*a,**k): raise AssertionError('daily classification must not read or write RemNote')
    for name,source,expected in cases:
        route=await routing.plan(cfg,None,forbidden,'',source,{'recorded_at':'2026-10-03T00:00:00+08:00'})
        passed=route.get('branch_path',[])[-1:]==[expected]
        result={'case':name,'expected':expected,'actual':route,'passed':passed}
        results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if not passed: raise AssertionError('daily semantic canary failed: '+name)

if not args.split_only:
    asyncio.run(check_daily())
source='新建一个 RemNote 文档，叫海边观察。今天海面上有两只白色的鸟。这段记录到这里结束。再记个购物想法，明天要补两支中性笔。再说个独立的想法：家务安排不用追求每个步骤都自动化。'
parts=splitter.split(cfg,source,'# 海边观察和另外两件事情')
assert any(p['document_action']=='create' and p['destination']=='海边观察' for p in parts)
assert any(p['kind']=='shopping' and '中性笔' in p['raw_text'] for p in parts)
last=next(p for p in parts if '家务安排' in p['raw_text'])
assert not last['destination'] and last['document_action']=='append'
assert all(p['kind']!='command' for p in parts)
results.append({'case':'new-document-and-topic-boundary','passed':True,'segments':parts})
print('new-document-and-topic-boundary: passed',flush=True)
source='这条信息记在邮件对应的信息清单：合同说明会在下周发给我。再帮我记一个待办，星期三去取修好的手表。'
parts=splitter.split(cfg,source,'# 渠道信息和杂事待办')
assert any(p['kind']=='information' and p['software'] for p in parts)
assert any(p['kind']=='todo' and '手表' in p['raw_text'] for p in parts)
assert all(p['kind']!='command' for p in parts)
results.append({'case':'software-information-and-errand','passed':True,'segments':parts})
print('software-information-and-errand: passed',flush=True)
source='下面这些都放到邮件消息里。告诉对方我看完了报价，周五会再确认预算。再补充另一个话题，下周的展览我还需要看一下时间安排。'
parts=splitter.split(cfg,source,'# 邮件消息草稿')
assert parts and all(p['kind']=='information' and p['software']=='邮件' and p['route']=='remnote' for p in parts)
results.append({'case':'software-message-draft-scope','passed':True,'segments':parts})
print('software-message-draft-scope: passed',flush=True)
suffix='-split' if args.split_only else ''
atomic(cfg.root/('routing-canary-'+routing.ROUTING_REVISION+suffix+'.json'),{'at':now(),'routing_revision':routing.ROUTING_REVISION,'results':results})
