import {declareIndexPlugin, type ReactRNPlugin} from '@remnote/plugin-sdk';
import {WorkflowAPI} from './api';
import {CommandInboxAPI} from './command-inbox';

let socket:WebSocket|undefined;
let stopped=false;
let timer:ReturnType<typeof setTimeout>|undefined;
async function activate(plugin:ReactRNPlugin) {
  stopped=false;
  const api=new WorkflowAPI(plugin);
  const commands=new CommandInboxAPI(plugin);
  let chain=Promise.resolve();
  async function connect() {
    if(stopped) return;
    try {
      const token=(globalThis as typeof globalThis & {__ASYNC_BRIDGE_TOKEN?:string}).__ASYNC_BRIDGE_TOKEN;
      if(!token) throw new Error('plugin_frame_credential_missing');
      const kb=await plugin.kb.getCurrentKnowledgeBaseData();
      const current=new WebSocket('ws://localhost:43141/plugin-socket');
      socket=current;
      current.onopen=()=>current.send(JSON.stringify({kind:'hello',token,kb_id:kb._id,protocol:1}));
      current.onmessage=(event)=>{
        const request=JSON.parse(event.data);
        if(!request.id || !request.method) return;
        const run=async()=>{
          try {
            const handler=request.method.startsWith('command_inbox_')?commands:api;
            const result=await handler.dispatch(request.method,request.args || {});
            if(current.readyState===WebSocket.OPEN) current.send(JSON.stringify({id:request.id,result}));
          } catch(error) {
            if(current.readyState===WebSocket.OPEN) current.send(JSON.stringify({id:request.id,error:error instanceof Error?error.message:'plugin_error'}));
          }
        };
        // A slow write or diagnostic must not block the read-only health probe.
        if(request.method==='status') void run();
        else chain=chain.then(run);
      };
      current.onclose=()=>{if(!stopped)timer=setTimeout(connect,5000);};
      current.onerror=()=>current.close();
    } catch {if(!stopped)timer=setTimeout(connect,5000);}
  }
  await connect();
}
async function deactivate() {stopped=true;if(timer)clearTimeout(timer);socket?.close();}
declareIndexPlugin(activate,deactivate);
