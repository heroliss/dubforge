import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createRenderer,ref} from 'vue'
let server:ViteDevServer,useTrace:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  useTrace=(await server.ssrLoadModule('/src/composables/useLlmTrace.ts')).useLlmTrace
})
after(async()=>{mock.restoreAll();await server.close()})
const host=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),setText(){},setElementText(){},patchProp(){},parentNode(){return null},nextSibling(){return null},remove(){},insert(){}})
async function flush(){for(let i=0;i<15;i++)await Promise.resolve()}
async function harness(t:any){
  const id=ref('a'),enabled=ref(false);let trace:any
  const app=host.createApp({setup(){trace=useTrace(id,enabled);return()=>null}})
  app.mount({});t.after(()=>app.unmount())
  return{id,enabled,trace}
}
test('只读游标增量加载与新轮重置，不发送模型重试',async t=>{
  const {trace,enabled}=await harness(t),calls:any[]=[]
  const responses=[{id:'a',events:[{event:'request',content:'输入'}],cursor:100,trace_id:'t',reset:true,active:true},
    {id:'a',events:[{event:'assistant_response',content:'输出'}],cursor:200,trace_id:'t',active:false,status:'completed'},
    {id:'a',events:[{event:'request',content:'新轮'}],cursor:80,trace_id:'u',reset:true}]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return{ok:true,json:async()=>responses.shift()}})
  enabled.value=true;await flush();await trace.load();assert.equal(trace.events.value.length,2)
  assert.match(calls[1][0],/after=100&trace_id=t/);assert.equal(trace.status.value,'completed')
  await trace.load();assert.equal(trace.events.value.length,1);assert.equal(trace.events.value[0].content,'新轮')
  assert.ok(calls.every(([,options])=>!options.method&&!options.body))
})
test('切换对话或关闭观察时中止旧读取，迟到记录不能混入新对话',async t=>{
  const {id,enabled,trace}=await harness(t);let release:any,signal:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).includes('/a?')){signal=options.signal;return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({events:[{content:'旧记录'}],cursor:10})})})}
    return{ok:true,json:async()=>({events:[{content:'新记录'}],cursor:10,trace_id:'b'})}
  })
  enabled.value=true;await flush();id.value='b';assert.equal(signal.aborted,true)
  await flush();release();await flush()
  assert.equal(trace.events.value[0].content,'新记录')
  enabled.value=false;await trace.load();assert.equal(trace.events.value.length,0)
})
test('读取失败保留已看到的记录，双击刷新不会堆积请求',async t=>{
  const {trace,enabled}=await harness(t);let release:any,calls=0
  trace.events.value=[{content:'已读'}]
  t.mock.method(globalThis,'fetch',()=>{calls++;return new Promise(resolve=>{release=()=>resolve({ok:false,json:async()=>({message:'读取失败'})})})})
  enabled.value=true;trace.events.value=[{content:'已读'}];await trace.load();assert.equal(calls,1)
  release();await flush();assert.equal(trace.events.value[0].content,'已读');assert.equal(trace.error.value,'读取失败')
})
