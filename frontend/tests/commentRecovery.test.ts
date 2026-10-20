import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
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
const countStatus=(page:any,status:string)=>page.rows.filter((row:any)=>page.candidateStatus(row).status===status).length
const responseFor=(url:any)=>String(url).includes('/api/configuration/')
  ? {name:'本项目',overrides:{},defaults:{},sections:{},sources:{},parent_sources:{}}
  : activeFixture
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/CommentStudioPanel.vue')).default
  mock.method(globalThis,'fetch',async(url)=>({ok:true,json:async()=>responseFor(url)} as any))
})
after(async()=>{mock.restoreAll();await server.close()})
async function render(data:any=fixture(),drafts?:any[],options:{props?:Record<string,any>;seed?:(page:any)=>void;openMore?:boolean}={}){
  activeFixture=data
  let page:any,targetNumberInput:any,targetTextInput:any,commentPreview:any
  const overlayTextInputs:any[]=[]
  const styleControls:any[]=[]
  const resetButtons:any[]=[]
  const app=createSSRApp({render:()=>h(NMessageProvider,()=>h(NDialogProvider,()=>h(Panel,{uid:'fixture',...options.props})))})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  app.use(router);await router.push('/projects/fixture/steps/download');await router.isReady()
  setup(app)
  app.mixin({created(){
    if(this.$options.__name==='CommentStudioPanel'){page=this.$.setupState;page.state=data;page.hydrate(data);if(drafts)page.items=drafts;page.prompt='观众反馈';options.seed?.(page)}
    if(this.$options.__name==='CommentPreview')commentPreview=this
    // SSR通过Naive UI的真实受控属性展开“更多处理”，验证折叠区保留的高级操作。
    if(options.openMore&&this.$options.name==='Collapse')this.$.props.expandedNames=['filter','plan']
    if(this.$options.name==='InputNumber'&&this.$props.inputProps?.['aria-label']==='目标上屏评论数量（不调用 AI）')targetNumberInput=this
    if(this.$options.name==='Input'&&this.$parent===targetNumberInput)targetTextInput=this
    if(this.$options.name==='Input'&&/^第 \d+ 条(?:中文|原文)上屏文字$/.test(this.$props.inputProps?.['aria-label']||''))overlayTextInputs.push(this)
    if(this.$options.__name==='ParamControl'&&this.$props.field?.path?.startsWith('comments.'))styleControls.push(this)
    if(this.$options.__name==='ParameterResetButton')resetButtons.push(this)
  }})
  return {html:await renderToString(app),page,targetNumberInput,targetTextInput,overlayTextInputs,commentPreview,styleControls,resetButtons}
}
test('新增评论不隐藏已验证评分；评分和翻译失败分别显示',async()=>{
  const {html,page}=await render(fixture(),undefined,{openMore:true})
  assert.equal(page.suggestions.length,1)
  for(const label of ['88 分','评分：服务商审核拦截（请求范围未记录）','翻译：暂缓处理','仅用已完成评分','重试未完成翻译','补译未翻译评论（1 条）'])assert.ok(html.includes(label),label)
  assert.match(html,/服务商审核拦截 1 条/);assert.match(html,/暂缓处理 1 条/)
  assert.match(html,/重试只补处理失败与暂缓项/)
  assert.doesNotMatch(html,/重试未完成评分/)
  assert.deepEqual(page.retryIds('filter'),[])
  assert.deepEqual(page.retryIds('translate'),['b'])
  assert.doesNotMatch(html,/旧筛选建议已停用/)
})

test('补译新条目与暂缓项，重试只含失败与暂缓，审核拦截和本地命中分别展示', async () => {
  const data={...fixture(),rows:[fixture().rows[0],
    {id:'failed',text:'失败原文',text_zh:'',source_hash:'failed',ai_issues:{translate:{status:'failed',message:'格式无效'}}},
    {id:'blocked',text:'请求中的原文',text_zh:'',source_hash:'blocked',ai_issues:{translate:{status:'blocked',request_scope:'batch',message:'批次返回审核拒绝'}}},
    {id:'deferred',text:'暂缓原文',text_zh:'',source_hash:'deferred',ai_issues:{translate:{status:'deferred',message:'尚未独立处理'}}},
    {id:'new',text:'新的原文',text_zh:'  ',source_hash:'new',review_blocked:true,review_reasons:['本地规则命中<script>理由</script>']}],
    job:{status:'partial',action:'translate',processed:0,cached:1,blocked:1,blocked_cached:1,failed:1,deferred:1,skipped:2,warning:'尚有未完成项'}}
  const {html,page}=await render(data,undefined,{openMore:true})
  assert.equal(page.untranslated,4);assert.equal(page.translationPending,2)
  assert.deepEqual(page.retryIds('translate'),['failed','deferred'])
  assert.match(html,/5 条原文 · 有效译文 1 条/)
  assert.equal(page.untranslated,4)
  assert.match(html,/补译未翻译评论（2 条）/)
  assert.match(html,/所在请求被服务商审核拦截/)
  assert.match(html,/不能据此确定哪条评论触发了审核/)
  assert.match(html,/本地审查命中 · 不上屏/)
  assert.match(html,/本地规则命中&lt;script&gt;理由&lt;\/script&gt;/)
  assert.doesNotMatch(html,/<script>理由|已全部翻译/)
  assert.match(html,/href="\/settings\?section=models"/)
  assert.equal(page.rows[4].text_zh,'  ');assert.equal(page.rows[4].text,'新的原文')
})

test('仅剩审核拦截记录时补译入口停用，未把未译项显示为全部完成', async () => {
  const data={...fixture(),rows:[fixture().rows[0],
    {id:'blocked',text:'保留原文',text_zh:'',source_hash:'blocked',ai_issues:{translate:{status:'blocked',message:'旧记录'}}}],
    job:{status:'partial',action:'translate',processed:0,cached:1,skipped:1,deferred:0,warning:'可查看逐条原因或重试未完成项。'}}
  const {html,page}=await render(data,undefined,{openMore:true})
  assert.equal(page.translationPending,0);assert.equal(page.untranslated,1)
  const button=[...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match=>match[0]).find(value=>value.replace(/<[^>]*>/g,'').trim()==='暂无可自动补译项')
  assert.ok(button);assert.match(button,/disabled/)
  assert.match(html,/请求范围未记录/);assert.match(html,/不能确定这条评论是否被服务商单独拒绝/)
  assert.match(html,/本次新增译文 0 条；复用有效译文 1 条；跳过问题项 1 条/)
  assert.match(html,/重试只补处理失败与暂缓项/)
  assert.doesNotMatch(html,/可查看逐条原因或重试未完成项/)
  assert.doesNotMatch(html,/已全部有译文|重试未完成翻译/)
})

test('本次零请求仍展示已保存的服务商原因，原因按文本转义且不展开任意字段', async () => {
  const data={...fixture(),rows:[{id:'blocked',text:'保留原文',text_zh:'',source_hash:'blocked',
    ai_issues:{translate:{status:'blocked',request_scope:'unknown',message:'已保存的审核拦截',
      cause:[{profile:'主配置<script>',provider_code:'content_policy_violation',detail:'内部任意字段'},
        {profile:'备用配置',finish_reason:'sensitive'},{profile:'第三配置',code:'LLM_CONTENT_REJECTED'}]}}}],
    job:{status:'partial',action:'translate',processed:0,cached:0,blocked:1,blocked_cached:1,failed:0,deferred:0,skipped:1}}
  const {html}=await render(data)
  assert.match(html,/本次新增译文 0 条/)
  assert.match(html,/主配置&lt;script&gt;：content_policy_violation/)
  assert.match(html,/备用配置：结束原因 sensitive/)
  assert.match(html,/第三配置：LLM_CONTENT_REJECTED/)
  assert.match(html,/其中旧记录 1 条未重发/)
  assert.doesNotMatch(html,/<script>|内部任意字段|JSON\.stringify/)
})

test('全量复用时提示没有新增译文，不发送重译意图，也保留原文与人工文字', async t => {
  const data={...fixture(),rows:[fixture().rows[0],{id:'b',text:'Original B',text_zh:'中文 B',source_hash:'b'}],
    job:{status:'done',action:'translate',processed:0,cached:2,blocked:0,blocked_cached:0,failed:0,deferred:0,skipped:0}}
  const {page}=await render(data), calls:any[]=[], notes:any[]=[]
  page.items=[{id:'a',source_hash:'a',display_text:'人工编辑的上屏文字'}]
  t.mock.method(page.message,'info',text=>{notes.push(['info',text])})
  t.mock.method(page.message,'success',text=>{notes.push(['success',text])})
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return{ok:true,json:async()=>responseFor(url)} as any})
  await page.process('translate')
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1);assert.deepEqual(JSON.parse(writes[0][1].body),{})
  assert.equal(notes.length,1);assert.equal(notes[0][0],'info')
  assert.match(notes[0][1],/本次没有新增译文/);assert.match(notes[0][1],/复用有效译文 2 条/)
  assert.doesNotMatch(notes[0][1],/译文已更新|全部成功翻译/)
  assert.equal(page.items[0].display_text,'人工编辑的上屏文字')
  assert.equal(page.rows[0].text,'Original A');assert.equal(page.rows[0].text_zh,'中文 A')
  assert.ok(writes.every(call=>!call[0].includes('/overlay')&&!call[0].includes('/publish')))
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
  assert.ok(html.includes('自动候选建议：中文 240 · 原文 600 字符'))
  page.limit=2
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['valid','extra'])
  page.toggle(page.rows[1],true)
  page.toggle(page.rows[2],true)
  assert.deepEqual(page.items.map((row:any)=>row.id),['valid','extra'])
  page.settings.language_mode='translated'
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['long','valid'])
  page.settings.language_mode='bilingual'
  page.toggle(page.rows[0],true)
  assert.equal(page.excludedItems.length,0,'手动选入的超长原文允许完整上屏')
  assert.ok(page.plannedItems.some((row:any)=>row.id==='long'))
  assert.equal(page.previewInvalid,false)
  page.items.find((row:any)=>row.id==='long').source_text='保留含义的精简上屏文字'
  assert.equal(page.excludedItems.length,0)
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['long','valid'])
  assert.equal(page.rows[0].text.length,720)
})

test('超长手选仅提示，完整参与预览与确认且不截断文字；取消保留后恢复自动限制',async t=>{
  const data=lengthFixture(), drafts=data.rows.map(row=>({...row,source_text:row.text,display_text:row.text_zh,selection_origin:'manual'}))
  const {html,page}=await render(data,drafts)
  assert.ok(html.includes('中文')&&html.includes('超出'))
  assert.deepEqual(page.plannedItems.map((row:any)=>row.id),['long','valid','extra'])
  assert.deepEqual(page.previewItems.map((row:any)=>row.id),['long','valid','extra'])
  assert.equal(page.invalid,false)
  assert.equal(page.previewInvalid,false)
  assert.equal(page.excludedItems.length,0)
  assert.equal(page.plannedPosition(page.items[1]),'第 2 条 · 20 秒')
  assert.doesNotMatch(html,/全部符合要求后可统一确认|调整提示的文字或移除该条后再确认/)
  let confirmation:any, saved:any
  t.mock.method(page.dialog,'info',(options:any)=>{confirmation=options})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT')saved=JSON.parse(options.body as string)
    return {ok:true,json:async()=>responseFor(url)} as any
  })
  page.confirmPlan()
  assert.equal(confirmation.title,'确认 3 条上屏评论')
  await confirmation.onPositiveClick()
  assert.deepEqual(saved.items.map((row:any)=>row.id),['long','valid','extra'])
  assert.equal(saved.items[0].source_text,'x'.repeat(720),'保存完整手选文字，不做截断')
  page.items=drafts
  page.setSelectionOrigin(page.items[0],'automatic')
  assert.deepEqual(page.plannedItems.map((row:any)=>row.id),['valid','extra'])
  assert.equal(page.invalid,true)
  assert.equal(page.excludedItems.length,1)
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

test('手动超长评论在真实列表仅提示但可定位预览，空文字和排版错误仍阻止确认',async t=>{
  const data=lengthFixture()
  const long=data.rows[0]
  data.overlay={items:[{...long,source_text:long.text,display_text:'中'.repeat(241),selection_origin:'manual'}],settings:{language_mode:'bilingual'}} as any
  const {page,html,commentPreview}=await render(data)
  assert.equal(page.invalid,false)
  assert.equal(page.previewInvalid,false)
  assert.equal(page.candidateStatus(page.rows[0]).status,'ready')
  assert.equal(page.candidateStatus(page.rows[0]).issues.length,2)
  assert.match(html,/原文 720\/600 字符，超出 120 字符/)
  assert.match(html,/中文 241\/240 字符，超出 1 字符/)
  assert.match(html,/字号达到下限后会扩展高度/)
  assert.doesNotMatch(html,/文字自动缩小到放下|保证全部放入画面/)
  assert.doesNotMatch(html,/不上屏|暂不进入预览|需调整 1 条|全部符合要求后可统一确认/)
  assert.equal(commentPreview.$props.invalid,false,'实际预览子组件接收完整手选超长评论')
  assert.equal(commentPreview.$props.items.length,1)
  assert.equal(commentPreview.$props.items[0].source_text,long.text)
  assert.equal(commentPreview.$props.items[0].display_text.length,241)
  const snapshot=page.snapshot
  await page.previewComment(page.rows[0])
  assert.equal(page.temporaryPreviewId,'','手选超长已有真实位置，无需临时试放')
  assert.equal(page.previewFocusId,long.id)
  assert.equal(page.snapshot,snapshot)
  page.items[0].display_text='  '
  assert.equal(page.invalid,true)
  assert.equal(page.plannedItems.length,0)
  let confirmation:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  page.confirmPlan()
  assert.equal(confirmation,undefined,'手动保留超长例外不豁免缺少当前语言文字')
  page.items[0].display_text='中'.repeat(241)
  page.settings.duration=30
  assert.equal(page.invalid,true,'超长手选也不得绕过真实排版参数错误')
  assert.equal(page.previewInvalid,true)
  page.confirmPlan()
  assert.equal(confirmation,undefined)
})

const layoutFields=()=>[
  ['font_size','译文最大字号（通用）'],['min_font_size','译文最小字号（通用）'],['height_percent','评论目标最大高度'],
  ['source_style_enabled','原文独立样式'],['source_font_size','原文最大字号'],['source_min_font_size','原文最小字号'],
].map(([key,label])=>({path:'comments.'+key,label,type:key==='source_style_enabled'?'bool':'int',min:key==='height_percent'?1:8,max:key==='height_percent'?100:120,
  ...(key.startsWith('source_')&&key!=='source_style_enabled'?{depends_on:[['comments.source_style_enabled',true]]}:{})}))

test('评论默认字号范围和目标高度完整回填；项目覆盖与原文隐藏不丢失保存值',async()=>{
  const initial=await render(fixture())
  assert.equal(initial.page.settings.font_size,36);assert.equal(initial.page.settings.min_font_size,18)
  assert.equal(initial.page.settings.source_font_size,30);assert.equal(initial.page.settings.source_min_font_size,18)
  assert.equal(initial.page.settings.height_percent,30)
  const data:any=targetFixture(2,2)
  data.fields=layoutFields()
  data.defaults={font_size:44,min_font_size:20,height_percent:35,source_font_size:32,source_min_font_size:16}
  data.overlay.settings={font_size:48,min_font_size:24,source_font_size:34,source_min_font_size:17,source_style_enabled:false}
  const {page,html,styleControls}=await render(data)
  assert.equal(page.settings.font_size,48);assert.equal(page.settings.min_font_size,24)
  assert.equal(page.settings.height_percent,35)
  assert.equal(page.settings.source_font_size,34);assert.equal(page.settings.source_min_font_size,17)
  assert.match(html,/role="group" aria-label="译文字号范围（通用）"/)
  assert.doesNotMatch(html,/aria-label="原文字号范围"/)
  assert.deepEqual(styleControls.filter((control:any)=>/font_size$/.test(control.$props.field.path)).map((control:any)=>control.$props.field.path),['comments.min_font_size','comments.font_size'])
  assert.equal(styleControls.find((control:any)=>control.$props.field.path==='comments.min_font_size').$props.modelValue,24)
  const visible=await render(data,undefined,{seed:page=>page.setStyle('source_style_enabled',true)})
  assert.match(visible.html,/aria-label="原文字号范围"/)
  assert.deepEqual(visible.styleControls.filter((control:any)=>/source_.*font_size$/.test(control.$props.field.path)).map((control:any)=>control.$props.field.path),['comments.source_min_font_size','comments.source_font_size'])
  assert.equal(visible.page.settings.source_min_font_size,17)
  assert.equal(visible.page.settings.source_font_size,34)
  assert.equal(page.items[0].selection_origin,'automatic','展示字号范围不能把旧自动选择改成手动')
})

test('字号范围逐项继承和修改，确认请求完整保存范围、高度与显式来源意图',async t=>{
  const data:any=targetFixture(2,2)
  data.fields=layoutFields()
  data.defaults={font_size:44,min_font_size:20,height_percent:35,source_font_size:32,source_min_font_size:16}
  data.overlay.settings={font_size:48,min_font_size:24,source_font_size:34,source_min_font_size:17}
  data.configuration={...data.configuration,defaults:{'comments.font_size':44,'comments.source_min_font_size':16},
    sources:{'comments.font_size':{kind:'project'},'comments.source_min_font_size':{kind:'project'}},
    parent_sources:{'comments.font_size':{kind:'template',name:'评论模板'},'comments.source_min_font_size':{kind:'global'}},
    overrides:{'comments.font_size':48,'comments.source_min_font_size':17}}
  const {page,styleControls}=await render(data)
  const minimum=styleControls.find((control:any)=>control.$props.field.path==='comments.min_font_size')
  assert.ok(minimum)
  const update=minimum.$.vnode.props?.['onUpdate:modelValue']
  assert.equal(typeof update,'function');update(22)
  assert.equal(page.settings.min_font_size,22);assert.equal(page.ownsStyle('min_font_size'),true)
  assert.equal(page.styleSource('min_font_size').kind,'project')
  page.inheritStyle('font_size');page.inheritStyle('source_min_font_size')
  assert.equal(page.settings.font_size,44);assert.equal(page.settings.source_min_font_size,16)
  assert.equal(page.ownsStyle('font_size'),false);assert.equal(page.styleSource('font_size').kind,'template')
  assert.equal(page.styleSource('source_min_font_size').kind,'global')
  page.setStyle('height_percent',42)
  const originalItems=JSON.stringify(page.items)
  let confirmation:any,saved:any
  t.mock.method(page.dialog,'info',(options:any)=>{confirmation=options})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT')saved=JSON.parse(options.body as string)
    return {ok:true,json:async()=>responseFor(url)} as any
  })
  page.confirmPlan();assert.ok(confirmation);await confirmation.onPositiveClick()
  for(const [key,value] of Object.entries({font_size:44,min_font_size:22,height_percent:42,source_font_size:34,source_min_font_size:16}))assert.equal(saved.settings[key],value,key)
  assert.deepEqual(saved.settings_explicit.sort(),['height_percent','min_font_size'])
  assert.deepEqual(saved.settings_reset.sort(),['font_size','source_min_font_size'])
  assert.equal(JSON.stringify(saved.items),originalItems,'修改排版不得修改上屏文字或手动保留来源')
})

test('评论排版逐项显示恢复入口，字号两端独立恢复且不自动保存',async t=>{
  const data:any=targetFixture(2,2)
  data.fields=layoutFields()
  data.overlay.settings={font_size:48,min_font_size:24,height_percent:42}
  data.configuration={...data.configuration,
    defaults:{'comments.font_size':44,'comments.min_font_size':20,'comments.height_percent':35},
    overrides:{'comments.font_size':48,'comments.min_font_size':24},
    parent_sources:{'comments.font_size':{kind:'series',name:'字幕模板'},'comments.min_font_size':{kind:'global'}},
    sources:{'comments.font_size':{kind:'project'},'comments.min_font_size':{kind:'project'}}}
  const {page,html,resetButtons}=await render(data)
  const byLabel=(label:string)=>resetButtons.find(button=>button.$props.label===label)
  const minimum=byLabel('译文最小字号（通用）'),maximum=byLabel('译文最大字号（通用）')
  assert.ok(minimum);assert.ok(maximum)
  assert.equal(minimum.$props.disabled,false);assert.equal(maximum.$props.disabled,false)
  assert.equal(byLabel('评论目标最大高度').$props.disabled,true)
  assert.match(html,/译文最小字号（通用）恢复项目基础值/)
  assert.match(html,/译文最大字号（通用）恢复项目基础值/)
  const originalItems=JSON.stringify(page.items),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,options]);return {ok:true,json:async()=>responseFor(url)} as any})
  minimum.$.setupState.reset()
  assert.equal(page.settings.min_font_size,20);assert.equal(page.settings.font_size,48)
  assert.equal(page.ownsStyle('min_font_size'),false);assert.equal(page.ownsStyle('font_size'),true)
  assert.equal(page.styleSource('min_font_size').kind,'global')
  assert.deepEqual([...page.styleReset],['min_font_size'])
  assert.equal(page.dirty,true);assert.equal(JSON.stringify(page.items),originalItems)
  page.inheritStyle('height_percent')
  assert.equal(page.settings.height_percent,42,'未覆盖的选项不能由迟到恢复事件改动')
  assert.equal(calls.filter(call=>call[1]?.method).length,0)
})

test('排版恢复遇到缺失基础值或运行锁定时保留当前值和覆盖',async()=>{
  const data:any=targetFixture(2,2)
  data.fields=layoutFields();data.overlay.settings={font_size:48,min_font_size:24}
  data.configuration={...data.configuration,defaults:{'comments.font_size':44},overrides:{'comments.font_size':48,'comments.min_font_size':24}}
  const missing=await render(data)
  assert.equal(missing.resetButtons.find(button=>button.$props.label==='译文最小字号（通用）').$props.disabled,true)
  missing.page.inheritStyle('min_font_size')
  assert.equal(missing.page.settings.min_font_size,24);assert.equal(missing.page.ownsStyle('min_font_size'),true)
  const locked=await render(data,undefined,{props:{blocked:true}})
  const maximum=locked.resetButtons.find(button=>button.$props.label==='译文最大字号（通用）')
  assert.equal(maximum.$props.disabled,true);maximum.$.setupState.reset();locked.page.inheritStyle('font_size')
  assert.equal(locked.page.settings.font_size,48);assert.equal(locked.page.ownsStyle('font_size'),true)
  assert.equal(locked.page.styleReset.size,0)
})

test('最小字号大于最大字号阻止预览、准备和确认；原文独立样式关闭时保留但不校验范围',async t=>{
  const data:any=targetFixture(2,2)
  data.fields=layoutFields()
  const invalid=await render(data,undefined,{seed:page=>page.setStyle('min_font_size',50)})
  assert.equal(invalid.page.settingsError,'译文最小字号不能大于最大字号。')
  assert.equal(invalid.page.previewInvalid,true);assert.equal(invalid.commentPreview.$props.invalid,true)
  let confirmation:any
  const calls:any[]=[]
  t.mock.method(invalid.page.dialog,'info',(options:any)=>{confirmation=options})
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,options]);return {ok:true,json:async()=>responseFor(url)} as any})
  invalid.page.confirmPlan();await invalid.page.prepareTarget()
  assert.equal(confirmation,undefined);assert.equal(calls.filter(call=>call[1]?.method).length,0)
  invalid.page.setStyle('min_font_size',18)
  invalid.page.setStyle('source_min_font_size',50)
  assert.equal(invalid.page.settingsError,'');assert.equal(invalid.page.previewInvalid,false)
  invalid.page.setStyle('source_style_enabled',true)
  assert.equal(invalid.page.settingsError,'原文最小字号不能大于最大字号。')
  assert.equal(invalid.page.previewInvalid,true)
  invalid.page.confirmPlan();assert.equal(confirmation,undefined)
  invalid.page.setStyle('source_style_enabled',false)
  assert.equal(invalid.page.settingsError,'');assert.equal(invalid.page.settings.source_min_font_size,50)
  invalid.page.confirmPlan();assert.ok(confirmation,'关闭独立样式后不使用该范围，允许确认且保留数值')
})

test('已选评论编辑区域至少扩大到旧高度三倍，并支持用户纵向调整',async()=>{
  const {html}=await render(targetFixture(30,30))
  assert.match(html,/class="ordered-list" role="region" aria-label="已选评论编辑区"/)
  const source=await readFile(new URL('../src/components/CommentStudioPanel.vue',import.meta.url),'utf8')
  const {descriptor}=parse(source),css=postcss.parse(descriptor.styles.map(style=>style.content).join('\n'))
  const declarations:Record<string,string>={}
  css.walkRules('.ordered-list',rule=>rule.walkDecls(declaration=>{declarations[declaration.prop]=declaration.value}))
  assert.ok(Number.parseFloat(declarations['max-height'])>=380*3)
  assert.equal(declarations.resize,'vertical');assert.equal(declarations.overflow,'auto')
})
test('重试未完成翻译与仅用缓存传递明确意图，不发布或覆盖用户上屏文字',async t=>{
  const {page}=await render(), calls:any[]=[]
  page.items=[{id:'a',source_hash:'a',display_text:'用户编辑的文案'}]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.retry('filter')
  assert.equal(calls.filter(c=>c[1]?.method==='POST').length,0,'只有审核拦截的评分不再发送重试')
  await page.retry('translate')
  assert.equal(page.working,'','未完成上一请求收尾')
  assert.equal(page.rulesDirty,false,'规则编辑状态不应被只读加载改变')
  assert.ok(page.prompt,'必须保留评分要求')
  await page.process('filter',false,{only_cached:true})
  const writes=calls.filter(c=>c[1]?.method==='POST')
  assert.equal(writes.length,2)
  assert.deepEqual(JSON.parse(writes[0][1].body),{retry_failed:true,ids:['b']})
  assert.equal(JSON.parse(writes[1][1].body).only_cached,true)
  assert.equal(page.items[0].display_text,'用户编辑的文案')
  assert.ok(calls.every(c=>!c[0].includes('/publish')&&!c[0].includes('/overlay')))
})

const processingFixture=()=>({...fixture(),rows:[
  {id:'unscored',author:'尚未评分作者',source_hash:'unscored',text:'x'.repeat(1011),text_zh:''},
  {id:'waiting',author:'等待中文作者',source_hash:'waiting',text:'x'.repeat(1011),text_zh:''},
  {id:'oversize',author:'已完成超长作者',source_hash:'oversize',text:'Valid source',text_zh:'中'.repeat(241)},
  {id:'ready',author:'已完成可用作者',source_hash:'ready',text:'Ready source',text_zh:'可用中文'},
],available_assessments:[
  {id:'waiting',eligible:true,score:99,reason:'等待中文的高分评论'},
  {id:'oversize',eligible:true,score:98,reason:'已经有中文的高分评论'},
  {id:'ready',eligible:true,score:90,reason:'评分和文字均已具备'},
],filter_stale:false,filter:{items:[],assessments:[]},rating_progress:{saved:3,total:4,remaining:1},
  defaults:{language_mode:'bilingual'},job:{action:'filter',status:'running',processed:3,total:4,cached:0,skipped:0}})

test('评分运行中按逐条已完成结果显示候选，不把未译或未评分冒充排除',async()=>{
  const {html,page}=await render(processingFixture())
  assert.equal(page.candidateStatus(page.rows[0]).status,'pending-score')
  assert.equal(page.candidateStatus(page.rows[1]).status,'pending-translation')
  assert.equal(page.candidateStatus(page.rows[2]).status,'needs-text')
  assert.equal(page.candidateStatus(page.rows[3]).status,'ready')
  assert.equal(countStatus(page,'pending-translation'),1)
  assert.equal(countStatus(page,'needs-text'),1)
  assert.deepEqual(page.suggestions.map((row:any)=>row.id),['ready'])
  assert.match(html,/评论仍在处理中/)
  assert.match(html,/待翻译后检查上屏条件/)
  assert.match(html,/上屏文字待调整/)
  assert.match(html,/中文 241\/240 字符，超出 1 字符/)
  assert.doesNotMatch(html,/中文上屏文字为空|原文 1011\/600 字符|评分合格的评论因超长或缺少所选语言文字已排除/)
  assert.doesNotMatch(page.assessmentReason('waiting'),/文字不满足上屏要求/)
  assert.match(page.assessmentReason('waiting'),/翻译|中文/)
})

test('评分结束、局部失败或中断后，仍待翻译的评论保持中性状态',async()=>{
  for(const status of ['done','partial','failed','interrupted','cancelled']){
    const data=processingFixture();data.job={...data.job,status}
    const {html,page}=await render(data)
    assert.equal(page.candidateStatus(page.rows[1]).status,'pending-translation',status)
    assert.equal(countStatus(page,'pending-translation'),1,status)
    assert.equal(countStatus(page,'needs-text'),1,status)
    assert.match(html,/待翻译后检查上屏条件/,status)
    assert.doesNotMatch(html,/中文上屏文字为空|评分合格的评论因超长或缺少所选语言文字已排除/,status)
    assert.equal(page.candidateStatus(page.rows[3]).status,'ready',status)
  }
})

test('准备提交、进度中运行的独立任务和自动流程均不会显示最终排除总结',async()=>{
  const data=processingFixture();data.job={...data.job,status:'done'}
  const pending=await render(data,undefined,{seed:page=>{page.pending='translate'}})
  assert.equal(pending.page.working,'translate')
  assert.match(pending.html,/评论仍在处理中/)
  const progress=await render(data,undefined,{seed:page=>{
    page.progressFeed.supported.value=true
    page.progressFeed.snapshot.value={job:{action:'translate',status:'done'},jobs:{filter:{action:'filter',status:'running',processed:1,total:2}}}
  }})
  assert.equal(progress.page.working,'filter')
  assert.match(progress.html,/评论仍在处理中/)
  const automatic=await render(data,undefined,{props:{automation:{state:'running',message:'正在准备评论上屏方案'}}})
  assert.match(automatic.html,/评论仍在处理中/)
  assert.doesNotMatch(automatic.html,/评分合格的评论因超长或缺少所选语言文字已排除/)
})

test('候选尚在处理时，本地审查及真实AI问题仍展示，人工草稿依旧严格校验',async()=>{
  const data=processingFixture()
  data.rows[0]={...data.rows[0],review_blocked:true,review_reasons:['需要人工核对'],ai_issues:{translate:{status:'failed',message:'模型结果格式不完整'}}} as any
  const draft={id:'waiting',source_hash:'waiting',source_text:data.rows[1].text,display_text:''}
  const {html,page}=await render(data,[draft])
  assert.match(html,/本地审查命中 · 不上屏/)
  assert.match(html,/翻译：处理失败/)
  assert.match(html,/模型结果格式不完整/)
  assert.equal(page.excludedItems.length,1)
  assert.equal(page.plannedItems.length,0)
  assert.equal(page.invalid,true)
  assert.equal(page.candidateStatus(page.rows[1]).status,'pending-translation')
  assert.equal(countStatus(page,'pending-translation'),1)
})

test('当前语言即时影响可用候选，原文模式不等待中文且不截断原文',async()=>{
  const data=processingFixture()
  data.rows[1].text='短原文也可以直接上屏'
  data.defaults.language_mode='original'
  const {page}=await render(data)
  assert.equal(page.candidateStatus(page.rows[1]).status,'ready')
  assert.equal(countStatus(page,'pending-translation'),0)
  assert.ok(page.suggestions.some((row:any)=>row.id==='waiting'))
  page.rows[1].text='x'.repeat(1011)
  page.settings.language_mode='translated'
  assert.equal(page.candidateStatus(page.rows[1]).status,'pending-translation')
  page.settings.language_mode='bilingual'
  assert.equal(page.candidateStatus(page.rows[1]).status,'pending-translation','缺译文仍显示处理中性状态，具体准备校验由服务端统一完成')
  assert.equal(page.rows[1].text.length,1011)
})

const targetFixture=(translated=30,total=120)=>{
  const rows=Array.from({length:total},(_,index)=>({id:`target-${index+1}`,author:`观众 ${index+1}`,
    source_hash:`hash-${index+1}`,text:`Original comment ${index+1}`,text_zh:index<translated?`已完成译文 ${index+1}`:''}))
  const overlayItems=rows.slice(0,30).map(row=>({id:row.id,source_hash:row.source_hash,
    source_text:row.text,display_text:row.text_zh}))
  return {...fixture(),rows,available_assessments:rows.map((row,index)=>({id:row.id,eligible:true,
    score:100-index/10,reason:'符合评分要求'})),filter_stale:false,
    rating_progress:{saved:total,total,remaining:0},overlay:{items:overlayItems,settings:{}},
    configuration:{...fixture().configuration,values:{'comments.prompt':'观众反馈','comments.count':30}},
    text_limits:{translated:240,original:600},job:{action:'filter',status:'done',processed:total,total}}
}
const draftRow=(row:any,origin='manual')=>({id:row.id,source_hash:row.source_hash,text:row.text,author:row.author,
  source_text:row.text,display_text:row.text_zh,selection_origin:origin})
const preparedState=(data:any,items:any[],target:number,excluded_ids:any[]=[],order_mode='chronological')=>({...data,
  preparation:{draft:{items,excluded_ids,order_mode,settings:{...data.defaults,language_mode:'translated'}},
    summary:{target,ready:items.length,selected:items.length,manual:items.filter(row=>row.selection_origin==='manual').length,
      automatic:items.filter(row=>row.selection_origin!=='manual').length,translated:Math.max(0,items.length-30),
      cached:Math.min(30,items.length),shortfall:Math.max(0,target-items.length)},warnings:[]}})

test('尚未评分也有统一准备入口，逐条翻译与恢复人工排除在列表中直接可见',async()=>{
  const data={...fixture(),available_assessments:[],rating_progress:{saved:0,total:2,remaining:2},job:null,
    rows:[fixture().rows[0],{id:'b',author:'乙',text:'尚未翻译的原文',text_zh:'',source_hash:'b'}]}
  const {html,page}=await render(data,undefined,{seed:page=>{page.excluded=[{id:'b',source_hash:'b'}]}})
  assert.equal(page.assessments.size,0)
  const buttons=[...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match=>match[0])
  const prepare=buttons.find(value=>value.replace(/<[^>]*>/g,'').trim()==='准备目标数量评论')
  assert.ok(prepare)
  assert.doesNotMatch(prepare,/\bdisabled(?:=|\s|>)/,'没有缓存评分不应阻止一键准备')
  assert.match(html,/翻译本条/)
  assert.match(html,/手动排除/)
  assert.match(html,/恢复自动候选/)
  assert.match(html,/任意评论均可单独翻译、勾选并编辑上屏文字/)
  assert.doesNotMatch(html,/补译候选（本次|选入已就绪|全部重新评分/,'次要处理默认折叠，让主要路径清楚')
})

test('旧选择来源缺失默认归自动，不展示交接提示或按钮',async()=>{
  const {html,page}=await render(targetFixture(),undefined,{seed:page=>{page.limit=10}})
  const text=html.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ')
  assert.equal(page.items.length,30)
  assert.ok(page.items.every((item:any)=>item.selection_origin==='automatic'))
  assert.equal(page.manualItems.length,0)
  assert.equal(page.automaticItems.length,30)
  assert.match(text,/自动选择 30 条/)
  assert.doesNotMatch(text,/旧版来源未记录|旧版选择交给自动|交给自动选择/)
  assert.doesNotMatch(text,/手动保留 30 条|已手动保留 30 条/)
  assert.match(text,/按评分选入；上屏按发布时间从早到晚排列/)
})

test('目标30改60即时更新准备入口，修改输入本身不调用AI或改写已确认方案',async t=>{
  const data=targetFixture(),{page}=await render(data,undefined,{seed:page=>{page.limit=30}})
  await page.load()
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  const previousItems=JSON.stringify(page.items),previousBaseline=page.baseline
  assert.equal(page.candidateTarget,30)
  assert.equal(page.suggestions.length,30)
  page.limit=60
  await nextTick()
  assert.equal(page.candidateTarget,60)
  assert.equal(page.suggestions.length,30)
  assert.equal(JSON.stringify(page.items),previousItems)
  assert.equal(page.baseline,previousBaseline)
  assert.equal(page.dirty,true,'目标数量也是待确认草稿的一部分')
  assert.equal(calls.length,0)
  assert.equal(data.configuration.values['comments.count'],30,'本地目标不会擅自改写已保存的项目规则')
  const {html}=await render(data,undefined,{seed:page=>{page.limit=60}})
  const text=html.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ')
  assert.match(text,/目标数量/)
  assert.match(text,/准备目标数量评论/)
  assert.match(html,/aria-label="候选准备情况"/)
  for(const expected of ['目标 60 条','待上屏 30 条','手动保留 0 条','自动选择 30 条'])assert.ok(text.includes(expected),expected)
  assert.match(html,/aria-label="目标上屏评论数量（不调用 AI）"/)
  assert.match(text,/自动复用评分和译文，只补缺少的部分/)
  assert.match(text,/未确认修改/)
  assert.doesNotMatch(text,/补译候选（本次|选入已就绪|翻译高分候选|使用前 30 条可用评论/)
})

test('真实数字输入框键入60无需失焦即更新目标，整数校验不使用阻断实时更新的precision',async t=>{
  const data=targetFixture(),{page,targetNumberInput,targetTextInput}=await render(data,undefined,{seed:page=>{page.limit=30}})
  await page.load()
  assert.ok(targetNumberInput,'必须验证实际目标NInputNumber，而非直接修改父组件的limit')
  assert.ok(targetTextInput,'必须验证NInputNumber内部实际NInput的输入事件')
  const numberProps=targetNumberInput.$props
  assert.equal(numberProps.precision,undefined,'Naive UI设置precision时不会在键入阶段更新value')
  assert.equal(numberProps.updateValueOnInput,true)
  assert.equal(numberProps.step,1)
  assert.equal(numberProps.max,100)
  assert.equal(numberProps.validator(60),true)
  assert.equal(numberProps.validator(60.5),false)
  assert.equal(numberProps.validator(.5),false)
  const calls:any[]=[],oldItems=JSON.stringify(page.items),oldBaseline=page.baseline
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  // 内部NInput真实绑定到InputNumber.handleUpdateDisplayedValue；不触发blur、Enter或增减按钮。
  targetTextInput.$props.onUpdateValue('60',{source:0})
  await nextTick()
  assert.equal(targetNumberInput.$.setupState.displayedValue,'60')
  assert.equal(page.limit,60)
  assert.equal(page.candidateTarget,60)
  assert.equal(page.suggestions.length,30)
  targetTextInput.$props.onUpdateValue('60.5',{source:0})
  await nextTick()
  assert.equal(page.limit,60,'非整数文本不能扩大补译请求范围')
  assert.equal(JSON.stringify(page.items),oldItems)
  assert.equal(page.baseline,oldBaseline)
  assert.equal(page.dirty,true,'目标数量修改需随最终方案一起确认')
  assert.equal(calls.length,0)
})

test('准备目标一次获得60条草稿，复用评分译文且不会自行确认或发布',async t=>{
  const data=targetFixture(),{page}=await render(data,undefined,{seed:page=>{page.limit=60}})
  await page.load()
  const completed=targetFixture(60),calls:any[]=[]
  activeFixture=completed
  const prepared=preparedState(completed,completed.rows.slice(0,60).map(row=>draftRow(row,'automatic')),60)
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>String(url).endsWith('/prepare')?prepared:responseFor(url)} as any})
  const oldOverlay=JSON.stringify(page.state.overlay),oldBaseline=page.baseline
  await page.prepareTarget()
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1)
  assert.match(writes[0][0],/\/comments\/prepare$/)
  const body=JSON.parse(writes[0][1].body)
  assert.equal(body.target,60)
  assert.equal(body.prompt,'观众反馈')
  assert.equal(body.revision,'rev')
  assert.equal(body.items.length,30)
  assert.ok(body.items.every((row:any)=>row.selection_origin==='automatic'),'旧方案来源未记录时按自动选择重新准备，不伪造人工保留')
  assert.deepEqual(body.excluded_ids,[])
  assert.equal(body.settings.language_mode,'translated')
  assert.equal(page.suggestions.length,60)
  assert.equal(page.items.length,60)
  assert.deepEqual(page.items.map((row:any)=>row.id),completed.rows.slice(0,60).map(row=>row.id))
  assert.equal(page.items.filter((row:any)=>row.selection_origin==='manual').length,0)
  assert.equal(page.items.filter((row:any)=>row.selection_origin==='automatic').length,60)
  assert.equal(page.dirty,true,'准备结果仍是待确认草稿')
  assert.equal(page.baseline,oldBaseline)
  assert.equal(JSON.stringify(page.state.overlay),oldOverlay,'轮询与准备不能提前改写已确认方案')
  assert.ok(calls.every(call=>!call[0].includes('/overlay')&&!call[0].includes('/publish')))
})

test('手选低分或未评分评论独立保留，移除会记住原文身份且重新勾选可撤销排除',async()=>{
  const data=targetFixture(60)
  data.available_assessments[40].eligible=false
  data.available_assessments=data.available_assessments.filter(item=>item.id!==data.rows[41].id)
  const {page}=await render(data)
  const row=page.rows[40]
  page.toggle(row,true)
  let picked=page.items.find((item:any)=>item.id===row.id)
  assert.equal(picked.selection_origin,'manual','逐条选择不能被当成自动补位')
  picked.display_text='人工调整后需要保留的中文'
  page.toggle(row,false)
  assert.ok(!page.items.some((item:any)=>item.id===row.id))
  assert.deepEqual(JSON.parse(JSON.stringify(page.excluded)),[{id:row.id,source_hash:row.source_hash}])
  page.toggle(row,true)
  picked=page.items.find((item:any)=>item.id===row.id)
  assert.equal(picked.selection_origin,'manual')
  assert.deepEqual(page.excluded,[])
  assert.equal(page.rows[40].text_zh,'已完成译文 41','上屏编辑不改写完整译文')
  page.toggle(page.rows[41],true)
  assert.equal(page.items.find((item:any)=>item.id===page.rows[41].id).selection_origin,'manual','无评分条目仍能由人选择')
})

test('降低目标只整理自动项，准备请求包含手选文字和人工排除而不即时保存',async t=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,5).map((row,index)=>draftRow(row,index===0?'manual':'automatic'))
  const {page}=await render(data,undefined,{seed:page=>{page.limit=2}})
  await page.load()
  page.items[0].display_text='这条手工文字需要保留'
  page.toggle(page.rows[1],false)
  const excluded=[{id:data.rows[1].id,source_hash:data.rows[1].source_hash}]
  const manual=JSON.parse(JSON.stringify(page.items[0]))
  const response=preparedState(data,[manual,draftRow(data.rows[2],'automatic')],2,excluded)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>String(url).endsWith('/prepare')?response:responseFor(url)} as any})
  await page.prepareTarget()
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1)
  const body=JSON.parse(writes[0][1].body)
  assert.equal(body.target,2)
  assert.deepEqual(body.excluded_ids,excluded)
  assert.equal(body.items.find((row:any)=>row.id===manual.id).display_text,'这条手工文字需要保留')
  assert.equal(body.items.find((row:any)=>row.id===manual.id).selection_origin,'manual')
  assert.deepEqual(page.items.map((row:any)=>row.id),[manual.id,data.rows[2].id])
  assert.equal(page.items[0].display_text,'这条手工文字需要保留')
  assert.deepEqual(JSON.parse(JSON.stringify(page.excluded)),excluded)
  assert.equal(page.dirty,true)
  assert.ok(calls.every(call=>call[1]?.method!=='PUT'&&!call[0].includes('/publish')))
})

test('单条补译可独立于评分与上屏选择执行，不覆盖已有人工文字或自动勾选',async t=>{
  const data=targetFixture(),{page}=await render(data)
  await page.load()
  page.items[0].display_text='已精调的上屏文案'
  const target=data.rows[40]
  data.available_assessments[40].eligible=false
  const previousSelection=page.items.map((row:any)=>row.id)
  const completed=structuredClone(data)
  completed.rows[40].text_zh='单独补译后的中文'
  completed.job={action:'translate',status:'done',processed:1,total:1,cached:0,failed:0,blocked:0,deferred:0} as any
  activeFixture=completed
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.translateRow(target)
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1)
  assert.match(writes[0][0],/\/comments\/translate$/)
  assert.deepEqual(JSON.parse(writes[0][1].body),{ids:[target.id],retry_failed:true})
  assert.equal(page.rows[40].text_zh,'单独补译后的中文')
  assert.deepEqual(page.items.map((row:any)=>row.id),previousSelection)
  assert.equal(page.items[0].display_text,'已精调的上屏文案')
  assert.ok(calls.every(call=>!call[0].includes('/overlay')&&!call[0].includes('/publish')))
})

test('准备失败保留人工选择文字与排除，可继续编辑并重试',async t=>{
  const data=targetFixture(),{page}=await render(data)
  await page.load()
  page.items[0].display_text='失败后仍应保留的上屏文字'
  page.toggle(page.rows[2],false)
  const before=JSON.stringify({items:page.items,excluded:page.excluded}),oldBaseline=page.baseline,calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options])
    const failed=String(url).endsWith('/prepare')
    return {ok:!failed,json:async()=>failed?{message:'模型服务暂时不可用'}:responseFor(url)} as any
  })
  await page.prepareTarget()
  assert.equal(JSON.stringify({items:page.items,excluded:page.excluded}),before)
  assert.equal(page.baseline,oldBaseline)
  assert.equal(page.dirty,true)
  assert.match(page.error,/模型服务暂时不可用/)
  assert.equal(page.pending,'')
  assert.equal(page.editLocked,false)
  assert.ok(calls.every(call=>call[1]?.method!=='PUT'))
})

test('确认上屏方案保存手动、自动和人工排除，旧版来源读取与保存均归自动',async t=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic'),draftRow(data.rows[3],'legacy')]
  const {page}=await render(data)
  await page.load()
  page.toggle(page.rows[2],false)
  page.items[0].display_text='确认保存的人工文字'
  let confirmation:any,saved:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT'){
      saved=JSON.parse(options.body as string)
      activeFixture={...data,overlay:{items:saved.items,settings:saved.settings,excluded_ids:saved.excluded_ids},overlay_revision:'next'} as any
    }
    return {ok:true,json:async()=>responseFor(url)} as any
  })
  page.confirmPlan()
  assert.ok(confirmation)
  await confirmation.onPositiveClick()
  assert.equal(saved.items[0].selection_origin,'manual')
  assert.equal(saved.items[1].selection_origin,'automatic')
  assert.equal(saved.items[2].selection_origin,'automatic')
  assert.deepEqual(saved.excluded_ids,[{id:data.rows[2].id,source_hash:data.rows[2].source_hash}])
  assert.equal(page.items[0].display_text,'确认保存的人工文字')
  assert.equal(page.items[0].selection_origin,'manual')
  assert.equal(page.items[1].selection_origin,'automatic')
  assert.equal(page.items[2].selection_origin,'automatic')
  assert.equal(page.manualItems.length,1)
  assert.equal(page.automaticItems.length,2)
  assert.deepEqual(JSON.parse(JSON.stringify(page.excluded)),saved.excluded_ids)
  assert.equal(page.dirty,false)
})

test('旧选择默认自动，明确设置手动保留只改意图，不改文字或请求模型',async t=>{
  const data=targetFixture(),{page}=await render(data)
  await page.load()
  assert.ok(page.items.every((item:any)=>item.selection_origin==='automatic'))
  page.items[0].display_text='交给自动时也应保留的文字'
  const before=page.items.map((item:any)=>({id:item.id,display_text:item.display_text})),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  page.setSelectionOrigin(page.items[0],'automatic')
  assert.equal(page.items[0].selection_origin,'automatic')
  assert.equal(page.items[0].display_text,'交给自动时也应保留的文字')
  assert.deepEqual(page.items.map((item:any)=>({id:item.id,display_text:item.display_text})),before)
  page.setSelectionOrigin(page.items[0],'manual')
  assert.equal(page.items[0].selection_origin,'manual')
  assert.equal(calls.length,0)
  assert.equal(page.dirty,true)
})

test('仅明确manual计为手动，缺失与legacy归自动且读取预览不改变意图',async t=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic'),
    draftRow(data.rows[2],'legacy'),{...draftRow(data.rows[3]),selection_origin:undefined}]
  const {page,html}=await render(data)
  assert.equal(page.manualItems.length,1)
  assert.equal(page.automaticItems.length,3)
  assert.deepEqual(page.items.map((item:any)=>item.selection_origin),['manual','automatic','automatic','automatic'])
  const text=html.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ')
  assert.match(text,/手动保留 1 条/)
  assert.match(text,/自动选择 3 条/)
  assert.doesNotMatch(text,/旧版来源未记录|旧版选择交给自动|交给自动选择/)
  const oldSnapshot=page.snapshot,calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.load()
  await page.previewComment(data.rows[1])
  await page.previewComment(data.rows[41])
  page.returnToPlanPreview()
  assert.equal(page.snapshot,oldSnapshot)
  assert.equal(page.dirty,false)
  assert.ok(calls.every(call=>!call[1]?.method||call[1]?.method==='GET'))
})

test('取消全部手动保留只解除手动项，保持选择、文字、顺序、排除和设置并可撤销',async t=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic'),draftRow(data.rows[2],'legacy')]
  const {page}=await render(data)
  page.orderMode='manual'
  page.excluded=[{id:data.rows[5].id,source_hash:data.rows[5].source_hash}]
  page.settings.start=12
  page.limit=17
  page.items[0].display_text='需要保留的精调文字'
  const before=JSON.parse(page.snapshot),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  page.releaseManualSelections()
  const released=JSON.parse(page.snapshot)
  assert.deepEqual(released.items.map((item:any)=>item.selection_origin),['automatic','automatic','automatic'])
  released.items[0].selection_origin='manual'
  assert.deepEqual(released,before,'解除保留不得顺带移除评论、修改文字、顺序、排版或人工排除')
  assert.equal(page.manualItems.length,0)
  assert.equal(page.automaticItems.length,3)
  assert.equal(page.dirty,true)
  page.undoSelectionRelease()
  assert.deepEqual(JSON.parse(page.snapshot),before)
  assert.equal(calls.length,0,'批量解除及撤销均是本地草稿操作，不调用模型或保存')
})

test('默认自动旧选择不产生额外解除范围，批量解除只处理真正手动项',async()=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic'),draftRow(data.rows[2],'legacy')]
  const {page}=await render(data)
  const ids=page.items.map((item:any)=>item.id)
  assert.equal(page.manualItems.length,1)
  page.releaseManualSelections()
  assert.deepEqual(page.items.map((item:any)=>item.selection_origin),['automatic','automatic','automatic'])
  page.undoSelectionRelease()
  assert.deepEqual(page.items.map((item:any)=>item.selection_origin),['manual','automatic','automatic'])
  assert.deepEqual(page.items.map((item:any)=>item.id),ids)
})

test('批量解除入口在准备区域可见且明确数量，解除后提供撤销',async()=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'manual'),draftRow(data.rows[2],'legacy')]
  const {html}=await render(data)
  const text=html.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ')
  assert.match(text,/取消全部手动保留（2）/)
  assert.doesNotMatch(text,/旧版选择交给自动|旧版来源未记录|全部交给自动选择/,'只保留取消全部手动保留这一个批量入口')
  const released=await render(data,undefined,{seed:page=>page.releaseManualSelections()})
  assert.match(released.html,/撤销解除/)
  assert.doesNotMatch(released.html,/取消全部手动保留（2）/)
})

test('运行锁定时批量解除与撤销均不能修改草稿，真实按钮也保持禁用',async()=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'legacy')]
  const {page,html}=await render(data,undefined,{seed:page=>{page.pending='prepare'}})
  const before=page.snapshot
  page.releaseManualSelections()
  page.undoSelectionRelease()
  assert.equal(page.snapshot,before)
  const buttons=[...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match=>match[0])
  for(const label of ['取消全部手动保留（1）']){
    const button=buttons.find(value=>value.replace(/<[^>]*>/g,'').trim()===label)
    assert.ok(button,label)
    assert.match(button,/\bdisabled(?:=|\s|>)/)
  }
  page.pending=''
  page.releaseManualSelections()
  page.pending='prepare'
  const released=page.snapshot
  page.undoSelectionRelease()
  assert.equal(page.snapshot,released,'不能通过撤销绕过执行锁')
})

test('撤销只恢复同一来源且仍为自动的条目，不污染变更来源或后续人工决定',async()=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,3).map(row=>draftRow(row,'manual'))
  const {page}=await render(data)
  page.releaseManualSelections()
  page.items[0].source_hash='另一个来源版本'
  page.items[1].selection_origin='manual'
  page.undoSelectionRelease()
  assert.deepEqual(page.items.map((item:any)=>item.selection_origin),['automatic','manual','manual'])
})

test('解除后编辑或改变选择使旧撤销失效，不将后续决定复写为旧保留',async t=>{
  for(const action of ['edit','select','remove','origin','hydrate'])await t.test(action,async()=>{
    const data=targetFixture(60)
    data.overlay.items=data.rows.slice(0,2).map(row=>draftRow(row,'manual'))
    const {page}=await render(data)
    page.releaseManualSelections()
    if(action==='edit')page.editText(page.items[0],'display_text','解除后继续修改文字')
    if(action==='select')page.toggle(data.rows[4],true)
    if(action==='remove')page.toggle(data.rows[0],false)
    if(action==='origin')page.setSelectionOrigin(page.items[0],'manual')
    if(action==='hydrate')page.hydrate({...data,overlay:{...data.overlay,items:data.overlay.items.map((row:any)=>({...row,selection_origin:'automatic'}))}})
    const changed=page.snapshot
    page.undoSelectionRelease()
    assert.equal(page.snapshot,changed,'后续编辑完成后旧批量解除不能再撤销')
  })
})

test('开始准备后清除解除撤销，即使准备失败也不恢复此前固定保留',async t=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,2).map(row=>draftRow(row,'manual'))
  const {page}=await render(data)
  page.releaseManualSelections()
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options])
    return {ok:!String(url).endsWith('/prepare'),json:async()=>String(url).endsWith('/prepare')?{message:'暂时无法完成准备'}:responseFor(url)} as any
  })
  await page.prepareTarget()
  assert.ok(calls.some(call=>call[0].endsWith('/prepare')))
  const beforeUndo=page.snapshot
  page.undoSelectionRelease()
  assert.equal(page.snapshot,beforeUndo)
  assert.ok(page.items.every((item:any)=>item.selection_origin==='automatic'))
})

test('输入框同值更新不会将自动评论误锁为手动，也不会清空有效撤销',async()=>{
  const data=targetFixture(60)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic'),draftRow(data.rows[2],'legacy')]
  const {page,overlayTextInputs}=await render(data,undefined,{seed:page=>{page.settings.language_mode='bilingual'}})
  page.releaseManualSelections()
  const before=page.snapshot
  for(const input of overlayTextInputs){
    const update=input.$props['onUpdate:value']||input.$props.onUpdateValue
    update(input.$props.value)
  }
  assert.equal(page.snapshot,before)
  page.undoSelectionRelease()
  assert.deepEqual(page.items.map((item:any)=>item.selection_origin),['manual','automatic','automatic'])
})

test('准备中的真实评分进度与诊断动作保留，停止发送准备任务ID而非旧评分ID',async t=>{
  const data=targetFixture()
  const {page,html}=await render(data,undefined,{seed:page=>{
    page.pending='prepare'
    page.progressFeed.supported.value=true
    page.progressFeed.snapshot.value={prepare_job:{id:'preparation-active',status:'running',target:60,message:'正在补齐评分'},
      job:{id:'old-translation',action:'translate',status:'done',processed:30,total:30},
      jobs:{filter:{id:'filter-active',action:'filter',status:'running',phase:'thinking',processed:3,total:10,batch:1,batches:2}}}
  }})
  assert.equal(page.working,'prepare')
  assert.equal(page.job.action,'filter','准备任务不能遮掉当前真实LLM评分动作')
  assert.match(html,/正在准备目标数量评论/)
  assert.match(html,/正在补齐评分/)
  assert.match(html,/停止准备/)
  assert.doesNotMatch(html,/旧筛选建议已停用/)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.stop()
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1)
  assert.match(writes[0][0],/\/comments\/cancel$/)
  assert.deepEqual(JSON.parse(writes[0][1].body),{job_id:'preparation-active'})
})

test('新准备提交后不借用旧已完成任务的目标与停止按钮，取得运行任务后才展示',async()=>{
  const data={...fixture(),job:null,prepare_job:{id:'old-preparation',status:'done',target:40,message:'上次准备已完成，请保存旧结果'}}
  const pending=await render(data,undefined,{seed:page=>{page.pending='prepare'}})
  const pendingBanner=pending.html.match(/<div\b[^>]*class="preparation-progress"[^>]*>[\s\S]*?<\/div>/)?.[0]
  assert.ok(pendingBanner,'提交后需要立即显示当前准备状态')
  assert.match(pendingBanner,/正在准备目标数量评论/)
  assert.match(pendingBanner,/正在检查缓存与人工选择/)
  assert.doesNotMatch(pendingBanner,/40|上次准备已完成|旧结果/,'旧终态不代表本次目标或阶段')
  assert.doesNotMatch(pending.html,/停止准备|停止当前处理/,'尚未取得运行任务ID时不能给无效停止入口')

  const active=await render({...data,prepare_job:{id:'new-preparation',status:'running',target:70,message:'正在补译当前目标评论'}},undefined,
    {seed:page=>{page.pending='prepare'}})
  const activeBanner=active.html.match(/<div\b[^>]*class="preparation-progress"[^>]*>[\s\S]*?<\/div>/)?.[0]
  assert.ok(activeBanner)
  assert.match(activeBanner,/· 70 条/)
  assert.match(activeBanner,/正在补译当前目标评论/)
  assert.match(active.html,/停止准备/)
  assert.doesNotMatch(activeBanner,/上次准备已完成|40/)
})

test('准备在途期间阻止重复提交，完成后解除锁定并保留未确认草稿',async t=>{
  const data=targetFixture(60),{page}=await render(data)
  await page.load()
  let complete:(value:any)=>void=()=>{}
  const gate=new Promise<any>(resolve=>{complete=resolve}),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options])
    return String(url).endsWith('/prepare')?await gate:{ok:true,json:async()=>responseFor(url)} as any
  })
  const task=page.prepareTarget()
  await nextTick()
  assert.equal(page.pending,'prepare')
  assert.equal(page.editLocked,true)
  await page.prepareTarget()
  assert.equal(calls.filter(call=>call[1]?.method==='POST').length,1)
  complete({ok:true,json:async()=>preparedState(data,data.rows.slice(0,35).map((row,index)=>draftRow(row,index<30?'manual':'automatic')),35)})
  await task
  assert.equal(page.pending,'')
  assert.equal(page.editLocked,false)
  assert.equal(page.items.length,35)
  assert.equal(page.dirty,true)
})

test('只有编辑自动项才转手动保留，移动顺序不隐式锁定被交换评论',async()=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,4).map(row=>draftRow(row,'automatic'))
  const {page}=await render(data)
  page.editText(page.items[0],'display_text','手工微调的文字')
  assert.equal(page.items[0].selection_origin,'manual')
  page.move(2,-1)
  assert.equal(page.items[1].selection_origin,'automatic')
  assert.equal(page.items[2].selection_origin,'automatic')
  const before=page.items.map((item:any)=>({id:item.id,display_text:item.display_text}))
  page.releaseManualSelections()
  assert.ok(page.items.every((item:any)=>item.selection_origin==='automatic'))
  assert.deepEqual(page.items.map((item:any)=>({id:item.id,display_text:item.display_text})),before)
})

test('真实上屏文字输入框精调自动项后立即转手动保留，不等待保存或下次准备',async()=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,2).map(row=>draftRow(row,'automatic'))
  const {page,overlayTextInputs}=await render(data,undefined,{seed:page=>{page.settings.language_mode='bilingual'}})
  const chinese=overlayTextInputs.find(input=>input.$props.inputProps['aria-label']==='第 1 条中文上屏文字')
  const source=overlayTextInputs.find(input=>input.$props.inputProps['aria-label']==='第 2 条原文上屏文字')
  assert.ok(chinese,'必须验证真实Naive UI文字输入框的绑定')
  assert.ok(source)
  const updateChinese=chinese.$props['onUpdate:value']||chinese.$props.onUpdateValue
  const updateSource=source.$props['onUpdate:value']||source.$props.onUpdateValue
  updateChinese('刚在输入框修改的中文')
  updateSource('Manually refined source')
  await nextTick()
  assert.equal(page.items[0].display_text,'刚在输入框修改的中文')
  assert.equal(page.items[0].selection_origin,'manual')
  assert.equal(page.items[1].source_text,'Manually refined source')
  assert.equal(page.items[1].selection_origin,'manual')
  assert.equal(page.dirty,true)
})

test('视频容量不足时保留全部手选并禁止确认，调整出现节奏后可继续',async t=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,3).map(row=>draftRow(row,'manual'))
  const {page,html}=await render(data,undefined,{props:{videoDuration:26}})
  assert.equal(page.timelineCapacity,2)
  assert.equal(page.capacityExceeded,true)
  assert.equal(page.invalid,true)
  assert.equal(page.items.length,3)
  assert.ok(page.items.every((item:any)=>item.selection_origin==='manual'))
  assert.match(html,/当前视频与出现节奏最多容纳 2 条，已保留 3 条/)
  let confirmation:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  page.confirmPlan()
  assert.equal(confirmation,undefined,'容量不足不能静默截掉人工评论再保存')
  page.setStyle('interval',6)
  assert.equal(page.timelineCapacity,3)
  assert.equal(page.invalid,false)
  assert.equal(page.items.length,3)
  assert.ok(page.items.every((item:any)=>item.selection_origin==='manual'))
  page.confirmPlan()
  assert.equal(confirmation.title,'确认 3 条上屏评论')
})

test('没有本地视频时也遵守准备响应的容量限制，不自动丢弃手选项',async t=>{
  const data=targetFixture(60)
  data.overlay.items=data.rows.slice(0,3).map(row=>draftRow(row,'manual'))
  const {page}=await render(data)
  await page.load()
  page.preparation={summary:{capacity:2,capacity_exceeded:true},invalid_manual:[],warnings:['当前出现节奏容纳不下全部手选']}
  assert.equal(page.timelineCapacity,2)
  assert.equal(page.invalid,true)
  assert.equal(page.items.length,3)
  let confirmation:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  page.confirmPlan()
  assert.equal(confirmation,undefined)
  page.toggle(page.rows[2],false)
  assert.equal(page.items.length,2)
  assert.equal(page.invalid,false)
})

test('手选缺少译文经单条翻译补齐后清除准备时旧错误，保持手选并允许确认',async t=>{
  const data=targetFixture(0,1)
  data.overlay.items=[draftRow(data.rows[0],'manual')]
  const {page}=await render(data)
  await page.load()
  page.preparation={summary:{target:1,ready:0},invalid_manual:[{id:data.rows[0].id,reasons:['中文上屏文字为空']}],warnings:['手选评论尚待翻译']}
  assert.equal(page.invalid,true)
  assert.equal(page.preparationIssues.length,1)
  const completed=structuredClone(data)
  completed.rows[0].text_zh='单条翻译补齐的中文'
  completed.job={action:'translate',status:'done',processed:1,total:1,cached:0,failed:0,blocked:0,deferred:0} as any
  activeFixture=completed
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.translateRow(page.rows[0])
  assert.equal(page.items.length,1)
  assert.equal(page.items[0].display_text,'单条翻译补齐的中文')
  assert.equal(page.items[0].selection_origin,'manual')
  assert.equal(page.preparationIssues.length,0,'补译成功不能继续被旧invalid_manual阻挡')
  assert.equal(page.invalid,false)
  assert.equal(page.dirty,true)
  assert.ok(calls.every(call=>call[1]?.method!=='PUT'&&!call[0].includes('/prepare')))
})

test('准备任务无评分翻译子任务时仍直接展示失败原因，不只显示有待处理项',async()=>{
  const data={...fixture(),job:null,prepare_job:{id:'prepare-failed',status:'failed',error:'评论准备失败：视频容量不足 <需调整>'}}
  const {html,page}=await render(data)
  assert.equal(page.job,null)
  assert.match(html,/有待处理项/)
  assert.match(html,/评论准备失败：视频容量不足 &lt;需调整&gt;/)
  assert.match(html,/class="process-error"/)
  assert.doesNotMatch(html,/正在准备目标数量评论|<需调整>/)
})

test('准备发现手选来源变化时保留草稿但拦住确认，用户明确移除后才能继续',async t=>{
  const data=targetFixture(60)
  data.overlay.items[0].selection_origin='manual'
  const {page}=await render(data)
  await page.load()
  const manual=JSON.parse(JSON.stringify(page.items[0]))
  const next=structuredClone(data)
  next.rows[0].source_hash='changed-source-hash'
  next.rows[0].text='已经变化的新原文'
  const prepared=preparedState(next,[manual,draftRow(next.rows[1],'automatic')],2)
  prepared.preparation.invalid_manual=[{id:manual.id,reasons:['评论来源已变化，请重新核对']}]
  let confirmation:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  t.mock.method(globalThis,'fetch',async(url)=>({ok:true,json:async()=>String(url).endsWith('/prepare')?prepared:next} as any))
  await page.prepareTarget()
  assert.equal(page.items[0].source_hash,manual.source_hash,'不能把旧人工文字直接绑定到新来源')
  assert.equal(page.invalid,true)
  page.confirmPlan()
  assert.equal(confirmation,undefined)
  page.toggle(page.rows[0],false)
  assert.equal(page.invalid,false)
  assert.ok(!page.items.some((item:any)=>item.id===manual.id))
  assert.deepEqual(JSON.parse(JSON.stringify(page.excluded)),[{id:manual.id,source_hash:'changed-source-hash'}])
})

test('自动超长译文仍不是可用候选，准备携带完整草稿而不擅自截断',async t=>{
  const data=targetFixture()
  data.rows[0].text_zh='中'.repeat(241)
  data.overlay.items[0].display_text='中'.repeat(241)
  const {page}=await render(data,undefined,{seed:page=>{page.limit=60}})
  await page.load()
  assert.equal(page.suggestions.length,29)
  assert.equal(page.invalid,true)
  const prepared=preparedState(data,JSON.parse(JSON.stringify(page.items)),60),calls:any[]=[]
  prepared.preparation.invalid_manual=[{id:data.rows[0].id,reasons:['中文超过上屏长度限制']}]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>String(url).endsWith('/prepare')?prepared:responseFor(url)} as any})
  await page.prepareTarget()
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,1)
  assert.match(writes[0][0],/\/comments\/prepare$/)
  assert.equal(JSON.parse(writes[0][1].body).items[0].display_text.length,241)
  assert.equal(page.items[0].display_text.length,241)
  assert.equal(page.invalid,true)
  assert.equal(page.rows[0].text_zh.length,241)
})

test('重新准备复用完整手选超长文字，结果仍可预览确认且不会被自动候选限制排除',async t=>{
  const data=targetFixture(60)
  data.rows[0].text='x'.repeat(601)
  data.rows[0].text_zh='中'.repeat(241)
  data.overlay.items=[draftRow(data.rows[0],'manual'),draftRow(data.rows[1],'automatic')]
  const {page}=await render(data,undefined,{seed:page=>{page.limit=2}})
  const prepared=preparedState(data,JSON.parse(JSON.stringify(page.items)),2),calls:any[]=[]
  prepared.preparation.warnings=['有 1 条手动保留评论较长，将完整上屏']
  prepared.preparation.invalid_manual=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {
    ok:true,json:async()=>String(url).endsWith('/prepare')?prepared:responseFor(url)} as any})
  await page.prepareTarget()
  const body=JSON.parse(calls.find(call=>call[0].endsWith('/prepare'))[1].body)
  assert.equal(body.items[0].selection_origin,'manual')
  assert.equal(body.items[0].source_text.length,601)
  assert.equal(body.items[0].display_text.length,241)
  assert.equal(page.preparationIssues.length,0)
  assert.equal(page.plannedItems.length,2)
  assert.equal(page.previewItems.length,2)
  assert.equal(page.previewInvalid,false)
  assert.equal(page.invalid,false)
  assert.equal(page.items[0].source_text.length,601)
  assert.equal(page.items[0].display_text.length,241)
  assert.equal(page.candidateStatus(page.rows[0]).status,'ready')
  assert.ok(!page.suggestions.some((row:any)=>row.id===data.rows[0].id),'人工明确保留不放宽自动候选池的长度要求')
  let confirmation:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  page.confirmPlan()
  assert.equal(confirmation.title,'确认 2 条上屏评论')
  assert.ok(calls.every(call=>call[1]?.method!=='PUT'&&!call[0].includes('/publish')))
})

test('人工中文参与候选状态，清空或来源变化即时回到待处理状态，不改完整译文',async()=>{
  const data=targetFixture(),drafts=[...data.overlay.items,
    ...data.rows.slice(30,35).map(row=>({id:row.id,source_hash:row.source_hash,source_text:row.text,display_text:'人工撰写的上屏中文'}))]
  const {page}=await render(data,drafts,{seed:page=>{page.limit=60}})
  assert.equal(page.suggestions.length,35)
  assert.equal(page.candidateStatus(page.rows[30]).status,'ready')
  page.items[0].display_text=''
  assert.equal(page.suggestions.length,34)
  assert.equal(page.rows[0].text_zh,'已完成译文 1')
  page.items.find((row:any)=>row.id===data.rows[30].id).source_hash='过期原文'
  assert.equal(page.candidateStatus(page.rows[30]).status,'pending-translation','旧来源草稿不能计作当前已就绪文字')
})

test('本地审查阻止仍不列为可用，问题记录保留且重试范围仅失败与暂缓',async()=>{
  const data=targetFixture()
  Object.assign(data.rows[0],{review_blocked:true,review_reasons:['本地规则命中']})
  Object.assign(data.rows[30],{review_blocked:true,review_reasons:['本地规则命中']})
  for(const [index,status] of [[31,'failed'],[32,'blocked'],[33,'deferred']] as const)
    Object.assign(data.rows[index],{ai_issues:{translate:{status,message:'保留问题记录'}}})
  data.available_assessments[34].eligible=false
  const {page}=await render(data,undefined,{seed:page=>{page.limit=60}})
  assert.equal(page.suggestions.length,29)
  assert.ok(!page.suggestions.some((row:any)=>row.id===data.rows[0].id))
  assert.deepEqual(page.retryIds('translate'),[data.rows[31].id,data.rows[33].id],'失败和暂缓依旧有独立重试入口')
  assert.equal(page.rows[32].ai_issues.translate.status,'blocked')
})

test('准备请求传当前显示语言与人工原文，语言和候选补位由后端共享逻辑处理',async t=>{
  const data=targetFixture(0,4)
  data.overlay.items=[]
  data.rows[0].text='x'.repeat(601)
  data.rows[1].text=''
  data.rows[2].text='x'.repeat(601)
  const drafts=[{id:data.rows[0].id,source_hash:data.rows[0].source_hash,source_text:'人工缩短的原文',display_text:''}]
  const {page}=await render(data,drafts,{seed:page=>{page.limit=4;page.settings.language_mode='bilingual'}})
  await page.load()
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options])
    return {ok:true,json:async()=>String(url).endsWith('/prepare')?preparedState(data,JSON.parse(JSON.stringify(page.items)),4):responseFor(url)} as any
  })
  for(const mode of ['bilingual','translated','original']){
    page.settings.language_mode=mode
    await page.prepareTarget()
  }
  const writes=calls.filter(call=>call[1]?.method==='POST')
  assert.equal(writes.length,3)
  assert.ok(writes.every(call=>call[0].endsWith('/prepare')))
  assert.deepEqual(writes.map(call=>JSON.parse(call[1].body).settings.language_mode),['bilingual','translated','original'])
  assert.ok(writes.every(call=>JSON.parse(call[1].body).items[0].source_text==='人工缩短的原文'))
  assert.equal(page.rows[0].text.length,601,'不为准备截断平台原文')
})

test('手动上屏目标与100条保存上限一致，空值和小数不会扩大请求范围',async()=>{
  const data=targetFixture(120),{page,html}=await render(data,undefined,{seed:page=>{page.limit=100}})
  assert.match(html,/aria-valuemax="100"/)
  assert.equal(page.candidateTarget,100)
  assert.equal(page.suggestions.length,100)
  for(const [input,wanted] of [[500,100],[60.9,60],[-5,0],[null,0]] as const){
    page.limit=input
    assert.equal(page.candidateTarget,wanted)
    assert.equal(page.suggestions.length,wanted)
  }
})

const chronologyFixture=()=>{
  const data=targetFixture(6,6)
  const timestamps=[300,100,200,200,null,0]
  data.rows=data.rows.map((row,index)=>({...row,timestamp:timestamps[index]}))
  data.overlay.items=data.rows.map(row=>draftRow(row,'automatic'))
  return data
}
const itemIds=(rows:any[])=>rows.map(row=>row.id)

test('默认按评论发布时间浏览，切换评分浏览不改变上屏顺序或保存草稿',async()=>{
  const data=chronologyFixture(),{page}=await render(data)
  const chronological=[data.rows[1],data.rows[2],data.rows[3],data.rows[0],data.rows[4],data.rows[5]]
  assert.equal(page.orderMode,'chronological')
  assert.equal(page.sortByScore,false)
  assert.deepEqual(itemIds(page.visibleRows),itemIds(chronological),'有时间的评论早到晚，同刻及缺时间按采集顺序')
  const before=JSON.stringify(page.items),snapshot=page.snapshot,baseline=page.baseline
  page.sortByScore=true
  assert.deepEqual(itemIds(page.visibleRows),itemIds(data.rows),'评分只用于候选阅读排序')
  assert.equal(JSON.stringify(page.items),before)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.baseline,baseline)
  assert.equal(page.dirty,false)
  page.sortByScore=false
  assert.deepEqual(itemIds(page.visibleRows),itemIds(chronological))
  assert.equal(JSON.stringify(page.items),before)
})

test('旧确认方案保持原有预览顺序，仅点击按发布时间排列或再次准备才改变草稿',async()=>{
  const data=chronologyFixture(),saved=JSON.stringify(data.overlay.items),{page,html}=await render(data)
  assert.equal(page.orderMode,'chronological','缺少排序策略的旧方案下次准备采用时间顺序')
  assert.deepEqual(itemIds(page.items),itemIds(data.overlay.items),'不能仅因打开页面就改变已有方案的预览顺序')
  assert.equal(JSON.stringify(page.state.overlay.items),saved,'不能重新解释后端已确认数组')
  assert.equal(page.isTimeOrdered,false)
  assert.equal(page.dirty,false)
  assert.match(html,/按发布时间排列/)
  page.sortByTime()
  assert.equal(page.isTimeOrdered,true)
  assert.equal(page.dirty,true)
  assert.equal(JSON.stringify(page.state.overlay.items),saved,'重排只修改待确认草稿')
})

test('读取已确认手动顺序保留排序策略，与手动保留某条的含义独立',async()=>{
  const data=chronologyFixture()
  Object.assign(data.overlay,{order_mode:'manual'})
  const {page}=await render(data)
  assert.equal(page.orderMode,'manual')
  assert.deepEqual(itemIds(page.items),itemIds(data.overlay.items))
  assert.ok(page.items.every((item:any)=>item.selection_origin==='automatic'),'手动排列不能凭空改为人工保留')
  assert.equal(JSON.parse(page.snapshot).order_mode,'manual')
  assert.equal(page.dirty,false)
})

test('按时间重排使用来源时间，同刻和未知时间稳定排序且保留所有人工文字和选择',async()=>{
  const data=chronologyFixture(),{page}=await render(data)
  const picked=[4,3,0,2,1,5].map((index)=>({...draftRow(data.rows[index],index===1?'manual':'automatic'),
    timestamp:index===4?1:999,display_text:`人工上屏文字 ${index}`}))
  page.items=picked
  page.orderMode='manual'
  const existing=new Map(page.items.map((item:any)=>[item.id,{origin:item.selection_origin,text:item.display_text}]))
  page.sortByTime()
  assert.equal(page.orderMode,'chronological')
  assert.deepEqual(itemIds(page.items),[1,2,3,0,4,5].map(index=>data.rows[index].id))
  for(const item of page.items){
    assert.equal(item.selection_origin,existing.get(item.id).origin)
    assert.equal(item.display_text,existing.get(item.id).text)
  }
  assert.equal(JSON.parse(page.snapshot).order_mode,'chronological')
  assert.equal(page.dirty,true)
})

test('时间模式新手选评论即时归队，手动顺序模式只追加且保持手动选择',async()=>{
  const data=chronologyFixture()
  data.overlay.items=[draftRow(data.rows[2],'automatic'),draftRow(data.rows[0],'automatic')]
  const chronological=await render(data)
  chronological.page.toggle(data.rows[1],true)
  assert.deepEqual(itemIds(chronological.page.items),[1,2,0].map(index=>data.rows[index].id))
  assert.equal(chronological.page.items[0].selection_origin,'manual')
  assert.equal(chronological.page.orderMode,'chronological')
  const manual=await render(data,undefined,{seed:page=>{page.orderMode='manual'}})
  manual.page.toggle(data.rows[1],true)
  assert.deepEqual(itemIds(manual.page.items),[2,0,1].map(index=>data.rows[index].id))
  assert.equal(manual.page.items[2].selection_origin,'manual')
  assert.equal(manual.page.orderMode,'manual')
})

test('编辑文字只保留人工选择，移动上下位置才切换手动上屏顺序',async()=>{
  const data=chronologyFixture(),{page}=await render(data)
  page.editText(page.items[0],'display_text','只修改文字，不改变时间顺序规则')
  assert.equal(page.items[0].selection_origin,'manual')
  assert.equal(page.orderMode,'chronological')
  const before=itemIds(page.items)
  page.move(1,1)
  assert.equal(page.orderMode,'manual')
  assert.deepEqual(itemIds(page.items),[before[0],before[2],before[1],...before.slice(3)])
  assert.equal(page.items[1].selection_origin,'automatic')
  assert.equal(page.items[2].selection_origin,'automatic')
  assert.equal(JSON.parse(page.snapshot).order_mode,'manual')
  page.editText(page.items[0],'display_text','手动排序之后继续修改文字')
  assert.equal(page.orderMode,'manual')
})

test('重新准备传递手动排序意图并采用服务端草稿顺序，不因评分浏览重排',async t=>{
  const data=chronologyFixture(),{page}=await render(data)
  page.move(0,1)
  page.sortByScore=true
  const draft=JSON.parse(JSON.stringify(page.items)),expected=itemIds(draft),calls:any[]=[]
  const prepared=preparedState(data,draft,6,[],'manual')
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {
    ok:true,json:async()=>String(url).endsWith('/prepare')?prepared:responseFor(url)} as any})
  await page.prepareTarget()
  const request=calls.find(call=>call[1]?.method==='POST'&&call[0].endsWith('/prepare'))
  assert.ok(request)
  const body=JSON.parse(request[1].body)
  assert.equal(body.order_mode,'manual')
  assert.deepEqual(itemIds(body.items),expected)
  assert.equal(page.orderMode,'manual')
  assert.deepEqual(itemIds(page.items),expected)
  assert.ok(calls.every(call=>call[1]?.method!=='PUT'),'重新准备仍不确认保存')
})

test('确认保存的顺序和预览完全一致，手动顺序读取后保留且不被评分覆盖',async t=>{
  const data=chronologyFixture(),{page}=await render(data)
  page.move(0,1)
  const expected=itemIds(page.plannedItems),positions=page.plannedItems.map((item:any)=>page.plannedPosition(item))
  page.sortByScore=true
  let confirmation:any,saved:any
  t.mock.method(page.dialog,'info',(value:any)=>{confirmation=value})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT'){
      saved=JSON.parse(options.body as string)
      activeFixture={...data,overlay:{items:saved.items,settings:saved.settings,excluded_ids:saved.excluded_ids,
        order_mode:saved.order_mode},overlay_revision:'chronology-saved'} as any
    }
    return {ok:true,json:async()=>responseFor(url)} as any
  })
  page.confirmPlan()
  assert.ok(confirmation)
  await confirmation.onPositiveClick()
  assert.equal(saved.order_mode,'manual')
  assert.deepEqual(itemIds(saved.items),expected)
  assert.deepEqual(itemIds(page.items),expected)
  assert.deepEqual(page.plannedItems.map((item:any)=>page.plannedPosition(item)),positions)
  assert.equal(page.orderMode,'manual')
  assert.equal(page.dirty,false)
})

test('预览已选有效评论定位完整方案，再次点击同条也发出新的定位请求且不修改草稿',async t=>{
  const data=targetFixture(100,100)
  Object.assign(data.overlay,{order_mode:'manual'})
  const {page}=await render(data,undefined,{seed:page=>{page.planExpanded=[]}})
  const target=data.rows[25],snapshot=page.snapshot,baseline=page.baseline,calls:any[]=[]
  const initialItems=JSON.stringify(page.items),initialSettings=JSON.stringify(page.settings)
  const initialPositions=page.plannedItems.map((item:any)=>page.plannedPosition(item))
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  const previousRequest=page.focusRequest
  await page.previewComment(target)
  assert.ok(page.planExpanded.includes('plan'),'定位预览时自动展开包含预览的折叠区域')
  assert.equal(page.previewFocusId,target.id)
  assert.ok(page.focusRequest>previousRequest)
  assert.equal(page.temporaryPreviewId,'')
  assert.deepEqual(itemIds(page.previewItems),itemIds(page.plannedItems),'已选评论应定位完整上屏方案，保留真实时间上下文')
  const firstRequest=page.focusRequest
  await page.previewComment(target)
  assert.ok(page.focusRequest>firstRequest,'同条再次点击也应允许重新定位')
  assert.equal(page.orderMode,'manual')
  assert.equal(JSON.stringify(page.items),initialItems,'视频式定位不能重建或修改上屏条目')
  assert.equal(JSON.stringify(page.settings),initialSettings)
  assert.deepEqual(page.plannedItems.map((item:any)=>page.plannedPosition(item)),initialPositions,'定位保持各条真实出现时间')
  assert.equal(page.items.length,30)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.baseline,baseline)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0,'只移动预览不调用模型、渲染保存或发布 API')
})

test('未选或被排除的评论在完整方案末尾临时试放，不自动勾选、不清除排除记录或改变保存顺序',async t=>{
  const data=targetFixture(100,100),target=data.rows[75]
  Object.assign(data.overlay,{excluded_ids:[{id:target.id,source_hash:target.source_hash}],order_mode:'manual'})
  const {page}=await render(data),snapshot=page.snapshot,selection=itemIds(page.items),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.previewComment(target)
  assert.equal(page.temporaryPreviewId,target.id)
  assert.equal(page.previewFocusId,target.id)
  assert.deepEqual(itemIds(page.previewItems),[...selection,target.id],'手动顺序试放只在原方案末尾插入候选，保留整条预览时间轴')
  assert.equal(page.previewItems.length,selection.length+1)
  assert.ok(selection.every(id=>page.previewItems.some((item:any)=>item.id===id)),'原有上屏评论不得因试放消失')
  assert.deepEqual(itemIds(page.items),selection)
  assert.equal(page.selected(target.id),false)
  assert.equal(page.isExcluded(target),true)
  assert.equal(page.orderMode,'manual')
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0)
  const temporaryRequest=page.focusRequest
  page.returnToPlanPreview()
  assert.equal(page.temporaryPreviewId,'')
  assert.deepEqual(itemIds(page.previewItems),selection)
  assert.equal(page.previewFocusId,'','结束试放不应请求跳到第一条评论')
  assert.equal(page.focusRequest,temporaryRequest,'保留播放器当前位置，不制造额外定位请求')
  assert.equal(page.snapshot,snapshot)
  await page.previewComment(target)
  await page.previewComment(data.rows[0])
  assert.equal(page.temporaryPreviewId,'','回到已选评论时恢复完整方案预览')
  assert.deepEqual(itemIds(page.previewItems),selection)
  assert.equal(page.snapshot,snapshot)
})

test('按发布时间试放候选插入对应时间顺序，返回后完整确认方案保持不变',async t=>{
  const data=chronologyFixture(),target=data.rows[2]
  data.overlay.items=[draftRow(data.rows[1],'manual'),draftRow(data.rows[0],'automatic')]
  Object.assign(data.overlay,{order_mode:'chronological'})
  const {page}=await render(data),snapshot=page.snapshot,selection=itemIds(page.items),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.previewComment(target)
  assert.deepEqual(itemIds(page.previewItems),[data.rows[1].id,target.id,data.rows[0].id])
  assert.equal(page.previewFocusId,target.id)
  assert.equal(page.temporaryPreviewId,target.id)
  assert.equal(page.orderMode,'chronological')
  assert.deepEqual(itemIds(page.items),selection)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
  page.returnToPlanPreview()
  assert.deepEqual(itemIds(page.previewItems),selection)
  assert.equal(page.snapshot,snapshot)
  assert.equal(calls.length,0)
})

test('没有上屏方案时仍可试放单条，退出后恢复空时间轴且不创建保存草稿',async t=>{
  const data=targetFixture(2,2),target=data.rows[1]
  data.overlay.items=[]
  const {page}=await render(data),snapshot=page.snapshot,calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.previewComment(target)
  assert.deepEqual(itemIds(page.previewItems),[target.id])
  assert.equal(page.previewFocusId,target.id)
  assert.deepEqual(itemIds(page.items),[])
  assert.equal(page.dirty,false)
  page.returnToPlanPreview()
  assert.deepEqual(itemIds(page.previewItems),[])
  assert.equal(page.previewFocusId,'')
  assert.equal(page.snapshot,snapshot)
  assert.equal(calls.length,0)
})

test('切换试放条目不累积临时选择，返回已选时保留未保存文字、设置和手动顺序',async t=>{
  const data=targetFixture(100,100)
  Object.assign(data.overlay,{order_mode:'manual'})
  const {page}=await render(data),baseIds=itemIds(page.items),calls:any[]=[]
  page.editText(page.items[0],'display_text','尚未确认保存的人工文字')
  page.settings.start=7
  const snapshot=page.snapshot,baseline=page.baseline
  assert.equal(page.dirty,true)
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.previewComment(data.rows[75])
  assert.deepEqual(itemIds(page.previewItems),[...baseIds,data.rows[75].id])
  await page.previewComment(data.rows[85])
  assert.deepEqual(itemIds(page.previewItems),[...baseIds,data.rows[85].id],'更换试放只替换临时候选，不残留上一条')
  assert.equal(page.previewFocusId,data.rows[85].id)
  await page.previewComment(data.rows[0])
  assert.equal(page.temporaryPreviewId,'')
  assert.equal(page.previewFocusId,data.rows[0].id)
  assert.deepEqual(itemIds(page.previewItems),baseIds)
  assert.equal(page.previewItems[0].display_text,'尚未确认保存的人工文字')
  assert.equal(page.previewSettings.start,7)
  assert.equal(page.orderMode,'manual')
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.baseline,baseline)
  assert.equal(page.dirty,true,'定位不能将待保存的修改误标为已保存')
  assert.equal(calls.length,0)
})

test('完整方案已有100条时不再插入试放候选，保留全部评论且已选条目仍可定位',async t=>{
  const data=targetFixture(101,101),target=data.rows[100]
  data.overlay.items=data.rows.slice(0,100).map(row=>draftRow(row,'automatic'))
  Object.assign(data.overlay,{order_mode:'manual'})
  const {page}=await render(data),snapshot=page.snapshot,baseline=page.baseline,calls:any[]=[],notes:any[]=[]
  t.mock.method(page.message,'info',(text:any)=>{notes.push(text)})
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.previewComment(data.rows[25])
  const previousFocus=page.previewFocusId,previousRequest=page.focusRequest
  await page.previewComment(target)
  assert.equal(notes.length,1,'达到上限时明确说明无法插入额外试放条目')
  assert.match(notes[0],/100/)
  assert.equal(page.previewFocusId,previousFocus)
  assert.equal(page.focusRequest,previousRequest,'拒绝额外试放不得制造无效定位或渲染请求')
  assert.equal(page.temporaryPreviewId,'')
  assert.deepEqual(itemIds(page.previewItems),itemIds(data.overlay.items),'不能截断已选评论来腾出试放名额')
  assert.equal(page.previewItems.length,100)
  assert.equal(page.selected(target.id),false)
  await page.previewComment(data.rows[99])
  assert.equal(page.previewFocusId,data.rows[99].id,'满额只限制插入候选，已选评论仍能自由定位')
  assert.ok(page.focusRequest>previousRequest)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.baseline,baseline)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0)
})

test('文字待调整的手选评论试放保留其他所有上屏条目，不因此丢弃人工草稿或启动翻译',async t=>{
  const data=targetFixture(100,100)
  data.overlay.items[1].display_text=''
  const {page}=await render(data),target=data.rows[1],snapshot=page.snapshot,calls:any[]=[]
  const plannedIds=itemIds(page.plannedItems)
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  assert.ok(!page.plannedItems.some((item:any)=>item.id===target.id))
  await page.previewComment(target)
  assert.equal(page.temporaryPreviewId,target.id)
  assert.equal(page.previewItems.length,plannedIds.length+1)
  assert.ok(plannedIds.every(id=>page.previewItems.some((item:any)=>item.id===id)))
  assert.equal(page.previewItems.filter((item:any)=>item.id===target.id).length,1)
  assert.equal(page.selected(target.id),true)
  assert.equal(page.items.find((item:any)=>item.id===target.id).display_text,'','临时显示不能偷偷修补或覆盖人工编辑')
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0)
  page.returnToPlanPreview()
  assert.deepEqual(itemIds(page.previewItems),plannedIds)
  assert.equal(page.snapshot,snapshot)
})

test('未翻译评论临时改用原文预览，中文或双语主方案及全部保存状态保持不变',async t=>{
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  for(const mode of ['translated','bilingual']){
    const data=targetFixture(1,4),target=data.rows[3]
    data.overlay.items=[draftRow(data.rows[0],'manual')]
    Object.assign(data.overlay.settings,{language_mode:mode})
    const {page}=await render(data),snapshot=page.snapshot,settings=JSON.stringify(page.settings),items=JSON.stringify(page.items)
    await page.previewComment(target)
    assert.equal(page.temporaryPreviewId,target.id)
    assert.equal(page.previewSourceOnly,true)
    assert.equal(page.previewSettings.language_mode,'original')
    assert.deepEqual(itemIds(page.previewItems),[data.rows[0].id,target.id],'缺译文的试放也保留已有方案，不能只显示候选')
    assert.equal(page.previewInvalid,false,'原文短且可用时不被主方案缺中文的校验挡住')
    assert.equal(page.settings.language_mode,mode)
    assert.equal(JSON.stringify(page.settings),settings)
    assert.equal(JSON.stringify(page.items),items)
    assert.equal(page.selected(target.id),false)
    assert.equal(page.snapshot,snapshot)
    assert.equal(page.dirty,false)
    const visible=await render(data,undefined,{seed:page=>{
      page.temporaryPreviewId=target.id;page.previewFocusId=target.id;page.focusRequest=1
    }})
    assert.match(visible.html,/尚无译文，临时显示原文/)
    assert.equal(visible.commentPreview.$props.settings.language_mode,'original','实际子组件接收临时显示语言')
    assert.deepEqual(itemIds(visible.commentPreview.$props.items),[data.rows[0].id,target.id])
    assert.equal(visible.commentPreview.$props.invalid,false)
    assert.equal(visible.page.settings.language_mode,mode)
    page.rows[3].text='x'.repeat(601)
    assert.equal(page.previewSourceOnly,true)
    assert.equal(page.previewInvalid,true,'原文预览仍遵守原文长度限制，不能借临时模式绕过校验')
    assert.equal(page.snapshot,snapshot)
  }
  assert.ok(calls.every(call=>!['POST','PUT','PATCH','DELETE'].includes(call[1]?.method)),
    '组件读取配置可以复用，临时显示原文不得请求模型、预生成或保存上屏方案')
  assert.ok(calls.every(call=>!/\/comments\/(?:prepare|translate|filter|overlay)(?:$|\?)/.test(call[0])))
})

test('返回仍在当前候选中的评论保留过滤，来源已消失时保留当前页面与草稿',async t=>{
  const data=targetFixture(100,100),target=data.rows[3]
  const {page}=await render(data,undefined,{seed:page=>{page.onlySuggested=true}})
  const snapshot=page.snapshot,notes:any[]=[]
  t.mock.method(page.message,'info',(text:any)=>{notes.push(text)})
  await page.locateComment(target.id)
  assert.equal(page.onlySuggested,true,'目标本来可见时不打断用户过滤习惯')
  assert.equal(page.highlightedId,target.id)
  const previousPage=page.page,expanded=JSON.stringify(page.expandedRows)
  await page.locateComment('removed-from-current-source')
  assert.equal(page.page,previousPage)
  assert.equal(page.highlightedId,target.id)
  assert.equal(JSON.stringify(page.expandedRows),expanded)
  assert.equal(notes.length,1)
  assert.match(notes[0],/不在当前来源/)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
})

test('从预览返回已被候选过滤隐藏的低分手选评论时取消过滤、跳页、高亮并展开原文',async t=>{
  const data=targetFixture(100,100),target=data.rows[75]
  data.available_assessments[75].eligible=false
  data.overlay.items=[draftRow(data.rows[0],'automatic'),draftRow(target,'manual')]
  const {page}=await render(data,undefined,{seed:page=>{page.onlySuggested=true}})
  assert.ok(!page.visibleRows.some((row:any)=>row.id===target.id))
  const snapshot=page.snapshot,calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.locateComment(target.id)
  assert.equal(page.onlySuggested,false)
  assert.equal(page.page,4)
  assert.ok(page.pageRows.some((row:any)=>row.id===target.id))
  assert.equal(page.highlightedId,target.id)
  assert.ok(page.expandedRows.includes(target.id))
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0)
})

test('从预览返回未选评论依据当前评分浏览顺序定位，不改变上屏排列策略或选择',async t=>{
  const data=targetFixture(100,100),target=data.rows[85]
  data.available_assessments[85].score=1000
  Object.assign(data.overlay,{order_mode:'manual'})
  const {page}=await render(data,undefined,{seed:page=>{page.sortByScore=true;page.page=5}})
  const snapshot=page.snapshot,selection=itemIds(page.items),calls:any[]=[]
  assert.equal(page.visibleRows[0].id,target.id)
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>responseFor(url)} as any})
  await page.locateComment(target.id)
  assert.equal(page.page,1,'评分浏览应定位到当前页面序列，而非使用原始采集下标')
  assert.ok(page.pageRows.some((row:any)=>row.id===target.id))
  assert.equal(page.highlightedId,target.id)
  assert.equal(page.sortByScore,true)
  assert.equal(page.orderMode,'manual')
  assert.equal(page.selected(target.id),false)
  assert.deepEqual(itemIds(page.items),selection)
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
  assert.equal(calls.length,0)
})

test('真实预览组件接收聚焦请求并把返回列表事件绑定到父组件跨页定位',async()=>{
  const data=targetFixture(100,100),target=data.rows[75]
  data.overlay.items=[draftRow(data.rows[0],'automatic'),draftRow(target,'manual')]
  const {page,html,commentPreview}=await render(data,undefined,{seed:page=>{
    page.previewFocusId=target.id;page.focusRequest=3
  }})
  assert.ok(commentPreview,'必须验证真实 CommentPreview 的父子绑定')
  assert.equal(commentPreview.$props.focusId,target.id)
  assert.equal(commentPreview.$props.focusRequest,3)
  assert.deepEqual(itemIds(commentPreview.$props.items),[data.rows[0].id,target.id],'聚焦ID不能把实际子组件数据改成单条')
  assert.match(html,/定位预览/)
  const locate=commentPreview.$.vnode.props?.onLocateComment
  assert.equal(typeof locate,'function')
  const snapshot=page.snapshot
  await locate(target.id)
  assert.equal(page.page,4)
  assert.equal(page.highlightedId,target.id)
  assert.ok(page.pageRows.some((row:any)=>row.id===target.id))
  assert.equal(page.snapshot,snapshot)
  assert.equal(page.dirty,false)
})
