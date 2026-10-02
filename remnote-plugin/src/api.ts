import type {ReactRNPlugin, PluginRem, RichTextInterface} from '@remnote/plugin-sdk';

type Outline = {text:string; children:Outline[]};
type NodeState = {id?:string; phase:'creating'|'created'|'ready'; parent:string; text:RichTextInterface};
type WriteState = {signature:string; done?:boolean; root_id?:string; nodes:Record<string,NodeState>};
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
  async snapshot(id:string) {
    let count=0;
    const visit=async(rem:PluginRem,depth:number):Promise<SnapshotNode>=>{
      if(depth>12 || ++count>500) throw new Error('template_too_large');
      const children=[];
      for(const child of await rem.getChildrenRem()) children.push(await visit(child,depth+1));
      return {id:rem._id,title:await this.title(rem),children};
    };
    const root=await visit(await this.rem(id),0);
    await this.plugin.storage.setLocal(key('template'),root);
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
  private async ensureNode(recordKey:string,state:NodeState|undefined,parent:string,text:RichTextInterface) {
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
    if(!actualParent) {
      if(state.phase==='ready') throw new Error('owned_rem_moved');
      await rem.setParent(parent);
    }
    const read=await this.rem(state.id);
    if((await read.getParentRem())?._id!==parent || !(await this.plugin.richText.equals(read.text||[],text))) throw new Error('readback_mismatch');
    state.phase='ready';
    await this.plugin.storage.setLocal(recordKey,state);
    return state;
  }
  async publish(effect:string,parentId:string,tree:Outline,metadata:string) {
    if(!/^[a-zA-Z0-9:._/-]{1,180}$/.test(effect)) throw new Error('effect_key_invalid');
    let checked=0;
    const check=(node:Outline,depth:number)=>{
      if(++checked>799 || depth>15 || typeof node?.text!=='string' || !node.text.trim() || !Array.isArray(node.children)) throw new Error('outline_invalid');
      node.children.forEach(child=>check(child,depth+1));
    };
    check(tree,0);
    if(typeof metadata!=='string' || !metadata.trim()) throw new Error('metadata_required');
    await this.rem(parentId);
    const recordKey=key(`publish:${effect}`);
    const signature=JSON.stringify({parentId,tree,metadata});
    let state=await this.plugin.storage.getLocal<WriteState>(recordKey);
    if(state && state.signature!==signature) throw new Error('effect_input_changed');
    if(state?.done) return {root_id:state.root_id,status:'verified',reused:true};
    state ||= {signature,nodes:{}};
    await this.plugin.storage.setLocal(recordKey,state);
    let count=0;
    const visit=async(node:Outline,parent:string,address:string,depth:number):Promise<string>=>{
      if(++count>800 || depth>16 || typeof node.text!=='string' || !Array.isArray(node.children)) throw new Error('outline_invalid');
      const text=await this.plugin.richText.normalize(await this.plugin.richText.parseFromMarkdown(node.text));
      const nodeKey=key(`publish-node:${effect}:${address}`);
      const stored=await this.plugin.storage.getLocal<NodeState>(nodeKey);
      const owned=await this.ensureNode(nodeKey,stored,parent,text);
      state!.nodes[address]=owned;
      await this.plugin.storage.setLocal(recordKey,state!);
      for(let i=0;i<node.children.length;i++) await visit(node.children[i],owned.id!,`${address}/${i}`,depth+1);
      return owned.id!;
    };
    const root={text:tree.text,children:[{text:metadata,children:[]},...tree.children]};
    state.root_id=await visit(root,parentId,'root',0);
    state.done=true;
    await this.plugin.storage.setLocal(recordKey,state);
    return {root_id:state.root_id,status:'verified',node_count:count};
  }
  async dispatch(method:string,args:any) {
    switch(method) {
      case 'status': return {connected:true,protocol:1,kb_id:(await this.plugin.kb.getCurrentKnowledgeBaseData())._id};
      case 'template_snapshot': return this.snapshot(args.rem_id);
      case 'search_documents': return this.search(args.queries);
      case 'resolve_daily': return this.daily(args.date,args.branch_path);
      case 'publish_outline': return this.publish(args.effect_key,args.parent_id,args.tree,args.metadata);
      default: throw new Error('unsupported_method');
    }
  }
}
