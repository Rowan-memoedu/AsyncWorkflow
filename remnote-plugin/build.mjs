import {build} from 'esbuild';
import {mkdir, copyFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const out = resolve(process.env.ASYNC_PLUGIN_OUTPUT || 'D:/Artifacts/AsyncWorkflow/remnote-plugin');
await mkdir(out, {recursive:true});
for (const sandbox of [false, true]) {
  await build({entryPoints:['src/index.ts'], bundle:true, minify:true, format:'esm', target:'es2020',
    outfile:resolve(out,sandbox?'index-sandbox.js':'index.js'),
    banner:sandbox?{}:{js:'const IMPORT_META=import.meta;'},
    define:{'process.env.NODE_ENV':'"production"'}});
}
await copyFile('public/manifest.json',resolve(out,'manifest.json'));
await writeFile(resolve(out,'index.html'),`<!doctype html><meta charset="utf-8"><title>异步工作流</title><body><script type="module">
const widget=new URLSearchParams(location.search).get('widgetName');
if(widget==='index'){await import('./index-sandbox.js');}else{document.body.textContent='异步工作流插件已就绪，请在 RemNote 中加载此插件地址。';}
</script></body>`);
console.log(`Independent plugin built: ${out}`);
