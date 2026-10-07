import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

// 执行真实共享器，只替换生命周期注册；不装载界面或访问真实后端。
const root = fileURLToPath(new URL('..', import.meta.url))
const bundle = await build({ stdin: { contents: `export * from './src/utils/configurationEditability';
 export { mountAll, disposeAll, ref } from 'vue';`, loader:'ts', resolveDir:root },
 bundle:true, write:false, format:'esm', platform:'node', plugins:[{name:'lifecycle', setup(build) {
  build.onResolve({filter:/^vue$/}, () => ({path:'vue',namespace:'fixture'}))
  build.onLoad({filter:/.*/,namespace:'fixture'}, () => ({resolveDir:root,contents:`
   export {ref,computed,watch} from '@vue/reactivity';
   const mounted=[],disposed=[];
   export const onMounted=fn=>mounted.push(fn),onUnmounted=fn=>disposed.push(fn);
   export const mountAll=()=>mounted.splice(0).forEach(fn=>fn());
   export const disposeAll=()=>disposed.splice(0).forEach(fn=>fn());`}))
 }}] })
let count=0
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve()}
async function fixture(t:any) {
 const api=await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text+`\n//fixture${count++}`).toString('base64')}`)
 const requests:any[]=[],timers:Function[]=[],cleared:any[]=[]
 t.mock.method(globalThis,'fetch',(url:any,options:any)=>new Promise(resolve=>requests.push({url,options,resolve})))
 t.mock.method(globalThis,'setInterval',((fn:Function)=>{timers.push(fn);return timers.length}) as any)
 t.mock.method(globalThis,'clearInterval',(id:any)=>{cleared.push(id)})
 t.after(()=>api.disposeAll())
 const respond=async(i:number,value:any,status=200)=>{requests[i].resolve(new Response(JSON.stringify(value),{status}));await flush()}
 return {api,requests,timers,cleared,respond}
}

test('同scope首读关闭编辑且合并请求；允许响应后可编辑，保存前重新核对',async t=>{
 const {api,requests,respond}=await fixture(t)
 const a=api.useConfigurationEditability('project:p'),b=api.useConfigurationEditability('project:p')
 assert.equal(requests.length,0)
 api.mountAll()
 assert.equal(a.locked.value,true);assert.match(a.reason.value,/核对/)
 assert.equal(requests.length,1)
 await respond(0,{locked:false})
 assert.equal(a.locked.value,false);assert.equal(b.locked.value,false)
 const first=a.canEdit(),second=b.canEdit()
 assert.equal(requests.length,2)
 await respond(1,{locked:true,reason:'正在上传，结束后才能编辑'})
 assert.deepEqual(await Promise.all([first,second]),[false,false])
 assert.match(b.reason.value,/上传/)
})

test('接口失败、错误schema仍关闭编辑；重试恢复但不把monitoring自行当运行',async t=>{
 const {api,requests,respond}=await fixture(t),a=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{message:'后端断线'},503)
 assert.equal(a.locked.value,true);assert.match(a.reason.value,/未能核对/)
 const retry=a.refresh();await respond(1,{locked:false});assert.equal(await retry,true)
 assert.equal(a.locked.value,false)
 const invalid=a.refresh();await respond(2,{status:'monitoring'});assert.equal(await invalid,false)
 assert.equal(a.locked.value,true)
})

test('项目本地运行立即收紧同项目和全局权限，其他项目不受影响；暂停旧nav后服从服务端',async t=>{
 const {api,respond}=await fixture(t)
 const a=api.useConfigurationEditability('project:a'),b=api.useConfigurationEditability('project:b'),global=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{locked:false});await respond(1,{locked:false});await respond(2,{locked:false})
 api.setNavigationExecution('a',true)
 assert.equal(a.locked.value,true);assert.equal(global.locked.value,true);assert.equal(b.locked.value,false)
 api.setNavigationExecution('a',true,true)
 assert.equal(a.locked.value,false);assert.equal(global.locked.value,false)
})

test('切换scope取消旧请求，忽略旧结果；卸载取消在途核对并清理轮询',async t=>{
 const {api,requests,respond,cleared}=await fixture(t),scope=api.ref('project:old'),a=api.useConfigurationEditability(scope)
 api.mountAll();scope.value='project:new'
 assert.equal(requests[0].options.signal.aborted,true)
 await respond(0,{locked:false});assert.equal(a.locked.value,true)
 await respond(1,{locked:false});assert.equal(a.locked.value,false)
 const pending=a.refresh();api.disposeAll()
 assert.equal(requests[2].options.signal.aborted,true);assert.equal(cleared.length,2)
 await respond(2,{locked:true});assert.equal(await pending,false)
 assert.equal(a.observing.value,false)
})

test('隐藏页面暂停周期核对，回来共享一次fresh读取；卸载移除listener',async t=>{
 const listeners=new Map<string,Function>(),removed:string[]=[]
 const doc={hidden:false,addEventListener:(key:string,fn:Function)=>listeners.set(key,fn),removeEventListener:(key:string)=>{removed.push(key);listeners.delete(key)}}
 const original=Object.getOwnPropertyDescriptor(globalThis,'document')
 Object.defineProperty(globalThis,'document',{configurable:true,value:doc})
 t.after(()=>{if(original)Object.defineProperty(globalThis,'document',original);else delete (globalThis as any).document})
 const {api,requests,respond,timers}=await fixture(t),a=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{locked:false})
 doc.hidden=true;timers[0]();assert.equal(requests.length,1)
 doc.hidden=false;listeners.get('visibilitychange')!();assert.equal(requests.length,2)
 await respond(1,{locked:true});assert.equal(a.locked.value,true)
 api.disposeAll();assert.deepEqual(removed,['visibilitychange'])
})

test('空scope不轮询，批量只订阅实际选择并与单项目共享；取消选择释放',async t=>{
 const {api,requests,respond,timers,cleared}=await fixture(t)
 const empty=api.useConfigurationEditability(''),scopes=api.ref(['project:a','project:b'])
 const batch=api.useConfigurationBatchEditability(()=>scopes.value),a=api.useConfigurationEditability('project:a')
 api.mountAll();assert.equal(empty.locked.value,false);assert.equal(timers.length,2);assert.equal(requests.length,2)
 await respond(0,{locked:false});await respond(1,{locked:true})
 assert.equal(a.locked.value,false);assert.equal(batch.locked.value,true)
 scopes.value=['project:a'];assert.equal(batch.locked.value,false);assert.equal(cleared.length,1)
})
