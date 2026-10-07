import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia } from 'pinia'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
let server:ViteDevServer,Panel:any,useNavStore:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/WorkflowPanel.vue')).default
  ;({useNavStore}=await server.ssrLoadModule('/src/stores/nav.ts'))
  mock.method(globalThis,'fetch',()=>{throw new Error('不允许未声明网络请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
const version={id:'a',name:'原声双语',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}}
const recipe={mode:'publish',part_titles:{a:'原声版'},version_ids:['a']}
const plan=()=>({recipe:structuredClone(recipe),steps:['download','assemble'],labels:{download:'下载',assemble:'合成'},version_specs:[version],blockers:[],warnings:[],fingerprint:'f',recovery_fingerprint:'recovery-f',recovery:{start_step:'split',start_label:'语义整理',previous_step:'translate',previous_label:'翻译',reason:'输入已变化',rows:[{key:'download',label:'下载',action:'reuse',reason:'已有结果可复用'},{key:'split',label:'语义整理',action:'update',reason:'设置已变化'}]},settings:{visibility:'public',season_id:1},account_name:'测试账号'})
const state=()=>({recipe:structuredClone(recipe),version_catalog:{versions:[version]},runs:[]})
async function render(seed:(p:any,n:any)=>void=()=>{}){
  let page:any
  const pinia=createPinia(),nav=useNavStore(pinia)
  nav.$patch({uid:'fixture',workflow:{mode:'publish',mode_source:{kind:'series',name:'旅行系列'},run:null}})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Panel,{uid:'fixture'})})})})
  app.use(pinia);app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='WorkflowPanel'){page=this.$.setupState;page.state=state();page.mode='publish';seed(page,nav)}}})
  const html=await renderToString(app);return {page,nav,html}
}
test('运行卡显示执行终点与统一来源，不重复保存配置或自行轮询完整方案',async()=>{
  const {html,page}=await render()
  assert.match(html,/自动运行/);assert.match(html,/使用各步骤已保存的设置/);assert.match(html,/自动投稿/)
  assert.match(html,/data-source-kind="series"/);assert.match(html,/检查并运行完整流程/);assert.equal(page.save,undefined)
  const source=readFileSync(new URL('../src/components/WorkflowPanel.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/保存到本项目|保存为全局默认|PublishSettingsForm|setTimeout|dialog.warning|rights_confirmed|n-checkbox/)
  assert.match(source,/PublicationRightsHint v-if="mode==='publish'"/)
})
test('打开即读取并预检，通过后可明确确认，无需额外勾选且不会自动启动',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([url,options?.method,options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?plan():state()}
  })
  await page.open()
  assert.equal(page.show,true);assert.equal(page.mode,'publish')
  assert.deepEqual(calls.map(c=>c[1]),['GET','POST'])
  assert.deepEqual(calls[1][2],{mode:'publish'})
  assert.equal(page.canConfirm,true)
  assert.equal(page.confirmLabel,'确认制作并投稿')
  assert.ok(page.preview);assert.ok(calls.every(c=>!c[0].endsWith('/start')))
})
test('改终点后预检只传当次意图；继续任务还校验对应运行记录',async t=>{
  const {page}=await render();page.mode='draft';page.selectedRun='run-old';const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,JSON.parse(String(options?.body))]);return {ok:true,json:async()=>({...plan(),recipe:{...recipe,mode:'draft'}})}})
  await page.inspect()
  assert.deepEqual(calls,[['/api/projects/fixture/workflow/plan',{mode:'draft',resume_id:'run-old'}]])
  assert.equal(page.canConfirm,true)
})
test('一次明确确认启动冻结范围；双击只发一次，启动回执即锁定手动步骤',async t=>{
  const {page,nav}=await render(p=>{p.preview=plan();p.show=true})
  const writes:any[]=[];let resolve:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(!String(url).endsWith('/start'))return new Promise(()=>{})
    writes.push(JSON.parse(String(options?.body)));return new Promise(done=>{resolve=()=>done({ok:true,json:async()=>({id:'run',status:'queued',recipe,plan:plan()})})})
  })
  assert.equal(writes.length,0)
  const starting=page.confirmStart();await page.confirmStart();page.preview.recipe.part_titles.a='后来改名'
  assert.equal(writes.length,1);assert.equal(writes[0].recipe.part_titles.a,'原声版');assert.equal(writes[0].confirmed,true)
  resolve();await starting
  assert.equal(page.show,false);assert.equal(nav.busy,true)
})
test('阻塞或终点不一致不允许启动，取消不发请求',async()=>{
  const {page}=await render(p=>{p.preview={...plan(),blockers:['缺少字幕']}})
  await page.confirmStart();assert.equal(page.canConfirm,false)
  page.preview=plan();page.mode='draft';await page.confirmStart();assert.equal(page.canConfirm,false)
  page.close();await page.confirmStart();assert.equal(page.canConfirm,false);assert.equal(page.busy,false)
})
test('启动响应失败可重试，同一预检沿用请求ID，保留确认内容',async t=>{
  const {page}=await render(p=>{p.preview=plan();p.show=true});const ids:string[]=[]
  t.mock.method(globalThis,'fetch',async(_url,options)=>{ids.push(JSON.parse(String(options?.body)).request_id);throw new Error('连接中断')})
  await page.confirmStart();await page.confirmStart()
  assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);assert.equal(page.error,'连接中断');assert.equal(page.show,true);assert.equal(page.busy,false)
})
test('等待审核不显示新建运行；暂停制作可继续，已有稿件引导继续现有记录',async()=>{
  const monitoring=await render((_p,n)=>n.workflow.run={id:'r',mode:'publish',status:'monitoring',phase:'审核中',total:2,cursor:2,publication_id:'pub'})
  assert.match(monitoring.html,/停止自动查询/);assert.doesNotMatch(monitoring.html,/检查并运行完整流程/)
  await monitoring.page.open();assert.equal(monitoring.page.show,false)
  const paused=await render((_p,n)=>n.workflow.run={id:'r',mode:'publish',status:'paused',phase:'合成',total:2,cursor:1})
  assert.match(paused.html,/检查恢复方案/)
  const ready=await render((_p,n)=>{n.workflow.existing_publication_id='pub';n.workflow.run={id:'r',mode:'draft',status:'ready',phase:'就绪',publication_id:'pub'}})
  assert.match(ready.html,/查看或继续现有稿件/);assert.doesNotMatch(ready.html,/检查并运行完整流程/)
})
test('预检失败清除旧通过结果，不允许继续用旧快照启动',async t=>{
  const {page}=await render(p=>{p.preview=plan()})
  t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({message:'配置读取失败'})}))
  await page.inspect();assert.equal(page.preview,null);assert.equal(page.canConfirm,false);assert.equal(page.error,'配置读取失败')
})

test('耗时预检可以取消，旧响应不会复活抽屉或确认按钮',async t=>{
  const {page}=await render();let resolve:any,signal:any
  t.mock.method(globalThis,'fetch',async(_url,options)=>{signal=options?.signal;return new Promise(done=>resolve=()=>done({ok:true,json:async()=>plan()}))})
  page.show=true
  const pending=page.inspect();assert.equal(page.checking,true)
  page.close();assert.equal(signal.aborted,true);assert.equal(page.busy,false)
  resolve();await pending
  assert.equal(page.show,false);assert.equal(page.preview,null);assert.equal(page.canConfirm,false)
})

test('恢复确认发送权威恢复指纹并沿用同一请求UUID，缺少恢复方案不可确认',async t=>{
  const {page}=await render(p=>{p.selectedRun='run-old';p.preview=plan();p.show=true});const writes:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{writes.push([String(url),JSON.parse(String(options?.body))]);throw new Error('回执未收到')})
  assert.equal(page.confirmLabel,'确认恢复并投稿');await page.confirmStart();await page.confirmStart()
  assert.equal(writes[0][0],'/api/projects/fixture/workflow/run-old/resume')
  assert.equal(writes[0][1].recovery_fingerprint,'recovery-f');assert.equal(writes[0][1].request_id,writes[1][1].request_id)
  page.preview={...plan(),recovery_fingerprint:undefined};assert.equal(page.canConfirm,false)
})
test('HTTP400恢复方案冲突清除旧预检，不再发送旧确认',async t=>{
  const {page}=await render(p=>{p.selectedRun='run-old';p.preview=plan();p.show=true});let writes=0
  t.mock.method(globalThis,'fetch',async()=>{writes++;return {ok:false,status:400,json:async()=>({code:'WORKFLOW_PLAN_CHANGED',message:'恢复范围已变化'})}})
  await page.confirmStart();assert.equal(page.preview,null);assert.equal(page.canConfirm,false);assert.match(page.error,/重新检查/)
  await page.confirmStart();assert.equal(writes,1)
})
