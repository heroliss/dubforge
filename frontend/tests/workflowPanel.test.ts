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
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'
import { runningWorkflowStep } from './fixtures/workflowExecution.ts'
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
const updatePlan=()=>({...plan(),recipe:{...recipe,mode:'update',update:{publication_id:'original',replacements:[{old_cid:11,version_id:'a'}]}},
  update_plan:{publication_id:'original',bvid:'BVOriginal',title:'原稿标题',account_name:'历史账号',
    parts:[{old_cid:11,title:'原声版',version_id:'a',version_name:'原声双语'}],chapters:{enabled:true,translate:true},collection:{enabled:false}},fingerprint:'update-f'})
async function render(seed:(p:any,n:any)=>void=()=>{}, panelProps:any={}){
  let page:any
  const pinia=createPinia(),nav=useNavStore(pinia)
  nav.$patch({uid:'fixture',workflow:{mode:'publish',mode_source:{kind:'series',name:'旅行系列'},run:null}})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Panel,{uid:'fixture',...panelProps})})})})
  app.use(pinia);app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='WorkflowPanel'){page=this.$.setupState;page.state=state();page.mode='publish';seed(page,nav)}}})
  const context:any={}
  const html=await renderToString(app,context)+Object.values(context.teleports || {}).join('');return {page,nav,html}
}

test('正常运行只读取项目保存目标，不传终点或制作范围覆盖且不自动启动',async t=>{
  const {page,html}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.method,options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?plan():state()}
  })
  await page.open()
  assert.deepEqual(calls.map(call=>call[1]),['GET','POST'])
  assert.deepEqual(calls[1][2],{target_driven:true})
  assert.equal(page.confirmLabel,'确认运行并投稿');assert.equal(page.canConfirm,true)
  assert.match(html,/项目交付终点/);assert.match(html,/交付设置/)
  assert.equal(page.onProductionScope,undefined);assert.equal(page.onModeChange,undefined)
  assert.equal(page.onUpdateSource,undefined);assert.equal(page.onDubbingChange,undefined)
  assert.ok(calls.every(call=>!call[0].endsWith('/start')))
})

test('工作流主进度统一使用当前子Job句数与ETA，父阶段记录折叠保留',async()=>{
  const fixture=runningWorkflowStep(),before=structuredClone(fixture)
  const {html}=await render((_page,nav)=>{
    nav.workflow={mode:'publish',run:fixture.run};nav.workflowPlan=fixture.plan;nav.steps=[fixture.step];nav.stepsLoaded=true
  })
  assert.match(html,/处理第 454 句配音/);assert.match(html,/已完成 453 \/ 646 句/);assert.match(html,/本阶段 70.5%/)
  assert.match(html,/已完成工作阶段/);assert.match(html,/流程阶段记录 · 2 项/)
  assert.match(html,/class="workflow-stage-history"/);assert.match(html,/翻译/)
  assert.doesNotMatch(html,/距进度更新 54|距流程状态更新 54|5 \/ 8 制作步骤/)
  assert.equal((html.match(/aria-label="执行过程"/g)||[]).length,1)
  assert.deepEqual(fixture,before)
})

test('工作流合成阶段ETA只跟随当前编码，音频复核不借旧编码剩余时间',async()=>{
  for(const phase of ['correcting_output_audio','encoding','encoding_timeline']) {
    const fixture=runningWorkflowStep()
    Object.assign(fixture.run,{cursor:7,phase:'合成成片'})
    Object.assign(fixture.run.execution,{phase:'production.assemble',label:'合成成片'})
    Object.assign(fixture.step,{key:'assemble',label:'合成成片',progress:49.5,eta_scope:'phase',eta_seconds:phase==='correcting_output_audio'?.1:120})
    Object.assign(fixture.step.execution,{phase,label:phase==='correcting_output_audio'?'复核并纠正 AAC 编码真峰值':'编码成片',
      completed:phase==='correcting_output_audio'?0:10,total:phase==='correcting_output_audio'?3:100,
      unit:phase==='correcting_output_audio'?'轮':'成片秒',metrics:{},history:[]})
    const {html,nav}=await render((_page,nav)=>{
      nav.workflow={mode:'publish',run:fixture.run};nav.workflowPlan=fixture.plan;nav.steps=[fixture.step];nav.stepsLoaded=true
    })
    try {
      assert.match(html,/aria-label="执行过程"/)
      if(phase==='correcting_output_audio') {
        assert.match(html,/复核并纠正 AAC 编码真峰值/);assert.match(html,/已完成 0 \/ 3 轮/)
        assert.match(html,/49\.5%/);assert.doesNotMatch(html,/当前阶段预计剩余|预计剩余约 5 秒/)
      } else assert.match(html,/当前阶段预计剩余约 2 分钟/)
    } finally {nav.reset()}
  }
})

test('已有store getter保留旧模块投影时，组件仍让执行事实与百分比保持同源',async()=>{
  const fixture=runningWorkflowStep()
  const {html,page}=await render((_page,nav)=>{
    nav.workflow={mode:'publish',run:fixture.run};nav.workflowPlan=fixture.plan;nav.steps=[fixture.step];nav.stepsLoaded=true
    Object.defineProperty(nav,'currentExecution',{configurable:true,get:()=>fixture.run.execution})
  })
  assert.equal(page.currentExecution.owner,'step');assert.equal(page.currentStep.job_id,520)
  assert.match(html,/已完成 453 \/ 646 句/);assert.match(html,/本阶段 70.5%/)
  assert.doesNotMatch(html,/5 \/ 8 制作步骤|距进度更新 54/)
})

test('身份不明和停止期间仅显示父状态，不混用子步骤百分比ETA，间隔明确属于流程',async()=>{
  for(const control of ['old-plan','stopping','paused','monitoring']) {
    const fixture=runningWorkflowStep()
    if(control==='old-plan')fixture.plan.id='old'
    else {fixture.run.status=control;fixture.run.execution.state=control==='monitoring'?'waiting':control}
    const {html}=await render((_page,nav)=>{
      nav.workflow={mode:'publish',run:fixture.run};nav.workflowPlan=fixture.plan;nav.steps=[fixture.step];nav.stepsLoaded=true
    })
    assert.doesNotMatch(html,/本阶段 70.5%|已完成 453 \/ 646 句|流程阶段记录/)
    assert.match(html,/距流程状态更新 54 分 13 秒/)
    assert.doesNotMatch(html,/距进度更新 54/)
  }
})

test('已有BV且项目仅制作不擅自改为更新原稿',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?{...plan(),recipe:{...recipe,mode:'produce',delivery_mode:'produce'}}:
      {...state(),has_remote_publication:true,recipe:{...recipe,mode:'produce'}}}
  })
  await page.open();assert.equal(page.mode,'produce');assert.equal(page.canConfirm,true)
  assert.equal(page.newPublicationWarning,false)
  assert.deepEqual(calls[1][1],{target_driven:true})
  assert.ok(calls.every(call=>!call[0].includes('/publishing/publications')))
})

test('明确从步骤继续只增加起点意图，仍读取项目保存的版本和交付设置',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?plan():state()}
  })
  await page.open('', 'translate');assert.deepEqual(calls[1][1],{target_driven:true,from_step:'translate'})
  assert.ok(calls.every(call=>!call[0].endsWith('/start')))
})

test('只上传终点明确等待确认，新稿与原稿都不显示自动投稿授权',async t=>{
  for(const mode of ['upload','update']){
    const checked=mode==='update'?updatePlan():plan()
    checked.recipe={...checked.recipe,mode,delivery_mode:'upload'} as any
    const {page,html}=await render(page=>{page.show=true;page.mode=mode;page.preview=checked})
    assert.equal(page.confirmLabel,'确认运行并上传（待确认投稿）')
    assert.match(html,/等待你在发布页确认投稿/)
    assert.doesNotMatch(html,/确认运行并投稿|确认运行并更新原稿件/)
  }
})

test('制作三个版本仅发布两个时，分P编号严格遵循发布清单',async()=>{
  const versions=[version,{...version,id:'b',name:'配音版'},{...version,id:'c',name:'仅本地收藏版'}]
  const {html}=await render(page=>{page.show=true;page.preview={...plan(),version_specs:versions,
    recipe:{...recipe,version_ids:['b','a'],part_titles:{a:'原声P',b:'配音P'}}}})
  const production=html.match(/<section\b[^>]*aria-label="制作与复用目标版本"[^>]*>[\s\S]*?<\/section>/)?.[0]||''
  const publish=html.match(/<section\b[^>]*aria-label="发布分P范围"[^>]*>[\s\S]*?<\/section>/)?.[0]||''
  assert.match(production,/仅本地收藏版/)
  assert.match(publish,/P1 · 配音P/);assert.match(publish,/P2 · 原声P/)
  assert.doesNotMatch(publish,/仅本地收藏版|P3/)
})

test('恢复只传原任务ID，当前设置终点不扩大保存授权',async t=>{
  const {page}=await render();const calls:any[]=[]
  const old={...recipe,mode:'produce',delivery_mode:'produce',production_steps:['preprocess']}
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?{...plan(),recipe:old}:
      {...state(),recipe:{...recipe,mode:'publish'},runs:[{id:'old',recipe:old}]}}
  })
  await page.open('old');assert.deepEqual(calls[1][1],{resume_id:'old'})
  assert.equal(page.mode,'produce');assert.equal(page.canConfirm,true)
  assert.equal(page.confirmLabel,'确认恢复制作成片')
})

test('重新检查保存设置时旧响应迟到不能覆盖最新确认',async t=>{
  const {page}=await render(page=>page.show=true);const pending:any[]=[]
  t.mock.method(globalThis,'fetch',async(_url,options)=>new Promise(resolve=>pending.push({resolve,signal:options?.signal})))
  const first=page.inspect();page.close();page.show=true;const second=page.inspect()
  pending[1].resolve({ok:true,json:async()=>({...plan(),fingerprint:'latest'})});await second
  pending[0].resolve({ok:true,json:async()=>({...plan(),fingerprint:'obsolete'})});await first
  assert.equal(pending[0].signal.aborted,true);assert.equal(page.preview.fingerprint,'latest')
  assert.equal(page.canConfirm,true)
})
test('归档项目禁用完整流程；已打开的方案不能预检或提交',async()=>{
  const {page,nav,html}=await render((page,nav)=>{nav.archived=true;page.preview=plan();page.show=true})
  assert.match(html,/项目已归档.*恢复项目/);assert.equal(page.canConfirm,false)
  await page.open();await page.inspect();await page.confirmStart()
  assert.equal(page.preview,null);assert.match(page.error,/已归档.*恢复/)
  nav.archived=false
})
test('更新预检警告完整去重显示，末尾通用提示不覆盖前面的具体问题',async()=>{
  const result=await render(page=>{page.show=true;page.mode='update';page.preview={...updatePlan(),update_plan:{...updatePlan().update_plan,
    warnings:['章节时间轴尚无法证明',{message:'请核对源视频与原稿是否一致'},' 章节时间轴尚无法证明 ','原稿曾被驳回，请处理涉及内容']}}})
  assert.match(result.html,/章节时间轴尚无法证明/);assert.match(result.html,/请核对源视频与原稿是否一致/);assert.match(result.html,/原稿曾被驳回/)
  assert.equal((result.html.match(/章节时间轴尚无法证明/g)||[]).length,1)
})

test('真实更新组件显示分P新名称和最终顺序，保留视频时也可核对改名与新增P',async()=>{
  const result=await render(page=>{page.show=true;page.mode='update';page.preview={...updatePlan(),recipe:{...updatePlan().recipe,update:{...updatePlan().recipe.update,replace_videos:false,sync_fields:['part_titles','part_order','add_parts']}},update_plan:{...updatePlan().update_plan,
      remote_parts:[{cid:99123401,title:'旧原声名称'},{cid:99123402,title:'已有配音名称'}],
      parts:[{old_cid:99123401,title:'新原声名称',version_id:'a'},{old_cid:99123402,title:'已有配音名称',version_id:'b'},
        {old_cid:-1,title:'新增解说版',version_id:'c',version_name:'解说',added:true}],
      part_titles:{'99123401':'新原声名称','99123402':'已有配音名称'},part_order:[-1,99123402,99123401]}}
  })
  assert.match(result.html,/P1 · 旧原声名称/);assert.match(result.html,/将改为：新原声名称/)
  assert.match(result.html,/名称已一致：已有配音名称/);assert.match(result.html,/保留原视频文件/)
  const order=result.html.match(/<section\b[^>]*aria-label="更新后分P顺序"[^>]*>[\s\S]*?<\/section>/)?.[0] || ''
  assert.match(order,/新增分P/);assert.ok(order.indexOf('新增解说版')<order.indexOf('已有配音名称'))
  assert.ok(order.indexOf('已有配音名称')<order.indexOf('新原声名称'))
  assert.doesNotMatch(result.html,/99123401|99123402/)
})

test('分P目标名称或顺序关联未知时明确待核对，不借版本名称或位置猜测',async()=>{
  const result=await render(page=>{page.show=true;page.mode='update';page.preview={...updatePlan(),recipe:{...updatePlan().recipe,update:{...updatePlan().recipe.update,replace_videos:false,sync_fields:['part_titles','part_order']}},update_plan:{...updatePlan().update_plan,
      remote_parts:[{cid:88111101,title:'确知的原名称'}],parts:[{old_cid:88111101,title:'确知的原名称',version_id:'a',version_name:'不得作为新标题的版本名'}],
      part_titles:{},part_order:[88111101,88111102]}}
  })
  const order=result.html.match(/<section\b[^>]*aria-label="更新后分P顺序"[^>]*>[\s\S]*?<\/section>/)?.[0] || ''
  assert.equal((order.match(/分P名称待核对/g)||[]).length,2)
  assert.match(result.html,/class="part-title-change"[^>]*>分P名称待核对/)
  assert.doesNotMatch(order,/不得作为新标题|确知的原名称|88111101|88111102/)
  const unselected=await render(page=>{page.show=true;page.mode='update';page.preview={...updatePlan(),recipe:{...updatePlan().recipe,update:{...updatePlan().recipe.update,sync_fields:[]}},update_plan:{...updatePlan().update_plan,part_titles:{'11':'未勾选的新名'},part_order:[11]}}
  })
  assert.doesNotMatch(unselected.html,/class="part-title-change"|aria-label="更新后分P顺序"/)
})

test('已有稿件可明确另发新BV，额外确认前不可提交，勾选后重新预检冻结意图',async t=>{
  const {page}=await render((page,nav)=>{nav.workflow.existing_publication_id='original';page.mode='publish';page.preview=plan()})
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=JSON.parse(String(options?.body));calls.push({path:String(url),body})
    return {ok:true,json:async()=>String(url).endsWith('/plan')?{...plan(),recipe:{...recipe,confirm_new_publication:body.confirm_new_publication}}:{id:'new-publication',status:'queued',recipe:body.recipe,plan:plan()}}
  })
  assert.equal(page.canConfirm,false);await page.confirmStart();assert.equal(calls.length,0)
  page.onNewPublicationConfirmation(true);for(let i=0;i<30&&page.busy;i++)await Promise.resolve()
  assert.equal(calls[0].body.confirm_new_publication,true);assert.equal(page.canConfirm,true)
  await page.confirmStart();assert.equal(calls.at(-1).body.recipe.confirm_new_publication,true);assert.equal(calls.at(-1).body.recipe.mode,'publish')
})

test('后端确认只有草稿没有远端BV时，不要求重复投稿确认',async()=>{
  const result=await render((page,nav)=>{
    nav.workflow.existing_publication_id='draft';page.state={...state(),has_remote_publication:false};page.mode='publish';page.preview=plan();page.show=true
  })
  assert.equal(result.page.existingPublication,false);assert.equal(result.page.newPublicationWarning,false);assert.equal(result.page.canConfirm,true)
  assert.doesNotMatch(result.html,/我确认另发新稿件/)
})

test('原稿更新展示BV、对应分P与新时间轴章节，运行中独立展示更新子阶段',async()=>{
  const checked=await render(page=>{page.show=true;page.mode='update';page.preview=updatePlan()})
  assert.match(checked.html,/本次更新的原稿与分P/);assert.match(checked.html,/BVOriginal/);assert.match(checked.html,/历史账号/)
  assert.match(checked.html,/新成片时间轴/);assert.match(checked.html,/确认运行并更新原稿件/)
  assert.doesNotMatch(checked.html,/仅生成所列成片，不上传、不投稿/)
  const running=await render((_page,nav)=>{nav.workflow.run={id:'u',mode:'update',status:'monitoring',publication_id:'original',update_id:'task',
    publication:{status:'delivering',execution:{stages:{upload:{status:'completed'},chapters:{status:'running'}}}}}})
  assert.match(running.html,/自动原稿更新进度/);assert.match(running.html,/同步视频章节/);assert.match(running.html,/查看原稿更新详情/)
  for (const [type,text] of [['success','已完成'],['info','进行中'],['default','待执行']]) {
    assert.match(running.html,new RegExp(`class="n-text update-progress-state"[^>]*data-status-type="${type}"[^>]*>${text}`))
  }
})

test('已进入原稿交付的恢复只列未完成交付，不展示重新制作或配音选项',async()=>{
  const task={id:'task',status:'failed',execution:{stages:{upload:{status:'completed'},edit:{status:'completed'},review:{status:'completed'},chapters:{status:'failed'},collection:{status:'skipped'},verify:{status:'pending'}}}}
  const {html,page}=await render(page=>{page.show=true;page.mode='update';page.selectedRun='saved-update';page.preview={...updatePlan(),publication_update:task}})
  assert.equal(page.deliveryOnly,true);assert.equal(page.confirmLabel,'确认继续原稿交付')
  assert.deepEqual(page.pendingDeliveryStages.map(item=>item.key),['chapters','verify'])
  assert.match(html,/继续已确认的原稿更新方案/);assert.match(html,/本次不重新合成/)
  assert.match(html,/同步视频章节 · 未完成/);assert.match(html,/回读核验 · 待执行/)
  assert.doesNotMatch(html,/按当前保存的设置合成|确认后连续完成制作|获取视频 → 合成|data-testid="dubbing-run-options"/)
})

test('原稿更新未知结果以核对为确认意图，并说明先只读核对再继续',async()=>{
  const {html,page}=await render(page=>{page.show=true;page.mode='update';page.selectedRun='saved-update';page.preview={...updatePlan(),publication_update:{id:'task',status:'unknown'}}})
  assert.equal(page.confirmLabel,'确认核对原稿更新结果');assert.equal(page.reconcileUpdate,true)
  assert.match(html,/先只读核对B站实际结果/);assert.match(html,/不盲目重发修改请求/)
  assert.doesNotMatch(html,/确认恢复并更新原稿件/)
})

test('新版配音预检只有统一执行摘要，旧可用性摘要仅兼容旧服务',async()=>{
  const policy={...defaultDubbingRunPolicy(),mode:'changed'}
  const modern=await render(page=>{
    page.show=true;page.dubbing=policy
    page.preview={...plan(),dubbing:{policy,needed:true,known:true,required:8,ready:8,pending:0,generate:8,reuse:0,settings_changed:8}}
  })
  assert.match(modern.html,/data-testid="dubbing-run-options"/)
  assert.match(modern.html,/生成 <strong[^>]*>8<\/strong> 句/)
  assert.doesNotMatch(modern.html,/class="dubbing-readiness"|无需补配|跳过配音步骤/)
  const legacy=await render(page=>{
    page.show=true;page.preview={...plan(),dubbing:{needed:true,known:true,required:8,ready:8,pending:0}}
  })
  assert.match(legacy.html,/class="dubbing-readiness"/)
  assert.match(legacy.html,/已就绪 8 \/ 8 句/)
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

test('恢复提交立即显示中性请求状态，保留旧失败事实，拒绝后恢复当前错误说明',async t=>{
  const {page,nav}=await render((p,n)=>{p.selectedRun='old';p.preview=plan();p.show=true;n.workflow.run={id:'old',mode:'publish',status:'failed',cursor:0,error:'原来的失败',phase:'旧失败阶段'};n.workflowPlan={id:'old',steps:['download']}})
  let release:(value:any)=>void=()=>{},writes=0
  t.mock.method(globalThis,'fetch',async()=>{writes++;return new Promise(resolve=>{release=resolve})})
  const pending=page.confirmStart();await page.confirmStart()
  assert.equal(page.submitting,true);assert.match(page.stage.text,/正在提交恢复请求/)
  assert.equal(page.failureStatus,null);assert.equal(nav.workflow.run.status,'failed');assert.equal(nav.workflow.run.error,'原来的失败')
  assert.equal(writes,1)
  release({ok:false,status:400,json:async()=>({code:'TTS_SERVICE_DRAINING',message:'配音服务仍在处理上次请求，请等待收尾。'})});await pending
  assert.equal(page.submitting,false);assert.match(page.error,/配音服务仍在处理/);assert.ok(page.failureStatus)
})

test('未知恢复回执重新检查相同方案仍沿用UUID，确定受理后下轮恢复使用新UUID',async t=>{
  const {page}=await render(p=>{p.selectedRun='old';p.show=true})
  const ids:string[]=[];let accepted=false
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/plan'))return {ok:true,json:async()=>plan()}
    if(!String(url).endsWith('/resume'))return new Promise(()=>{})
    ids.push(JSON.parse(String(options?.body)).request_id)
    if(!accepted)throw new Error('回执未知')
    return {ok:true,json:async()=>({id:'old',status:'failed',error:'这次失败',recipe,plan:plan()})}
  })
  await page.inspect();await page.confirmStart();await page.inspect();await page.confirmStart()
  assert.equal(ids[0],ids[1])
  accepted=true;await page.confirmStart();page.show=true;await page.inspect();await page.confirmStart()
  assert.notEqual(ids[2],ids[3],'被受理的UUID不能成为以后恢复的旧回执')
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
  assert.match(paused.html,/运行项目/)
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

test('评论自动准备失败有工作区和实际诊断入口，其他步骤失败不误导到评论',async()=>{
  const failed=await render((_page,nav)=>{
    nav.workflow.run={id:'r',mode:'produce',status:'failed',phase:'准备评论',error:'评论 AI 处理失败',cursor:1,total:3}
    nav.workflowPlan={id:'r',steps:['tts','comments','assemble'],labels:{comments:'评论准备'}}
  })
  assert.match(failed.html,/\/projects\/fixture\/comments/)
  assert.match(failed.html,/\/projects\/fixture\/comments#llm-conversation/)
  assert.match(failed.html,/运行项目.*核对复用范围/)
  const encoding=await render((_page,nav)=>nav.workflow.run={id:'r',mode:'produce',status:'failed',phase:'编码',error:'编码失败',cursor:1,total:2})
  assert.doesNotMatch(encoding.html,/核对评论与上屏方案/)
})

test('旧配音取消记录只作为上次自动流程中断；当前完成时轻提示，原因默认折叠',async()=>{
  const {html,page,nav}=await render((_page,nav)=>{
    nav.workflow.run={id:'old',mode:'produce',status:'failed',phase:'字幕与配音',cursor:1,total:3,error:'tts 未完成：步骤被用户取消'}
    nav.workflowPlan={id:'old',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音'}}
    nav.stepsLoaded=true
    nav.steps=[{key:'tts',label:'字幕与配音',status:'done',fresh:true},{key:'assemble',label:'合成成片',status:'running',progress:29.9}]
  })
  assert.match(html,/上次自动流程在字幕与配音中断；该步骤目前已完成/)
  assert.match(html,/完整流程仍保留中断记录/);assert.match(html,/历史中断/)
  assert.match(html,/<details[^>]*><summary[^>]*>查看上次中断原因/);assert.doesNotMatch(html,/<details[^>]*\bopen/)
  assert.match(html,/tts 未完成：步骤被用户取消/);assert.doesNotMatch(html,/role="alert"/)
  assert.equal(nav.workflow.run.status,'failed');assert.equal(page.resumable,true);assert.equal(nav.busy,true)
  assert.match(html,/运行项目/)
  await page.open('old');assert.equal(page.show,false,'当前合成时不能提交恢复或打开运行检查')
})

test('未恢复、读取失败或不能对应当前步骤的旧流程保持明确上次语义的警告',async()=>{
  for(const state of [{fresh:false,stepsLoaded:true,stepsError:''},{fresh:true,stepsLoaded:false,stepsError:''},{fresh:true,stepsLoaded:true,stepsError:'读取失败'}]) {
    const {html,page}=await render((_page,nav)=>{
      nav.workflow.run={id:'old',mode:'produce',status:'failed',phase:'字幕与配音',cursor:1,total:3,error:'tts 未完成：步骤被用户取消'}
      nav.workflowPlan={id:'old',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音'}}
      nav.stepsLoaded=state.stepsLoaded;nav.stepsError=state.stepsError
      nav.steps=[{key:'tts',label:'字幕与配音',status:'done',fresh:state.fresh}]
    })
    assert.match(html,/上次自动流程在字幕与配音中断/);assert.match(html,/role="alert"/)
    assert.doesNotMatch(html,/该步骤目前已完成|查看上次中断原因/);assert.equal(page.failureStatus.recovered,false)
  }
  const missing=await render((_page,nav)=>{
    nav.workflow.run={id:'new',mode:'produce',status:'failed',error:'tts 未完成：步骤被用户取消',cursor:1,total:3}
    nav.workflowPlan={id:'old',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音'}}
    nav.steps=[{key:'tts',status:'done',fresh:true}]
  })
  assert.match(missing.html,/上次自动流程已中断/);assert.equal(missing.page.failureStatus.recovered,false)
})

test('正在运行的新流程错误保持警告，不继承旧中断摘要或折叠处理',async()=>{
  const {html,page}=await render((_page,nav)=>{
    nav.workflow.run={id:'new',mode:'produce',status:'running',phase:'合成成片',cursor:1,total:3,error:'当前编码需要处理'}
    nav.workflowPlan={id:'new',steps:['tts','assemble'],labels:{assemble:'合成成片'}}
    nav.steps=[{key:'assemble',status:'done',fresh:true}]
  })
  assert.match(html,/当前自动流程需要处理/);assert.match(html,/当前编码需要处理/);assert.match(html,/role="alert"/)
  assert.doesNotMatch(html,/查看上次中断原因|步骤被用户取消|历史中断/)
  assert.equal(page.failureStatus.recovered,false)
})

test('发布资料准备失败记录说明历史制作完成，当前单独合成仍保留进行中语义和恢复指引',async()=>{
  const {html,page,nav}=await render((_page,nav)=>{
    nav.workflow.run={id:'old',mode:'publish',status:'failed',phase:'准备发布资料',cursor:3,total:3,error:'AI 文案请求超时'}
    nav.workflowPlan={id:'old',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音',assemble:'合成成片'}}
    nav.stepsLoaded=true
    nav.steps=[{key:'assemble',label:'合成成片',status:'running',fresh:false,progress:42}]
  })
  assert.match(html,/上次自动流程在准备发布资料时中断；制作步骤已完成。/)
  assert.match(html,/当前制作任务仍在进行；此提示属于上次自动流程。/)
  assert.match(html,/运行项目.*选择继续上次任务，核对当前制作结果后恢复发布资料准备/)
  assert.doesNotMatch(html,/该步骤目前已完成|当前自动流程需要处理|查看上次中断原因/)
  assert.equal(page.failureStatus.recovered,false);assert.equal(page.failureStatus.stepKey,undefined)
  assert.equal(nav.workflow.run.status,'failed');assert.equal(nav.busy,true)
})

test('成片有效且没有稿件时提示继续发布，不读取方案或自动投稿',async()=>{
  const {html,page}=await render((_page,nav)=>{
    nav.workflow.mode='draft'
    nav.workflow.run={id:'old',mode:'publish',status:'failed',phase:'字幕与配音',cursor:1,total:3,error:'tts 未完成：步骤被用户取消'}
    nav.workflowPlan={id:'old',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音'}}
    nav.stepsLoaded=true
    nav.steps=[{key:'tts',status:'done',fresh:true},{key:'assemble',status:'done',fresh:true}]
  })
  assert.equal(page.readyForPublication,true)
  assert.match(html,/已有有效成片，可继续发布流程/)
  assert.match(html,/到发布步骤保存交付方式与稿件目标后，再运行项目/)
  assert.match(html,/项目交付终点.*准备发布草稿/s)
  assert.match(html,/运行项目/)
  assert.match(html,/上次终点：自动投稿/)
  assert.match(html,/上次已完成工作阶段/)
  assert.match(html,/aria-label="上次流程的步骤记录"/)
  assert.match(html,/\/projects\/fixture\/publish/)
  assert.equal(page.show,false)
  assert.equal(page.preview,null)
})

test('旧冻结计划缺少标签或终点时仍用中文名称，缺失记录不冒充持续读取',async()=>{
  const {html}=await render((_page,nav)=>{
    nav.workflow.run={id:'old-labels',status:'running',phase:'正在整理内容',cursor:2,total:3}
    nav.workflowPlan={id:'old-labels',steps:['download','preprocess','split'],labels:{}}
  })
  const sequence=html.match(/<ol[^>]*class="run-sequence"[\s\S]*?<\/ol>/)?.[0] || ''
  assert.match(sequence,/获取视频/);assert.match(sequence,/音轨分离/);assert.match(sequence,/语义整理/)
  assert.doesNotMatch(sequence,/>download<|>preprocess<|>split</)
  assert.match(html,/本次终点：未记录/)
})

test('真实流程卡合并配音准备和字幕内部任务，全部完成后才计入同一工作阶段',async()=>{
  for(const [cursor,completed,current] of [[3,1,'字幕与配音'],[4,2,'上屏评论'],[5,3,'合成成片'],[6,4,null]] as const){
    const {html,page}=await render((_page,nav)=>{
      nav.workflow.run={id:'merged-run',mode:'produce',status:'running',phase:'制作中',cursor,total:6}
      nav.workflowPlan={id:'merged-run',steps:['download','dubtask','tts','subtitles','comments','assemble'],labels:{}}
    })
    assert.equal(page.completedStages,completed)
    const sequence=html.match(/<ol[^>]*class="run-sequence"[\s\S]*?<\/ol>/)?.[0]||''
    assert.equal((sequence.match(/<li\b/g)||[]).length,4)
    assert.equal((sequence.match(/字幕与配音/g)||[]).length,1)
    assert.doesNotMatch(sequence,/字幕导出|配音计划/)
    assert.match(html,new RegExp(`已完成工作阶段<\\/span><span[^>]*>${completed} / 4<\\/span>`))
    assert.equal((sequence.match(/class="done"/g)||[]).length,completed)
    if(current)assert.match(sequence,new RegExp(`aria-current="step"[^>]*>${current}<\\/li>`))
    else assert.doesNotMatch(sequence,/aria-current="step"/)
  }
})

test('未知或损坏的冻结范围不崩溃、不显示0/0阶段，仍保留真实流程状态',async()=>{
  for(const steps of [undefined,null,'tts',{0:'tts',length:1},[],['tts',null],['tts',{}]]) {
    const {html,page}=await render((_page,nav)=>{
      nav.workflow.run={id:'unknown-scope',mode:'publish',status:'running',phase:'上屏评论',cursor:0,total:8}
      nav.workflowPlan={id:'unknown-scope',...(steps===undefined?{}:{steps})}
    })
    assert.deepEqual(page.stageSequence,[])
    assert.equal(page.hasStageSequence,false)
    assert.match(html,/执行中/);assert.match(html,/上屏评论/)
    assert.doesNotMatch(html,/class="run-progress"|class="run-sequence"|0 \/ 0/)
  }
})

test('成片更新中、状态未知、项目不可操作或已有稿件时不宣称尚未发布',async()=>{
  const variants=[
    (nav:any)=>nav.steps[0].fresh=false,
    (nav:any)=>nav.steps[0].status='running',
    (nav:any)=>nav.stepsLoaded=false,
    (nav:any)=>nav.stepsError='步骤读取失败',
    (nav:any)=>nav.workflow=null,
    (nav:any)=>nav.workflowError='流程读取失败',
    (nav:any)=>nav.publicationError='发布记录读取失败',
    (nav:any)=>nav.archived=true,
    (nav:any)=>nav.dirMissing=true,
    (nav:any)=>nav.uid='另一项目',
    (nav:any)=>nav.workflow.existing_publication_id='旧稿',
    (nav:any)=>nav.publication={id:'本地草稿',status:'draft'},
    (nav:any)=>nav.workflow.run={id:'本次',mode:'publish',status:'monitoring',publication_id:'稿件'},
    (nav:any)=>nav.chain={state:'active',remaining:['assemble']},
  ]
  for(const change of variants){
    const {html,page}=await render((_page,nav)=>{
      nav.stepsLoaded=true;nav.steps=[{key:'assemble',status:'done',fresh:true}];change(nav)
    })
    assert.equal(page.readyForPublication,false)
    assert.doesNotMatch(html,/已有有效成片，可继续发布流程/)
  }
})

test('恢复旧流程沿用它的投稿终点，新流程草稿默认不悄悄改变恢复意图',async t=>{
  const {page}=await render((_page,nav)=>{nav.workflow.mode='draft'})
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.method,options?.body&&JSON.parse(String(options.body))])
    return {ok:true,json:async()=>String(url).endsWith('/plan')?plan():{
      ...state(),recipe:{...recipe,mode:'draft'},runs:[{id:'old',recipe:{...recipe,mode:'publish'}}],
    }}
  })
  await page.open('old')
  assert.equal(page.mode,'publish')
  assert.equal(page.confirmLabel,'确认恢复并投稿')
  assert.deepEqual(calls[1],[ '/api/projects/fixture/workflow/plan','POST',{resume_id:'old'} ])
  assert.ok(calls.every(call=>!call[0].endsWith('/resume')&&!call[0].endsWith('/start')))
})


test('完整流程抽屉显示分区合集小节名称，ID使用次级样式，不额外读取平台',async()=>{
  const {html}=await render(page=>{page.show=true;page.preview={...plan(),recipe:{...recipe,account_id:'12'},
    settings:{visibility:'public',human_type2:1029,season_id:34,section_id:56},
    settings_display:{account_id:'12',human_type2:1029,season_id:34,section_id:56,category_title:'生活 / 旅行',season_title:'旅行合集',section_title:'正篇',source:'cache'}}})
  assert.match(html,/aria-label="发布目标"/);assert.match(html,/生活 \/ 旅行/)
  assert.match(html,/旅行合集/);assert.match(html,/正篇/);assert.match(html,/class="target-id"/)
  assert.match(html,/名称为最近读取的快照/)
})

test('仅制作成片的流程不显示无关发布目标',async()=>{
  const {html}=await render(page=>{page.show=true;page.mode='produce';page.preview={...plan(),recipe:{...recipe,mode:'produce'},settings:{}}})
  assert.doesNotMatch(html,/aria-label="发布目标"/)
})
