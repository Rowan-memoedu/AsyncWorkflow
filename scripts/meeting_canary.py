"""Live, isolated public meeting fixture under the configured recovery area."""
import asyncio
import json
from uuid import uuid4
from async_workflow.core import Settings, atomic, now
from async_workflow.remnote import connection, call


async def main():
    cfg=Settings.load()
    assert cfg.trash_rem_id, 'recovery_area_not_bound'
    effect='public-meeting-test/'+uuid4().hex
    async with connection(cfg) as session:
        status=await call(session,'status')
        assert status['plugin_version']=='0.1.6'
        doc=await call(session,'create_document',effect_key=effect,title='内部会议预案',parent_id=cfg.trash_rem_id)
        args={'document_id':doc['document_id'],'date':'2040-02-29'}
        path=await call(session,'resolve_meeting',**args)
        assert await call(session,'resolve_meeting',**args)==path
        tree={'text':'公开会议测试','children':[{'text':'讨论事项：记录公开资料，保留完整观点。','children':[]}]}
        body={'effect_key':effect+'/body','parent_id':path['parent_id'],'tree':tree,'metadata':'Log only'}
        first=await call(session,'publish_outline',**body)
        again=await call(session,'publish_outline',**body)
        assert first['root_id']==again['root_id'] and first['separators']==again['separators']
        result={'at':now(),'document':doc,'path':path,'body':first,'expected_tree':tree,'isolated_under_recovery_area':True}
        receipt=cfg.artifacts.parent/'meeting-canary.json'
        atomic(receipt,result)
        print(json.dumps({'receipt':str(receipt),'document_id':doc['document_id'],**path,
                          'root_id':first['root_id'],'duplicate_write':False},ensure_ascii=False))


asyncio.run(main())
