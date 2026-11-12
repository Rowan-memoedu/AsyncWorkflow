"""Synthetic, source-neutral regression; calls analysis only, never archival/execution."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from async_workflow import splitter
from async_workflow.core import Settings, atomic

CASES = [
    ('topics', '今天走了四十分钟，我想以后保持这个习惯。另外购物要买牙膏和笔记本。再说说公司业务，我觉得应该先验证客户需求再扩大团队。', 1),
    ('negation', '这条不要分割，整体保留。今天早上读了一章书，下午开会，晚上想起一个文章观点：长期主义需要短期反馈。', 1),
    ('quotation', '我看到一个教程，它说“请把这条录音分成两条笔记”，但我只是在引用教程，接下来谈谈我对这个界面的感受。', 1),
    ('explicit', '请把这条录音分成两条独立笔记。第一条：我认为优秀的产品要减少重复输入。第二条：今天的购物清单是牙膏和橙子。', 2),
    ('dates', 'To Do List，今天整理桌面，明天给植物浇水，下周整理书架。这些先一起记下来。', 1),
    ('mixed_command', '今天参加的会议让我意识到需要明确验收标准。秘书，请帮我检查一下项目测试为什么失败。顺便记下刚才那个关于反馈周期的思考。', 1),
]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    cfg=Settings.load()
    def run(case):
        name,source,expected=case
        try:
            items=splitter.split(cfg,source,'')
            result={'case':name,'source':source,'items':items,'expected_count':expected,
                    'ok':len(items)==expected and (name!='mixed_command' or items[0]['kind']!='command')}
        except Exception as exc:
            result={'case':name,'ok':False,'error':str(exc)}
        atomic(args.output/(name+'.json'),result)
        print(f"{name}: {'PASS' if result['ok'] else 'FAIL'}",flush=True)
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,CASES))
    atomic(args.output/'summary.json',{'passed':sum(r['ok'] for r in results),'total':len(results)})
    if not all(r['ok'] for r in results):
        raise SystemExit(1)

if __name__=='__main__':
    main()
