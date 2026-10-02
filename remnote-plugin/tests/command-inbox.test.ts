import {describe,it,expect} from 'vitest';
import {CommandInboxAPI} from '../src/command-inbox';

function fixture() {
  const nodes=new Map<string,any>(),storage=new Map<string,any>();
  let creates=0,loseMove=false,loseCreate=false;
  function node(id:string,text:string,parent?:string) {
    const n:any={_id:id,text:[text],backText:[],parent,children:[],
      isDocument:async()=>id==='doc',getParentRem:async()=>nodes.get(n.parent),
      getChildrenRem:async()=>n.children.map((key:string)=>nodes.get(key)),
      setParent:async(p:string,index?:number)=>{
        if(n.parent)nodes.get(n.parent).children=nodes.get(n.parent).children.filter((x:string)=>x!==id);
        n.parent=p;nodes.get(p).children.splice(index??nodes.get(p).children.length,0,id);
        if(loseMove){loseMove=false;throw new Error('lost_move_reply');}
      }};
    nodes.set(id,n);if(parent)nodes.get(parent).children.push(id);return n;
  }
  node('doc','codex命令');node('in','一次性命令','doc');node('out','已递交','doc');
  node('rule','一次性命令分隔规则','doc');node('model','一次性命令当前要求使用的模型：6.1-sol high','rule');
  node('a','%原文第一条','in');node('detail','子节点','a');node('b','补充原文','in');node('gap','','in');node('draft','草稿','in');
  const plugin:any={rem:{findOne:async(id:string)=>nodes.get(id),createRem:async()=>{
    const n=node('blank-'+(++creates),'');if(loseCreate){loseCreate=false;throw new Error('lost_create_reply');}return n;}},
    richText:{toString:async(t:string[])=>t.join(''),toMarkdown:async(t:string[])=>t.join('')},
    storage:{getLocal:async(k:string)=>structuredClone(storage.get(k)),setLocal:async(k:string,v:any)=>{storage.set(k,structuredClone(v));}}};
  const api=new CommandInboxAPI(plugin),scope={document_id:'doc',inbox_id:'in',archive_id:'out'};
  async function args(){const read=await api.read(scope);return {...scope,effect_key:'a'.repeat(64),thread_id:'thread',turn_id:'turn',nodes:read.inbox.children.slice(0,3)};}
  return {api,scope,args,node,nodes,storage,creates:()=>creates,
    loseMove:()=>{loseMove=true;},loseCreate:()=>{loseCreate=true;}};
}

describe('mechanical command inbox',()=>{
  it('moves original IDs and complete subtrees in order, once',async()=>{
    const f=fixture(),args=await f.args();
    expect((await f.api.archive(args)).status).toBe('verified');
    expect(f.nodes.get('out').children).toEqual(['a','b','gap']);
    expect(f.nodes.get('in').children).toEqual(['draft']);
    expect(f.nodes.get('detail').parent).toBe('a');
    await f.api.archive(args);expect(f.creates()).toBe(0);
  });
  it('recovers a move that succeeded but lost its response',async()=>{
    const f=fixture(),args=await f.args();f.loseMove();
    await expect(f.api.archive(args)).rejects.toThrow('lost_move_reply');
    await f.api.archive(args);
    expect(f.nodes.get('out').children).toEqual(['a','b','gap']);
  });
  it('rejects stale content before moving any source',async()=>{
    const f=fixture(),args=await f.args();f.nodes.get('detail').text=['用户修改'];
    await expect(f.api.archive(args)).rejects.toThrow('source_edited');
    expect(f.nodes.get('out').children).toEqual([]);
  });
  it('refuses a scope moved outside the source document',async()=>{
    const f=fixture(),args=await f.args();f.node('other','other');await f.nodes.get('out').setParent('other');
    await expect(f.api.archive(args)).rejects.toThrow('scope_changed');
  });
  it('does not accept a partial block with a nonblank successor',async()=>{
    const f=fixture(),args=await f.args();args.nodes=args.nodes.slice(0,1);
    await expect(f.api.archive(args)).rejects.toThrow('end_boundary_changed');
    expect(f.nodes.get('out').children).toEqual([]);
  });
  it('creates exactly one blank separator after existing archive content',async()=>{
    const f=fixture();f.node('old','previous command','out');const args=await f.args();
    await f.api.archive(args);await f.api.archive(args);
    expect(f.nodes.get('out').children).toEqual(['old','blank-1','a','b','gap']);
    expect(f.creates()).toBe(1);
  });
  it('never duplicates a separator after unknown create',async()=>{
    const f=fixture();f.node('old','previous','out');const args=await f.args();f.loseCreate();
    await expect(f.api.archive(args)).rejects.toThrow('lost_create_reply');
    await expect(f.api.archive(args)).rejects.toThrow('create_unknown_no_replay');
    expect(f.creates()).toBe(1);expect(f.nodes.get('a').parent).toBe('in');
  });
  it('refuses edits or parent moves during partial recovery',async()=>{
    const f=fixture(),args=await f.args();f.loseMove();
    await expect(f.api.archive(args)).rejects.toThrow();f.nodes.get('b').text=['changed'];
    await expect(f.api.archive(args)).rejects.toThrow('source_edited');
  });
  it('does not read unrelated projects or alter the template cache',async()=>{
    const f=fixture();f.node('projects','项目','doc');const bad=f.node('irrelevant','无关','projects');bad.backText=['not a command'];
    const result=await f.api.read(f.scope);
    expect(result.rules.map(n=>n.id)).toEqual(['rule']);expect(f.storage.size).toBe(0);
  });
  it('detects a reordered archive instead of reporting a false readback',async()=>{
    const f=fixture(),args=await f.args();await f.api.archive(args);
    f.nodes.get('out').children=['b','a','gap'];
    await expect(f.api.archive(args)).rejects.toThrow('archive_order_changed');
  });
});
