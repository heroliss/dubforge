import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { createRouter, createMemoryHistory } from 'vue-router'

let server:ViteDevServer, Panel:any
const fixture=()=>({rows:[{id:'a',author:'甲',text:'Original A',text_zh:'中文 A',source_hash:'a'},
  {id:'b',author:'乙',text:'Original B',text_zh:'',source_hash:'b',ai_issues:{filter:{status:'blocked',message:'评分审核拦截'},translate:{status:'deferred',message:'尚未独立处理'}}}],
  available_assessments:[{id:'a',eligible:true,score:88,reason:'有共鸣'}],filter_stale:true,filter:{items:[],assessments:[]},
  rating_progress:{saved:1,total:2,remaining:1},overlay:null,overlay_revision:'rev',defaults:{},
  configuration:{values:{'comments.prompt':'观众反馈','comments.count':10},sources:{},overrides:{}},fields:[],
  job:{status:'partial',action:'filter',warning:'复用一条，另一条待处理'}})
let activeFixture:any=fixture()
const responseFor=(url:any)=>String(url).includes('/api/configuration/')
  ? {name:'本项目',overrides:{},defaults:{},sections:{},sources:{},parent_sources:{}}
  : activeFixture
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null}})
  Panel=(await server.ssrLoadModule('/src/components/CommentStudioPanel.vue')).default
  mock.method(globalThis,'fetch',async(url)=>({ok:true,json:async()=>responseFor(url)} as any))
})
after(async()=>{mock.restoreAll();await server.close()})
async function render(data:any=fixture(),drafts?:any[]){
  activeFixture=data
  let page:any
  const app=createSSRApp({render:()=>h(NMessageProvider,()=>h(NDialogProvider,()=>h(Panel,{uid:'fixture'})))})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  app.use(router);await router.push('/projects/fixture/steps/download');await router.isReady()
  setup(app)
  app.mixin({created(){if(this.$options.__name==='CommentStudioPanel'){page=this.$.setupState;page.state=data;page.hydrate(data);if(drafts)page.items=drafts;page.prompt='观众反馈'}}})
  return {html:await renderToString(app),page}
}
test('新增评论不隐藏已验证评分；评分和翻译失败分别显示',async()=>{
  const {html,page}=await render()
  assert.equal(page.suggestions.length,1)
  for(const label of ['88 分','评分：审核拦截','翻译：待处理','仅用已完成评分','重试未完成翻译','全部翻译成中文'])assert.ok(html.includes(label),label)
  assert.doesNotMatch(html,/旧筛选建议已停用/)
})

const lengthFixture=()=>({...fixture(),rows:[
  {id:'long',author:'长评论',source_hash:'long',text:'x'.repeat(720),text_zh:'中'.repeat(219)},
  {id:'valid',author:'可用评论',source_hash:'valid',text:'a'.repeat(600),text_zh:'中'.repeat(240)},
  {id:'extra',author:'递补评论',source_hash:'extra',text:'good',text_zh:'好'},
],available_assessments:[{id:'long',eligible:true,score:99,reason:'高分'},
  {id:'valid',eligible:true,score:90,reason:'可用'},{id:'extra',eligible:true,score:80,reason:'可递补'}],
text_limits:{translated:240,original:600},defaults:{language_mode:'bilingual'},job:null,filter_stale:false})

test('双语候选跳过超长并递补；中文模式可纳入原文超长项，修改草稿不改变完整原文',async()=>{
  const {html,page}=await render(lengthFixture())
  assert.ok(html.includes('原文 720/600 字符，超出 120 字符'))
  assert.ok(html.includes('上屏上限：中文 240 · 原文 600 字符'))
  page.limit=2
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['valid','extra'])
  page.useCandidates()
  assert.deepEqual(page.items.map((row:any)=>row.id),['valid','extra'])
  page.settings.language_mode='translated'
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['long','valid'])
  page.settings.language_mode='bilingual'
  page.toggle(page.rows[0],true)
  assert.equal(page.excludedItems.length,1)
  page.items.find((row:any)=>row.id==='long').source_text='保留含义的精简上屏文字'
  assert.equal(page.excludedItems.length,0)
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['long','valid'])
  assert.equal(page.rows[0].text.length,720)
})

test('超长草稿保留供编辑，但不进入预览或确认请求；全部排除时不清空已有方案',async t=>{
  const data=lengthFixture(), drafts=data.rows.map(row=>({...row,source_text:row.text,display_text:row.text_zh}))
  const {html,page}=await render(data,drafts)
  assert.ok(html.includes('已排除 · 不会上屏'))
  assert.ok(html.includes('确认上屏方案（2 条）'))
  assert.deepEqual(page.plannedItems.map((row:any)=>row.id),['valid','extra'])
  assert.equal(page.invalid,false)
  assert.equal(page.plannedPosition(page.items[1]),'第 1 条 · 5 秒')
  let confirmation:any, saved:any
  t.mock.method(page.dialog,'info',(options:any)=>{confirmation=options})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT')saved=JSON.parse(options.body as string)
    return {ok:true,json:async()=>responseFor(url)} as any
  })
  page.confirmPlan()
  assert.equal(confirmation.title,'确认 2 条上屏评论')
  assert.ok(confirmation.content.includes('已排除 1 条'))
  await confirmation.onPositiveClick()
  assert.deepEqual(saved.items.map((row:any)=>row.id),['valid','extra'])
  page.items=[drafts[0]]
  assert.equal(page.invalid,true)
  confirmation=null
  page.confirmPlan()
  assert.equal(confirmation,null)
})

test('修改上屏语言后，缺字或超长项即时排除，时长提示不再混用文字错误',async()=>{
  const {page}=await render(lengthFixture())
  page.items=[{id:'original',source_text:'原文足够',display_text:''}]
  page.settings.language_mode='original'
  assert.equal(page.plannedItems.length,1)
  page.settings.language_mode='bilingual'
  assert.equal(page.plannedItems.length,0)
  assert.equal(page.settingsError,'')
  page.settings.duration=30
  page.settings.interval=20
  assert.equal(page.settingsError,'每条停留 30 秒，超过出现间隔 20 秒；请缩短停留或增大间隔。')
})
test('重试未完成翻译与仅用缓存传递明确意图，不发布或覆盖用户上屏文字',async t=>{
  const {page}=await render(), calls:any[]=[]
  page.items=[{id:'a',source_hash:'a',display_text:'用户编辑的文案'}]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.process('translate',false,{retry_failed:true})
  assert.equal(page.working,'','未完成上一请求收尾')
  assert.equal(page.rulesDirty,false,'规则编辑状态不应被只读加载改变')
  assert.ok(page.prompt,'必须保留评分要求')
  await page.process('filter',false,{only_cached:true})
  const writes=calls.filter(c=>c[1]?.method==='POST')
  assert.equal(writes.length,2)
  assert.deepEqual(JSON.parse(writes[0][1].body),{retry_failed:true})
  assert.equal(JSON.parse(writes[1][1].body).only_cached,true)
  assert.equal(page.items[0].display_text,'用户编辑的文案')
  assert.ok(calls.every(c=>!c[0].includes('/publish')&&!c[0].includes('/overlay')))
})
