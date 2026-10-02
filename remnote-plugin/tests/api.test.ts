import {describe,it,expect} from 'vitest';
import {WorkflowAPI} from '../src/api';

function fixture() {
  const values=new Map<string,any>(), nodes=new Map<string,any>();
  let creates=0, loseText=false, loseCreate=false, dailyCalls=0;
  function node(id:string,text:any[]=[],parent?:string) {
    const value={_id:id,text,parent,
      getParentRem:async()=>nodes.get(value.parent),
      getChildrenRem:async()=>[...nodes.values()].filter(n=>n.parent===id),
      setParent:async(p:string)=>{value.parent=p;},
      setText:async(t:any)=>{value.text=t;if(loseText){loseText=false;throw new Error('lost_text_reply');}},
      isDocument:async()=>true};
    nodes.set(id,value);return value;
  }
  const parent=node('parent',['Target']);
  const plugin:any={
    storage:{getLocal:async(k:string)=>values.has(k)?structuredClone(values.get(k)):undefined,
      setLocal:async(k:string,v:any)=>{values.set(k,structuredClone(v));}},
    rem:{findOne:async(id:string)=>nodes.get(id),createRem:async()=>{
      const result=node('owned-'+(++creates));if(loseCreate){loseCreate=false;throw new Error('lost_create_reply');}return result;}},
    richText:{normalize:async(t:any)=>t,parseFromMarkdown:async(t:string)=>[t],toString:async(t:string[])=>t.join(''),
      equals:async(a:any,b:any)=>JSON.stringify(a)===JSON.stringify(b),empty:async(a:any)=>a.length===0},
    date:{getDailyDoc:async(date:Date)=>{dailyCalls++;return nodes.get('daily') || node('daily',[`${date.getFullYear()}-${date.getMonth()+1}-${date.getDate()}`]);}},
    kb:{getCurrentKnowledgeBaseData:async()=>({_id:'kb'})},
  };
  return {api:new WorkflowAPI(plugin),plugin,parent,node,nodes,values,counts:()=>({creates,dailyCalls}),
    loseText:()=>{loseText=true;},loseCreate:()=>{loseCreate=true;}};
}
const outline={text:'Title :: literal → $x^2$',children:[{text:'Reason',children:[{text:'Example',children:[]}]}]};

describe('owned write recovery',()=>{
  it('reuses a completed effect after its response is lost',async()=>{
    const f=fixture(); const first=await f.api.publish('record/part','parent',outline,'source');
    const second=await f.api.publish('record/part','parent',outline,'source');
    expect(second.root_id).toBe(first.root_id);expect(f.counts().creates).toBe(4);
    expect(f.nodes.get(first.root_id).text).toEqual([outline.text]);
  });
  it('recovers a lost setText response without recreating the rem',async()=>{
    const f=fixture();f.loseText();
    await expect(f.api.publish('record/part','parent',outline,'source')).rejects.toThrow('lost_text_reply');
    expect((await f.api.publish('record/part','parent',outline,'source')).status).toBe('verified');
    expect(f.counts().creates).toBe(4);
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
