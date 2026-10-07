// 真实组件 + 模拟 fetch：检查只读、恢复范围冻结、取消与去重均不触发生产任务。
import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
let server: ViteDevServer, Panel: any, Recovery: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/BatchWorkflowResume.vue')).default
  Recovery = (await server.ssrLoadModule('/src/components/RecoveryPlan.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('不允许未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
const projects = [{ uid: 'one', title: '项目一' }, { uid: 'two', title: '项目二' }]
const recovery = () => ({ start_step:'split',start_label:'语义整理',previous_step:'translate',previous_label:'翻译',reason:'语义整理输入已变化，先更新上游',rows:[
  {key:'download',label:'下载',action:'reuse',reason:'结果可复用'}, {key:'split',label:'语义整理',action:'update',reason:'输入已变化'},
  {key:'translate',label:'翻译',action:'retry',reason:'上次失败'}, {key:'tts',label:'配音',action:'run',reason:'复用有效音频，补齐待配音句'},
] })
const planOf = (uid:string) => ({recipe:{mode:'draft',part_titles:{}},fingerprint:'fp-'+uid,recovery_fingerprint:'rf-'+uid,recovery:recovery(),steps:['split','translate','tts'],labels:{split:'语义整理',translate:'翻译',tts:'配音'},blockers:[],version_specs:[]})
const held = {remaining:['translate','tts'],state:'held',hold:{step:'translate',code:'STEP_FAILED',message:'失败'}}
const reply = (data:any,ok=true,status=200) => ({ok,status,json:async()=>data})
async function render(options:any={}) {
  let page:any
  const input=reactive({projects:projects.map(p=>({...p})),...options})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(Panel,input)});app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='BatchWorkflowResume')page=this.$.setupState}})
  const html=await renderToString(app);return {page,html,input}
}
function fixtures(t:any, adjust:(uid:string,path:string)=>any=()=>undefined) {
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    const match=String(url).match(/\/projects\/([^/]+)(.*)/)!,uid=match[1],path=match[2]
    const body=options?.body&&JSON.parse(String(options.body));calls.push({uid,path,body,signal:options?.signal})
    const custom=adjust(uid,path);if(custom!==undefined)return custom
    if(path==='')return reply({steps:[],chain:null})
    if(path==='/workflow/status')return reply({run:{id:'run-'+uid,status:'failed',mode:'draft',publication_id:''}})
    if(path==='/workflow/plan')return reply(planOf(uid))
    if(path==='/chain/plan')return reply({...planOf(uid),recovery_fingerprint:undefined})
    if(path.endsWith('/resume'))return reply({status:'queued'})
    throw new Error('意外请求 '+path)
  });return calls
}
test('检查读取项目与工作流，优先完整流程且不启动任何任务',async t=>{
  const {page}=await render();const calls=fixtures(t)
  await page.open();assert.equal(page.ready.length,2);assert.equal(page.canConfirm,true)
  assert.deepEqual(calls.map(c=>c.path),['','/workflow/status','/workflow/plan','','/workflow/status','/workflow/plan'])
  assert.ok(calls.every(c=>!c.path.endsWith('/resume')&&!c.path.endsWith('/start')))
  assert.deepEqual(calls[2].body,{mode:'draft',resume_id:'run-one'})
})
test('已有发布记录引导发布页；没有恢复历史引导完整流程',async t=>{
  const {page}=await render();fixtures(t,(uid,path)=>path==='/workflow/status'?reply({run:uid==='one'?{id:'old',status:'completed',publication_id:'pub'}:null}):undefined)
  await page.open();assert.equal(page.ready.length,0);assert.equal(page.items[0].link,'publish')
  assert.match(page.items[1].note,/没有保留的恢复计划/);assert.equal(page.items[1].link,'project')
})
test('已发布完整流程旁的挂起制作链仍可恢复，并且只恢复制作',async t=>{
  const {page}=await render({projects:[projects[0]]});const calls=fixtures(t,(_uid,path)=>path===''?reply({steps:[],chain:held}):path==='/workflow/status'?reply({run:{id:'old',status:'completed',publication_id:'pub'}}):undefined)
  await page.open();assert.equal(page.ready[0].kind,'chain');await page.resumeAll()
  const write=calls.find(c=>c.path==='/chain/resume');assert.ok(write);assert.equal(write.body.confirmed,true);assert.equal(write.body.fingerprint,'fp-one')
  assert.equal(write.body.recipe,undefined);assert.ok(calls.every(c=>c.path!=='/workflow/plan'))
})
test('显式链入口不会误选最近失败的完整流程',async t=>{
  const {page}=await render({projects:[projects[0]],kind:'chain'});const calls=fixtures(t,(_uid,path)=>path===''?reply({steps:[],chain:held}):undefined)
  await page.open();assert.equal(page.ready[0].kind,'chain');assert.ok(calls.every(c=>c.path!=='/workflow/plan'))
})
test('任何制作或完整流程正在执行都不可恢复',async t=>{
  const {page}=await render();const calls=fixtures(t,(uid,path)=>uid==='one'&&path===''?reply({steps:[{status:'running'}],chain:held}):uid==='two'&&path==='/workflow/status'?reply({run:{status:'monitoring'}}):undefined)
  await page.open();assert.equal(page.ready.length,0);assert.ok(page.items.every((i:any)=>/正在执行/.test(i.note)))
  assert.ok(calls.every(c=>!c.path.endsWith('/plan')))
})
test('阻塞项目保留原因，勾选范围之外的项目不恢复',async t=>{
  const {page}=await render();const calls=fixtures(t,(uid,path)=>uid==='two'&&path==='/workflow/plan'?reply({...planOf(uid),blockers:['服务未就绪']}):undefined)
  await page.open();assert.equal(page.ready.length,1);assert.match(page.items[1].blockers[0],/服务未就绪/)
  await page.resumeAll();const writes=calls.filter(c=>c.path.endsWith('/resume'));assert.equal(writes.length,1)
  assert.equal(writes[0].body.recovery_fingerprint,'rf-one');assert.equal(writes[0].body.request_id.length,36)
  assert.equal(page.items.length,2);assert.equal(page.items[0].submitted,true)
})
test('逐项目取消勾选与输入范围变化都阻止越范围执行',async t=>{
  const {page,input}=await render();const calls=fixtures(t);await page.open()
  page.select(page.items[1],false);await page.resumeAll();assert.equal(calls.filter(c=>c.path.endsWith('/resume')).length,1)
  await page.inspect();input.projects.push({uid:'hidden',title:'隐藏选中项目'})
  assert.equal(page.canConfirm,false);await page.resumeAll();assert.equal(calls.filter(c=>c.path.endsWith('/resume')).length,1)
})
test('网络回执丢失后重试同一冻结方案沿用UUID，成功项目不重复恢复',async t=>{
  const {page}=await render();let attempts=0
  const calls=fixtures(t,(uid,path)=>uid==='two'&&path.endsWith('/resume')? (++attempts===1?Promise.reject(new Error('连接中断')):reply({status:'failed'})):undefined)
  await page.open();await page.resumeAll();assert.equal(page.items[0].submitted,true);assert.equal(page.items[1].submitted,false)
  await page.resumeAll();const writes=calls.filter(c=>c.uid==='two'&&c.path.endsWith('/resume'))
  assert.equal(writes.length,2);assert.equal(writes[0].body.request_id,writes[1].body.request_id)
  assert.equal(calls.filter(c=>c.uid==='one'&&c.path.endsWith('/resume')).length,1)
  assert.match(page.items[1].result,/当前：需处理/);assert.doesNotMatch(page.items[1].result,/已交给后台继续执行/)
})
test('确认双击只发送一次，发送内容不随页面草稿修改漂移',async t=>{
  const {page}=await render({projects:[projects[0]]});let resolve:any
  const calls=fixtures(t,(_uid,path)=>path.endsWith('/resume')?new Promise(done=>resolve=()=>done(reply({status:'queued'}))):undefined)
  await page.open();const pending=page.resumeAll();await page.resumeAll();page.items[0].plan.recipe.mode='publish'
  const writes=calls.filter(c=>c.path.endsWith('/resume'));assert.equal(writes.length,1);assert.equal(writes[0].body.recipe.mode,'draft')
  resolve();await pending
})
test('HTTP400方案冲突也清除旧计划并要求重新检查',async t=>{
  const {page}=await render({projects:[projects[0]]});const calls=fixtures(t,(_uid,path)=>path.endsWith('/resume')?reply({code:'WORKFLOW_PLAN_CHANGED',message:'输入已变化'},false,400):undefined)
  await page.open();await page.resumeAll();assert.equal(page.items[0].plan,null);assert.equal(page.canConfirm,false)
  assert.match(page.items[0].blockers[0],/重新检查/);await page.resumeAll();assert.equal(calls.filter(c=>c.path.endsWith('/resume')).length,1)
})
test('慢检查可以取消；旧响应不得复活抽屉或替换新检查',async t=>{
  const {page}=await render({projects:[projects[0]]});let count=0;const gates:any[]=[]
  t.mock.method(globalThis,'fetch',(url:any,options:any)=>{
    count++;if(count<=2)return new Promise(done=>gates.push({signal:options.signal,resolve:()=>done(reply(String(url).endsWith('/status')?{run:null}:{steps:[],chain:null}))}))
    return Promise.resolve(reply(String(url).endsWith('/plan')?planOf('one'):String(url).endsWith('/status')?{run:{id:'r',status:'failed',mode:'draft'}}:{steps:[],chain:null}))
  })
  const old=page.open();assert.equal(page.checking,true);page.close();assert.ok(gates.every(g=>g.signal.aborted))
  await page.open();assert.equal(page.ready.length,1);gates.forEach(g=>g.resolve());await old
  assert.equal(page.show,true);assert.equal(page.ready.length,1);assert.equal(page.items.length,1)
})
test('超时与读取失败逐项显示，检查失败不能确认旧结果',async t=>{
  const {page}=await render();fixtures(t,(_uid,path)=>path===''?Promise.reject(new DOMException('超时','TimeoutError')):undefined)
  await page.open();assert.equal(page.ready.length,0);assert.equal(page.items.length,2);assert.match(page.items[0].blockers[0],/超时/)
})
test('共享恢复方案显示早于失败点的起点、逐步动作与解释',async()=>{
  const app=createSSRApp({render:()=>h(Recovery,{recovery:recovery()})});setup(app);const html=await renderToString(app)
  assert.match(html,/上次停在/);assert.match(html,/翻译/);assert.match(html,/本次从/);assert.match(html,/语义整理/)
  assert.match(html,/data-action="reuse"/);assert.match(html,/data-action="update"/);assert.match(html,/补齐待配音句/)
})
test('复用步骤紧凑显示且理由仍可访问，需要执行的步骤保留完整说明',async()=>{
  const app=createSSRApp({render:()=>h(Recovery,{recovery:{start_label:'翻译',rows:[
    {key:'split',label:'语义整理',action:'reuse',reason:'本次所需产物有效，直接复用'},
    {key:'translate',label:'翻译',action:'retry',reason:'上次失败，需重新翻译'},
  ]}})});setup(app);const html=await renderToString(app)
  assert.match(html,/有效步骤直接复用/);assert.match(html,/class="recovery-start"/)
  const reused=html.match(/<li[^>]*data-step="split"[\s\S]*?<\/li>/)?.[0]||''
  assert.match(reused,/is-reused/);assert.match(reused,/title="本次所需产物有效，直接复用"/)
  assert.match(reused,/class="visually-hidden"/);assert.doesNotMatch(reused,/<p\b/)
  const retry=html.match(/<li[^>]*data-step="translate"[\s\S]*?<\/li>/)?.[0]||''
  assert.match(retry,/<p[^>]*>上次失败，需重新翻译<\/p>/)
})
test('所有入口统一检查恢复且项目选择包含隐藏选择，无直接续跑请求',()=>{
  const projectsSource=readFileSync(new URL('../src/views/ProjectsView.vue',import.meta.url),'utf8')
  const stepSource=readFileSync(new URL('../src/views/StepView.vue',import.meta.url),'utf8')
  assert.match(projectsSource,/recoveryProjects/);assert.match(projectsSource,/\[\.\.\.sel\.value\]/)
  assert.doesNotMatch(projectsSource,/resumeAllHeld|resumeOneChain|chain\/resume/)
  assert.match(stepSource,/BatchWorkflowResume/);assert.doesNotMatch(stepSource,/@click="run\(false, true\)"/)
})
