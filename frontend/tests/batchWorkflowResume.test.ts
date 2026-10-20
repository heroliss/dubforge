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
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'
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
const ttsBlocker = '配音服务未就绪，请先在配音页启动服务'
const onlineService = {backend:'indextts25',online:true,status:'online',managed:true,can_start:false,can_stop:true,pid:42,message:'',log_path:null}
const reply = (data:any,ok=true,status=200) => ({ok,status,json:async()=>data})
async function until(predicate:()=>boolean) {
  for(let i=0;i<120&&!predicate();i++)await Promise.resolve()
  assert.ok(predicate(),'模拟异步检查应在有限轮次内到达预期状态')
}
async function render(options:any={},seed:(page:any)=>void=()=>{}) {
  let page:any,componentProps:any
  const input=reactive({projects:projects.map(p=>({...p})),...options})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const feedback:any[]=[], resumed:any[]=[]
  const app=createSSRApp({render:()=>h(Panel,{...input,onRunFeedback:(row:any)=>feedback.push(row),onResumed:()=>resumed.push(true)})});app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='BatchWorkflowResume'){page=this.$.setupState;componentProps=this.$.props;seed(page)}}})
  const context:any={}
  const html=await renderToString(app,context)+Object.values(context.teleports || {}).join('');return {page,html,input,componentProps,feedback,resumed}
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
  assert.deepEqual(calls[2].body,{resume_id:'run-one'})
})

test('逐项恢复立即上报核对与受理，不等待另一个项目的慢请求且不重复提交',async t=>{
  const {page,feedback,resumed}=await render()
  let release:(value:any)=>void=()=>{}
  const waiting=new Promise(resolve=>{release=resolve})
  const calls=fixtures(t,(uid,path)=>path.endsWith('/resume')&&uid==='two'?waiting:undefined)
  await page.open();const running=page.resumeAll();await page.resumeAll()
  await until(()=>feedback.some(row=>row.projectUids[0]==='two'&&row.stage==='submitting'))
  assert.deepEqual(feedback.filter(row=>row.projectUids[0]==='one').map(row=>row.stage),['checking','submitting','accepted'])
  assert.equal(resumed.length,1,'第一项回执即通知进度，不等待第二项')
  assert.equal(page.items[0].run.status,'queued')
  assert.equal(calls.filter(call=>call.uid==='one'&&call.path.endsWith('/resume')).length,1)
  release(reply({status:'queued'}));await running;assert.equal(resumed.length,2)
})

test('恢复回执未知后重新检查同一方案仍复用UUID，服务返回失败回执显示原因',async t=>{
  const {page,feedback}=await render({projects:[projects[0]]});let attempts=0
  const calls=fixtures(t,(_uid,path)=>path.endsWith('/resume')?(++attempts===1?Promise.reject(new Error('连接中断')):reply({id:'run-one',status:'failed',error:'配音服务仍有请求尚未结束，请先核对服务。'})):undefined)
  await page.open();await page.resumeAll();assert.equal(feedback.at(-1).stage,'unknown')
  const first=calls.find(call=>call.path.endsWith('/resume')).body.request_id
  await page.inspect();await page.resumeAll()
  assert.equal(calls.filter(call=>call.path.endsWith('/resume')).at(-1).body.request_id,first)
  assert.equal(feedback.at(-1).stage,'blocked');assert.match(page.items[0].result,/配音服务仍有请求尚未结束/)
  assert.equal(page.items[0].submitted,true);assert.equal(page.canConfirm,false)
})

test('原稿更新即使已有发布记录也恢复原workflow，不降级为制作或另发新稿',async t=>{
  const {page}=await render({projects:[projects[0]]})
  const calls=fixtures(t,(_uid,path)=>path==='/workflow/status'?reply({run:{id:'saved-update',mode:'update',status:'failed',publication_id:'original',update_id:'task'}})
    :path==='/workflow/plan'?reply({...planOf('one'),recipe:{mode:'update',update:{publication_id:'original',replacements:[{old_cid:11,version_id:'a'}]}},
      update_plan:{bvid:'BVOriginal',title:'原稿标题',account_name:'历史账号',parts:[{old_cid:11,title:'原声版',version_id:'a',version_name:'原声双语'}],chapters:{enabled:true}}}):undefined)
  await page.open();assert.equal(page.items[0].kind,'workflow');assert.equal(page.ready.length,1)
  assert.deepEqual(calls.find(call=>call.path==='/workflow/plan').body,{resume_id:'saved-update'});assert.equal(page.updateCount,1);assert.equal(page.publicationCount,1)
  const visible=await render({},p=>{p.show=true;p.items=page.items})
  assert.match(visible.html,/沿用原BV与原交付终点/);assert.match(visible.html,/BVOriginal/);assert.match(visible.html,/新成片时间轴/)
  assert.doesNotMatch(visible.html,/仅制作成片，不上传、不投稿/)
})

test('原稿交付恢复显示已确认计划，未知结果只有核对语义且不展示配音制作策略',async()=>{
  const task={id:'task',status:'unknown',execution:{stages:{upload:{status:'completed'},edit:{status:'unknown'},chapters:{status:'pending'}}}}
  const restored={...planOf('one'),recipe:{mode:'update'},update_plan:{bvid:'BVOriginal',parts:[],chapters:{enabled:true}},publication_update:task}
  const {html,page}=await render({},p=>{p.show=true;p.items=[{uid:'one',title:'项目一',kind:'workflow',selected:true,blockers:[],policy:defaultDubbingRunPolicy(),plan:restored}]})
  assert.equal(page.singleReconcile,true);assert.match(html,/确认核对原稿更新结果/);assert.match(html,/先只读核对平台结果/)
  assert.match(html,/继续已确认的原稿更新方案/);assert.doesNotMatch(html,/按当前保存的设置合成|data-testid="dubbing-run-options"|恢复继续使用保留的配音策略/)
})

test('恢复读取已保存策略，修改链方案重检，确认带入返回的冻结策略',async t=>{
  const {page}=await render({kind:'chain'}),policy={...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:4}
  const calls=fixtures(t,(_uid,path)=>path===''?reply({steps:[],chain:held}):path==='/chain/plan'
    ? reply({...planOf('one'),dubbing:{policy,resumed:true,completed:8,generate:4,reuse:8}}):undefined)
  await page.open();assert.deepEqual(page.items[0].policy,policy)
  assert.deepEqual(calls.find(call=>call.path==='/chain/plan').body,{})
  page.onDubbingChange(page.items[0],{...policy,line_settings:'project'})
  assert.equal(page.canConfirm,false)
  for(let i=0;i<40&&page.busy;i++)await Promise.resolve()
  const checks=calls.filter(call=>call.path==='/chain/plan')
  assert.equal(checks[2].body.dubbing.line_settings,'project')
  await page.resumeAll()
  assert.deepEqual(calls.find(call=>call.path==='/chain/resume').body.dubbing,policy,'以服务端返回并展示的策略为准')
})

test('归档项目不预检恢复方案，保留明确恢复项目原因和查看入口',async t=>{
  const {page}=await render()
  const calls=fixtures(t,(uid,path)=>uid==='one'&&path===''?reply({archived:true,steps:[],chain:held}):undefined)
  await page.open()
  assert.deepEqual(page.ready.map((item:any)=>item.uid),['two'])
  assert.match(page.items[0].blockers.join('；'),/已归档.*恢复/)
  assert.equal(page.items[0].link,'project')
  assert.ok(calls.every(call=>call.uid!=='one'||!call.path.endsWith('/plan')))
})

test('旧恢复方案提交前发现项目已归档，跳过该项目且保留其它恢复回执',async t=>{
  const {page}=await render();let archived=false
  const calls=fixtures(t,(uid,path)=>uid==='one'&&path===''?reply({archived,steps:[],chain:null}):undefined)
  await page.open();assert.equal(page.ready.length,2)
  archived=true;await page.resumeAll()
  assert.deepEqual(calls.filter(call=>call.path.endsWith('/resume')).map(call=>call.uid),['two'])
  assert.match(page.items[0].result,/已归档.*恢复/);assert.equal(page.items[0].plan,null)
  assert.equal(page.items[1].submitted,true)
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
  assert.match(page.items[1].result,/恢复未继续/);assert.doesNotMatch(page.items[1].result,/已交给后台继续执行/)
})
test('确认双击只发送一次，发送内容不随页面草稿修改漂移',async t=>{
  const {page}=await render({projects:[projects[0]]});let resolve:any
  const calls=fixtures(t,(_uid,path)=>path.endsWith('/resume')?new Promise(done=>resolve=()=>done(reply({status:'queued'}))):undefined)
  await page.open();const pending=page.resumeAll();await page.resumeAll();page.items[0].plan.recipe.mode='publish'
  for(let i=0;i<20&&!calls.some(c=>c.path.endsWith('/resume'));i++)await Promise.resolve()
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
test('配音服务在线只更新受阻项目的恢复方案与提交身份，不自动恢复制作',async t=>{
  const {page}=await render({projects:[projects[0]]});let online=false,checks=0
  const calls=fixtures(t,(uid,path)=>path==='/workflow/plan'?reply({...planOf(uid),
    fingerprint:'fp-'+(++checks),recovery_fingerprint:'rf-'+checks,blockers:online?[]:[ttsBlocker]}):undefined)
  await page.open();const item=page.items[0],requestId=item.requestId
  assert.equal(page.canConfirm,false);assert.equal(page.hasTtsServiceBlocker(item),true)
  online=true;const start=calls.length
  await page.onTtsServiceStatus(item,onlineService)
  const refreshed=page.items[0]
  assert.deepEqual(calls.slice(start).map(call=>[call.uid,call.path]),[
    ['one',''],['one','/workflow/status'],['one','/workflow/plan'],
  ])
  assert.equal(refreshed.plan.fingerprint,'fp-2');assert.equal(refreshed.plan.recovery_fingerprint,'rf-2')
  assert.equal(refreshed.blockers.length,0);assert.equal(page.hasTtsServiceBlocker(refreshed),false)
  assert.notEqual(refreshed.requestId,requestId)
  assert.ok(calls.every(call=>!call.path.endsWith('/resume')&&!call.path.endsWith('/start')))
})

test('服务重检保留其他项目的取消选择、已提交结果与未知回执UUID',async t=>{
  const range=[...projects,{uid:'three',title:'项目三'},{uid:'four',title:'项目四'}]
  const {page}=await render({projects:range});let online=false
  const calls=fixtures(t,(uid,path)=>uid==='one'&&path==='/workflow/plan'
    ?reply({...planOf(uid),blockers:online?[]:[ttsBlocker]})
    :uid==='four'&&path.endsWith('/resume')?Promise.reject(new Error('连接中断，回执未知')):undefined)
  await page.open();page.select(page.items[1],false);await page.resumeAll()
  assert.equal(page.items[2].submitted,true);assert.equal(page.items[3].submitted,false)
  const others=page.items.slice(1),snapshots=others.map((item:any)=>({selected:item.selected,submitted:item.submitted,
    requestId:item.requestId,result:item.result,plan:item.plan})),results=JSON.parse(JSON.stringify(page.results))
  const start=calls.length;online=true
  await page.onTtsServiceStatus(page.items[0],onlineService)
  assert.ok(calls.slice(start).every(call=>call.uid==='one'&&!call.path.endsWith('/resume')))
  for(let i=0;i<others.length;i++){
    const item=page.items[i+1],snapshot=snapshots[i]
    assert.equal(item,others[i]);assert.equal(item.plan,snapshot.plan)
    assert.equal(item.selected,snapshot.selected);assert.equal(item.submitted,snapshot.submitted)
    assert.equal(item.requestId,snapshot.requestId);assert.equal(item.result,snapshot.result)
  }
  assert.deepEqual(page.results,results)
})

test('重复在线状态不会在同一配音阻塞下无限重检，失败状态不发检查请求',async t=>{
  const {page}=await render({projects:[projects[0]]})
  const calls=fixtures(t,(uid,path)=>path==='/workflow/plan'?reply({...planOf(uid),blockers:[ttsBlocker]}):undefined)
  await page.open();await page.onTtsServiceStatus(page.items[0],{...onlineService,online:false,status:'failed',message:'模型启动失败'})
  assert.equal(calls.filter(call=>call.path==='/workflow/plan').length,1)
  await page.onTtsServiceStatus(page.items[0],onlineService)
  await page.onTtsServiceStatus(page.items[0],onlineService)
  await page.onTtsServiceStatus(page.items[0],onlineService)
  assert.equal(calls.filter(call=>call.path==='/workflow/plan').length,2)
  assert.equal(page.canConfirm,false);assert.deepEqual(page.items[0].blockers,[ttsBlocker])
})

test('服务在线重检仍保留其他阻塞，不能确认被参考音频问题挡住的计划',async t=>{
  const {page}=await render({projects:[projects[0]]});let online=false
  fixtures(t,(uid,path)=>path==='/workflow/plan'?reply({...planOf(uid),
    blockers:online?['音色参考音频不存在，请选择预设或原声参考模式']:[ttsBlocker]}):undefined)
  await page.open();online=true;await page.onTtsServiceStatus(page.items[0],onlineService)
  assert.equal(page.hasTtsServiceBlocker(page.items[0]),false)
  assert.match(page.items[0].blockers[0],/音色参考音频不存在/);assert.equal(page.canConfirm,false)
})

test('制作链服务重检保留配音策略并更新链计划，不误走完整流程恢复',async t=>{
  const {page}=await render({projects:[projects[0]],kind:'chain'});let online=false,checks=0
  const policy={...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:4}
  const calls=fixtures(t,(uid,path)=>path===''
    ?reply({steps:[],chain:held})
    :path==='/chain/plan'?reply({...planOf(uid),fingerprint:'chain-'+(++checks),recovery_fingerprint:undefined,
      dubbing:{policy,resumed:true,generate:4,reuse:8},blockers:online?[]:[ttsBlocker]}):undefined)
  await page.open();const requestId=page.items[0].requestId
  online=true;await page.onTtsServiceStatus(page.items[0],onlineService)
  const checksSent=calls.filter(call=>call.path==='/chain/plan')
  assert.equal(checksSent.length,2);assert.deepEqual(checksSent[1].body.dubbing,policy)
  assert.equal(page.items[0].kind,'chain');assert.equal(page.items[0].plan.fingerprint,'chain-2')
  assert.deepEqual(page.items[0].policy,policy);assert.notEqual(page.items[0].requestId,requestId)
  assert.ok(calls.every(call=>call.path!=='/workflow/plan'&&!call.path.endsWith('/resume')))
})

test('批量初检期间收到在线事件合并为一次排队重检，不中断其他项目检查',async t=>{
  const {page}=await render();let online=false,release:any
  const calls=fixtures(t,(uid,path)=>uid==='one'&&path==='/workflow/plan'
    ?reply({...planOf(uid),blockers:online?[]:[ttsBlocker]})
    :uid==='two'&&path===''?new Promise(done=>release=()=>done(reply({steps:[],chain:null}))):undefined)
  const opening=page.open();await until(()=>page.items.length===1&&!!release)
  const item=page.items[0];assert.equal(page.checking,true)
  online=true;await page.onTtsServiceStatus(item,onlineService);await page.onTtsServiceStatus(item,onlineService)
  assert.equal(calls.filter(call=>call.uid==='one'&&call.path==='/workflow/plan').length,1)
  release();await opening
  assert.equal(calls.filter(call=>call.uid==='one'&&call.path==='/workflow/plan').length,2)
  assert.equal(calls.filter(call=>call.uid==='two'&&call.path==='/workflow/plan').length,1)
  assert.equal(page.items.length,2);assert.equal(page.items[0].blockers.length,0)
  assert.equal(page.busy,false);assert.equal(page.checking,false)
  assert.ok(calls.every(call=>!call.path.endsWith('/resume')))
})

for(const reason of ['closed','scope','disabled','old-item'] as const){
  test(`服务状态回调在${reason}失效后不读取或改写计划`,async t=>{
    const {page,input,componentProps}=await render({projects:[{...projects[0]}]})
    const calls=fixtures(t,(uid,path)=>path==='/workflow/plan'?reply({...planOf(uid),blockers:[ttsBlocker]}):undefined)
    await page.open();const staleItem=page.items[0]
    if(reason==='closed')page.close()
    else if(reason==='scope')input.projects.push({...projects[1]})
    else if(reason==='disabled')componentProps.disabled=true
    else await page.inspect()
    const count=calls.length,currentItem=page.items[0],fingerprint=currentItem.plan.fingerprint
    await page.onTtsServiceStatus(staleItem,onlineService)
    assert.equal(calls.length,count);assert.equal(page.items[0],currentItem)
    assert.equal(currentItem.plan.fingerprint,fingerprint)
  })
}

test('服务重检的慢响应在关闭重开后不能覆盖新抽屉方案',async t=>{
  const {page}=await render({projects:[projects[0]]});let checks=0,release:any
  const calls=fixtures(t,(uid,path)=>path==='/workflow/plan'
    ?++checks===2?new Promise(done=>release=()=>done(reply({...planOf(uid),fingerprint:'late-plan',blockers:[]})))
      :reply({...planOf(uid),fingerprint:'fp-'+checks,blockers:[ttsBlocker]}):undefined)
  await page.open();const refreshing=page.onTtsServiceStatus(page.items[0],onlineService)
  await until(()=>!!release);page.close();await page.open()
  const freshItem=page.items[0],freshRequestId=freshItem.requestId
  release();await refreshing
  assert.equal(page.show,true);assert.equal(page.items[0],freshItem)
  assert.equal(freshItem.plan.fingerprint,'fp-3');assert.equal(freshItem.requestId,freshRequestId)
  assert.deepEqual(freshItem.blockers,[ttsBlocker]);assert.equal(page.canConfirm,false)
  assert.ok(calls.every(call=>!call.path.endsWith('/resume')))
})

test('恢复抽屉在精确配音阻塞旁复用服务控制与失效禁用条件',()=>{
  const source=readFileSync(new URL('../src/components/BatchWorkflowResume.vue',import.meta.url),'utf8')
  assert.match(source,/import TtsServiceControl from ['"]\.\/TtsServiceControl\.vue['"]/)
  assert.match(source,/v-if="hasTtsServiceBlocker\(item\)"[\s\S]*?<TtsServiceControl/)
  assert.match(source,/:uid="item.uid"/)
  assert.match(source,/:disabled="busy\s*\|\|\s*disabled\s*\|\|\s*inspectedScope\s*!==\s*scopeKey"/)
  assert.match(source,/@status="[^\"]*onTtsServiceStatus\(item,\s*[^)]+\)/)
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


test('旧恢复方案缺少label时翻译步骤键，不把内部步骤名直接给用户',async()=>{
  const app=createSSRApp({render:()=>h(Recovery,{recovery:{previous_step:'translate',start_step:'split',rows:[
    {key:'tts',label:'tts',action:'run',reason:'补齐配音'}, {key:'future_internal_step',action:'run',reason:'兼容后续版本'},
  ]}})});setup(app);const html=await renderToString(app)
  assert.match(html,/>翻译</);assert.match(html,/>语义整理</);assert.match(html,/>字幕与配音</);assert.match(html,/>处理步骤</)
  assert.doesNotMatch(html,/<strong>(?:translate|split|tts|future_internal_step)<\/strong>/)
})


test('完整流程恢复抽屉展示同一冻结发布目标的可读名称',async()=>{
  const {html}=await render({},page=>{page.show=true;page.items=[{uid:'one',title:'项目一',kind:'workflow',
    selected:true,blockers:[],plan:{...planOf('one'),recipe:{mode:'publish',account_id:'12'},
      settings:{season_id:34,section_id:56},settings_display:{account_id:'12',season_id:34,section_id:56,
        account_name:'旅行账号',season_title:'旅行合集',section_title:'正篇',source:'cache'}}}]})
  assert.match(html,/旅行账号/);assert.match(html,/旅行合集/);assert.match(html,/正篇/)
  assert.match(html,/class="target-id"/);assert.match(html,/名称为最近读取的快照/)
})


test('只上传的原稿恢复显示原终点，不允许改配音策略或暗示自动提交',async t=>{
  const restored={...planOf('one'),recipe:{mode:'update',delivery_mode:'upload',update:{replace_videos:false,sync_fields:['title']}},dubbing:{policy:defaultDubbingRunPolicy(),needed:false},update_plan:{bvid:'BVOriginal',parts:[],values:{title:'新标题'}}}
  const {page,html}=await render({},p=>{p.show=true;p.items=[{uid:'one',title:'项目一',kind:'workflow',selected:true,blockers:[],policy:defaultDubbingRunPolicy(),plan:restored}]})
  assert.equal(page.uploadCount,1);assert.equal(page.publicationCount,0)
  assert.match(html,/上传后等待确认投稿|上传后等待手动确认投稿/);assert.match(html,/不自动提交/)
  assert.doesNotMatch(html,/仅制作成片，不上传、不投稿|制作后自动上传并投稿/)
  const saved=page.items[0].plan;page.onDubbingChange(page.items[0],{...defaultDubbingRunPolicy(),mode:'all'})
  assert.equal(page.items[0].plan,saved);assert.equal(page.busy,false)
})


test('上游需更新时恢复方案将旧复用行标为重新核对，不承诺直接复用',async()=>{
 const app=createSSRApp({render:()=>h(Recovery,{recovery:{rows:[{key:'translate',label:'翻译',action:'reuse',reason:'原文件存在'}]},pendingSteps:['translate']})});setup(app)
 const html=await renderToString(app)
 assert.match(html,/更新输入后核对/);assert.match(html,/上游需更新/);assert.doesNotMatch(html,/有效步骤直接复用/)
})
