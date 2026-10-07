"""Independent RemNote plugin client. No MCP server or MCP client dependency."""
from __future__ import annotations
from contextlib import asynccontextmanager
import httpx
from .core import Attention, UnknownOutcome, atomic, digest, now
from .markdown import parse_outline
from .remnote_bridge import token_for
from . import routing

RECONNECT_REASONS = {
    'independent_remnote_plugin_connection_lost',
    'independent_remnote_plugin_not_ready_or_reply_lost',
}

@asynccontextmanager
async def connection(cfg, *, timeout=190):
    token=token_for(cfg)
    if not token: raise Attention('independent_remnote_plugin_not_initialized')
    async with httpx.AsyncClient(base_url=cfg.remnote_url,headers={'Authorization':'Bearer '+token},timeout=timeout) as session:
        yield session

async def call(session, method, **arguments):
    try:
        response=await session.post('/rpc',json={'method':method,'args':arguments})
    except httpx.HTTPError:
        raise UnknownOutcome('independent_remnote_plugin_connection_lost') from None
    if response.status_code==503:
        raise UnknownOutcome('independent_remnote_plugin_not_ready_or_reply_lost')
    if response.is_error:
        error=response.json().get('error',response.json().get('detail','rpc_failed'))
        raise Attention('independent_remnote_plugin:'+str(error))
    return response.json()

async def is_ready(cfg):
    try:
        async with connection(cfg,timeout=5) as session:
            result=await call(session,'status')
        return result.get('connected') is True and result.get('protocol')==1
    except Attention:
        return False

async def sync_template(cfg):
    if not cfg.template_id: raise Attention('template_document_not_bound')
    async with connection(cfg) as session:
        tree=await call(session,'template_snapshot',rem_id=cfg.template_id)
    paths=routing.template_paths(tree)
    if ['其他碎片'] not in paths: raise Attention('template_missing_other_fragments_branch')
    import json
    version=digest(json.dumps(tree,ensure_ascii=False,sort_keys=True))
    result={'template_id':cfg.template_id,'tree':tree,'version':version,'synced_at':now()}
    atomic(cfg.root/'daily-template.json',result)
    return {'version':version,'branch_count':len(paths),'synced_at':result['synced_at']}

async def sync_targets(cfg):
    if not cfg.todo_document_id: raise Attention('todo_document_not_bound')
    async with connection(cfg) as session:
        tree=await call(session,'targets_snapshot',rem_id=cfg.todo_document_id)
    branches={name:[c for c in tree['children'] if c['title']==name] for name in ('购物清单','信息清单')}
    if any(len(values)!=1 for values in branches.values()): raise Attention('todo_branches_not_unique')
    import json
    result={'todo_id':cfg.todo_document_id,'version':digest(json.dumps(tree,ensure_ascii=False,sort_keys=True)),
            'shopping_id':branches['购物清单'][0]['id'],'information_id':branches['信息清单'][0]['id'],
            'software':[{'id':c['id'],'title':c['title']} for c in branches['信息清单'][0]['children'] if c['title'].strip()],
            'synced_at':now()}
    atomic(cfg.root/'todo-targets.json',result)
    return result

async def resolve_target(cfg,session,route,recording_id):
    if route['type']=='meeting':
        target=await call(session,'resolve_meeting',document_id=route['document_id'],date=route['recorded_day'])
        return {'parent':target['parent_id'],'meeting_date_id':target['date_id']}
    if route['type']=='daily':
        target=await call(session,'resolve_daily',date=route['recorded_day'],branch_path=route['branch_path'])
        return {'parent':target['parent_id'],'daily_id':target['daily_id']}
    if route['type']=='create_document':
        import json
        identity=digest(json.dumps([route['title'],route.get('parent_id')],ensure_ascii=False))[:24]
        target=await call(session,'create_document',effect_key=recording_id+'/document/'+identity,
                          title=route['title'],parent_id=route.get('parent_id'))
        if not target.get('is_document') or not target.get('document_id'): raise UnknownOutcome('new_document_readback_missing')
        return {'parent':target['document_id'],'created_document_id':target['document_id']}
    if route['type']=='todo':
        target=await call(session,'resolve_todo',category=route['category'],software=route.get('software',''))
        return {'parent':target['parent_id']}
    return {'parent':route['parent_id']}

async def publish(cfg, manifest, effect_key, markdown, requested, metadata, raw_text=None, *, item=None):
    saved=manifest.effect(effect_key)
    if saved and saved.get('status')=='verified': return saved
    if saved and saved.get('backend')!='independent-remnote-plugin':
        raise UnknownOutcome('legacy_remnote_effect_requires_readback_before_migration')
    tree=parse_outline(markdown) if markdown.strip() else None
    async with connection(cfg) as session:
        if not saved:
            route=await routing.plan(cfg,session,call,requested,raw_text or markdown,manifest.read().get('provenance',{}),item=item)
            saved={'backend':'independent-remnote-plugin','status':'not_started','routing':route}
            manifest.save_effect(effect_key,saved)
        route=saved['routing']
        if not saved.get('parent'):
            saved.update(await resolve_target(cfg,session,route,manifest.folder.name))
            manifest.save_effect(effect_key,saved)
        if tree is None:
            if route['type'] != 'create_document':
                raise Attention('archival_instruction_without_content')
            saved.update(status='verified', content_format='empty-document', verified_at=now(), metadata_log=metadata)
            manifest.save_effect(effect_key,saved)
            return saved
        meta=metadata+'\n录音日期：'+route['recorded_day']
        if requested: meta+='\n口述去处：'+requested
        saved['metadata_log']=meta
        saved['plugin_effect_key']=manifest.folder.name+'/'+effect_key
        saved['content_format']='content-with-separators'
        saved['status']='outcome_unknown'
        manifest.save_effect(effect_key,saved)
        result=await call(session,'publish_outline',effect_key=manifest.folder.name+'/'+effect_key,
                          parent_id=saved['parent'],tree=tree,metadata=meta)
        if result.get('status')!='verified' or not result.get('root_id'):
            raise UnknownOutcome('independent_remnote_plugin_readback_missing')
        saved.update(status='verified',root_id=result['root_id'],separators=result.get('separators'),verified_at=now())
        manifest.save_effect(effect_key,saved)
        return saved
