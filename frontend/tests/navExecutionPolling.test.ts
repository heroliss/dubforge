import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({stdin:{contents:`export { useNavStore } from './src/stores/nav'; export { useConfigurationEditability } from './src/utils/configurationEditability'; export { createPinia, setActivePinia } from 'pinia'; export { effectScope } from 'vue';`,
  resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'ts'},bundle:true,write:false,format:'esm',platform:'browser',define:{'process.env.NODE_ENV':'"production"'}})
const {useNavStore,useConfigurationEditability,createPinia,setActivePinia,effectScope} = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const flush = async () => { for(let i=0;i<20;i++)await Promise.resolve() }
const reply = (value:any) => new Response(JSON.stringify(value))
const active = (job=8,completed=2) => ({key:'tts',label:'字幕与配音',status:'running',job_id:job,progress:completed,
  selected:false,fresh:false,cfg:{voice:'已选音色'},artifacts:[{id:7}],execution:{schema:1,state:'running',job_id:job,completed,total:20}})
const runtime = (uid:string,step:any) => ({workspace_id:'fixture',projects:[{uid,steps:[step],running_step:'tts'}]})

test('1.5秒终态交接不等慢full：核验态保持只读，旧full不复活，重试可达',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  const uid='terminal-handoff',full:any[]=[],urls:string[]=[]
  t.mock.method(globalThis,'fetch',(input:any)=>{
    const url=String(input);urls.push(url)
    if(url.endsWith('/steps'))return new Promise(resolve=>full.push(resolve))
    if(url.includes('/execution?'))return Promise.resolve(reply(runtime(uid,{...active(),status:'done',progress:100,
      execution:{schema:1,owner:'step',state:'completed',job_id:8},eta_seconds:0})))
    return Promise.resolve(reply(base(url)))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter(uid);await flush();full[0](reply([active()]));await flush()
    t.mock.timers.tick(1500);await flush()
    assert.equal(nav.stepMap.tts.status,'checking');assert.equal(nav.stepMap.tts.fresh,null)
    assert.equal(nav.busy,true);assert.equal(full.length,2)
    t.mock.timers.tick(1500);await flush();assert.equal(full.length,2)
    assert.equal(urls.filter(url=>url.includes('/execution?')).length,2)
    full[1](reply([active()]));await flush()
    assert.equal(nav.stepMap.tts.status,'checking','即使full请求开始于终态之后，迟到同Job running也不得复活')
    const retry=nav.refreshSteps(true);await flush();assert.equal(full.length,3)
    full[2](reply([{...active(),status:'stale',fresh:false,job_id:null,execution:{schema:1,state:'completed',job_id:8}}]));await retry
    assert.equal(nav.stepMap.tts.status,'stale');assert.equal(nav.busy,false)
  } finally {nav.reset()}
})

test('终态full超时仍保持核验未知，可通过原只读重试恢复真实完成',async t=>{
  const uid='terminal-read-error',reads:any[]=[]
  t.mock.method(globalThis,'fetch',(input:any)=>{
    const url=String(input)
    if(url.endsWith('/steps'))return new Promise((resolve,reject)=>reads.push({resolve,reject}))
    if(url.includes('/execution?'))return Promise.resolve(reply(runtime(uid,{...active(),status:'done',
      execution:{schema:1,owner:'step',state:'completed',job_id:8}})))
    return Promise.resolve(reply(base(url)))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid,steps:[active()],stepsLoaded:true})
  try {
    await nav.pollExecution();await flush()
    reads[0].reject(new DOMException('fixture timeout','TimeoutError'));await flush()
    assert.equal(nav.stepMap.tts.status,'checking');assert.equal(nav.stepMap.tts.fresh,null)
    assert.equal(nav.busy,true);assert.match(nav.stepsError,/请求超时/)
    const retry=nav.refreshSteps(true);await flush()
    reads[1].resolve(reply([{...active(),status:'done',job_id:null,fresh:true,execution:{schema:1,job_id:8,state:'completed'}}]));await retry
    assert.equal(nav.stepMap.tts.status,'done');assert.equal(nav.stepsError,'');assert.equal(nav.busy,false)
  } finally {nav.reset()}
})

test('导航仅核验时传递中性资源锁理由，混真实任务及作用域重建不残留旧理由',async t=>{
  t.mock.method(globalThis,'fetch',async(input:any)=>reply(String(input).endsWith('/steps')?[]:base(String(input))))
  // 这里只读取真实共享器的本地 computed；不挂载它的服务端订阅。
  t.mock.method(console,'warn',()=>{})
  setActivePinia(createPinia());const nav=useNavStore(),layoutScope=effectScope()
  layoutScope.run(()=>nav.enter('guard-handoff'));await flush()
  const locks=useConfigurationEditability('project:guard-handoff')
  try {
    nav.steps=[{...active(),status:'checking',fresh:null}]
    assert.equal(nav.busy,true);assert.match(nav.verificationReason,/核验产物/)
    assert.equal(locks.resourceLocked.value,true);assert.match(locks.resourceReason.value,/任务已结束.*核验产物/)
    assert.equal(locks.executionActive.value,false)
    nav.steps=[...nav.steps,{...active(9),key:'assemble',status:'queued'}]
    assert.equal(nav.verificationReason,'');assert.match(locks.resourceReason.value,/任务正在执行/);assert.equal(locks.executionActive.value,true)
    nav.steps=[{...active(),status:'done',job_id:null,fresh:true}]
    assert.equal(nav.busy,false);assert.equal(locks.resourceLocked.value,false);assert.equal(locks.resourceReason.value,'')
    layoutScope.stop();nav.steps=[{...active(),status:'checking',fresh:null}]
    assert.match(locks.resourceReason.value,/核验产物/,'共享watch在布局scope退出后仍存在')
    nav.suspend();assert.equal(locks.resourceLocked.value,false);assert.equal(locks.resourceReason.value,'')
    nav.enter('guard-other');await flush();nav.steps=[active(9)]
    assert.equal(locks.resourceLocked.value,false);assert.equal(locks.resourceReason.value,'','另一UID不借前项目理由')
  } finally {layoutScope.stop();nav.reset()}
})
function base(url:string) {
  if(url.endsWith('/navigation'))return {title:'测试项目'}
  if(url.endsWith('/steps'))return [active()]
  if(url.endsWith('/workflow/status'))return {run:null}
  if(url.includes('/log?'))return {lines:[]}
  return {}
}

test('运行时每1.5秒只读取轻量进度，完整状态阻塞期间仍更新且迟到结果不回退',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  const urls:string[]=[],uid='nav-cadence'
  let slowFull:any,fullCount=0,telemetryCount=0
  t.mock.method(globalThis,'fetch',async (input:any)=>{
    const url=String(input);urls.push(url)
    if(url.endsWith('/steps')&&++fullCount>1)return new Promise(resolve=>{slowFull=resolve})
    if(url.includes('/execution?'))return reply(runtime(uid,active(8,++telemetryCount+2)))
    return reply(base(url))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter(uid);await flush()
    t.mock.timers.tick(1499);await flush();assert.equal(telemetryCount,0)
    t.mock.timers.tick(1);await flush();assert.equal(telemetryCount,1);assert.equal(fullCount,1)
    assert.equal(nav.stepMap.tts.execution.completed,3);assert.equal(nav.stepMap.tts.selected,false)
    t.mock.timers.tick(1500);await flush();assert.equal(telemetryCount,2)
    t.mock.timers.tick(1000);await flush();assert.equal(fullCount,2)
    t.mock.timers.tick(500);await flush();assert.equal(telemetryCount,3)
    assert.equal(nav.stepMap.tts.execution.completed,5)
    slowFull(reply([{...active(8,1),cfg:{voice:'完整接口新设置'}}]));await flush()
    assert.equal(nav.stepMap.tts.execution.completed,5)
    assert.equal(nav.stepMap.tts.cfg.voice,'完整接口新设置')
    assert.equal(urls.filter(url=>url.includes('/execution?')).length,3)
  } finally {nav.reset()}
})

test('终态只补一次完整核对，未经新鲜度检查不会把实时 done 当作制作完成',async t=>{
  const uid='nav-terminal',reads:any[]=[]
  t.mock.method(globalThis,'fetch',(input:any,options:any)=>{
    const url=String(input)
    if(url.endsWith('/steps'))return new Promise(resolve=>reads.push({url,options,resolve}))
    if(url.includes('/execution?'))return Promise.resolve(reply(runtime(uid,{...active(),status:'done',progress:100,
      eta_seconds:0,progress_phase:'',execution:{schema:1,state:'completed',job_id:8,completed:20,total:20}})))
    return Promise.resolve(reply(base(url)))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid,steps:[active()],stepsLoaded:true})
  try {
    await nav.pollExecution();await flush()
    assert.equal(reads.length,1);assert.equal(nav.stepMap.tts.status,'checking');assert.equal(nav.busy,true)
    assert.equal(nav.stepMap.tts.fresh,null);assert.equal(nav.stepMap.tts.execution.state,'completed')
    await nav.pollExecution();await nav.pollExecution();assert.equal(reads.length,1)
    reads[0].resolve(reply([{...active(),status:'done',job_id:null,fresh:true,execution:{schema:1,job_id:8,state:'completed'}}]));await flush()
    assert.equal(nav.stepMap.tts.status,'done');assert.equal(nav.stepMap.tts.fresh,true)
    await nav.pollExecution();assert.equal(reads.length,1)
  } finally {nav.reset()}
})

test('隐藏页面取消在途实时读取，恢复立即读取，切换项目和暂停均不接收旧响应',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  class Page extends EventTarget {hidden=false}
  const page=new Page(),descriptor=Object.getOwnPropertyDescriptor(globalThis,'document')
  Object.defineProperty(globalThis,'document',{configurable:true,value:page})
  t.after(()=>{if(descriptor)Object.defineProperty(globalThis,'document',descriptor);else delete (globalThis as any).document})
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',(input:any,options:any)=>{
    const url=String(input)
    if(url.includes('/execution?'))return new Promise(resolve=>reads.push({url,options,resolve}))
    return Promise.resolve(reply(base(url)))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter('nav-visibility');await flush();t.mock.timers.tick(1500);await flush()
    assert.equal(reads.length,1)
    page.hidden=true;page.dispatchEvent(new Event('visibilitychange'))
    assert.equal(reads[0].options.signal.aborted,true)
    t.mock.timers.tick(6000);await flush();assert.equal(reads.length,1)
    reads[0].resolve(reply(runtime('nav-visibility',active(8,18))));await flush()
    assert.equal(nav.stepMap.tts.execution.completed,2)
    page.hidden=false;page.dispatchEvent(new Event('visibilitychange'));await flush();assert.equal(reads.length,2)
    nav.enter('nav-other');await flush();assert.equal(reads[1].options.signal.aborted,true)
    reads[1].resolve(reply(runtime('nav-visibility',active(8,19))));await flush()
    assert.equal(nav.stepMap.tts.execution.completed,2)
    t.mock.timers.tick(1500);await flush();assert.equal(reads.length,3)
    nav.suspend();assert.equal(reads[2].options.signal.aborted,true);assert.equal(nav.executionTimer,0)
    reads[2].resolve(reply(runtime('nav-other',active(8,20))));await flush()
    assert.equal(nav.stepMap.tts.execution.completed,2)
  } finally {nav.reset()}
})

test('新启动回执取消旧实时请求，不允许旧代次覆盖新排队任务',async t=>{
  let resolve:any,signal:any
  t.mock.method(globalThis,'fetch',(input:any,options:any)=>{
    if(String(input).includes('/execution?')) {signal=options.signal;return new Promise(accept=>{resolve=accept})}
    return Promise.resolve(reply(base(String(input))))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid:'nav-replaced',steps:[active()]})
  try {
    const pending=nav.pollExecution()
    nav.stepStarted('nav-replaced','tts',{status:'started',job_id:9})
    assert.equal(signal.aborted,true)
    resolve(reply(runtime('nav-replaced',active(8,19))));await pending
    assert.equal(nav.stepMap.tts.job_id,9);assert.equal(nav.stepMap.tts.status,'queued')
  } finally {nav.reset()}
})

test('同UID工作目录变化停止实时合并并提示重新打开，不自动切到新目录或覆盖当前进度',async t=>{
  const uid='nav-workspace';let workspace='first',reads=0
  t.mock.method(globalThis,'fetch',async (input:any)=>{
    const url=String(input)
    if(url.includes('/execution?')) {
      reads++
      return new Response(JSON.stringify({...runtime(uid,active(8,workspace==='first'?4:19)),workspace_id:workspace}),
        {headers:{'X-DubForge-Workspace':workspace}})
    }
    return reply(base(url))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid,steps:[active()]})
  try {
    await nav.pollExecution();assert.equal(nav.executionWorkspace,'first');assert.equal(nav.stepMap.tts.execution.completed,4)
    workspace='second';await nav.pollExecution()
    assert.equal(nav.executionBlocked,true);assert.equal(nav.executionWorkspace,'first')
    assert.equal(nav.stepMap.tts.execution.completed,4);assert.match(nav.stepsError,/工作目录已变化.*重新打开/)
    await nav.pollExecution();assert.equal(reads,2)
    await nav.refreshSteps();assert.match(nav.stepsError,/重新打开/,'完整核对成功不能隐藏实时身份变化提示')
    nav.enter('nav-workspace-other');assert.equal(nav.executionBlocked,false);assert.equal(nav.executionWorkspace,'')
  } finally {nav.reset()}
})

test('工作目录header与body不一致的实时响应不建立身份或应用进度',async t=>{
  const uid='nav-workspace-envelope'
  t.mock.method(globalThis,'fetch',async()=>new Response(JSON.stringify(runtime(uid,active(8,19))),{headers:{'X-DubForge-Workspace':'different'}}))
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid,steps:[active()]})
  try {
    await nav.pollExecution()
    assert.equal(nav.stepMap.tts.execution.completed,2);assert.equal(nav.executionWorkspace,'')
    assert.equal(nav.readFailures.execution?.kind,'response')
  } finally {nav.reset()}
})

test('工作目录变化后完整轮询也冻结，迟到响应不覆盖旧页，明确重新进入后才恢复读取',async t=>{
  const uid='nav-workspace-freeze',fullReads:any[]=[];let workspace='first',requestCount=0
  t.mock.method(globalThis,'fetch',(input:any,options:any)=>{
    const url=String(input);requestCount++
    if(url.endsWith('/steps'))return new Promise(resolve=>fullReads.push({resolve,signal:options.signal}))
    if(url.includes('/execution?'))return Promise.resolve(reply({...runtime(uid,active(8,4)),workspace_id:workspace}))
    return Promise.resolve(reply(base(url)))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid,steps:[active()],stepsLoaded:true})
  try {
    await nav.pollExecution()
    const pendingFull=nav.refreshSteps()
    workspace='second';await nav.pollExecution()
    assert.equal(nav.executionBlocked,true);assert.equal(fullReads[0].signal.aborted,true)
    fullReads[0].resolve(reply([{...active(99,19),cfg:{voice:'其他目录音色'}}]));await pendingFull
    assert.equal(nav.stepMap.tts.job_id,8);assert.equal(nav.stepMap.tts.cfg.voice,'已选音色')
    const stoppedCount=requestCount
    await nav.poll();await nav.refreshSteps();await nav.refreshWorkflow();await nav.pollExecution()
    assert.equal(requestCount,stoppedCount,'冻结期间所有导航状态读取均停止，不重复扫描产物')
    assert.match(nav.stepsError,/重新打开/)
    nav.suspend();nav.enter(uid);await flush()
    assert.equal(nav.executionBlocked,false);assert.equal(nav.executionWorkspace,'')
    assert.ok(requestCount>stoppedCount)
    fullReads[1].resolve(reply([{...active(99,0),cfg:{voice:'重新进入后音色'}}]));await flush()
    assert.equal(nav.stepMap.tts.job_id,99);assert.equal(nav.stepMap.tts.cfg.voice,'重新进入后音色')
    assert.equal(nav.stepsError,'')
  } finally {nav.reset()}
})

test('旧后端不支持新接口时一次404后回退完整轮询，重新进入前不再高频请求或提示错误',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  let telemetryReads=0,fullReads=0
  t.mock.method(globalThis,'fetch',async(input:any)=>{
    const url=String(input)
    if(url.includes('/execution?')) {telemetryReads++;return new Response('',{status:404})}
    if(url.endsWith('/steps'))fullReads++
    return reply(base(url))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter('nav-legacy-backend');await flush()
    t.mock.timers.tick(1500);await flush()
    assert.equal(telemetryReads,1);assert.equal(nav.executionUnavailable,true)
    assert.equal(nav.executionError,'');assert.equal(nav.stepsError,'');assert.equal(nav.readFailures.execution,undefined)
    assert.equal(nav.executionTimer,0)
    const previousFull=fullReads
    t.mock.timers.tick(12000);await flush();await nav.pollExecution()
    assert.equal(telemetryReads,1);assert.ok(fullReads>previousFull,'原4秒完整状态轮询仍继续')
    nav.suspend();nav.enter('nav-legacy-backend');await flush()
    assert.equal(nav.executionUnavailable,false)
    t.mock.timers.tick(1500);await flush()
    assert.equal(telemetryReads,2,'明确重新进入后仅重新探测一次接口能力')
    assert.equal(nav.stepsError,'');assert.equal(nav.readFailures.execution,undefined)
  } finally {nav.reset()}
})

test('共享进度监听不随创建它的组件作用域卸载而失效，离开项目后仍完整释放并可重新进入',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  let telemetryReads=0
  t.mock.method(globalThis,'fetch',async(input:any)=>{
    const url=String(input)
    if(url.includes('/execution?')) {telemetryReads++;return reply(runtime('nav-layout-replaced',active()))}
    if(url.endsWith('/steps'))return reply([])
    return reply(base(url))
  })
  setActivePinia(createPinia());const nav=useNavStore(),layoutScope=effectScope()
  try {
    layoutScope.run(()=>nav.enter('nav-layout-replaced'));await flush()
    assert.equal(nav.executionTimer,0)
    layoutScope.stop()
    nav.steps=[active()];await flush()
    assert.notEqual(nav.executionTimer,0,'新任务仍应启动共享轻量轮询')
    t.mock.timers.tick(1500);await flush();assert.equal(telemetryReads,1)
    nav.suspend();assert.equal(nav.executionTimer,0)
    nav.steps=[active(9)];await flush();assert.equal(nav.executionTimer,0,'离开项目后监听必须释放')
    nav.enter('nav-layout-replaced');await flush()
    nav.steps=[active(9)];await flush()
    t.mock.timers.tick(1500);await flush();assert.equal(telemetryReads,2)
  } finally {layoutScope.stop();nav.reset()}
})

test('返回前台立即刷新旧后端完整状态，不等待下一个四秒计时',async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  class Page extends EventTarget {hidden=false}
  const page=new Page(),descriptor=Object.getOwnPropertyDescriptor(globalThis,'document')
  Object.defineProperty(globalThis,'document',{configurable:true,value:page})
  t.after(()=>{if(descriptor)Object.defineProperty(globalThis,'document',descriptor);else delete (globalThis as any).document})
  let fullReads=0,failed=false
  t.mock.method(globalThis,'fetch',async(input:any)=>{
    const url=String(input)
    if(url.includes('/execution?'))return new Response('',{status:404})
    if(url.endsWith('/steps')){fullReads++;return reply([failed?{...active(),status:'failed',job_id:null,error:{message:'实际失败'}}:active()])}
    return reply(base(url))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter('nav-return-visible');await flush();t.mock.timers.tick(1500);await flush()
    assert.equal(nav.executionUnavailable,true)
    page.hidden=true;page.dispatchEvent(new Event('visibilitychange'));failed=true
    const before=fullReads
    page.hidden=false;page.dispatchEvent(new Event('visibilitychange'));await flush()
    assert.equal(fullReads,before+1);assert.equal(nav.stepMap.tts.status,'failed')
  } finally {nav.reset()}
})
