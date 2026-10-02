import type {ReactRNPlugin} from '@remnote/plugin-sdk';

export interface CommandNode {
  id:string; text:string; prompt_text:string; raw_text:unknown; raw_back:unknown;
  children:CommandNode[];
}
type Scope={document_id:string;inbox_id:string;archive_id:string};
type ArchiveArgs=Scope & {effect_key:string;thread_id:string;turn_id:string;nodes:CommandNode[]};
const equal=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);
const blank=(n:CommandNode)=>!n.text.trim()&&!n.prompt_text.trim()&&!n.children.length;

/** Narrow inbox API. No MCP imports, LLM interpretation or generic mutation API. */
export class CommandInboxAPI {
  constructor(private plugin:ReactRNPlugin) {}
  private async rem(id:string) {
    if(typeof id!=='string'||!id)throw new Error('command_id_invalid');
    const rem=await this.plugin.rem.findOne(id);
    if(!rem)throw new Error('command_rem_missing');
    return rem;
  }
  private async scope(args:Scope) {
    if(new Set([args.document_id,args.inbox_id,args.archive_id]).size!==3)throw new Error('command_scope_invalid');
    const document=await this.rem(args.document_id),inbox=await this.rem(args.inbox_id),archive=await this.rem(args.archive_id);
    if(!await document.isDocument() || (await inbox.getParentRem())?._id!==document._id ||
       (await archive.getParentRem())?._id!==document._id)throw new Error('command_scope_changed');
    const title=async(r:typeof document)=>this.plugin.richText.toString(r.text||[]);
    if(await title(document)!=='codex命令'||await title(inbox)!=='一次性命令'||await title(archive)!=='已递交')
      throw new Error('command_scope_names_changed');
    return {document,inbox,archive};
  }
  private async tree(id:string,budget={left:4000},depth=0):Promise<CommandNode> {
    if(--budget.left<0||depth>40)throw new Error('command_tree_limit');
    const rem=await this.rem(id),raw=rem.text||[],back=rem.backText||[];
    // Card backs are not silently dropped from command text.
    if((await this.plugin.richText.toString(back)).length)throw new Error('command_card_back_unsupported');
    const children=[];
    for(const child of await rem.getChildrenRem())children.push(await this.tree(child._id,budget,depth+1));
    const text=await this.plugin.richText.toString(raw);
    const prompt_text=raw.every(x=>typeof x==='string')?raw.join(''):await this.plugin.richText.toMarkdown(raw);
    return {id,text,prompt_text,raw_text:raw,raw_back:back,children};
  }
  async read(args:Scope) {
    const {document,inbox,archive}=await this.scope(args),rules=[];
    // Only direct rule nodes; unrelated project sections are never command inputs.
    for(const child of await document.getChildrenRem()) {
      if([inbox._id,archive._id].includes(child._id))continue;
      const text=await this.plugin.richText.toString(child.text||[]);
      if(text.includes('一次性命令'))rules.push(await this.tree(child._id));
    }
    return {protocol:'command-inbox-1',document_id:document._id,rules,
      inbox:await this.tree(inbox._id),archive_ids:(await archive.getChildrenRem()).map(x=>x._id)};
  }
  async archive(args:ArchiveArgs) {
    const {inbox,archive}=await this.scope(args);
    if(!/^[a-f0-9]{64}$/.test(args.effect_key)||!args.thread_id||!args.turn_id||
       !Array.isArray(args.nodes)||!args.nodes.length||!args.nodes[0].text.trimStart().startsWith('%'))
      throw new Error('command_archive_invalid');
    const ids=args.nodes.map(n=>n.id);
    if(new Set(ids).size!==ids.length||ids.some(id=>[args.document_id,inbox._id,archive._id].includes(id)))
      throw new Error('command_archive_invalid');
    // One block plus optional trailing delimiter; never move two commands at once.
    if(args.nodes.slice(0,-1).some(blank))throw new Error('command_archive_multiple_blocks');
    const storageKey='command-inbox/'+args.effect_key;
    const signature=JSON.stringify(args);
    let saved:any=await this.plugin.storage.getLocal(storageKey);
    if(saved&&saved.signature!==signature)throw new Error('command_archive_input_changed');
    const parents=[];
    for(const node of args.nodes) {
      if(!equal(await this.tree(node.id),node))throw new Error('command_source_edited');
      const parent=(await (await this.rem(node.id)).getParentRem())?._id;
      if(parent!==inbox._id&&parent!==archive._id)throw new Error('command_source_moved');
      parents.push(parent);
    }
    if(saved?.done) {
      if(parents.some(p=>p!==archive._id))throw new Error('command_archive_moved_after_completion');
      const order=(await archive.getChildrenRem()).map(x=>x._id),start=order.indexOf(ids[0]);
      if(start<0||!equal(order.slice(start,start+ids.length),ids))throw new Error('command_archive_order_changed');
      return {status:'verified',ids,thread_id:args.thread_id,turn_id:args.turn_id};
    }
    if(!saved) {
      // Validate the entire source block and its sibling boundaries before the first move.
      const children=await inbox.getChildrenRem(),sourceIds=children.map(x=>x._id);
      const start=sourceIds.indexOf(ids[0]);
      if(start<0||!equal(sourceIds.slice(start,start+ids.length),ids)||parents.some(p=>p!==inbox._id))
        throw new Error('command_source_order_changed');
      if(start>0&&!blank(await this.tree(sourceIds[start-1])))throw new Error('command_start_boundary_changed');
      const after=sourceIds[start+ids.length];
      if(after&&!blank(args.nodes.at(-1)!)&&!blank(await this.tree(after)))throw new Error('command_end_boundary_changed');
      const archived=await archive.getChildrenRem();
      saved={signature,anchor:archived.at(-1)?._id||null,separator:null,create_pending:false,done:false};
      await this.plugin.storage.setLocal(storageKey,saved);
    }
    // Supply a delimiter if there was no trailing source blank in the preceding block.
    let anchor=saved.anchor as string|null;
    if(anchor&&!blank(await this.tree(anchor))) {
      if(saved.create_pending&&!saved.separator)throw new Error('command_separator_create_unknown_no_replay');
      if(!saved.separator) {
        saved.create_pending=true;await this.plugin.storage.setLocal(storageKey,saved);
        const separator=await this.plugin.rem.createRem();
        if(!separator)throw new Error('command_separator_create_failed');
        saved.separator=separator._id;await this.plugin.storage.setLocal(storageKey,saved);
      }
      const separator=await this.rem(saved.separator);
      if(!blank(await this.tree(separator._id)))throw new Error('command_separator_edited');
      const parent=(await separator.getParentRem())?._id;
      if(parent&&parent!==archive._id)throw new Error('command_separator_moved');
      if(!parent) {
        const siblings=await archive.getChildrenRem(),index=siblings.findIndex(x=>x._id===anchor);
        if(index<0)throw new Error('command_archive_anchor_missing');
        await separator.setParent(archive._id,index+1);
      }
      anchor=separator._id;
    }
    for(const node of args.nodes) {
      // Recheck immediately before each side effect to preserve concurrent edits.
      if(!equal(await this.tree(node.id),node))throw new Error('command_source_edited');
      const rem=await this.rem(node.id),parent=(await rem.getParentRem())?._id;
      const siblings=await archive.getChildrenRem(),position=anchor?siblings.findIndex(x=>x._id===anchor)+1:0;
      if(anchor&&position===0)throw new Error('command_archive_anchor_missing');
      if(parent===inbox._id)await rem.setParent(archive._id,position);
      else if(parent!==archive._id||siblings[position]?._id!==node.id)throw new Error('command_archive_order_changed');
      anchor=node.id;
    }
    const resultIds=(await archive.getChildrenRem()).map(x=>x._id),start=resultIds.indexOf(ids[0]);
    if(start<0||!equal(resultIds.slice(start,start+ids.length),ids))throw new Error('command_archive_readback_failed');
    for(const node of args.nodes)if(!equal(await this.tree(node.id),node))throw new Error('command_archive_readback_failed');
    saved.done=true;await this.plugin.storage.setLocal(storageKey,saved);
    return {status:'verified',ids,thread_id:args.thread_id,turn_id:args.turn_id};
  }
  async dispatch(method:string,args:any) {
    if(method==='command_inbox_read')return this.read(args);
    if(method==='command_inbox_archive')return this.archive(args);
    throw new Error('unsupported_method');
  }
}
