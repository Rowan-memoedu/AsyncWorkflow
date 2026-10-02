import type {ReactRNPlugin,RichTextInterface} from '@remnote/plugin-sdk';

// RemNote's Markdown parser recognizes native card delimiters as structure.
// This workflow archives outlines, so preserve those characters as literal
// text while still using the official parser for Markdown and LaTeX.
export async function outlineRichText(plugin:ReactRNPlugin,source:string):Promise<RichTextInterface> {
  let prefix='AWLITERALX';
  while(source.includes(prefix)) prefix+='X';
  const literals:string[]=[];
  const protectedText=source.replace(/:{2,}|;{2,}|[:;][<>-]+|[<>]{2,}|>-|={2,}|\{\{|\}\}/g,(value)=>{
    const index=literals.push(value)-1;
    return `${prefix}${index}Z`;
  });
  const parsed=await plugin.richText.parseFromMarkdown(protectedText);
  const restore=(value:unknown):unknown=>{
    if(typeof value==='string') return value.replace(new RegExp(`${prefix}(\\d+)Z`,'g'),(_match,index)=>literals[Number(index)]);
    if(Array.isArray(value)) return value.map(restore);
    if(value && typeof value==='object') {
      if('i' in value && value.i==='s') throw new Error('unexpected_native_card_delimiter');
      return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,restore(item)]));
    }
    return value;
  };
  const result=await plugin.richText.normalize(restore(parsed) as RichTextInterface);
  if(JSON.stringify(result).includes(prefix)) throw new Error('literal_restoration_failed');
  return result;
}
