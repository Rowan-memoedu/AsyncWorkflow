"""Independent RemNote plugin client. No MCP server or MCP client dependency."""
from __future__ import annotations
from contextlib import asynccontextmanager
import httpx
from .core import Attention, UnknownOutcome, atomic, digest, now
from .markdown import parse_outline
from .remnote_bridge import token_for
from . import routing

@asynccontextmanager
async def connection(cfg):
    token=token_for(cfg)
    if not token: raise Attention('independent_remnote_plugin_not_initialized')
    async with httpx.AsyncClient(base_url=cfg.remnote_url,headers={'Authorization':'Bearer '+token},timeout=190) as session:
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

async def publish(cfg, manifest, effect_key, markdown, requested, metadata, raw_text=None):
    saved=manifest.effect(effect_key)
    if saved and saved.get('status')=='verified': return saved
    if saved and saved.get('backend')!='independent-remnote-plugin':
        raise UnknownOutcome('legacy_remnote_effect_requires_readback_before_migration')
    tree=parse_outline(markdown)
    async with connection(cfg) as session:
        if not saved:
            route=await routing.plan(cfg,session,call,requested,raw_text or markdown,manifest.read().get('provenance',{}))
            saved={'backend':'independent-remnote-plugin','status':'not_started','routing':route}
            manifest.save_effect(effect_key,saved)
        route=saved['routing']
        if not saved.get('parent'):
            if route['type']=='daily':
                target=await call(session,'resolve_daily',date=route['recorded_day'],branch_path=route['branch_path'])
                saved.update(parent=target['parent_id'],daily_id=target['daily_id'])
            else: saved['parent']=route['parent_id']
            manifest.save_effect(effect_key,saved)
        meta=metadata+'\n录音日期：'+route['recorded_day']
        if requested: meta+='\n口述去处：'+requested
        saved['status']='outcome_unknown'
        manifest.save_effect(effect_key,saved)
        result=await call(session,'publish_outline',effect_key=manifest.folder.name+'/'+effect_key,
                          parent_id=saved['parent'],tree=tree,metadata=meta)
        if result.get('status')!='verified' or not result.get('root_id'):
            raise UnknownOutcome('independent_remnote_plugin_readback_missing')
        saved.update(status='verified',root_id=result['root_id'],verified_at=now())
        manifest.save_effect(effect_key,saved)
        return saved
