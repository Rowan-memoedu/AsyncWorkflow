import asyncio
import pytest
from async_workflow import remnote
from async_workflow.core import Attention


@pytest.mark.parametrize('error,retries',[
    ('daily_template_pending',3),('daily_duplicate_branch',1),
])
def test_only_pending_native_template_is_retried(monkeypatch,error,retries):
    calls=[]
    async def call(_session,method,**kwargs):
        calls.append((method,kwargs))
        if error=='daily_template_pending' and len(calls)==3:
            return {'daily_id':'day','parent_id':'native-branch'}
        raise Attention('independent_remnote_plugin:'+error)
    async def sleep(_seconds): pass
    monkeypatch.setattr(remnote,'call',call)
    monkeypatch.setattr(remnote.asyncio,'sleep',sleep)
    request=remnote.resolve_target(None,None,
        {'type':'daily','recorded_day':'2026-10-01','branch_path':['Notes']},'recording')
    if error=='daily_template_pending':
        assert asyncio.run(request)=={'daily_id':'day','parent':'native-branch'}
    else:
        with pytest.raises(Attention,match=error): asyncio.run(request)
    assert len(calls)==retries
