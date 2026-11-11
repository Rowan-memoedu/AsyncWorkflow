"""Live local protocol/API checks using synthetic text only."""
import json
from pathlib import Path
import time
import httpx

from async_workflow.core import Settings, atomic, read_json, digest
from async_workflow.dictation_gateway import MODEL, PORT, identity, compile_bullets

CASES = [
    ('command', 'non_writing', '嗯，检查导入流程。先只检查重复项，暂时不要删除文件，每次最多处理十二条。失败的单独列出来，已经成功的不要重跑。把耗时记下来，把耗时记下来。',
     ['暂时不要删除', '不要重跑', '耗时']),
    ('writing', 'writing', '嗯，我其实一直觉得，学习最让人泄气的，不是某一道题特别难，而是你明明已经认真想了很久，却还是不知道自己究竟卡在哪里。我只是想先把那个模模糊糊的结解开。',
     ['其实一直觉得', '明明已经认真想了很久', '却还是', '模模糊糊的结']),
    ('math', 'non_writing', '说明这个条件：x大于零，y不等于零。然后写出a除以b的分式。不要求解，也不要补充任何结论。',
     ['$x', '$y', '\\frac{a}{b}', '不要']),
    ('tone', 'writing', '我问他，你真的愿意再等一会儿吗？他说，嗯，我愿意。听到这句话，我才稍微松了一口气。',
     ['嗯，我愿意', '才稍微松了一口气']),
    ('discourse', 'writing', '我其实担心的，是把注意力弄丢。你知道吧，就是说，我们要防止注意力被打断，嗯，把它放回自己真正想做的事情上。',
     ['其实担心', '防止注意力被打断', '真正想做']),
]


def main():
    cfg = Settings.load(); root = cfg.root/'dictation'
    out = Path('D:/Artifacts/ShandianshuoWritingRouting/implementation/api-canary')
    token = (root/'client-token.txt').read_text(encoding='utf-8').strip()
    results = []
    for name, route, source, terms in CASES:
        started = time.monotonic()
        response = httpx.post(f'http://127.0.0.1:{PORT}/v1/chat/completions',
            headers={'Authorization': 'Bearer '+token},
            json={'model': MODEL, 'messages': [{'role': 'user', 'content': source}]}, timeout=35)
        if response.status_code != 200:
            result = {'name':name, 'passed':False, 'status':response.status_code}
        else:
            value = response.json()['choices'][0]['message']['content']
            signature = digest(identity(cfg)+source)
            receipt = read_json(root/'receipts'/(signature+'.json'))
            actual_route = receipt['evidence']['decision']['route']
            checks = {'route':actual_route==route, 'required_terms':all(t in value for t in terms),
                      'bullet_format':compile_bullets(value)==value, 'trailing_empty_bullet':value.endswith('\n- '),
                      'no_filler': '嗯' not in value if name!='tone' else True}
            if name == 'discourse':
                checks['no_discourse_padding'] = '你知道吧' not in value and '就是说' not in value
            result = {'name':name, 'passed':all(checks.values()), 'route':actual_route,
                      'source':source, 'output':value, 'checks':checks,
                      'seconds':round(time.monotonic()-started, 3)}
            atomic(out/(name+'.md'), value, text=True)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    verdict = {'passed':all(r['passed'] for r in results), 'policy_identity':identity(cfg), 'results':results}
    atomic(out/'summary.json', verdict)
    return 0 if verdict['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
