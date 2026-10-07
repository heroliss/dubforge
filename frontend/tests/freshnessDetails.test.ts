import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h,nextTick} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {normalizeFreshnessReport,needsFreshnessDetails,freshnessStatus,freshnessPreview} from '../src/utils/freshness.ts'

let server:ViteDevServer,Details:any,Report:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Details=(await server.ssrLoadModule('/src/components/FreshnessDetails.vue')).default
  Report=(await server.ssrLoadModule('/src/components/FreshnessReport.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('摘要只能在用户悬停、聚焦或点击后请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
function fixture(overrides:any={}) {
  return {project_uid:'p',steps:[{key:'split',label:'语义整理',status:'done',fresh:false,history_available:true,
    summary:'语义整理的输入或设置已变化',changes:[
      {category:'parameter',key:'llm.temperature',label:'采样温度',before:'0',after:'0.5',before_known:true,origin_before:{kind:'project',label:'项目设置'},origin_after:{kind:'global',label:'全局默认'},message:'当前生效值与上次生成不同'},
      {category:'model',key:'llm_groups.split.model',label:'语义整理模型',before:'旧模型',after:'新模型',before_known:true,message:'本步骤模型已变化'},
      {category:'prompt',key:'split.prompt_version',label:'语义整理提示词',before:'1',after:'2',before_known:true,message:'软件中的提示词版本已升级'},
      {category:'upstream',key:'transcribe.json',label:'识别文字',before:'旧输入',after:'当前输入',before_known:true,message:'语音识别结果已变化'},
      {category:'missing',key:'split.json',label:'语义整理文件',before:'已登记',after:'文件缺失',before_known:true,message:'产物文件可能被移动或删除'},
    ],affected_steps:[{key:'translate',label:'翻译'},{key:'tts',label:'字幕与配音'}],warnings:[],...overrides}],warnings:[]}
}
async function renderReport(value:any) {
  const report=normalizeFreshnessReport(value);assert.ok(report)
  const app=createSSRApp({render:()=>h(Report,{report})});setup(app);return renderToString(app)
}
async function renderDetails(seed:(page:any)=>void=()=>{},props:any={uid:'p',stepKey:'split',label:'语义整理'}) {
  let page:any,instanceProps:any
  const app=createSSRApp({render:()=>h(Details,props)});setup(app)
  app.mixin({created(){if(this.$options.__name==='FreshnessDetails'){page=this.$.setupState;instanceProps=this.$.props;seed(page)}}})
  const context:any={};const html=await renderToString(app,context)
  return {page,props:instanceProps,html:html+Object.values(context.teleports || {}).join('')}
}

test('原因明细按旧值到当前值显示参数、模型、提示词、文件和上游变化及来源',async()=>{
  const html=await renderReport(fixture())
  assert.match(html,/上次生成时[\s\S]*当前[\s\S]*具体原因/)
  assert.match(html,/采样温度[\s\S]*>0<\/span>[\s\S]*项目设置[\s\S]*>0.5<\/span>[\s\S]*全局默认/)
  assert.match(html,/旧模型[\s\S]*新模型/);assert.match(html,/软件中的提示词版本已升级/)
  assert.match(html,/语音识别结果已变化/);assert.match(html,/产物文件可能被移动或删除/)
  assert.match(html,/依赖本步的后续步骤[\s\S]*翻译[\s\S]*字幕与配音/)
  assert.match(html,/不表示这些步骤都必须重跑/)
  assert.doesNotMatch(html,/全部重做|全项目重跑/)
})

test('旧快照缺失与未知旧值明确说明，不以当前配置来源冒充历史来源',async()=>{
  const html=await renderReport(fixture({history_available:false,changes:[{category:'unknown',key:'legacy',label:'旧输入记录',
    before:'没有证据的旧值',after:'当前摘要',before_known:false,origin_before:{kind:'global',label:'冒充的旧全局来源'},origin_after:{kind:'project',label:'当前项目设置'},message:'上次只记录输入指纹'}]}))
  assert.match(html,/缺少可对应这份结果的历史输入快照，无法还原具体旧值/)
  assert.match(html,/旧值未记录/);assert.match(html,/当前项目设置/)
  assert.doesNotMatch(html,/没有证据的旧值|冒充的旧全局来源/)
})

test('敏感字段防御隐藏且所有差异与说明都按文本转义',async()=>{
  const raw=fixture({summary:'核对<script>摘要</script>',changes:[
    {category:'model',key:'llm.api_key',label:'服务凭据',before:'fixture-private-before',after:'fixture-private-after',before_known:true,message:'fixture-private-reason'},
    {category:'parameter',key:'公开设置',label:'<img src=x onerror=bad()>',before:'sk-fixturecredential',after:'https://user:private@host/path',before_known:true,message:'<script>原因</script>'},
  ]})
  const html=await renderReport(raw)
  assert.match(html,/敏感值已隐藏/);assert.match(html,/&lt;script&gt;摘要&lt;\/script&gt;/)
  assert.match(html,/&lt;img src=x onerror=bad\(\)&gt;/);assert.match(html,/&lt;script&gt;原因&lt;\/script&gt;/)
  assert.doesNotMatch(html,/fixture-private|sk-fixturecredential|user:private|<script>原因|<img src=x/)
})

test('配音缺有效音频显示补齐范围，失败状态与当前有效结果可以并存',async()=>{
  const html=await renderReport(fixture({key:'tts',label:'字幕与配音',summary:'仍有2句缺少有效选用音频：第3、8句。可补齐这些句子。',changes:[],affected_steps:[{key:'assemble',label:'合成成片'}]}))
  assert.match(html,/仍有2句缺少有效选用音频：第3、8句/);assert.match(html,/配音完整性待核对/)
  assert.doesNotMatch(html,/全部重新配音|更换音色使旧音频失效/)
  const failed=await renderReport(fixture({key:'tts',label:'字幕与配音',status:'failed',fresh:true,summary:'当前选用的音频可继续使用；最近操作失败',changes:[]}))
  assert.match(failed,/✗ 失败/);assert.match(failed,/当前结果已是最新/)
  assert.equal(needsFreshnessDetails({key:'tts',status:'done',fresh:true}),false)
  assert.equal(needsFreshnessDetails({key:'tts',status:'running',fresh:false}),false)
  assert.equal(needsFreshnessDetails({key:'tts',status:'failed',fresh:false}),false)
  assert.equal(needsFreshnessDetails({key:'tts',status:'stale',fresh:true}),false)
  assert.equal(freshnessStatus({key:'tts',status:'done',fresh:false,freshness_label:'仍有2句待配音'}),'仍有2句待配音')
  assert.equal(freshnessStatus({key:'tts',status:'done',fresh:false,freshness_label:'暂无法核对配音完整性'}),'暂无法核对配音完整性')
  assert.equal(freshnessStatus({key:'tts',status:'stale',fresh:false,freshness_label:'仍有2句待配音'}),'仍有2句待配音')
  for(const status of ['running','queued','failed'])assert.equal(freshnessStatus({key:'tts',status,fresh:false,freshness_label:'仍有2句待配音'}),'')
})

test('状态标签渲染不读取，点击才GET原因，步骤与项目查询范围准确',async t=>{
  const {page,html}=await renderDetails()
  assert.match(html,/语义整理 · 查看更新原因/);assert.match(html,/aria-haspopup="dialog"/)
  assert.match(html,/type="button"/);assert.match(html,/>需更新<\/span>/)
  assert.doesNotMatch(html,/为什么需更新/);assert.equal(page.show,false)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url:String(url),method:options?.method,cache:options?.cache});return {ok:true,json:async()=>fixture()}})
  await page.open();assert.equal(page.show,true);assert.equal(page.busy,false);assert.equal(page.report.steps[0].key,'split')
  await page.open();assert.equal(calls.length,1,'重复点击不重复请求')
  page.close()
  const {page:project}=await renderDetails(()=>{},{uid:'p',label:'项目',statusText:'需更新 · 2步'})
  await project.open()
  assert.deepEqual(calls,[{url:'/api/projects/p/freshness?step=split',method:'GET',cache:'no-store'},{url:'/api/projects/p/freshness',method:'GET',cache:'no-store'}])
})

test('悬停或聚焦读取未完成时点击立即打开详情，共用同一请求和有效摘要',async t=>{
  const {page}=await renderDetails()
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url:String(url),signal:options?.signal,resolve})))
  const hovering=page.previewShown(true)
  assert.equal(page.show,false);assert.equal(page.hovered,true);assert.equal(page.busy,true)
  const clicking=page.open()
  assert.equal(page.show,true);assert.equal(requests.length,1)
  await page.previewShown(false)
  assert.equal(requests[0].signal.aborted,false,'详情已开，提示层关闭不能取消共享请求')
  requests[0].resolve({ok:true,json:async()=>fixture()})
  await Promise.all([hovering,clicking])
  assert.equal(page.report.steps[0].key,'split');assert.equal(page.busy,false)
  page.close()
  await page.previewShown(true)
  await page.open()
  assert.equal(requests.length,1,'短时再次悬停和点击共用当前范围缓存')
  const refreshing=page.load()
  assert.equal(requests.length,2,'显式重新检查始终读最新报告')
  requests[1].resolve({ok:true,json:async()=>fixture()});await refreshing
})

test('离开标签取消摘要请求，再进入后的迟到响应不能覆盖新摘要',async t=>{
  const {page}=await renderDetails()
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(_url,options)=>new Promise(resolve=>requests.push({signal:options?.signal,resolve})))
  const older=page.previewShown(true)
  await page.previewShown(false)
  assert.equal(requests[0].signal.aborted,true);assert.equal(page.show,false)
  const current=page.previewShown(true)
  requests[1].resolve({ok:true,json:async()=>fixture({summary:'最新摘要'})});await current
  requests[0].resolve({ok:true,json:async()=>fixture({summary:'迟到的旧摘要'})});await older
  assert.equal(page.report.steps[0].summary,'最新摘要');assert.equal(page.show,false)
})

test('同项目状态来源变化会重读摘要，旧请求不能覆盖已变化的输入',async t=>{
  const {page,props}=await renderDetails()
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(_url,options)=>new Promise(resolve=>requests.push({signal:options?.signal,resolve})))
  const older=page.previewShown(true)
  props.summary='当前设置已经变化'
  page.invalidateInput()
  assert.equal(requests[0].signal.aborted,true);assert.equal(requests.length,2)
  requests[1].resolve({ok:true,json:async()=>fixture({summary:'变化后的输入'})})
  await page.load()
  requests[0].resolve({ok:true,json:async()=>fixture({summary:'旧输入'})});await older
  assert.equal(page.report.steps[0].summary,'变化后的输入')
})

test('摘要显示已知参数前后值，未知旧值与敏感值不猜测，长列表限制为三项',()=>{
  const report=normalizeFreshnessReport(fixture());assert.ok(report)
  const preview=freshnessPreview(report)
  assert.equal(preview.items.length,3);assert.equal(preview.remaining,2)
  assert.equal(preview.items[0],'采样温度：0 → 0.5')
  assert.equal(preview.items[1],'语义整理模型：旧模型 → 新模型')
  const unknown=normalizeFreshnessReport(fixture({history_available:false,changes:[
    {category:'parameter',label:'分辨率',key:'download.resolution',before:'猜的1080',after:'720',before_known:false,message:'旧值未记录，只能核对当前输入'},
    {category:'model',label:'凭据',key:'llm.api_key',before:'private-old',after:'private-new',before_known:true,message:'private-reason'}]}))
  const summary=freshnessPreview(unknown).items.join('；')
  assert.match(summary,/旧值未记录/);assert.match(summary,/敏感设置值已隐藏/)
  assert.doesNotMatch(summary,/猜的1080|private-/)
})

test('关闭与项目切换取消请求，迟到结果不能复活旧弹窗或覆盖新上下文',async t=>{
  const {page,props}=await renderDetails()
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url:String(url),signal:options?.signal,resolve})))
  const pending=page.open();assert.equal(page.busy,true)
  await page.open();assert.equal(requests.length,1)
  page.close();assert.equal(requests[0].signal.aborted,true)
  requests[0].resolve({ok:true,json:async()=>fixture()});await pending
  assert.equal(page.show,false);assert.equal(page.report,null)
  const second=page.open();props.uid='other'
  // SSR 不执行挂载后的 watch；显式触发其上下文重置回调。
  page.resetContext();await nextTick()
  assert.equal(requests[1].signal.aborted,true);assert.equal(page.show,false)
  requests[1].resolve({ok:true,json:async()=>fixture()});await second
  assert.equal(page.report,null);assert.equal(page.busy,false)
})

test('读取失败或错误范围只显示错误，可手动重试而不会启动任何制作',async t=>{
  const {page}=await renderDetails()
  const calls:any[]=[];let value:any={error:true}
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url:String(url),method:options?.method});return {ok:!value.error,json:async()=>value.error?{message:'输入记录读取失败'}:value}})
  await page.open();assert.match(page.error,/输入记录读取失败/);assert.equal(page.report,null)
  value={};await page.load();assert.match(page.error,/报告不完整/)
  value={...fixture(),project_uid:'other'};await page.load();assert.match(page.error,/报告不完整/)
  value=fixture({key:'translate'});await page.load();assert.match(page.error,/未取得当前步骤/)
  value=fixture();await page.load();assert.equal(page.error,'');assert.equal(page.report.steps.length,1)
  assert.ok(calls.every(call=>call.method==='GET' && call.url.endsWith('/freshness?step=split')))
})

test('兼容可选字段缺失与零待更新报告，非法报告不假装已有完整明细',async()=>{
  assert.equal(normalizeFreshnessReport({}),null)
  assert.equal(normalizeFreshnessReport({steps:[null]}),null)
  const minimal=normalizeFreshnessReport({project_uid:'p',steps:[{key:'split'}]})
  assert.ok(minimal);assert.equal(minimal.steps[0].history_available,null);assert.deepEqual(minimal.steps[0].changes,[])
  assert.match(await renderReport({project_uid:'p',steps:[],warnings:[]}),/当前没有需更新或待补齐的旧结果/)
})

test('修订成片与待配音状态优先使用后端权威标签，不冒称常规产物最新',async()=>{
  const revision=await renderReport(fixture({key:'assemble',label:'合成成片',fresh:true,
    freshness_label:'审查修订有效（复用旧文字与音轨）',summary:'',freshness_reason:'按当前审查规则处理已选旧成片',changes:[]}))
  assert.match(revision,/审查修订有效（复用旧文字与音轨）/);assert.match(revision,/按当前审查规则处理已选旧成片/)
  assert.doesNotMatch(revision,/当前结果已是最新/)
  const tts=await renderReport(fixture({key:'tts',label:'字幕与配音',freshness_label:'仍有2句待配音',changes:[]}))
  assert.match(tts,/仍有2句待配音/);assert.doesNotMatch(tts,/配音完整性待核对/)
})

test('历史快照通用提示只显示一次，失败、范围不完整及评论证据警告完整保留',async()=>{
  const legacy='缺少可核对的上次生成输入快照，无法还原具体旧值；只显示当前能证实的原因。'
  const html=await renderReport(fixture({history_available:false,warnings:[legacy,'上次任务失败，请核对日志','历史快照范围不完整，请重新核对','旧评论缺少评分证据，请人工确认']}))
  assert.equal((html.match(/无法还原具体旧值/g)||[]).length,1)
  assert.doesNotMatch(html,/缺少可核对的上次生成输入快照/)
  assert.match(html,/上次任务失败，请核对日志/);assert.match(html,/历史快照范围不完整，请重新核对/);assert.match(html,/旧评论缺少评分证据，请人工确认/)
  assert.match(await renderReport(fixture({history_available:true,warnings:[legacy]})),/缺少可核对的上次生成输入快照/)
})

test('未提供数值的上游和缺失项显示可读状态，参数旧值未知仍不推断',async()=>{
  const html=await renderReport(fixture({changes:[{category:'upstream',key:'input',label:'上游结果',before:null,after:null,before_known:false,message:'文字已变化'},
    {category:'missing',key:'audio.pending',label:'选用音频',before:null,after:null,before_known:false,message:'第3句缺少有效选用音频'},
    {category:'parameter',key:'setting',label:'未记录参数',before:null,after:null,before_known:false,message:'没有历史值'}]}))
  assert.match(html,/需更新，请看具体原因/);assert.match(html,/缺少有效文件或音频，请看具体原因/)
  assert.equal((html.match(/旧值未记录/g)||[]).length,3);assert.match(html,/未提供/)
})
