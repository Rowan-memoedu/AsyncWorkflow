import type {ReactRNPlugin, PluginRem, RichTextInterface} from '@remnote/plugin-sdk';
import {outlineRichText} from './rich-text';

type Outline = {text:string; children:Outline[]};
type NodeState = {id?:string; phase:'creating'|'created'|'ready'; parent:string|null; text:RichTextInterface;
  readback?:{id:string;parent_id:string|null;text:RichTextInterface;back_text:RichTextInterface}};
type WriteState = {signature:string; done?:boolean; root_id?:string; nodes:Record<string,NodeState>;
  format?:'content-only'; current_parent?:string; metadata_removed?:boolean;
  separators?:Record<'before'|'after',NodeState>};
type SnapshotNode = {id:string; title:string; children:SnapshotNode[]};
const key = (value:string) => `async-workflow-v1:${value}`;

export class WorkflowAPI {
  constructor(private plugin:ReactRNPlugin) {}
  async rem(id:string):Promise<PluginRem> {
    const rem=await this.plugin.rem.findOne(id);
    if(!rem) throw new Error('rem_missing');
    return rem;
  }
  async title(rem:PluginRem) { return this.plugin.richText.toString(rem.text || []); }
  async snapshot(id:string,slot='template') {
    let count=0;
    const visit=async(rem:PluginRem,depth:number):Promise<SnapshotNode>=>{
      if(depth>12 || ++count>500) throw new Error('template_too_large');
      const children=[];
      for(const child of await rem.getChildrenRem()) children.push(await visit(child,depth+1));
      return {id:rem._id,title:await this.title(rem),children};
    };
    const root=await visit(await this.rem(id),0);
    await this.plugin.storage.setLocal(key(slot),root);
    return root;
  }
  async search(queries:string[]) {
    const results=new Map<string,{id:string; title:string; ancestors:string[]}>();
    for(const query of queries.slice(0,5)) {
      if(!query.trim()) continue;
      for(let rem of await this.plugin.search.search([query],undefined,{numResults:50})) {
        let depth=0;
        while(rem && !(await rem.isDocument()) && ++depth<12) {
          const parent=await rem.getParentRem();
          if(!parent) break;
          rem=parent;
        }
        if(!(await rem.isDocument()) || results.has(rem._id)) continue;
        const ancestors=[];
        let parent=await rem.getParentRem();
        for(let n=0;parent && n<12;n++) {
          ancestors.unshift(await this.title(parent));
          parent=await parent.getParentRem();
        }
        results.set(rem._id,{id:rem._id,title:await this.title(rem),ancestors});
        if(results.size>=80) break;
      }
    }
    return {candidates:[...results.values()]};
  }
  async daily(date:string,path:string[]) {
    if(!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error('date_invalid');
    const [year,month,day]=date.split('-').map(Number);
    const calendarDate=new Date(year,month-1,day,12);
    if(calendarDate.getFullYear()!==year || calendarDate.getMonth()!==month-1 || calendarDate.getDate()!==day) throw new Error('date_invalid');
    const template=await this.plugin.storage.getLocal<SnapshotNode>(key('template'));
    if(!template) throw new Error('template_not_synced');
    let branch=template;
    for(const label of path) {
      const found=branch.children.filter(n=>n.title===label && label.trim());
      if(found.length!==1) throw new Error('branch_not_in_template');
      branch=found[0];
    }
    if(!path.length) throw new Error('branch_required');
    const cacheKey=key(`daily:${date}:${JSON.stringify(path)}`);
    const cached=await this.plugin.storage.getLocal<{daily_id:string;parent_id:string}>(cacheKey);
    if(cached) {
      let current=await this.rem(cached.parent_id);
      for(const label of [...path].reverse()) {
        if((await this.title(current))!==label) throw new Error('daily_branch_changed');
        const parent=await current.getParentRem();
        if(!parent) throw new Error('daily_branch_changed');
        current=parent;
      }
      if(current._id!==cached.daily_id) throw new Error('daily_branch_changed');
      return cached;
    }
    const daily=await this.plugin.date.getDailyDoc(calendarDate);
    if(!daily) throw new Error('daily_unavailable');
    let parent=daily;
    for(const label of path) {
      const found=[];
      for(const child of await parent.getChildrenRem()) if((await this.title(child))===label) found.push(child);
      if(found.length>1) throw new Error('daily_duplicate_branch');
      if(found.length===1) parent=found[0];
      else {
        const recordKey=key(`daily-create:${date}:${parent._id}:${label}`);
        let state=await this.plugin.storage.getLocal<NodeState>(recordKey);
        state=await this.ensureNode(recordKey,state,parent._id,[label]);
        parent=await this.rem(state.id!);
      }
    }
    const result={daily_id:daily._id,parent_id:parent._id};
    await this.plugin.storage.setLocal(cacheKey,result);
    return result;
  }
  async meeting(documentId:string,date:string) {
    if(!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error('date_invalid');
    const [year,month,day]=date.split('-').map(Number);
    const check=new Date(year,month-1,day,12);
    if(check.getFullYear()!==year||check.getMonth()!==month-1||check.getDate()!==day)throw new Error('date_invalid');
    const root=await this.rem(documentId);
    if(!await root.isDocument() || await this.title(root)!=='内部会议预案')throw new Error('meeting_document_changed');
    const dates=[];
    for(const child of await root.getChildrenRem()) {
      const title=await this.title(child);
      if(title===date || title===`${month}.${day}`) dates.push(child);
    }
    if(dates.length>1)throw new Error('meeting_date_ambiguous');
    let dateRem=dates[0];
    if(!dateRem) {
      const created=await this.createDocument(`meeting/${documentId}/${date}`,date,documentId);
      dateRem=await this.rem(created.document_id!);
    }
    const branches=[];
    for(const child of await dateRem.getChildrenRem())if(await this.title(child)==='会议记录')branches.push(child);
    if(branches.length>1)throw new Error('meeting_branch_ambiguous');
    let parent=branches[0];
    if(!parent) {
      const recordKey=key(`meeting-branch:${documentId}:${dateRem._id}`);
      const saved=await this.plugin.storage.getLocal<NodeState>(recordKey);
      const state=await this.ensureNode(recordKey,saved,dateRem._id,['会议记录']);
      parent=await this.rem(state.id!);
    }
    return {date_id:dateRem._id,parent_id:parent._id};
  }
  private async ensureNode(recordKey:string,state:NodeState|undefined,parent:string|null,text:RichTextInterface) {
    if(!state) {
      state={phase:'creating',parent,text};
      await this.plugin.storage.setLocal(recordKey,state);
      const rem=await this.plugin.rem.createRem();
      if(!rem) throw new Error('create_unknown');
      state.id=rem._id; state.phase='created';
      await this.plugin.storage.setLocal(recordKey,state);
    }
    if(!state.id) throw new Error('create_unknown_no_replay');
    const rem=await this.rem(state.id);
    const actualParent=await rem.getParentRem();
    if(actualParent && actualParent._id!==parent) throw new Error('owned_rem_moved');
    if(!(await this.plugin.richText.equals(rem.text||[],text))) {
      if(state.phase!=='created' || !(await this.plugin.richText.empty(rem.text||[],true))) throw new Error('owned_rem_edited');
      await rem.setText(text);
    }
    if(!actualParent && parent!==null) {
      if(state.phase==='ready') throw new Error('owned_rem_moved');
      await rem.setParent(parent,(await (await this.rem(parent)).getChildrenRem()).length);
    }
    const read=await this.rem(state.id);
    if(((await read.getParentRem())?._id??null)!==parent || !(await this.plugin.richText.equals(read.text||[],text))) throw new Error('readback_mismatch');
    if(read.backText && !(await this.plugin.richText.empty(read.backText,true))) throw new Error('unexpected_back_text');
    state.readback={id:read._id,parent_id:parent,text:read.text||[],back_text:read.backText||[]};
    state.phase='ready';
    await this.plugin.storage.setLocal(recordKey,state);
    return state;
  }
  private async checkSeparators(state:WriteState,allowedParents:string[]) {
    for(const saved of Object.values(state.separators??{})) {
      if(!saved.id) throw new Error('create_unknown_no_replay');
      const rem=await this.rem(saved.id);
      if(!(await this.plugin.richText.empty(rem.text||[],true)) ||
         (rem.backText && !(await this.plugin.richText.empty(rem.backText,true))) ||
         (await rem.getChildrenRem()).length) throw new Error('owned_separator_edited');
      if(!allowedParents.includes((await rem.getParentRem())?._id??'')) throw new Error('owned_separator_moved');
    }
  }
  private async surround(effect:string,state:WriteState,parentId:string) {
    const recordKey=key(`publish:${effect}`);
    await this.checkSeparators(state,[state.current_parent??JSON.parse(state.signature).parentId,parentId]);
    for(const side of ['before','after'] as const) {
      const nodeKey=key(`publish-separator:${effect}:${side}`);
      let saved=state.separators?.[side]??await this.plugin.storage.getLocal<NodeState>(nodeKey);
      if(!state.separators?.[side]) saved=await this.ensureNode(nodeKey,saved,parentId,[]);
      state.separators ||= {} as Record<'before'|'after',NodeState>;
      state.separators[side]=saved!;
      await this.plugin.storage.setLocal(recordKey,state);
    }
    const ids=[state.separators!.before.id!,state.root_id!,state.separators!.after.id!];
    const parent=await this.rem(parentId);
    const children=(await parent.getChildrenRem()).map(n=>n._id);
    const rootIndex=children.indexOf(state.root_id!);
    if(rootIndex<0) throw new Error('owned_rem_moved');
    if(children[rootIndex-1]!==ids[0] || children[rootIndex+1]!==ids[2]) {
      const position=children.slice(0,rootIndex).filter(id=>!ids.includes(id)).length;
      await this.plugin.rem.moveRems(ids,parentId,position);
    }
    const observed=(await (await this.rem(parentId)).getChildrenRem()).map(n=>n._id);
    const start=observed.indexOf(ids[0]);
    if(start<0 || JSON.stringify(observed.slice(start,start+3))!==JSON.stringify(ids)) throw new Error('separator_order_readback_mismatch');
    await this.checkSeparators(state,[parentId]);
    for(const side of ['before','after'] as const) state.separators![side].parent=parentId;
    await this.plugin.storage.setLocal(recordKey,state);
    return {before_id:ids[0],after_id:ids[2],parent_id:parentId,verified:true};
  }
  async createDocument(effect:string,title:string,parentId:string|null=null) {
    if(!/^[a-zA-Z0-9:._/-]{1,180}$/.test(effect) || !title?.trim() || title.length>300) throw new Error('document_input_invalid');
    if(parentId) await this.rem(parentId);
    const recordKey=key(`document:${effect}`);
    const saved=await this.plugin.storage.getLocal<NodeState>(recordKey);
    if(saved && (saved.parent!==parentId || JSON.stringify(saved.text)!==JSON.stringify([title]))) throw new Error('effect_input_changed');
    const state=await this.ensureNode(recordKey,saved,parentId,[title]);
    const rem=await this.rem(state.id!);
    if(!(await rem.isDocument())) await rem.setIsDocument(true);
    if(!(await (await this.rem(state.id!)).isDocument())) throw new Error('document_readback_mismatch');
    return {document_id:state.id,parent_id:parentId,title,is_document:true,reused:!!saved};
  }
  async todo(category:string,software='') {
    const target=await this.plugin.storage.getLocal<SnapshotNode>(key('targets'));
    if(!target) throw new Error('todo_targets_not_synced');
    const root=await this.rem(target.id);
    if((await this.title(root))!==target.title) throw new Error('todo_root_changed');
    if(category==='todo') return {parent_id:root._id};
    const label=category==='shopping'?'购物清单':category==='information'?'信息清单':'';
    const branches=target.children.filter(n=>n.title===label);
    if(!label || branches.length!==1) throw new Error('todo_branch_unresolved');
    const branch=await this.rem(branches[0].id);
    if((await branch.getParentRem())?._id!==root._id || (await this.title(branch))!==label) throw new Error('todo_branch_changed');
    if(category==='shopping') return {parent_id:branch._id};
    if(!software.trim() || software.length>150) throw new Error('software_required');
    const matches=[];
    for(const child of await branch.getChildrenRem()) if((await this.title(child)).trim().toLocaleLowerCase()===software.trim().toLocaleLowerCase()) matches.push(child);
    if(matches.length>1) throw new Error('duplicate_software_branch');
    if(matches.length===1) return {parent_id:matches[0]._id,software:await this.title(matches[0])};
    const recordKey=key(`software:${branch._id}:${software.trim().toLocaleLowerCase()}`);
    const state=await this.ensureNode(recordKey,await this.plugin.storage.getLocal<NodeState>(recordKey),branch._id,[software.trim()]);
    return {parent_id:state.id,software:software.trim()};
  }
  async relocate(effect:string,operation:string,parentId:string,trashId:string,stripMetadata=true) {
    if(!/^[a-zA-Z0-9:._/-]{1,180}$/.test(effect) || !/^[a-zA-Z0-9:._/-]{1,180}$/.test(operation)) throw new Error('effect_key_invalid');
    const recordKey=key(`publish:${effect}`);
    const state=await this.plugin.storage.getLocal<WriteState>(recordKey);
    if(!state?.done || !state.root_id) throw new Error('completed_owned_outline_required');
    const source=JSON.parse(state.signature) as {parentId:string;tree:Outline;metadata:string};
    const repairKey=key(`relocate:${operation}`);
    const signature=JSON.stringify({effect,parentId,trashId,stripMetadata});
    const previous=await this.plugin.storage.getLocal<{signature:string;done?:boolean}>(repairKey);
    if(previous && previous.signature!==signature) throw new Error('effect_input_changed');
    await this.rem(parentId);
    const trash=await this.rem(trashId);
    if((await this.title(trash))!=='trash' || await trash.getParentRem()) throw new Error('root_trash_required');
    const root=await this.rem(state.root_id);
    const oldParent=(await root.getParentRem())?._id;
    if(!oldParent || ![state.current_parent??source.parentId,trashId,parentId].includes(oldParent)) throw new Error('owned_rem_moved');
    const metadataId=state.format==='content-only'?undefined:state.nodes['root/0']?.id;
    const metadataRem=metadataId?await this.rem(metadataId):undefined;
    if(metadataRem) {
      if(!(await this.plugin.richText.equals(metadataRem.text||[],await outlineRichText(this.plugin,source.metadata)))) throw new Error('owned_metadata_edited');
      if((await metadataRem.getChildrenRem()).length) throw new Error('owned_metadata_edited');
      if(![root._id,trashId].includes((await metadataRem.getParentRem())?._id??'')) throw new Error('owned_metadata_moved');
    }
    const readback:any[]=[];
    const verify=async(rem:PluginRem,want:Outline,isRoot=false):Promise<void>=>{
      if(!(await this.plugin.richText.equals(rem.text||[],await outlineRichText(this.plugin,want.text))) ||
         (rem.backText && !(await this.plugin.richText.empty(rem.backText,true)))) throw new Error('owned_rem_edited');
      const children=await rem.getChildrenRem();
      const body=isRoot?children.filter(n=>n._id!==metadataId):children;
      if(body.length!==want.children.length) throw new Error('owned_outline_children_changed');
      readback.push({id:rem._id,parent_id:(await rem.getParentRem())?._id??null,text:rem.text||[],back_text:rem.backText||[]});
      for(let i=0;i<body.length;i++) await verify(body[i],want.children[i]);
    };
    // Validate every body node before changing location or removing our metadata.
    await verify(root,source.tree,true);
    await this.checkSeparators(state,[state.current_parent??source.parentId,parentId]);
    await this.plugin.storage.setLocal(repairKey,{signature,done:false});
    if(stripMetadata && metadataRem && (await metadataRem.getParentRem())?._id===root._id) await metadataRem.setParent(trashId);
    if(oldParent!==parentId) await root.setParent(parentId);
    if((await (await this.rem(root._id)).getParentRem())?._id!==parentId) throw new Error('readback_mismatch');
    if(stripMetadata && metadataRem && (await metadataRem.getParentRem())?._id!==trashId) throw new Error('metadata_cleanup_readback_mismatch');
    readback.length=0;
    await verify(await this.rem(root._id),source.tree,true);
    const separators=await this.surround(effect,state,parentId);
    state.current_parent=parentId;
    if(stripMetadata) state.metadata_removed=true;
    await this.plugin.storage.setLocal(recordKey,state);
    await this.plugin.storage.setLocal(repairKey,{signature,done:true,root_id:root._id,parent_id:parentId});
    return {status:'verified',root_id:root._id,parent_id:parentId,separators,metadata_archived_id:stripMetadata?metadataId:undefined,reused:!!previous?.done,readback};
  }
  async publish(effect:string,parentId:string,tree:Outline,metadata:string) {
    if(!/^[a-zA-Z0-9:._/-]{1,180}$/.test(effect)) throw new Error('effect_key_invalid');
    let checked=0;
    const check=(node:Outline,depth:number)=>{
      if(++checked>799 || depth>15 || typeof node?.text!=='string' || !node.text.trim() || !Array.isArray(node.children)) throw new Error('outline_invalid');
      node.children.forEach(child=>check(child,depth+1));
    };
    check(tree,0);
    if(typeof metadata!=='string') throw new Error('metadata_invalid');
    await this.rem(parentId);
    const recordKey=key(`publish:${effect}`);
    const signature=JSON.stringify({parentId,tree,metadata});
    let state=await this.plugin.storage.getLocal<WriteState>(recordKey);
    if(state && state.signature!==signature) throw new Error('effect_input_changed');
    if(state?.done) return {root_id:state.root_id,status:'verified',reused:true,
      separators:await this.surround(effect,state,state.current_parent??parentId),
      readback:Object.values(state.nodes).map(n=>n.readback).filter(Boolean)};
    const root=tree;
    const prepared=new Map<string,RichTextInterface>();
    const prepare=async(node:Outline,address:string):Promise<void>=>{
      prepared.set(address,await outlineRichText(this.plugin,node.text));
      for(let i=0;i<node.children.length;i++) await prepare(node.children[i],`${address}/${i}`);
    };
    // A parser rejection must not leave a newly created partial outline.
    await prepare(root,'root');
    state ||= {signature,nodes:{},format:'content-only'};
    await this.plugin.storage.setLocal(recordKey,state);
    let count=0;
    const visit=async(node:Outline,parent:string,address:string,depth:number):Promise<string>=>{
      if(++count>800 || depth>16 || typeof node.text!=='string' || !Array.isArray(node.children)) throw new Error('outline_invalid');
      const text=prepared.get(address)!;
      const nodeKey=key(`publish-node:${effect}:${address}`);
      const stored=await this.plugin.storage.getLocal<NodeState>(nodeKey);
      const owned=await this.ensureNode(nodeKey,stored,parent,text);
      state!.nodes[address]=owned;
      await this.plugin.storage.setLocal(recordKey,state!);
      for(let i=0;i<node.children.length;i++) await visit(node.children[i],owned.id!,`${address}/${i}`,depth+1);
      return owned.id!;
    };
    state.root_id=await visit(root,parentId,'root',0);
    const separators=await this.surround(effect,state,parentId);
    state.done=true;
    await this.plugin.storage.setLocal(recordKey,state);
    return {root_id:state.root_id,status:'verified',node_count:count,separators,
      readback:Object.values(state.nodes).map(n=>n.readback).filter(Boolean)};
  }
  async dispatch(method:string,args:any) {
    switch(method) {
      case 'status': return {connected:true,protocol:1,plugin_version:'0.1.6',kb_id:(await this.plugin.kb.getCurrentKnowledgeBaseData())._id};
      case 'template_snapshot': return this.snapshot(args.rem_id);
      case 'targets_snapshot': return this.snapshot(args.rem_id,'targets');
      case 'search_documents': return this.search(args.queries);
      case 'create_document': return this.createDocument(args.effect_key,args.title,args.parent_id??null);
      case 'resolve_todo': return this.todo(args.category,args.software);
      case 'resolve_daily': return this.daily(args.date,args.branch_path);
      case 'resolve_meeting': return this.meeting(args.document_id,args.date);
      case 'relocate_outline': return this.relocate(args.effect_key,args.operation_id,args.parent_id,args.trash_id,args.strip_metadata??true);
      case 'publish_outline': return this.publish(args.effect_key,args.parent_id,args.tree,args.metadata);
      default: throw new Error('unsupported_method');
    }
  }
}
