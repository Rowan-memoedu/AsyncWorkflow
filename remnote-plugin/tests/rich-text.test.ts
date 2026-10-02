import {describe,it,expect} from 'vitest';
import {outlineRichText} from '../src/rich-text';

function sdk(parse:(source:string)=>unknown) {
  return {richText:{parseFromMarkdown:async(source:string)=>parse(source),normalize:async(value:unknown)=>value}} as any;
}

describe('literal outline text through the SDK parser',()=>{
  it('does not turn a literal colon pair into the observed native card separator',async()=>{
    const plugin=sdk(source=>source.includes('::') || source.includes('→')?['字面 ',{i:'s'},' 以及']:[source]);
    expect(await outlineRichText(plugin,'字面 :: 以及 →')).toEqual(['字面 :: 以及 →']);
  });
  it('preserves independent Unicode and ASCII direction notation',async()=>{
    const source='原因 ⇔ 结果；返回 ← 来源；映射 ⟶ 值；A -> B；C <- D';
    const plugin=sdk(text=>/[⇔←⟶]|->|<-/.test(text)?[{i:'s'}]:[{i:'m',text}]);
    expect(await outlineRichText(plugin,source)).toEqual([{i:'m',text:source}]);
  });
  it('restores independent delimiter cases inside formatted and mathematical text',async()=>{
    const plugin=sdk(source=>[{i:'m',text:source},{i:'a',url:source}]);
    const source='A >> B；C ;; D；x==y；{{注释}}；$a<<b$';
    expect(await outlineRichText(plugin,source)).toEqual([{i:'m',text:source},{i:'a',url:source}]);
  });
  it('does not collide with text resembling internal placeholders',async()=>{
    const source='AWLITERALX0Z :: AWLITERALXX1Z';
    expect(await outlineRichText(sdk(source=>[source]),source)).toEqual([source]);
  });
  it('fails visibly if an unsupported native separator survives parsing',async()=>{
    await expect(outlineRichText(sdk(()=>[{i:'s'}]),'unrecognized structure')).rejects.toThrow('unexpected_native_card_delimiter');
  });
});
