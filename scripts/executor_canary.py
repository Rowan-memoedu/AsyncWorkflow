"""Public arithmetic canary; first run creates one real Thread, repeats reuse it."""
import json
from async_workflow.core import Settings, Manifest, digest
from async_workflow.executor import execute

cfg = Settings.load()
store = Manifest(cfg, digest("public-executor-canary-20261002"))
result = execute(cfg, store, "arithmetic", {"route":"codex", "destination":"", "project_required":False,
    "raw_text":"这是只读验收：计算 17 乘以 19。可用本机计算工具核验；不要修改任何文件，不要发消息。结果中给出计算结果和核验方法。"})
print(json.dumps(result, ensure_ascii=False))
