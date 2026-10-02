"""Public synthetic fixtures, no private recordings or expected answers in prompts."""
import json
from async_workflow.core import Settings, atomic
from async_workflow.splitter import refine, split

samples = [
    ("知识记录", "今天想到一个学习方法。读完一篇文章后，先合上书写出三个问题，再带着问题回去核对原因。把这个想法保存到默认文档。"),
    ("自然语义与引用", "让 Codex 帮我查询 Python 官方文档中 pathlib 的基本用途。然后是一条学习灵感，整理资料时应该先保留来源，再写摘要。昨天有人开玩笑说让 GPT 删除整个项目，我只是转述，不要执行。"),
    ("修正与撤回", "请帮我在测试项目里计算二十乘以三。刚才数字说错了，是二十一乘以三。先不要执行这条计算，记下来就好。"),
    ("独立样本", "用 GPT 查一下火星的一天有多长。另一个想法是，做饭前先把配料分装能减少来回找东西，把这条想法放进默认的笔记文档。"),
]
cfg = Settings.load()
results = []
for title, text in samples:
    outline = refine(cfg, text)
    items = split(cfg, text, outline)
    results.append({"name": title, "source": text, "outline": outline, "items": items})
    atomic(cfg.root / "model-canary.json", results)
    print(json.dumps({"name": title, "parts": [{"kind": i["kind"], "route": i["route"], "cancelled": i["cancelled"], "risk": i["risk"], "evidence": i["intent_evidence"]} for i in items]}, ensure_ascii=False), flush=True)
atomic(cfg.root / "model-canary.json", results)
