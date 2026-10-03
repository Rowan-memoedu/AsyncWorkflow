"""Live public fixture for the independent plugin; cleanup uses its returned ID."""
import argparse,asyncio,json,uuid
from async_workflow.core import Settings,atomic,now
from async_workflow.remnote import connection,call

parser=argparse.ArgumentParser()
parser.add_argument('--parent-id',required=True)
parser.add_argument('--run-id',default=str(uuid.uuid4()))
args=parser.parse_args()
cfg=Settings.load()
effect_key='public-document-canary-'+args.run_id
async def main():
    async with connection(cfg) as session:
        status=await call(session,'status')
        assert status['plugin_version']=='0.1.5'
        document_args={'effect_key':effect_key,'title':'独立插件文档创建与正文验收','parent_id':None}
        first=await call(session,'create_document',**document_args)
        again=await call(session,'create_document',**document_args)
        assert first['document_id']==again['document_id'] and again['is_document']
        document_id=first['document_id']
        tree={'text':'公开测试条目','children':[{'text':'只保留正文。','children':[]},{'text':'数字 12 和符号 :: 保持原样。','children':[]}]}
        body={'effect_key':effect_key+'/body','parent_id':document_id,'tree':tree,'metadata':'This is a log-only marker, never a visible note.'}
        one=await call(session,'publish_outline',**body)
        two=await call(session,'publish_outline',**body)
        assert one['root_id']==two['root_id'] and len(one['readback'])==3
        assert one['separators']==two['separators'] and one['separators']['verified']
        moved=await call(session,'relocate_outline',effect_key=body['effect_key'],operation_id=effect_key+'/out',parent_id=args.parent_id,trash_id=cfg.trash_rem_id)
        restored=await call(session,'relocate_outline',effect_key=body['effect_key'],operation_id=effect_key+'/back',parent_id=document_id,trash_id=cfg.trash_rem_id)
        assert moved['root_id']==restored['root_id']==one['root_id']
        assert moved['separators']['before_id']==restored['separators']['before_id']==one['separators']['before_id']
        result={'at':now(),'document':first,'repeat':again,'body':one,'relocated':moved,'restored':restored,'expected_tree':tree}
        atomic(cfg.root/(effect_key+'.json'),result)
        print(json.dumps({'document_id':document_id,'root_id':one['root_id'],'nodes':len(one['readback']),'same_ids_after_relocation':True},ensure_ascii=False))
asyncio.run(main())
