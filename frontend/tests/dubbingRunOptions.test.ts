import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp,h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'

let server:ViteDevServer,Options:any,runPlannedSteps:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Options=(await server.ssrLoadModule('/src/components/DubbingRunOptions.vue')).default
  ;({runPlannedSteps}=await server.ssrLoadModule('/src/utils/runPlan.ts'))
})
after(async()=>await server.close())
const summary=(policy=defaultDubbingRunPolicy())=>({policy,repair_settings:{enabled:true,configured:true,legacy:false},generate:5,reuse:15,settings_changed:3,customized:2,original:1,silent:1,new_slot:3,slots:[1,2]})
async function render(props:any){let page:any;const updates:any[]=[]
  const app=createSSRApp({render:()=>h(Options,{...props,'onUpdate:modelValue':(value:any)=>updates.push(value)})})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/');app.use(router)
  app.mixin({created(){if(this.$options.__name==='DubbingRunOptions')page=this.$.setupState}})
  return {html:await renderToString(app),page,updates}
}
test('默认范围说明把参数不同与待补音频分开，保留原声、静音和精调',async()=>{
  const {html}=await render({summary:summary()})
  assert.match(html,/继续已有配音/);assert.match(html,/3 句可用配音尚未应用当前设置/)
  assert.match(html,/按当前内容与设置更新/);assert.match(html,/文字或时间已失效的句子仍会补齐/)
  assert.match(html,/保留逐句精调 2 句/);assert.match(html,/原声 1 句/);assert.match(html,/静音 1 句/)
  assert.doesNotMatch(html,/name="[^"]*dubbing-run-mode"/,'默认折叠高级选择')
  assert.doesNotMatch(html,/type="checkbox"/,'折叠方案不再提供普通设置开关')
  assert.match(html,/时间冲突处理/);assert.match(html,/自动修复/);assert.match(html,/使用已保存配音设置/)
  assert.match(html,/title="自动缩时适用于 IndexTTS；旧声音需要可复现生成快照。先排布，仍放不下才尝试缩时。"/)
})
test('自动修复独立于常规新生成计数，说明同槽记录保护与运行后复查',async()=>{
  const {html}=await render({summary:{...summary(),generate:0,repair:2,repair_ids:[7,9],repair_blocked:[{id:12,reason:'等待邻句配音完成后重新排布'}]}})
  assert.match(html,/生成 <strong[^>]*>0<\/strong> 句/)
  assert.match(html,/自动修复 <strong[^>]*>2<\/strong> 句/)
  assert.match(html,/待修复：第 7、9 句/);assert.doesNotMatch(html,/修复方式与保留内容|type="checkbox"/)
  assert.match(html,/另 1 句需在运行后重新检查/)
  assert.match(html,/第 12 句：等待邻句配音完成后重新排布/)
  assert.doesNotMatch(html,/需人工处理/,'暂不能修复的句子可能在邻句生成后解决')
})
test('普通修复设置以预检结果为准，旧运行字段不覆盖项目设置或进入新请求',async()=>{
  const policy={...defaultDubbingRunPolicy(),mode:'all',repair_timing:false}
  const {html,page,updates}=await render({modelValue:policy,summary:summary(policy as any)})
  assert.match(html,/自动修复/);assert.doesNotMatch(html,/历史运行策略/)
  page.update({mode:'fill'})
  assert.deepEqual(updates[0],defaultDubbingRunPolicy())
  page.update({repair_timing:true})
  assert.equal(Object.hasOwn(updates[1],'repair_timing'),false)
  const disabled=await render({summary:{repair_settings:{enabled:false,configured:false,legacy:false}}})
  assert.match(disabled.html,/仅排布并提示/);assert.doesNotMatch(disabled.html,/type="checkbox"/)
})
test('批量总控未预检时只说明按项目设置，明确项目才提供跳转',async()=>{
  const batch=await render({})
  assert.match(batch.html,/按各项目已保存的配音设置/)
  assert.doesNotMatch(batch.html,/自动修复|仅排布并提示|调整配音设置/)
  const project=await render({uid:'fixture',summary:summary()})
  assert.match(project.html,/href="\/projects\/fixture\/steps\/tts#dubbing-parameters"/)
  assert.match(project.html,/调整配音设置/)
  const busy=await render({uid:'fixture',summary:summary(),disabled:true})
  assert.doesNotMatch(busy.html,/href="\/projects\/fixture\/steps\/tts/)
})
test('旧历史只读显示当时策略，新预检没有修复配置时不猜测旧字段',async()=>{
  const legacy={policy:{...defaultDubbingRunPolicy(),repair_timing:false}}
  const history=await render({summary:legacy,readonly:true})
  assert.match(history.html,/仅排布并提示/);assert.match(history.html,/历史运行策略/)
  assert.doesNotMatch(history.html,/type="checkbox"/)
  const active=await render({summary:legacy})
  assert.match(active.html,/按各项目已保存的配音设置/);assert.doesNotMatch(active.html,/历史运行策略/)
})
test('重做展开统一槽选项，项目统一参数仅本次，不删除保存精调',async()=>{
  const policy={...defaultDubbingRunPolicy(),mode:'all',target:'new',line_settings:'project'}
  const {html,page,updates}=await render({modelValue:policy,summary:{...summary(policy as any),overwrite:2,confirmed:1}})
  assert.match(html,/name="[^"]*dubbing-run-mode"/);assert.match(html,/新配音槽 · 保留旧音频/)
  assert.match(html,/不删除保存的句子设置/);assert.match(html,/进入配音时统一分配并保存新槽/)
  assert.match(html,/当前候选为配音槽 3/);assert.match(html,/含 1 句已满意/)
  page.update({target:'slot',slot:2})
  assert.deepEqual(updates[0],{...policy,target:'slot',slot:2})
  page.update({mode:'fill'});assert.deepEqual(updates[1],defaultDubbingRunPolicy(),'切回继续时复原全部隐藏条件')
})
test('同页多个项目的模式与目标单选组互不干扰',async()=>{
  const policy={...defaultDubbingRunPolicy(),mode:'all'}
  const html=await renderToString(createSSRApp({render:()=>h('div',[h(Options,{modelValue:policy}),h(Options,{modelValue:policy})])}))
  const names=[...html.matchAll(/name="([^"]+dubbing-run-mode)"/g)].map(match=>match[1])
  assert.equal(new Set(names).size,2);assert.equal(names.length,6)
})
test('恢复摘要明确已完成成果与新一轮差别',async()=>{
  const resumed=await render({summary:{...summary(),resumed:true,completed:12}})
  assert.match(resumed.html,/保留已完成 12 句/);assert.match(resumed.html,/仍写入本轮目标槽/)
  const changed=await render({summary:{...summary(),resume_reset:true}})
  assert.match(changed.html,/将开始新一轮/);assert.match(changed.html,/上一轮已完成的音频仍保留/)
})
test('冻结批次只读且无配音的成片不展示无用操作',async()=>{
  const fixed=await render({summary:summary(),readonly:true});assert.doesNotMatch(fixed.html,/调整配音方案|type="radio"/)
  assert.doesNotMatch(fixed.html,/type="checkbox"/);assert.match(fixed.html,/使用已保存配音设置/)
  fixed.page.update({mode:'all'});assert.equal(fixed.updates.length,0)
  const original=await render({summary:{needed:false}})
  assert.match(original.html,/所选版本使用原声，无需配音/);assert.doesNotMatch(original.html,/调整配音方案|新参数用于待补句子/)
  assert.doesNotMatch(original.html,/自动修复配音时间冲突/)
})
test('字幕未就绪显示延迟计算，指定槽无效时给明确错误',async()=>{
  const deferred=await render({summary:{deferred:true}});assert.match(deferred.html,/原文与译文就绪后计算实际句数/)
  const invalid=await render({modelValue:{...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:null}})
  assert.match(invalid.html,/role="alert"/);assert.match(invalid.html,/目标配音槽/)
})
test('勾选制作与依赖入口调整策略立即停用旧方案，只执行重新预检后的冻结策略',async t=>{
  const calls:any[]=[],errors:string[]=[],policy=defaultDubbingRunPolicy();let shown:any,resolve:any
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    const body=JSON.parse(String(options.body));calls.push({url,body})
    if(String(url).endsWith('/run_chain'))return {ok:true,json:async()=>({started:'tts'})}
    if(calls.length>1)return new Promise(done=>{resolve=()=>done({ok:true,json:async()=>({steps:['tts','assemble'],labels:{tts:'配音',assemble:'成片'},blockers:[],fingerprint:'new-fp',dubbing:summary(body.dubbing)})})})
    return {ok:true,json:async()=>({steps:['tts','assemble'],labels:{tts:'配音',assemble:'成片'},blockers:[],fingerprint:'old-fp',dubbing:summary(policy)})}
  })
  await runPlannedSteps('isolated',['tts','assemble'],true,{warning:assert.fail,error:(s:string)=>errors.push(s),success:()=>{}},
    {info:(options:any)=>{shown=options;return options}},async()=>{},async()=>{})
  assert.equal(calls.length,1)
  const update=shown.content().children[0].props['onUpdate:modelValue']
  const changed={...policy,mode:'all',target:'slot',slot:2,line_settings:'project'}
  const pending=update(changed)
  assert.equal(shown.positiveButtonProps.disabled,true);await shown.onPositiveClick();assert.equal(calls.length,2)
  resolve();await pending;assert.equal(shown.positiveButtonProps.disabled,false)
  await shown.onPositiveClick()
  assert.deepEqual(calls[2].body,{steps:['tts','assemble'],dubbing:changed,plan_fingerprint:'new-fp'})
  assert.deepEqual(errors,[])
})
