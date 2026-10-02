import json
from async_workflow.core import Settings, Manifest, digest
from async_workflow.executor import execute
cfg=Settings.load()
store=Manifest(cfg,digest('public-native-exec-resume-canary-20261002'))
item={'route':'codex','destination':'','project_required':False,'raw_text':'这是只读验收。计算两个数的和，第一个数是 12，第二个数尚未提供。请先返回 needs_input 请求第二个数，不要猜测。用户补充后再计算，不修改任何文件。'}
first=execute(cfg,store,'sum',item)
print(json.dumps({'phase':'initial','status':first['status'],'native_session':bool(first.get('session_id'))}),flush=True)
if first['status']!='needs_input':
    raise SystemExit('Expected real pending input')
second=execute(cfg,store,'sum',{**item,'followup':'第二个数是 5，请继续计算。'})
print(json.dumps({'phase':'resumed','status':second['status'],'result':second.get('result')}),flush=True)
