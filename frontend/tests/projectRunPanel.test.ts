// 统一入口的选择只读取状态，确认制作/恢复仍交给原有权威预检组件。
import assert from 'node:assert/strict'
import test, {before, after, mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer, type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp, h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createMemoryHistory, createRouter} from 'vue-router'

let server:ViteDevServer, Panel:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',
    server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/ProjectRunPanel.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('禁止未声明的真实请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
const target={uid:'first',title:'第一个项目'}
const row=(fields:any={})=>({...target,run:null,chain:null,publication:'',batch:null,blocked:'',error:'',...fields})
const reply=(data:any)=>({ok:true,json:async()=>data})
async function render(t:any,props:any={},seed:(page:any)=>void=()=>{}){
  let page:any;const emissions:any[]=[],workflowCalls:any[]=[]
  const app=createSSRApp({render:()=>h(Panel,{projects:[target],...props,onWorkflow:(id:string,preferredMode?:string)=>{emissions.push(id);workflowCalls.push([id,preferredMode])}})})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/projects/:uid',component:{render:()=>null}}]})
  await router.push('/');app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='ProjectRunPanel'){page=this.$.setupState;seed(page)}}})
  const context:any={};const html=await renderToString(app,context)+Object.values(context.teleports||{}).join('')
  t.after(()=>page.close())
  return {page,html,emissions,workflowCalls,router}
}
function fixtures(t:any,status:any={},detail:any={}){
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),method:options?.method||'GET'})
    return reply(String(url).endsWith('/workflow/status')?status:{...target,steps:[],dir_exists:true,...detail})
  })
  return calls
}
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()}

test('没有中断任务时直接进入项目目标预检，不显示额外制作范围选择',async t=>{
  const {page,workflowCalls}=await render(t,{workflowHandler:true});const calls=fixtures(t)
  await page.open()
  assert.equal(page.show,false);assert.equal(page.intent,'workflow')
  assert.deepEqual(workflowCalls,[['',undefined]])
  assert.equal(calls.length,2);assert.ok(calls.every(call=>call.method==='GET'))
})

test('已有原BV只作为状态，不替调用者推断更新授权',async t=>{
  const {page,workflowCalls}=await render(t,{workflowHandler:true})
  fixtures(t,{existing_publication_id:'original',has_remote_publication:true})
  await page.open()
  assert.equal(page.rows[0].remotePublication,true)
  assert.deepEqual(workflowCalls,[['',undefined]])
})

test('无中断的单项目跳到总览统一预检，URL不携带上传或原BV覆盖',async t=>{
  const {page,router}=await render(t);fixtures(t,{existing_publication_id:'original'})
  await page.open()
  assert.equal(router.currentRoute.value.path,'/projects/first')
  assert.deepEqual(router.currentRoute.value.query,{run:'workflow'})
})

test('有中断时只保留继续原任务与按当前设置新执行两个正常入口',async t=>{
  const {html}=await render(t,{},page=>{page.show=true;page.targets=[target];page.rows=[row({run:{id:'old',mode:'publish',status:'failed'}})];page.intent='resume'})
  assert.match(html,/继续上次任务/);assert.match(html,/按当前设置开始新执行/)
  assert.doesNotMatch(html,/按当前勾选运行制作|设置本次流程|默认更新原BV/)
})

test('逐项检查实时显示已完成数量与结果，未完成全部检查前不能继续',async t=>{
  const {page}=await render(t);const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  const second={uid:'second',title:'第二项目'}
  const pending=page.open([target,second])
  assert.equal(page.checkedCount,0);assert.equal(page.currentTarget,target.title)
  requests[0].resolve(reply({...target,steps:[],dir_exists:true}));requests[1].resolve(reply({run:null}))
  await flush()
  assert.equal(page.checkedCount,1);assert.equal(page.rows[0].title,target.title)
  assert.equal(page.currentTarget,second.title);assert.equal(page.checking,true);assert.equal(page.canPreview,false)
  requests[2].resolve(reply({...second,steps:[],dir_exists:true}));requests[3].resolve(reply({run:null}));await pending
  assert.equal(page.checkedCount,2);assert.equal(page.currentTarget,'');assert.equal(page.show,false)
  const visible=await render(t,{},p=>{p.show=true;p.checking=true;p.targets=[target,second];p.rows=[row()];p.currentTarget=second.title})
  assert.match(visible.html,/已检查 1 \/ 2 个项目/);assert.match(visible.html,/正在读取：第二项目/)
  assert.match(visible.html,/data-project="first"/)
})

test('取消后重开不会被上轮迟到响应改写计数、当前项目或结果',async t=>{
  const {page}=await render(t);const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  const previous=page.open([target]);page.close()
  const newer=page.open([{uid:'new',title:'新的检查'}])
  requests[0].resolve(reply({...target,steps:[]}));requests[1].resolve(reply({run:{id:'late',status:'failed'}}));await previous
  assert.equal(page.checkedCount,0);assert.equal(page.currentTarget,'新的检查');assert.equal(page.checking,true)
  assert.ok(requests.slice(0,2).every(request=>request.options.signal.aborted))
  requests[2].resolve(reply({title:'新的检查',steps:[]}));requests[3].resolve(reply({run:null}));await newer
  assert.deepEqual(page.rows.map((entry:any)=>entry.uid),['new']);assert.equal(page.checkedCount,1)
})
test('打开统一入口只读最新状态，旧自动投稿默认继续且明确实际终点',async t=>{
  const {page}=await render(t);const calls=fixtures(t,{run:{id:'old',mode:'publish',status:'failed',phase:'配音'}})
  await page.open()
  assert.equal(page.intent,'resume');assert.equal(page.resumeEnd,'自动投稿');assert.equal(page.canPreview,true)
  assert.equal(calls.length,2);assert.ok(calls.every(call=>call.method==='GET'))
  assert.equal(page.rows[0].run.id,'old')
})
test('继续完整流程通过旧drawer事件传实际运行ID，不创建新任务',async t=>{
  const {page,emissions}=await render(t,{allowWorkflow:true,workflowHandler:true});const calls=fixtures(t,{run:{id:'old-publish',mode:'publish',status:'paused'}})
  await page.open();await page.preview()
  assert.deepEqual(emissions,['old-publish']);assert.equal(page.show,false);assert.equal(calls.length,2)
})
test('列表的新完整流程进入原批量预检，选择范围不触发确认或改变步骤勾选',async t=>{
  const {page,emissions}=await render(t,{allowWorkflow:true});const calls=fixtures(t)
  await page.open([target,{uid:'second',title:'第二项目'}]);page.intent='workflow'
  assert.equal(page.show,false)
  let opens=0;page.fullWorkflow={open:async()=>opens++}
  page.show=true;await page.preview();assert.equal(opens,1);assert.equal(page.show,false);assert.deepEqual(emissions,[])
  assert.equal(calls.length,4);assert.ok(calls.every(call=>call.method==='GET'))
})
test('列表恢复交给冻结恢复检查组件，旧完整流程与挂起制作可显式选择',async t=>{
  const {page}=await render(t);fixtures(t,{run:{id:'old',mode:'draft',status:'failed'}},{chain:{state:'held',remaining:['translate']}})
  await page.open();assert.equal(page.intent,'resume');assert.equal(page.separateChain,true)
  let calls=0;page.recovery={open:async()=>calls++};page.intent='chain';await page.preview()
  assert.equal(page.recoveryKind,'chain');assert.equal(calls,1);assert.equal(page.show,false)
})
test('原稿更新已有目标记录后仍可继续保留的更新流程',async t=>{
  const {page,emissions}=await render(t,{allowWorkflow:true,workflowHandler:true})
  fixtures(t,{run:{id:'update-run',mode:'update',status:'failed',publication_id:'original',update_id:'update-task'}})
  await page.open();assert.equal(page.intent,'resume');assert.match(page.resumeEnd,/原稿更新交付/)
  await page.preview();assert.deepEqual(emissions,['update-run'])
})

test('原稿更新已有子任务时恢复选择明确仅续交付，避免暗示重做制作',async t=>{
  const {html,page}=await render(t,{allowWorkflow:true},page=>{page.show=true;page.targets=[target];page.rows=[row({publication:'original',run:{id:'u',mode:'update',status:'failed',publication_id:'original',update_id:'task'}})];page.intent='resume'})
  assert.equal(page.deliveryResume,true);assert.match(html,/仅继续已确认原稿更新的剩余交付/)
  assert.match(html,/不重新制作/);assert.doesNotMatch(html,/需更新时可能从更早步骤开始/)
})
for(const [name,detail,status] of [
  ['归档',{archived:true},{}],['目录缺失',{dir_exists:false},{}],
  ['制作运行',{running_step:'tts'},{}],['链执行',{chain:{state:'active',remaining:['assemble']}},{}],
  ['平台审核',{}, {run:{status:'monitoring',mode:'publish'}}],['上传中',{publication:{status:'uploading'}},{}],
  ['批次排队',{release_batch:{id:'batch',status:'running',position:2,total:3,phase:'等待前项完成'}},{}],
] as const)test(`${name}不提供可执行选择，可查看项目进度`,async t=>{
  const {page}=await render(t,{allowWorkflow:true});fixtures(t,status,detail);await page.open()
  assert.equal(page.canPreview,false);assert.ok(page.rows[0].blocked);assert.equal(page.resumable.length,0)
})
test('参与批次的排队项目保留批次定位链接，不能从单项目入口并行启动',async t=>{
  const batch={id:'active-batch',status:'running',position:2,total:3,phase:'等待前项完成'}
  const {html,page}=await render(t,{},page=>{page.show=true;page.targets=[target];page.rows=[row({batch,blocked:'项目已纳入执行中的顺序发布批次，请查看批次进度。'})]})
  assert.equal(page.canPreview,false);assert.match(html,/\/#release-batch-active-batch/);assert.match(html,/查看顺序发布批次/)
})
test('取消读取后迟到响应不复活抽屉、不恢复可执行意图',async t=>{
  const {page}=await render(t);const resolvers:any[]=[],signals:any[]=[]
  t.mock.method(globalThis,'fetch',async(_url,options)=>{signals.push(options?.signal);return new Promise(done=>resolvers.push(()=>done(reply({run:{id:'late',status:'failed',mode:'publish'}}))))})
  const pending=page.open();assert.equal(page.checking,true);page.close();assert.ok(signals.every(signal=>signal.aborted))
  // 两个独立请求都可以迟到；abort已使它们不具备更新资格。
  resolvers.forEach(resolve=>resolve());await pending;assert.equal(page.show,false);assert.equal(page.canPreview,false)
})
