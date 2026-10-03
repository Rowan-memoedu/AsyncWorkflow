import {describe,it,expect} from 'vitest';
import {WorkflowAPI} from '../src/api';

function fixture() {
  const values=new Map<string,any>(), nodes=new Map<string,any>();
  let creates=0, loseText=false, loseCreate=false, loseMove=false, dailyCalls=0;
  function reorder(ids:string[],parent:string,position:number) {
    const siblings=[...nodes.values()].filter(n=>n.parent===parent&&!ids.includes(n._id)).sort((a,b)=>a.order-b.order);
    const moved=ids.map(id=>nodes.get(id));
    siblings.splice(position,0,...moved);
    siblings.forEach((n,i)=>{n.parent=parent;n.order=i;});
    if(loseMove){loseMove=false;throw new Error('lost_move_reply');}
  }
  function node(id:string,text:any[]=[],parent?:string) {
    const value={_id:id,text,parent,isDoc:false,order:[...nodes.values()].filter(n=>n.parent===parent).length,
      getParentRem:async()=>nodes.get(value.parent),
      getChildrenRem:async()=>[...nodes.values()].filter(n=>n.parent===id).sort((a,b)=>a.order-b.order),
      setParent:async(p:string,position=0)=>reorder([id],p,position),
      setText:async(t:any)=>{value.text=t;if(loseText){loseText=false;throw new Error('lost_text_reply');}},
      isDocument:async()=>value.isDoc,setIsDocument:async(v:boolean)=>{value.isDoc=v;}};
    nodes.set(id,value);return value;
  }
  const parent=node('parent',['Target']);
  const plugin:any={
    storage:{getLocal:async(k:string)=>values.has(k)?structuredClone(values.get(k)):undefined,
      setLocal:async(k:string,v:any)=>{values.set(k,structuredClone(v));}},
    rem:{moveRems:async(ids:string[],p:string,position:number)=>reorder(ids,p,position),findOne:async(id:string)=>nodes.get(id),createRem:async()=>{
      const result=node('owned-'+(++creates));if(loseCreate){loseCreate=false;throw new Error('lost_create_reply');}return result;}},
    richText:{normalize:async(t:any)=>t,parseFromMarkdown:async(t:string)=>[t],toString:async(t:string[])=>t.join(''),
      equals:async(a:any,b:any)=>JSON.stringify(a)===JSON.stringify(b),empty:async(a:any)=>a.length===0},
    date:{getDailyDoc:async(date:Date)=>{dailyCalls++;return nodes.get('daily') || node('daily',[`${date.getFullYear()}-${date.getMonth()+1}-${date.getDate()}`]);}},
    kb:{getCurrentKnowledgeBaseData:async()=>({_id:'kb'})},
  };
  return {api:new WorkflowAPI(plugin),plugin,parent,node,nodes,values,counts:()=>({creates,dailyCalls}),
    loseText:()=>{loseText=true;},loseCreate:()=>{loseCreate=true;},loseMove:()=>{loseMove=true;}};
}
const outline={text:'Title :: literal → $x^2$',children:[{text:'Reason',children:[{text:'Example',children:[]}]}]};

describe('owned write recovery',()=>{
  it('reuses a completed effect after its response is lost',async()=>{
    const f=fixture(); const first=await f.api.publish('record/part','parent',outline,'source');
    const second=await f.api.publish('record/part','parent',outline,'source');
    expect(second.root_id).toBe(first.root_id);expect(f.counts().creates).toBe(5);
    expect(f.nodes.get(first.root_id).text).toEqual([outline.text]);
  });
  it('recovers a lost setText response without recreating the rem',async()=>{
    const f=fixture();f.loseText();
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow('lost_text_reply');
    expect((await f.api.publish('record/part','parent',outline,'source')).status).toBe('verified');
    expect(f.counts().creates).toBe(5);
  });
  it('never overwrites an edit after interruption',async()=>{
    const f=fixture();f.loseText();
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow();
    f.nodes.get('owned-1').text=['User edit'];
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow('owned_rem_edited');
    expect(f.nodes.get('owned-1').text).toEqual(['User edit']);
  });
  it('keeps an unknown create unknown instead of replaying',async()=>{
    const f=fixture();f.loseCreate();
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow();
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow('create_unknown_no_replay');
    expect(f.counts().creates).toBe(1);
  });
  it('rejects changed content for the same effect',async()=>{
    const f=fixture();await f.api.publish('record/part','parent',outline,'source');
    await expect(f.api.publish('record/part','parent',{...outline,text:'Changed'},'source')).rejects.toThrow('effect_input_changed');
  });
  it('validates all nodes before creating any',async()=>{
    const f=fixture();await expect(f.api.publish('record/part','parent',{text:'Root',children:[{text:'',children:[]}]},'source')).rejects.toThrow('outline_invalid');
    expect(f.counts().creates).toBe(0);
  });
  it('rejects a later parser failure before creating a partial outline',async()=>{
    const f=fixture();
    f.plugin.richText.parseFromMarkdown=async(text:string)=>text==='Example'?[{i:'s'}]:[text];
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow('unexpected_native_card_delimiter');
    expect(f.counts().creates).toBe(0);
  });
});

describe('content-only writes, documents and list targets',()=>{
  it('writes the original outline without a metadata child',async()=>{
    const f=fixture();const result=await f.api.publish('content','parent',outline,'private log only');
    expect((await f.nodes.get(result.root_id).getChildrenRem()).map((n:any)=>n.text)).toEqual([['Reason']]);
    expect([...f.nodes.values()].some(n=>n.text.includes('private log only'))).toBe(false);
  });
  it('creates a real root document once and preserves its ID on retry',async()=>{
    const f=fixture();const first=await f.api.createDocument('new-doc','Research notes');
    const again=await f.api.createDocument('new-doc','Research notes');
    expect(first.document_id).toBe(again.document_id);expect(again.is_document).toBe(true);
    expect(f.nodes.get(first.document_id).parent).toBeUndefined();expect(f.counts().creates).toBe(1);
  });
  it('uses an explicit parent and does not repeat an unknown creation',async()=>{
    const f=fixture();const r=await f.api.createDocument('child-doc','Scoped notes','parent');
    expect(f.nodes.get(r.document_id).parent).toBe('parent');
    f.loseCreate();await expect(f.api.createDocument('unknown','Other')).rejects.toThrow();
    await expect(f.api.createDocument('unknown','Other')).rejects.toThrow('create_unknown_no_replay');
    expect(f.counts().creates).toBe(2);
  });
  it('resolves scoped shopping and software branches without replacing the daily template',async()=>{
    const f=fixture();f.node('template',['日记模板']);f.node('other',['其他碎片'],'template');await f.api.snapshot('template');
    f.node('todo',['待办清单']);f.node('shop',['购物清单'],'todo');f.node('info',['信息清单'],'todo');f.node('mail',['Email'],'info');
    await f.api.snapshot('todo','targets');
    expect(await f.api.todo('shopping')).toEqual({parent_id:'shop'});
    expect(await f.api.todo('todo')).toEqual({parent_id:'todo'});
    expect((await f.api.todo('information','email')).parent_id).toBe('mail');
    const first=await f.api.todo('information','Calendar');
    expect((await f.api.todo('information','calendar')).parent_id).toBe(first.parent_id);
    expect((await f.api.daily('2026-10-03',['其他碎片'])).parent_id).toBeTruthy();
  });
});

async function legacyFixture() {
  const f=fixture();f.node('trash',['trash']);f.node('destination',['Correct destination']);
  const result=await f.api.publish('legacy','parent',outline,'source');
  f.node('legacy-meta',['source'],result.root_id);
  const stored=f.values.get('async-workflow-v1:publish:legacy');delete stored.format;
  stored.nodes['root/0']={id:'legacy-meta',phase:'ready',parent:result.root_id,text:['source']};
  return {...f,rootId:result.root_id};
}

describe('stage correction of owned results',()=>{
  it('keeps the existing body and root IDs, archives only metadata and moves the root',async()=>{
    const f=await legacyFixture();const before=[...f.nodes.keys()];
    const r=await f.api.relocate('legacy','repair-1','destination','trash');
    expect(r.root_id).toBe(f.rootId);expect(f.nodes.get('legacy-meta').parent).toBe('trash');
    expect(f.nodes.get(f.rootId).parent).toBe('destination');expect([...f.nodes.keys()]).toEqual(before);
    expect((await f.api.relocate('legacy','repair-1','destination','trash')).reused).toBe(true);
  });
  it('recovers a lost move reply through readback without repeating creation',async()=>{
    const f=await legacyFixture();f.loseMove();
    await expect(f.api.relocate('legacy','repair-2','destination','trash')).rejects.toThrow('lost_move_reply');
    expect((await f.api.relocate('legacy','repair-2','destination','trash')).status).toBe('verified');
    expect(f.counts().creates).toBe(5);
  });
  it('refuses to change a result with user-edited body text',async()=>{
    const f=await legacyFixture();f.nodes.get('owned-2').text=['User changed the note'];
    await expect(f.api.relocate('legacy','repair-3','destination','trash')).rejects.toThrow('owned_rem_edited');
    expect(f.nodes.get(f.rootId).parent).toBe('parent');expect(f.nodes.get('legacy-meta').parent).toBe(f.rootId);
  });
  it('refuses to remove metadata that has acquired user children',async()=>{
    const f=await legacyFixture();f.node('user-child',['My annotation'],'legacy-meta');
    await expect(f.api.relocate('legacy','repair-4','destination','trash')).rejects.toThrow('owned_metadata_edited');
    expect(f.nodes.get('legacy-meta').parent).toBe(f.rootId);
  });
});
describe('blank siblings around whole blocks',()=>{
  it('keeps two independent blocks and their pairs ordered with no duplicates on retry',async()=>{
    const f=fixture();f.node('earlier',['Earlier user note'],'parent');
    const a=await f.api.publish('a','parent',outline,'log');
    const b=await f.api.publish('b','parent',{text:'Another block',children:[]},'log');
    await f.api.publish('a','parent',outline,'log');
    expect((await f.parent.getChildrenRem()).map((n:any)=>n._id)).toEqual([
      'earlier',a.separators.before_id,a.root_id,a.separators.after_id,b.separators.before_id,b.root_id,b.separators.after_id]);
    expect(f.nodes.get(a.separators.before_id).text).toEqual([]);
    expect(f.nodes.get(a.separators.after_id).text).toEqual([]);
    expect(f.counts().creates).toBe(8);
  });
  it('moves the same owned separator pair with its block and protects edits to a blank',async()=>{
    const f=fixture();f.node('trash',['trash']);f.node('target',['Other']);
    const a=await f.api.publish('a','parent',outline,'log');
    const moved=await f.api.relocate('a','move','target','trash');
    expect(moved.separators.before_id).toBe(a.separators.before_id);
    expect((await f.parent.getChildrenRem()).length).toBe(0);
    f.nodes.get(a.separators.after_id).text=['My new note'];
    await expect(f.api.relocate('a','back','parent','trash')).rejects.toThrow('owned_separator_edited');
    expect(f.nodes.get(a.root_id).parent).toBe('target');
  });
});
describe('template and recording day',()=>{
  it('uses the requested calendar day and caches branch IDs',async()=>{
    const f=fixture();f.node('template',['日记模板']);f.node('other',['其他碎片'],'template');
    await f.api.snapshot('template');
    const one=await f.api.daily('2026-09-30',['其他碎片']);
    const two=await f.api.daily('2026-09-30',['其他碎片']);
    expect(one).toEqual(two);expect(f.counts()).toEqual({creates:1,dailyCalls:1});
    expect(f.nodes.get('daily').text).toEqual(['2026-9-30']);
  });
  it('does not reread the template after a user edit until explicit sync',async()=>{
    const f=fixture();f.node('template',['日记模板']);const child=f.node('other',['其他碎片'],'template');
    await f.api.snapshot('template');child.text=['New branch'];
    expect((await f.api.daily('2026-10-01',['其他碎片'])).parent_id).toBeTruthy();
    await f.api.snapshot('template');
    await expect(f.api.daily('2026-10-02',['其他碎片'])).rejects.toThrow('branch_not_in_template');
  });
  it('does not write to a cached branch moved elsewhere',async()=>{
    const f=fixture();f.node('template',['日记模板']);f.node('other',['其他碎片'],'template');
    await f.api.snapshot('template');const r=await f.api.daily('2026-10-01',['其他碎片']);
    f.nodes.get(r.parent_id).parent='parent';
    await expect(f.api.daily('2026-10-01',['其他碎片'])).rejects.toThrow('daily_branch_changed');
  });
  it('rejects invalid dates and unsupported operations',async()=>{
    const f=fixture();await expect(f.api.daily('2026-02-30',['其他碎片'])).rejects.toThrow('date_invalid');
    await expect(f.api.dispatch('delete_note',{})).rejects.toThrow('unsupported_method');
    expect(f.counts().creates).toBe(0);
  });
});
