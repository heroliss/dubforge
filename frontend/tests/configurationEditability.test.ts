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

test('后台执行事实不从未知、失败、归档或单独资源锁推断，接受同项目导航和活动步骤',async t=>{
 const {api,respond}=await fixture(t),a=api.useConfigurationEditability('project:a')
 api.mountAll();assert.equal(a.executionActive.value,false)
 await respond(0,{locked:true,resource_locked:true,running_steps:[]});assert.equal(a.executionActive.value,false)
 const failed=a.refresh();await respond(1,{message:'读取失败'},503);await failed;assert.equal(a.executionActive.value,false)
 const running=a.refresh();await respond(2,{locked:false,resource_locked:true,running_steps:[{project_uid:'a',step_key:'tts',status:'running'}]});await running
 assert.equal(a.executionActive.value,true)
 api.setNavigationExecution('a',true,false,true);assert.equal(a.executionActive.value,false)
 api.setNavigationExecution('a',false,false,false)
 await respond(3,{locked:false,resource_locked:false,running_steps:[{project_uid:'other',step_key:'tts',status:'running'}]})
 assert.equal(a.executionActive.value,false)
 api.setNavigationExecution('a',true);assert.equal(a.executionActive.value,true)
 api.setNavigationExecution('a',true,true);assert.equal(a.executionActive.value,false)
 api.setNavigationExecution('other',true);assert.equal(a.executionActive.value,false)
})

test('排队任务只锁项目级管理操作，保留字段编辑状态',async t=>{
 const {api,requests,respond}=await fixture(t),a=api.useConfigurationEditability('series:queued')
 api.mountAll(); assert.equal(a.locked.value,true)
 await respond(0,{locked:false,resource_locked:true,resource_reason:'步骤已排队，项目级操作暂不能进行',locked_paths:[],running_steps:[]})
 assert.equal(a.locked.value,false)
 assert.equal(a.resourceLocked.value,true)
 assert.match(a.resourceReason.value,/排队/)
})

test('终态核验保留资源与保存门禁但不用物理执行文案，离开和新任务清理理由',async t=>{
 const {api,respond}=await fixture(t),a=api.useConfigurationEditability('project:a'),b=api.useConfigurationEditability('project:b')
 api.mountAll();await respond(0,{locked:false,resource_locked:false,running_steps:[]});await respond(1,{locked:false,resource_locked:false,running_steps:[]})
 const reason='任务已结束，正在核验产物，写操作暂不可用。'
 api.setNavigationExecution('a',true,false,false,reason)
 assert.equal(a.resourceLocked.value,true);assert.equal(a.resourceReason.value,reason)
 assert.equal(a.executionActive.value,false);assert.equal(b.resourceLocked.value,false)
 const save=a.canEdit();await respond(2,{locked:false,resource_locked:false,running_steps:[]});assert.equal(await save,false)
 api.setNavigationExecution('a',true)
 assert.equal(a.resourceLocked.value,true);assert.match(a.resourceReason.value,/任务正在执行/);assert.equal(a.executionActive.value,true)
 api.setNavigationExecution('a',true,false,false,reason);api.setNavigationExecution('a',true,true,false,reason)
 assert.equal(a.resourceLocked.value,false);assert.equal(a.resourceReason.value,'');assert.equal(a.executionActive.value,false)
 api.setNavigationExecution('b',false)
 assert.equal(a.resourceReason.value,'');assert.equal(b.resourceReason.value,'')
 api.setNavigationExecution('a',false)
 const checked=a.canEdit();await respond(3,{locked:false,resource_locked:false,running_steps:[]});assert.equal(await checked,true)
 assert.equal(a.resourceLocked.value,false);assert.equal(a.resourceReason.value,'')
})

test('本地终态核验不遮盖后台同范围活动、未知回执及不可用的独立资源理由',async t=>{
 const {api,respond}=await fixture(t),a=api.useConfigurationEditability('project:a')
 api.mountAll();api.setNavigationExecution('a',true,false,false,'任务已结束，正在核验产物，写操作暂不可用。')
 assert.equal(a.resourceLocked.value,true);assert.match(a.resourceReason.value,/核对运行状态/)
 let index=0
 for(const status of ['queued','running','stopping','cancelling','unknown','archived']) {
  const reason=`后台独立门禁：${status}`,read=index?a.refresh():null
  await respond(index++,{locked:false,resource_locked:true,resource_reason:reason,
   running_steps:['unknown','archived'].includes(status)?[]:[{project_uid:'a',step_key:'assemble',status}]})
  if(read)await read
  assert.equal(a.resourceLocked.value,true);assert.equal(a.resourceReason.value,reason)
 }
 const unavailable=a.refresh();await respond(index++,{message:'断线'},503);await unavailable
 assert.equal(a.resourceLocked.value,true);assert.match(a.resourceReason.value,/未能核对/)
 const clear=a.refresh();await respond(index,{locked:false,resource_locked:false,running_steps:[]});await clear
 assert.match(a.resourceReason.value,/任务已结束.*核验产物/);assert.equal(a.executionActive.value,false)
})

test('运行中保留下一次边界时限和连接字段，继续锁定当前模型参数',async t=>{
 const {api,requests,respond}=await fixture(t),a=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{locked:false,locked_paths:['llm','tts','runner.step_timeout_seconds.tts']})
 assert.equal(a.isPathLocked('llm.profiles'),true)
 assert.equal(a.isPathLocked('llm.timeout'),false)
 assert.equal(a.isPathLocked('runner.step_timeout_seconds.tts'),false)
 assert.equal(a.isPathLocked('tts.indextts25.url'),false)
 assert.equal(a.isPathLocked('tts.indextts25.timbre_mode'),true)
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

test('已知运行锁在后台核对前中后保持原提示；慢轮询与手动核对共享请求',async t=>{
 const {api,requests,respond,timers}=await fixture(t)
 const a=api.useConfigurationEditability('project:p'),b=api.useConfigurationEditability('project:p')
 const reason='项目有任务正在执行，结束后可修改设置。查看、导航和停止任务仍可用。'
 const assertLocked=(checking:boolean)=>{
  for(const observer of [a,b]) {
   assert.equal(observer.locked.value,true)
   assert.equal(observer.reason.value,reason)
   assert.equal(observer.checking.value,checking)
  }
 }
 api.mountAll();await respond(0,{locked:true,reason})
 assertLocked(false);assert.equal(timers.length,1)
 timers[0]();assert.equal(requests.length,2);assertLocked(true)
 // 后台请求尚未返回时重复轮询、点击刷新与保存前检查，都不解除业务锁或新增请求。
 timers[0]();const refresh=a.refresh(),second=b.refresh(),canEdit=a.canEdit()
 assert.equal(requests.length,2);assertLocked(true)
 await respond(1,{locked:true,reason})
 assert.deepEqual(await Promise.all([refresh,second,canEdit]),[false,false,false])
 assertLocked(false)
 timers[0]();assert.equal(requests.length,3);assertLocked(true)
 await respond(2,{locked:true,reason});assertLocked(false)
})

test('后台核对收到可编辑响应不伪造字段锁；保存前仍拒绝本地运行项目',async t=>{
 const {api,requests,respond,timers}=await fixture(t),a=api.useConfigurationEditability('project:a')
 api.mountAll();await respond(0,{locked:false})
 api.setNavigationExecution('a',true)
 const reason=a.reason.value
 assert.equal(reason,'')
 assert.equal(a.locked.value,false)
 timers[0]();const canEdit=a.canEdit()
 assert.equal(requests.length,2);assert.equal(a.checking.value,true)
 assert.equal(a.locked.value,false);assert.equal(a.reason.value,reason)
 await respond(1,{locked:false})
 assert.equal(await canEdit,false);assert.equal(a.checking.value,false)
 assert.equal(a.locked.value,false);assert.equal(a.reason.value,reason)
 // 真正结束本地任务后保存前核对才返回可编辑。
 api.setNavigationExecution('a',false)
 assert.equal(a.locked.value,false);assert.equal(a.reason.value,'')
})

test('路径保存门禁允许TTS运行时修改后续成片音量，仍阻止当前TTS参数及成片已开始',async t=>{
 const {api,requests,respond}=await fixture(t),a=api.useConfigurationEditability('project:a')
 const mix=['assemble.voice_volume','assemble.bgm_volume']
 api.mountAll();await respond(0,{locked:false,resource_locked:true,locked_paths:['tts'],running_steps:[{key:'tts'}]})
 api.setNavigationExecution('a',true)
 const saving=a.canEditPaths(mix)
 assert.equal(requests.length,2)
 await respond(1,{locked:false,resource_locked:true,locked_paths:['tts'],running_steps:[{key:'tts'}]})
 assert.equal(await saving,true);assert.equal(a.resourceLocked.value,true)
 const tts=a.canEditPaths(['tts.indextts25.timbre_mode'])
 await respond(2,{locked:false,resource_locked:true,locked_paths:['tts']});assert.equal(await tts,false)
 const assembling=a.canEditPaths(mix)
 await respond(3,{locked:false,resource_locked:true,locked_paths:['assemble'],running_steps:[{key:'assemble'}]})
 assert.equal(await assembling,false)
})

test('路径保存门禁未知或读取失败拒绝写入，可重新核对；归档状态不能绕过',async t=>{
 const {api,respond}=await fixture(t),a=api.useConfigurationEditability('project:a'),mix=['assemble.voice_volume','assemble.bgm_volume']
 api.mountAll()
 const unknown=a.canEditPaths(mix);await respond(0,{message:'断线'},503);assert.equal(await unknown,false)
 const retry=a.canEditPaths(mix);await respond(1,{locked:false,locked_paths:[]});assert.equal(await retry,true)
 api.setNavigationExecution('a',false,false,true)
 const archived=a.canEditPaths(mix);await respond(2,{locked:false,locked_paths:[]});assert.equal(await archived,false)
})

test('项目本地运行立即收紧同项目和全局权限，其他项目不受影响；暂停旧nav后服从服务端',async t=>{
 const {api,respond}=await fixture(t)
 const a=api.useConfigurationEditability('project:a'),b=api.useConfigurationEditability('project:b'),global=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{locked:false});await respond(1,{locked:false});await respond(2,{locked:false})
 api.setNavigationExecution('a',true)
 // ADR-0132 keeps unrelated settings editable; execution controls and their
 // save-time `canEdit()` checks still observe the local navigation busy flag.
 assert.equal(a.locked.value,false);assert.equal(global.locked.value,false);assert.equal(b.locked.value,false)
 assert.equal(a.resourceLocked.value,true);assert.equal(global.resourceLocked.value,true);assert.equal(b.resourceLocked.value,false)
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

test('归档导航立即锁项目且提示先恢复，不锁全局；旧响应不能提前解锁',async t=>{
 const {api,requests,respond}=await fixture(t)
 const project=api.useConfigurationEditability('project:a'),global=api.useConfigurationEditability('global')
 api.mountAll();await respond(0,{locked:false});await respond(1,{locked:false})
 api.setNavigationExecution('a',false,false,true)
 assert.equal(project.locked.value,true);assert.match(project.reason.value,/已归档.*恢复/)
 assert.doesNotMatch(project.reason.value,/任务正在执行/);assert.equal(global.locked.value,false)
 const pending=project.canEdit();await respond(2,{locked:false});assert.equal(await pending,false)
 assert.equal(project.locked.value,true)
 api.setNavigationExecution('a',false,false,false)
 assert.equal(project.locked.value,true);assert.match(project.reason.value,/核对/)
 await respond(3,{locked:false})
 assert.equal(project.locked.value,false)
 api.setNavigationExecution('a',false,false,true)
 api.setNavigationExecution('a',false,true,true)
 await respond(4,{locked:false})
 assert.equal(project.locked.value,false,'离开项目后旧导航归档快照不阻止服务端状态')
})

test('批量模板门禁识别已归档项目，说明无需等待任务结束',async t=>{
 const {api,respond}=await fixture(t),scopes=api.ref(['project:a','project:b'])
 const batch=api.useConfigurationBatchEditability(()=>scopes.value)
 api.mountAll();await respond(0,{locked:false});await respond(1,{locked:false})
 api.setNavigationExecution('a',false,false,true)
 assert.equal(batch.locked.value,true);assert.match(batch.reason.value,/已归档.*恢复/)
 scopes.value=['project:b'];assert.equal(batch.locked.value,false)
})

test('恢复立即作废归档缓存并共享新核验，迟到的归档响应不能再锁项目',async t=>{
 const {api,requests,respond,timers}=await fixture(t)
 const a=api.useConfigurationEditability('project:a'),b=api.useConfigurationEditability('project:a'),global=api.useConfigurationEditability('global')
 api.mountAll();api.setNavigationExecution('a',false,false,true)
 await respond(0,{locked:true,reason:'本项目已归档，请先恢复项目。'});await respond(1,{locked:false})
 timers[0]();assert.equal(requests.length,3)
 const old=a.refresh();api.setNavigationExecution('a',false,false,false)
 assert.equal(requests[2].options.signal.aborted,true);assert.equal(requests.length,4)
 assert.equal(a.locked.value,true);assert.match(a.reason.value,/核对/);assert.doesNotMatch(a.reason.value,/已归档/)
 assert.equal(b.checking.value,true);assert.equal(global.locked.value,false)
 const current=a.canEdit(),second=b.refresh();assert.equal(requests.length,4,'观察者共享即时新核验')
 await respond(3,{locked:false});assert.deepEqual(await Promise.all([current,second]),[true,true])
 assert.equal(a.locked.value,false);assert.equal(b.locked.value,false)
 await respond(2,{locked:true,reason:'本项目已归档，请先恢复项目。'});assert.equal(await old,false)
 assert.equal(a.locked.value,false);assert.equal(a.reason.value,'');assert.equal(b.checking.value,false)
 assert.equal(requests.length,4,'只刷新项目门禁，不额外核验全局')
})

test('无挂载订阅的归档状态变化不发网络，下次挂载重新读取而不用缓存',async t=>{
 const {api,requests,respond}=await fixture(t),a=api.useConfigurationEditability('project:a')
 api.setNavigationExecution('a',false,false,true)
 api.setNavigationExecution('a',false,false,false)
 assert.equal(requests.length,0,'SSR 不请求服务端')
 api.mountAll();await respond(0,{locked:true,reason:'本项目已归档，请先恢复项目。'})
 api.disposeAll();api.setNavigationExecution('a',false,false,true);api.setNavigationExecution('a',false,false,false)
 assert.equal(requests.length,1,'已经离开的页面不启动后台核验')
 const reopened=api.useConfigurationEditability('project:a');api.mountAll()
 assert.equal(requests.length,2);assert.equal(reopened.locked.value,true);assert.match(reopened.reason.value,/核对/)
 await respond(1,{locked:false});assert.equal(reopened.locked.value,false)
})
