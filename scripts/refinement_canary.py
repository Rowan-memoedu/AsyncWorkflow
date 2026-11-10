"""Isolated, synthetic-text canary. No audio, archival or task dispatch.

Expected labels/checks stay local and are never sent to either model call.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time

from async_workflow.core import Settings, atomic, digest, read_json
from async_workflow import refinement as r, splitter


def run_case(base, output, case):
    folder = output / case['id']
    cfg = base.model_copy(update={'root': folder/'state', 'artifacts': folder/'unused-artifacts',
                                 'inbox': folder/'unused-inbox', 'refinement_policy': 'content_routed'})
    cfg.prepare()
    fingerprint = digest(json.dumps(case, ensure_ascii=False, sort_keys=True) + r.identity(cfg))
    saved = read_json(folder/'result.json')
    if saved and saved.get('fingerprint') == fingerprint:
        return saved
    start = time.monotonic()
    decision = r.decide(cfg, case['text'])
    result = {'id': case['id'], 'split': case['split'], 'source': case['text'],
              'expected_route': case['expected_route'], 'decision': decision,
              'route_ok': decision['route'] == case['expected_route'], 'fingerprint': fingerprint}
    if case.get('generate'):
        value = splitter.refine(cfg, case['text'])
        atomic(folder/'output.md', value+'\n', text=True)
        result['output'] = value
        checks = []
        for group in case.get('required_groups', []):
            checks.append({'kind': 'retain', 'alternatives': group, 'ok': any(t in value for t in group)})
        for term in case.get('absent', []):
            checks.append({'kind': 'remove_filler', 'text': term, 'ok': term not in value})
        for term in case.get('once', []):
            checks.append({'kind': 'deduplicate', 'text': term, 'ok': value.count(term) == 1})
        result['checks'] = checks
        result['content_ok'] = all(c['ok'] for c in checks)
        if case.get('baseline'):
            # Same API/model and unmodified old prompt isolate the prompt difference.
            old = r.generate(cfg, case['text'], base.prompt.read_text(encoding='utf-8-sig'), route='legacy_comparison')
            atomic(folder/'baseline.md', old+'\n', text=True)
            result['baseline'] = old
    result['seconds'] = time.monotonic() - start
    result['ok'] = result['route_ok'] and result.get('content_ok', True)
    atomic(folder/'result.json', result)
    return result


def write_report(summary, output):
    labels = {'writing': '写作', 'non_writing': '非写作', 'uncertain': '不确定'}
    lines = ['# 写作与非写作：提示词和实际效果', '',
             f"Luna Decisions API 的 {summary['route_passed']}/{summary['count']} 条路线符合预期；"
             f"Responses API 生成的 {summary['content_passed']}/{summary['generated']} 份正文通过本地保真、去噪和格式检查。", '',
             '样本为合成口述文本。预期分类和保留词只用于本地检查，没有传给模型。'
             '下面的输出直接取自 API 回执，没有人工改稿；这些结果不代表真实录音的总体准确率。', '',
             '## 路由原则', '',
             '- 已经在表达观点、经历、感受或消息正文：写作。',
             '- 只交代任务、步骤、约束、日程或清单：非写作。',
             '- 要求将来写文章属于任务；已经口述的文章内容属于写作。',
             '- 混合内容、不确定内容和低置信非写作判断：优先保护写作。', '',
             '## 完整提示词', '']
    for name, title in [('decide-writing.md', 'Luna 决策'), ('refine-common.md', '共同保真与格式规则'),
                        ('refine-non-writing.md', '非写作整理'), ('refine-writing.md', '写作整理')]:
        lines += ['### ' + title, '', r.prompt(name).strip(), '']
    lines += ['## 决策结果', '', '| 输入 | 预期 | 模型选择 | 实际路线 |', '| --- | --- | --- | --- |']
    for item in summary['results']:
        if 'decision' not in item:
            lines.append('| 调用失败 | — | — | — |')
            continue
        excerpt = item['source'][:48].replace('|', '｜') + ('…' if len(item['source']) > 48 else '')
        lines.append(f"| {excerpt} | {labels[item['expected_route']]} | {labels[item['decision']['choice']]} | {labels[item['decision']['route']]} |")
    lines += ['', '## 实际整理结果', '']
    for index, item in enumerate((v for v in summary['results'] if 'output' in v), 1):
        title = item['output'].splitlines()[0].lstrip('# ').strip()
        lines += [f"### {index}. {title}（{labels[item['decision']['route']]}）", '', '原文：', '',
                  '> ' + item['source'], '']
        if item.get('baseline'):
            lines += ['旧提示词结果（同一生成模型与 API）：', '']
            lines += ['> ' + line for line in item['baseline'].splitlines()]
            lines.append('')
        lines += ['新路由结果：', '']
        lines += ['> ' + line for line in item['output'].splitlines()]
        lines.append('')
    atomic(output/'提示词与实际效果.md', '\n'.join(lines), text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--split', choices=['development', 'holdout', 'independent', 'all'], default='all')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    base = Settings.load()
    fixtures = Path(__file__).resolve().parents[1]/'tests/fixtures/refinement_cases.json'
    cases = [c for c in read_json(fixtures) if args.split == 'all' or c['split'] == args.split]
    args.output.mkdir(parents=True, exist_ok=True)
    atomic(args.output/'cases.json', cases)
    atomic(args.output/'prompts.json', {p.name:p.read_text(encoding='utf-8') for p in r.PROMPTS.glob('*.md')})
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run_case, base, args.output, case): case['id'] for case in cases}
        for job in as_completed(jobs):
            try:
                result = job.result()
            except Exception as exc:
                # Report only sanitized Attention messages, never raw SDK exceptions.
                from async_workflow.core import Attention
                result = {'id': jobs[job], 'ok': False,
                          'error': str(exc) if isinstance(exc, Attention) else type(exc).__name__}
                atomic(args.output/(jobs[job]+'-error.json'), result)
            results.append(result)
            print(json.dumps({'id':result['id'], 'ok':result['ok'], 'route':result.get('decision',{}).get('route'),
                              'error':result.get('error')}, ensure_ascii=False), flush=True)
    ordered = sorted(results, key=lambda item: next(i for i,c in enumerate(cases) if c['id']==item['id']))
    summary = {'passed':all(r['ok'] for r in ordered), 'count':len(ordered),
               'route_passed':sum(r.get('route_ok',False) for r in ordered),
               'generated':sum('output' in r for r in ordered),
               'content_passed':sum(r.get('content_ok',False) for r in ordered),
               'decision_model':r.DECISION_MODEL, 'generation_model':base.refinement_model,
               'policy_identity':r.identity(base), 'results':ordered}
    atomic(args.output/'summary.json', summary)
    write_report(summary, args.output)
    print(json.dumps({k:v for k,v in summary.items() if k!='results'}, ensure_ascii=False), flush=True)
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
