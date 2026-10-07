import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, PublishView: any
before(async () => {
  server = await createServer({ root:fileURLToPath(new URL('..', import.meta.url)),
    configFile:false, plugins:[vue()], appType:'custom', server:{middlewareMode:true,hmr:false,watch:null,ws:false} })
  ;({default:PublishView} = await server.ssrLoadModule('/src/views/PublishView.vue'))
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })

const settings = { title:'河内美食',description:'发布简介',tags:['旅行'],tid:21,human_type2:1001,
  copyright:3,source:'https://example.com/video',declaration_id:1,visibility:'public',
  watermark:false,close_comments:false,close_danmaku:false,selected_comments:false,
  dynamic:'',publish_at:null,season_id:null,section_id:null }

function collectionRecord(){const row=record();row.draft.overrides={season_id:1,section_id:2};return row}


test('发布表单展示目标失效的原ID，不自动更改合集或小节', async () => {
  const {page,html,form} = await render(p => {
    p.apply(collectionRecord());p.metadataReady=true;p.metadata={seasons:[]}
  })
  assert.match(html,/合集 ID 1 已不可用/)
  assert.equal(form.collectionState.status,'missing')
  assert.equal(form.seasons[0].value,1);assert.equal(form.seasons[0].disabled,true)
  assert.equal(page.dirty,false)
  // SSR 没有父组件更新循环，分别渲染失败与改名后的真实表单。
  const failed = await render(p => {p.apply(collectionRecord());p.metadataReady=true;p.metadata={seasons:[]};p.metadataError='网络失败'})
  assert.equal(failed.form.collectionState.status,'unknown')
  assert.doesNotMatch(failed.html,/合集 ID 1 已不可用/)
  const renamed = await render(p => {p.apply(collectionRecord());p.metadataReady=true;p.metadata={seasons:[{id:1,title:'新名称',sections:[{id:2,title:'正片'}]}]}})
  assert.equal(renamed.form.collectionState.status,'valid');assert.equal(renamed.form.seasons[0].label,'新名称')
  assert.equal(renamed.page.dirty,false)
})
function record(status = 'draft') {
  return {id:'fixture',project_uid:'fixture-project',revision:3,status,receipt:{},progress:0,error:'',
    base_settings:{...settings},base_origins:{},
    draft:{account_id:'123',template_id:'',parts:[],overrides:{},cover_id:'',content:{}},
    snapshot:status === 'draft' ? {} : {settings:{...settings},origins:{},base_settings:{...settings},base_origins:{},parts:[{artifact_id:1,title:'配音版'}],account_id:'123',account_name:'测试账号'},
  }
}
async function render(seed: (page:any) => void = () => {}, path='/publish') {
  let page: any, assistant: any, assistantProps: any, form: any, collection: any
  const switches: any[] = []
  const dialogs: any[] = []
  const router = createRouter({history:createMemoryHistory(),routes:[
    {path:'/publish',component:PublishView},{path:'/projects/:uid/publish',component:PublishView},{path:'/projects/:uid/steps/assemble',component:{render:() => null}},
  ]})
  await router.push(path)
  const Probe = defineComponent({setup() {
    const dialog = useDialog()
    dialog.warning = dialog.info = ((options:any) => {dialogs.push(options);return {destroy(){}}}) as any
    return () => h(RouterView)
  }})
  const app = createSSRApp({render:() => h(NMessageProvider, null, {default:() => h(NDialogProvider,null,{default:() => h(Probe)})})})
  app.use(router);setup(app)
  app.mixin({created() {
    if (this.$options.__name === 'PublishCopyAssistant') { assistant = this.$.setupState; assistantProps = this.$.props }
    if (this.$options.__name === 'PublishSettingsForm') form = this.$.setupState
    if (this.$options.__name === 'PublishCollectionCreate') collection = this.$.setupState
    if (this.$options.name === 'Switch') switches.push(this)
    if (this.$options.__name === 'PublishView') {
      page = this.$.setupState
      page.catalog = {defaults:{...settings},accounts:[{id:'123',name:'测试账号',defaults:{}}],templates:[],worker_ready:true}
      page.source = {files:[]};seed(page)
    }
  }})
  return {html:await renderToString(app),page,dialogs,assistant,switches,assistantProps,form,collection}
}
function actionButtons(html:string) {
  return [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match=>match[0]).filter(button=>/class="[^"]*\bn-button\b/.test(button))
}
function buttonText(button:string) { return button.replace(/<[^>]*>/g,'').trim() }
test('实际发布页面渲染：未选任何成片，上传按钮禁用且没有隐式默认', async () => {
  const {html,page} = await render(p => p.apply(record()))
  assert.match(html, /尚未选择成片，不会自动勾选任何版本/)
  assert.deepEqual(page.draft.parts, [])
  const buttons = actionButtons(html)
  for (const text of ['仅上传文件', '上传并投稿']) {
    const button = buttons.find(b => buttonText(b)===text)
    assert.ok(button)
    assert.match(button, /disabled/)
  }
  assert.doesNotMatch(html, /确认投稿/)
  assert.match(html, /请确保素材可用于本次发布/)
  assert.doesNotMatch(html, /已核对视频、封面与配音素材的发布权利/)
  assert.equal('rights_confirmed' in page.draft, false)
})

test('候选成片与已选分P同时展示需修订原因，仍允许选片且不误称审核通过', async () => {
  const {html,page}=await render(p=>{
    const row=record();row.draft.parts=[{artifact_id:11,title:'测试分P'}];p.apply(row)
    p.source={files:[{id:11,name:'final_src_sub.mp4',exists:true,size:4096,created_at:'2026-10-05T10:00:00',review_ready:false,review_status:'旧成片',review_reason:'需要重新生成<script>原因</script>'}]}
  })
  assert.equal((html.match(/class="candidate-review"/g)||[]).length,2)
  assert.match(html,/需审查修订/)
  assert.equal((html.match(/需要重新生成&lt;script&gt;原因&lt;\/script&gt;/g)||[]).length,2)
  assert.doesNotMatch(html, /<script>原因|已通过审核|安全可发布/)
  assert.deepEqual(page.draft.parts,[{artifact_id:11,title:'测试分P'}])
  page.addPart({id:12,name:'final_dub_sub.mp4',exists:true,review_ready:false},true)
  assert.equal(page.draft.parts.length,2)
})

test('候选列表区分已处理、零命中和关闭审查，旧接口缺字段保持可用', async () => {
  const {html}=await render(p=>{
    p.apply(record())
    p.source={files:[
      {id:1,name:'final_dub_sub.mp4',exists:true,review_ready:true,review_status:'已处理'},
      {id:2,name:'final_src_sub.mp4',exists:true,review_ready:null,review_status:'未命中本地规则',review_reason:'文本规则未命中，仍需核对画面'},
      {id:3,name:'final_src.mp4',exists:true,review_ready:null,review_status:'审查关闭',review_reason:'本项目关闭了自动屏蔽'},
      {id:4,name:'final_dub.mp4',exists:true},
    ]}
  })
  assert.match(html,/已按当前规则处理/);assert.match(html,/未命中本地规则/);assert.match(html,/审查关闭/)
  assert.match(html,/文本规则未命中，仍需核对画面/)
  assert.equal((html.match(/class="candidate-review"/g)||[]).length,3)
  assert.doesNotMatch(html,/已通过审核|安全可发布/)
})

test('已有草稿仅在手动点击时补入来源日期，不请求网络、不影响其他设置', async () => {
  const {page,form,html} = await render(p => {
    p.apply(record()); p.source = {files:[],source_published_date:'2026-09-01'}
  })
  assert.match(html, /补入原视频日期/)
  assert.equal(page.dirty, false)
  form.insertPublishedDate()
  assert.deepEqual(page.draft.overrides, {description:'发布简介\n\n原视频发布时间：2026-09-01'})
  assert.equal(page.dirty, true)
})

test('日期已有、未知或稿件锁定时不出现补入按钮，锁定调用也不修改', async () => {
  for (const mode of ['included','unknown','locked']) {
    const {page,form,html} = await render(p => {
      const r=record(mode==='locked' ? 'submitted' : 'draft')
      if(mode==='included') r.base_settings.description='简介\n\n原视频发布时间：2026-09-01'
      p.apply(r); p.source={files:[],source_published_date:mode==='unknown' ? '' : '2026-09-01'}
    })
    assert.doesNotMatch(html, /补入原视频日期/)
    form.insertPublishedDate()
    assert.equal(page.dirty, false)
  }
})

test('补入日期超出简介上限时不截断用户文案', async () => {
  const {page,form} = await render(p => {
    const r=record();r.base_settings.description='😀'.repeat(1000)
    p.apply(r);p.source={files:[],source_published_date:'2026-09-01'}
  })
  form.insertPublishedDate()
  assert.equal(page.dirty,false)
  assert.deepEqual(page.draft.overrides,{})
})

test('自动运行的发布规则链接直接打开项目配置，而不是稿件队列',async t=>{
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const {page,html}=await render(()=>{},'/projects/fixture-project/publish#project-rules')
  assert.equal(page.tab,'rules');assert.match(html,/项目发布设置/);assert.equal(page.current,null)
})
test('上传后共享资料仍可改，未保存时不能投稿，账号及分P保持冻结', async () => {
  const {page,dialogs,html} = await render(p => p.apply(record('uploaded')))
  assert.match(html, /确认投稿/)
  assert.equal(page.locked, true)
  assert.equal(page.metadataLocked, false)
  page.change('title', '修改后的标题')
  assert.equal(page.displayValues.title, '修改后的标题')
  assert.equal(page.dirty, true)
  page.submit()
  assert.equal(dialogs.length, 0)
})
test('投稿确认读取冻结快照，重复点击只有一个确认窗口', async t => {
  const {page,dialogs} = await render(p => p.apply(record('uploaded')))
  t.mock.method(globalThis, 'fetch', async () => ({ok:true,json:async () => ({snapshot:record('uploaded').snapshot,revision:3,fingerprint:'fixed'})}))
  await Promise.all([page.submit(),page.submit()])
  assert.equal(dialogs.length, 1)
  assert.match(dialogs[0].content, /测试账号.*1个稿件.*1个分P.*公开可见/)
  dialogs[0].onAfterLeave()
  assert.equal(page.confirming, false)
})
test('未知投稿结果只提供回执核对，不提供重复投稿或复制', async () => {
  const {html} = await render(p => p.apply(record('unknown')))
  assert.match(html, /投稿可能已成功/)
  assert.match(html, /核对并关联/)
  assert.doesNotMatch(actionButtons(html).map(buttonText).join('\n'), /确认投稿|复制为新草稿|上传并投稿|仅上传文件|使用审查修订版新建草稿/)
})

function reviewRecord(status='rejected'):any {
  const row:any=record(status)
  row.receipt={bvid:'BV1234567890',reject_reason:'请修订敏感片段'}
  row.snapshot.parts=[{artifact_id:11,title:'原声版'},{artifact_id:12,title:'配音版'}]
  row.draft.parts=[{artifact_id:11,title:'原声版'},{artifact_id:12,title:'配音版'}]
  return row
}
function reviewPlan():any {
  return {mode:'new_submission',project_uid:'fixture-project',source:{publication_id:'fixture',revision:3,status:'rejected',bvid:'BV1234567890',url:'https://www.bilibili.com/video/BV1234567890'},
    account_id:'123',account_name:'测试账号',cover_id:'old.jpg',settings:{...settings},fingerprint:'review-plan-fixed',
    parts:[{index:1,title:'原声版',source_artifact_id:11,source_name:'old-src.mp4',artifact_id:21,name:'review-src.mp4',version_name:'原声修订版'},
      {index:2,title:'配音版',source_artifact_id:12,source_name:'old-dub.mp4',artifact_id:22,name:'review-dub.mp4',version_name:'配音修订版'}],
    notices:['这会创建新投稿并获得新BV；原稿件、审核记录和已发布快照保持保留。',
      '创建草稿只保存到本地；上传与正式投稿仍需执行发布动作。',
      '本版创作管理可修改文案、排序或追加分P；替换原BV的视频请前往B站创作中心。',
      '审查修订沿用已有文字与混音，需要另行核对画面，不能保证平台审核通过。',
      '文本筛查不判断画面，请人工核对<script>提醒</script>']}
}
function reviewDraft(reused=false):any {
  const row:any=record()
  row.id='review-local';row.revision=1;row.reused=reused
  row.draft.parts=[{artifact_id:21,title:'原声版'},{artifact_id:22,title:'配音版'}]
  row.draft.cover_id='old.jpg';row.draft.review_source={mode:'new_submission',source_bvid:'BV1234567890',selection_changed:false}
  return row
}
async function flush() { for(let i=0;i<24;i++)await Promise.resolve() }

test('发布页按准备、资料、确认、回执排序，自动发布主按钮优先且保留保存和仅上传',async()=>{
  const row:any=record();row.draft.parts=[{artifact_id:11,title:'原声版'}]
  const {html,page}=await render(p=>p.apply(row))
  const stages=['publish-video','publish-metadata','publish-confirm','publish-receipt'].map(id=>html.indexOf(`id="${id}"`))
  assert.ok(stages.every((position,index)=>position>=0 && (!index || position>stages[index-1])))
  const buttons=actionButtons(html),main=buttons.find(button=>buttonText(button)==='上传并投稿')
  assert.ok(main);assert.match(main,/n-button--primary-type/);assert.match(main,/n-button--large-type/)
  assert.ok(buttons.findIndex(button=>buttonText(button)==='仅上传文件')>buttons.indexOf(main))
  assert.ok(buttons.some(button=>buttonText(button)==='保存资料'))
  assert.match(html,/确认一次后自动上传视频、封面并投稿/)
  assert.ok(html.indexOf('请确保素材可用于本次发布')<html.indexOf('class="primary-publish-action"'))
  page.change('title','未保存标题');page.switchWorkspace('studio');assert.equal(page.tab,'drafts')
  page.apply(row);page.switchWorkspace('studio');assert.equal(page.tab,'studio')
})

test('修订入口只出现一次且位于退回稿件首屏，明确新BV与官网替换；未知和活动记录不出现',async()=>{
  for(const status of ['rejected','published']) {
    const {html}=await render(p=>p.apply(reviewRecord(status)))
    assert.equal(actionButtons(html).filter(button=>buttonText(button)==='使用审查修订版新建草稿').length,1)
    assert.ok(html.indexOf('class="revision-workflow"')<html.indexOf('id="publish-video"'))
    assert.match(html,/发布后得到新BV/);assert.match(html,/到官网替换原BV视频/)
    assert.match(html,/https:\/\/member.bilibili.com\/platform\/home/)
    const revision=html.slice(html.indexOf('class="revision-workflow"'),html.indexOf('class="publishing-steps"'))
    assert.doesNotMatch(revision,/追加[^。]*会替换原视频|审核通过/)
  }
  for(const status of ['unknown','uploading','draft']) {
    const {html}=await render(p=>p.apply(reviewRecord(status)))
    assert.ok(!actionButtons(html).some(button=>buttonText(button)==='使用审查修订版新建草稿'))
  }
  const {html:live}=await render(p=>p.apply({...reviewRecord('uploading'),progress:25,progress_detail:{phase:'uploading',loaded:250,total:1000,current_part:1,completed_parts:0,total_parts:2}}))
  assert.ok(live.indexOf('class="live-progress"')<live.indexOf('id="publish-video"'))
  assert.match(live,/publication-progress compact/);assert.match(live,/查看实时进度与回执/)
})

test('已有空草稿保持原状，退回记录须由用户打开，编辑原BV入口明确不替换视频',async()=>{
  const current:any=record();current.id='existing-user-empty'
  const old=reviewRecord()
  const {page,html}=await render(p=>{p.apply(current);p.records=[current,old]})
  assert.equal(page.current.id,'existing-user-empty');assert.deepEqual(page.draft.parts,[])
  assert.match(html,/先打开原发布记录/);assert.match(html,/打开原记录 → 准备审查修订草稿/)
  assert.ok(!actionButtons(html).some(button=>buttonText(button)==='使用审查修订版新建草稿'))
  const {html:studio}=await render(p=>{p.tab='studio'})
  assert.match(studio,/保留原BV替换视频请到官网处理/);assert.match(studio,/发布后得到新BV/)
})

test('修订草稿先只读预览，取消不创建；确认内容含精确P映射与安全转义',async t=>{
  const {page,dialogs}=await render(p=>p.apply(reviewRecord()))
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),method:options?.method,body:options?.body?JSON.parse(String(options.body)):null})
    if(String(url).endsWith('/review-draft/plan'))return {ok:true,json:async()=>reviewPlan()}
    throw new Error('取消预览不应创建、上传或投稿')
  })
  await page.createReviewDraft()
  assert.deepEqual(calls,[{url:'/api/publishing/publications/fixture/review-draft/plan',method:'POST',body:{}}])
  assert.equal(dialogs.length,1);assert.equal(dialogs[0].positiveText,'创建本地修订草稿')
  assert.equal(dialogs[0].style.width,'min(680px, calc(100vw - 40px))')
  const confirmation=await renderToString(createSSRApp({render:()=>dialogs[0].content()}))
  assert.match(confirmation,/只创建本地草稿，不上传、不投稿/);assert.match(confirmation,/发布时将创建新BV/)
  assert.match(confirmation,/保留原稿账号、封面、冻结文案及分P顺序和名称/)
  assert.doesNotMatch(confirmation,/这会创建新投稿并获得新BV|创建草稿只保存到本地|本版创作管理可修改文案/)
  assert.match(confirmation,/需要另行核对画面，不能保证平台审核通过/)
  assert.equal((confirmation.match(/<p>/g)||[]).length,3,'摘要一段，并保留画面局限和新增警告')
  assert.match(confirmation,/P1 · 原声版[\s\S]*old-src\.mp4[\s\S]*原声修订版[\s\S]*P2 · 配音版[\s\S]*old-dub\.mp4[\s\S]*配音修订版/)
  assert.match(confirmation,/&lt;script&gt;提醒&lt;\/script&gt;/);assert.doesNotMatch(confirmation,/<script>提醒/)
  dialogs[0].onAfterLeave();assert.equal(page.confirming,false);assert.equal(page.current.id,'fixture')
})

test('确认修订草稿只发送一次版本化本地创建，保留旧记录且不调用任何上传动作',async t=>{
  const {page,dialogs}=await render(p=>p.apply(reviewRecord()))
  const calls:any[]=[]
  let release:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);calls.push({url:path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):null})
    if(path.endsWith('/review-draft/plan'))return {ok:true,json:async()=>reviewPlan()}
    if(path.endsWith('/review-draft'))return new Promise(resolve=>{release=resolve})
    if(path.endsWith('/publications'))return {ok:true,json:async()=>[reviewDraft(),reviewRecord()]}
    if(path.endsWith('/candidates'))return {ok:true,json:async()=>({files:[{id:21,exists:true},{id:22,exists:true}]})}
    if(path.endsWith('/metadata'))return {ok:true,json:async()=>({seasons:[]})}
    throw new Error(`修订创建不应调用：${path}`)
  })
  await page.createReviewDraft()
  const creating=dialogs[0].onPositiveClick();await flush()
  assert.equal(dialogs[0].onPositiveClick(),false)
  const writes=calls.filter(call=>call.url.endsWith('/review-draft'))
  assert.equal(writes.length,1);assert.equal(writes[0].body.revision,3);assert.equal(writes[0].body.fingerprint,'review-plan-fixed')
  assert.match(writes[0].body.request_id,/^[0-9a-f]{8}-[0-9a-f-]{27}$/)
  assert.deepEqual(Object.keys(writes[0].body).sort(),['fingerprint','request_id','revision'])
  release({ok:true,json:async()=>reviewDraft()});await creating;await flush()
  assert.equal(page.current.id,'review-local');assert.equal(page.current.status,'draft');assert.equal(page.dirty,false)
  assert.deepEqual(page.draft.parts,[{artifact_id:21,title:'原声版'},{artifact_id:22,title:'配音版'}])
  assert.equal(page.records.find((row:any)=>row.id==='fixture').status,'rejected')
  assert.ok(calls.every(call=>!call.url.endsWith('/actions') && !call.url.includes('/upload')))
  assert.equal(dialogs[0].onPositiveClick(),false)
})

test('大文件核对期间保持忙态并拒绝重复预览，迟到结果不覆盖其他草稿',async t=>{
  const {page,dialogs}=await render(p=>p.apply(reviewRecord()))
  let release:(value:any)=>void=()=>{},calls=0
  t.mock.method(globalThis,'fetch',()=>{calls++;return new Promise(resolve=>{release=resolve})})
  const pending=page.createReviewDraft()
  assert.equal(page.reviewDraftChecking,true);assert.equal(page.busy,true)
  await page.createReviewDraft();assert.equal(calls,1)
  const other:any=record();other.id='existing-user-empty';page.apply(other)
  release({ok:true,json:async()=>reviewPlan()});await pending
  assert.equal(page.reviewDraftChecking,false);assert.equal(page.busy,false);assert.equal(dialogs.length,0)
  assert.equal(page.current.id,'existing-user-empty');assert.deepEqual(page.draft.parts,[])
})

test('修订预览前、核对期间或确认之后的未保存审查修改都不能创建草稿',async t=>{
  const {page,dialogs}=await render(p=>p.apply(reviewRecord()))
  const calls:string[]=[]
  let release:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',url=>{calls.push(String(url));return new Promise(resolve=>{release=resolve})})
  page.reviewDirty=true;await page.createReviewDraft();assert.equal(calls.length,0)
  page.reviewDirty=false
  const pending=page.createReviewDraft();page.reviewDirty=true
  release({ok:true,json:async()=>reviewPlan()});await pending
  assert.equal(dialogs.length,0)
  page.reviewDirty=false
  t.mock.method(globalThis,'fetch',async url=>{calls.push(String(url));return {ok:true,json:async()=>reviewPlan()}})
  await page.createReviewDraft();assert.equal(dialogs.length,1)
  page.reviewDirty=true;assert.equal(dialogs[0].onPositiveClick(),false)
  page.reviewDirty=false;page.current.revision=4;assert.equal(dialogs[0].onPositiveClick(),false)
  assert.ok(calls.every(url=>url.endsWith('/review-draft/plan')))
})

test('缺少精确修订映射或创建冲突时保留原记录，返回可诊断错误不转为上传',async t=>{
  const {page,dialogs}=await render(p=>p.apply(reviewRecord()))
  const calls:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{
    calls.push(String(url));return {ok:false,json:async()=>({code:'PUBLISH_REVIEW_MAPPING',
      message:'无法完整匹配原投稿分P，未创建草稿；请先核对审查修订文件',
      detail:{parts:[{index:2,title:'配音版',reason:'没有与此原分P对应的有效审查修订文件'},
        {index:1,title:'原声<script>版</script>',reason:'存在多个仍有效的审查修订文件，请明确保留一个对应版本后重试'}]}})}
  })
  await page.createReviewDraft()
  assert.match(page.error,/无法完整匹配原投稿分P/)
  assert.match(page.error,/P2 · 配音版：没有与此原分P对应的有效审查修订文件/)
  assert.match(page.error,/P1 · 原声<script>版<\/script>：存在多个仍有效的审查修订文件/)
  const {html:failed}=await render(p=>{p.apply(reviewRecord());p.error=page.error})
  assert.match(failed,/P1 · 原声&lt;script&gt;版&lt;\/script&gt;/);assert.doesNotMatch(failed,/<script>版/)
  assert.equal(dialogs.length,0);assert.equal(page.current.id,'fixture')
  t.mock.method(globalThis,'fetch',async url=>{
    calls.push(String(url))
    return String(url).endsWith('/plan')?{ok:true,json:async()=>reviewPlan()}:{ok:false,json:async()=>({code:'PUBLISH_CONFLICT',message:'修订计划已变化，请重新预览'})}
  })
  await page.createReviewDraft();await dialogs[0].onPositiveClick()
  assert.match(page.error,/修订计划已变化/);assert.equal(page.current.id,'fixture');assert.equal(page.current.revision,3)
  assert.deepEqual(page.draft.parts,[{artifact_id:11,title:'原声版'},{artifact_id:12,title:'配音版'}])
  assert.ok(calls.every(url=>url.includes('/review-draft')))
})

test('全局发布页的成片编辑入口始终指向当前记录，不随新草稿来源选择改变',async()=>{
  const {html}=await render(p=>{p.apply(record());p.projectUid='different-new-project'})
  assert.match(html,/<a href="\/projects\/fixture-project\/steps\/assemble"[^>]*>修改成片版本效果/)
  assert.doesNotMatch(html,/<a href="\/projects\/different-new-project\/steps\/assemble"/)
})

test('发布页在上传分P、封面及提交阶段始终显示真实进度和阶段', async () => {
  for (const [status,phase,message] of [['uploading','uploading','正在上传视频文件'],['uploading','cover','正在处理并上传封面'],['submitting','submitting','正在提交稿件资料']]) {
    const {html} = await render(p => p.apply({...record(status),progress:phase==='uploading'?25:100,
      progress_detail:{phase,loaded:phase==='uploading'?250:1000,total:1000,current_part:phase==='uploading'?1:0,completed_parts:phase==='uploading'?0:1,total_parts:1}}))
    assert.match(html,/data-publication-progress/)
    assert.ok(html.includes(message))
  }
})

test('上传并投稿先展示范围，确认才发送单一后台动作，连续点击不会重复提交', async t => {
  const row = record()
  row.draft.parts = [{artifact_id:1,title:'配音版'}] as any
  const {page,dialogs,html} = await render(p => p.apply(row))
  assert.match(html, /上传并投稿/)
  const snapshot = record('uploaded').snapshot
  snapshot.settings = {...settings,visibility:'self',publish_at:2000000000} as any
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const body = options?.body ? JSON.parse(String(options.body)) : null
    requests.push({url:String(url),body})
    if (String(url).endsWith('/plan')) return {ok:true,json:async () => ({snapshot,revision:3,fingerprint:'fixed-plan'})}
    if (String(url).endsWith('/actions')) return {ok:true,json:async () => record('queued')}
    if (String(url).endsWith('/publications')) return {ok:true,json:async () => []}
    throw new Error(`非预期请求：${url}`)
  })
  await page.upload(true);await page.upload(true)
  assert.equal(dialogs.length,1)
  assert.deepEqual(requests.map(r => r.url),['/api/publishing/publications/fixture/plan'])
  assert.match(dialogs[0].content,/测试账号.*1个稿件.*1个分P.*仅自己可见.*定时发布/)
  assert.match(dialogs[0].content,/河内美食/)
  assert.match(dialogs[0].content,/自动正式投稿，不再二次询问/)
  await Promise.all([dialogs[0].onPositiveClick(),dialogs[0].onPositiveClick()])
  await dialogs[0].onPositiveClick()
  assert.deepEqual(requests.filter(r => r.url.endsWith('/actions')).map(r => r.body),[
    {action:'publish',revision:3,fingerprint:'fixed-plan',confirmed:true},
  ])
  dialogs[0].onAfterLeave()
  assert.equal(page.confirming,false)
})

test('投稿启动回执即时解锁弹窗，慢列表与旧响应不覆盖已确认的新状态', async t => {
  const {page,dialogs}=await render(p=>p.apply(record()))
  const lists:any[]=[]
  let posts=0
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url)
    if(path.endsWith('/publications'))return new Promise((resolve,reject)=>lists.push({resolve,reject}))
    if(path.endsWith('/plan'))return {ok:true,json:async()=>({snapshot:record('uploaded').snapshot,revision:3,fingerprint:'plan'})}
    if(path.endsWith('/actions')){posts++;return {ok:true,json:async()=>({...record('queued'),revision:4})}}
    throw new Error(`非预期请求：${path}`)
  })
  const oldList=page.refreshList()
  await page.upload(true)
  let settled=false
  const submitted=dialogs[0].onPositiveClick().then(()=>{settled=true})
  const flush=async()=>{for(let i=0;i<24;i++)await Promise.resolve()}
  await flush()
  try {
    assert.equal(settled,true,'后台列表请求不应占用确认回调')
    assert.equal(page.busy,false);assert.equal(page.current.status,'queued')
    assert.equal(page.records[0].status,'queued');assert.equal(lists.length,2)
    lists[0].resolve({ok:true,json:async()=>[record('draft')]});await oldList
    assert.equal(page.records[0].status,'queued','写入前发出的旧列表不能回滚新状态')
    lists[1].reject(new Error('列表暂时不可用'));await flush()
    assert.equal(page.error,'','列表刷新失败不误报为投稿失败')
    assert.equal(page.current.revision,4)
    await dialogs[0].onPositiveClick();assert.equal(posts,1)
  } finally {
    lists.forEach(item=>item.resolve({ok:true,json:async()=>[record('queued')]}))
    await submitted;await oldList;await flush()
  }
})

test('复制草稿只发一次写请求，直接打开完整回执且不等待慢列表', async t => {
  const {page} = await render(p => p.apply(record('uploaded')))
  const source = page.source
  const requests:any[] = []
  let completeCopy:any, completeList:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url)
    requests.push({path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):undefined})
    if(path.endsWith('/copy')) return new Promise(resolve=>{completeCopy=resolve})
    if(path.endsWith('/publications')) return new Promise(resolve=>{completeList=resolve})
    throw new Error(`复制不应再读取详情或扫描文件：${path}`)
  })
  const operation=page.copy()
  assert.equal(page.busy,true);assert.equal(page.copying,true)
  await page.copy()
  assert.equal(requests.length,1,'连续点击不发第二个复制请求')
  assert.equal(requests[0].body.revision,3)
  assert.match(requests[0].body.request_id,/^[0-9a-f-]{36}$/)
  const copied={...record(),id:'copied-fixture',revision:1,reused:false}
  completeCopy({ok:true,json:async()=>copied})
  let settled=false
  const finished=operation.then(()=>{settled=true})
  for(let i=0;i<24;i++) await Promise.resolve()
  try {
    assert.equal(settled,true,'后台队列列表慢不拖住复制操作')
    assert.equal(page.busy,false);assert.equal(page.copying,false)
    assert.equal(page.current.id,'copied-fixture')
    assert.equal(page.current.status,'draft');assert.equal(page.dirty,false)
    assert.deepEqual(page.resolved.values,settings)
    assert.equal(page.source,source,'同项目候选文件不重复扫描')
    assert.deepEqual(requests.map(r=>r.path),['/api/publishing/publications/fixture/copy','/api/publishing/publications'])
    completeList({ok:false,json:async()=>({message:'隔离慢列表失败'})})
    for(let i=0;i<24;i++) await Promise.resolve()
    assert.equal(page.error,'','刷新失败不误报为复制失败')
    assert.equal(page.current.id,'copied-fixture')
  } finally {
    completeList({ok:true,json:async()=>[copied]})
    await finished
  }
})

test('复制响应丢失后重试复用请求标识，源资料变化时建立新请求', async t => {
  const {page} = await render(p=>p.apply(record('uploaded')))
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/publications'))return {ok:true,json:async()=>[page.current]}
    assert.ok(String(url).endsWith('/copy'))
    requests.push(JSON.parse(String(options?.body)))
    if(requests.length<3)throw new Error('隔离响应丢失')
    return {ok:true,json:async()=>({...record(),id:'replayed-fixture',reused:true})}
  })
  await page.copy()
  assert.equal(page.current.id,'fixture');assert.equal(page.copying,false);assert.equal(page.busy,false)
  assert.equal(page.error,'隔离响应丢失')
  await page.copy()
  assert.deepEqual(requests[1],requests[0],'同一次失败重试不能再生成草稿')
  page.apply({...record('uploaded'),revision:4})
  await page.copy()
  assert.notEqual(requests[2].request_id,requests[0].request_id)
  assert.equal(requests[2].revision,4)
  assert.equal(page.current.id,'replayed-fixture');assert.equal(page.error,'')
})

test('复制按钮显示等待状态并禁用，未知或进行中稿件不能发复制请求', async()=>{
  const {html}=await render(p=>{p.apply(record('uploaded'));p.copying=true})
  const button=actionButtons(html).find(b=>buttonText(b)==='正在复制草稿')
  assert.ok(button);assert.match(button,/disabled/);assert.match(button,/n-button--loading/)
  for(const status of ['draft','unknown','uploading','submitting']) {
    const {page}=await render(p=>p.apply(record(status)))
    await page.copy()
    assert.equal(page.busy,false);assert.equal(page.copying,false)
    assert.equal(page.error,'')
  }
})

test('仅上传不会附带投稿确认；取消或确认前修改资料都不发送上传动作', async t => {
  const {page,dialogs} = await render(p => p.apply(record()))
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const body = options?.body ? JSON.parse(String(options.body)) : null
    requests.push({url:String(url),body})
    if (String(url).endsWith('/plan')) return {ok:true,json:async () => ({snapshot:record('uploaded').snapshot,revision:3,fingerprint:'fixed'})}
    if (String(url).endsWith('/actions')) return {ok:true,json:async () => record('queued')}
    if (String(url).endsWith('/publications')) return {ok:true,json:async () => []}
    throw new Error(`非预期请求：${url}`)
  })
  await page.upload(true)
  dialogs[0].onAfterLeave()
  assert.equal(requests.filter(r => r.url.endsWith('/actions')).length,0)
  await page.upload(true)
  page.change('title','确认窗口打开后改动')
  assert.equal(dialogs[1].onPositiveClick(),false)
  assert.equal(requests.filter(r => r.url.endsWith('/actions')).length,0)
  dialogs[1].onAfterLeave()
  page.apply(record())
  await page.upload(false)
  assert.match(dialogs[2].content,/不会正式投稿/)
  await dialogs[2].onPositiveClick()
  assert.deepEqual(requests.filter(r => r.url.endsWith('/actions')).map(r => r.body),[
    {action:'upload',revision:3,fingerprint:'fixed'},
  ])
})

test('AI配音提示紧邻创作声明且只跟随已选分P，不擅自修改声明', async () => {
  for (const ai of [true,false]) {
    const row = record()
    row.draft.parts = [{artifact_id:ai ? 1 : 2,title:'版本'}] as any
    row.draft.overrides = {declaration_id:-1}
    const {page,html} = await render(p => {
      p.apply(row)
      p.source = {files:[{id:1,has_dubbing:true},{id:2,has_dubbing:false}]}
    })
    assert.equal(page.hasAiDubbing,ai)
    assert.equal(page.displayValues.declaration_id,-1)
    assert.equal(page.dirty,false)
    if (ai) {
      assert.match(html,/aria-label="创作声明"[\s\S]*?配音版本 · 建议核对AI声明/)
      assert.match(html,/同一稿件的所有分P共用声明/)
      assert.match(html,/https:\/\/www.bilibili.com\/opus\/1106496554576904197/)
    } else assert.doesNotMatch(html,/配音版本 · 建议核对AI声明/)
  }
  const {page,html} = await render(p => {
    const row = record('uploaded')
    row.snapshot.parts = [{artifact_id:1,title:'配音版',has_dubbing:true}] as any
    p.apply(row)
  })
  assert.equal(page.hasAiDubbing,true)
  assert.match(html,/配音版本 · 建议核对AI声明/)
})

test('配音未选AI声明只在现有确认框提醒，可取消或保持原声明继续', async t => {
  const row = record()
  row.draft.parts = [{artifact_id:1,title:'配音版'}] as any
  row.draft.overrides = {declaration_id:-1}
  const {page,dialogs} = await render(p => p.apply(row))
  const snapshot = {...record('uploaded').snapshot,parts:[{artifact_id:1,title:'配音版',has_dubbing:true}],
    settings:{...settings,declaration_id:-1}}
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const body = options?.body ? JSON.parse(String(options.body)) : null
    requests.push({url:String(url),body})
    if (String(url).endsWith('/plan')) return {ok:true,json:async () => ({snapshot,revision:3,fingerprint:'choice-fixed',ai_declaration_acknowledged:false})}
    if (String(url).endsWith('/actions')) return {ok:true,json:async () => record('queued')}
    if (String(url).endsWith('/publications')) return {ok:true,json:async () => []}
    throw new Error(`非预期请求：${url}`)
  })
  await page.upload(true)
  assert.equal(dialogs.length,1)
  assert.match(dialogs[0].content,/建议核对AI声明.*按当前声明继续/)
  assert.equal(page.displayValues.declaration_id,-1)
  assert.equal(page.dirty,false)
  dialogs[0].onAfterLeave()
  assert.equal(requests.filter(r=>r.url.endsWith('/actions')).length,0)
  await page.upload(true)
  await Promise.all([dialogs[1].onPositiveClick(),dialogs[1].onPositiveClick()])
  assert.deepEqual(requests.filter(r=>r.url.endsWith('/actions')).map(r=>r.body),[
    {action:'publish',revision:3,fingerprint:'choice-fixed',confirmed:true,acknowledge_ai_notice:true},
  ])
})

test('已上传后的声明提醒由当前快照决定；确认过不重复，资料变化重新提醒', async t => {
  for (const acknowledged of [true,false]) {
    const row = record('uploaded')
    row.snapshot.parts = [{artifact_id:1,title:'配音版',has_dubbing:true}] as any
    row.snapshot.settings = {...settings,declaration_id:-1} as any
    const {page,dialogs} = await render(p => p.apply(row))
    const actions: any[] = []
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      if (String(url).endsWith('/plan')) return {ok:true,json:async () => ({snapshot:row.snapshot,revision:3,
        fingerprint:'current-snapshot',ai_declaration_acknowledged:acknowledged})}
      if (String(url).endsWith('/actions')) {actions.push(JSON.parse(String(options?.body)));return {ok:true,json:async () => record('queued')}}
      if (String(url).endsWith('/publications')) return {ok:true,json:async () => []}
      throw new Error(`非预期请求：${url}`)
    })
    await page.submit()
    assert.equal(dialogs.length,1)
    assert.equal(dialogs[0].content.includes('建议核对AI声明'),!acknowledged)
    assert.equal(actions.length,0)
    await Promise.all([dialogs[0].onPositiveClick(),dialogs[0].onPositiveClick()])
    assert.deepEqual(actions,[{action:'submit',revision:3,fingerprint:'current-snapshot',confirmed:true,
      ...(!acknowledged ? {acknowledge_ai_notice:true} : {})}])
    dialogs[0].onAfterLeave()
  }
})

test('投稿提醒打开后再修改资料不会沿用旧确认', async t => {
  const row = record('uploaded')
  const {page,dialogs} = await render(p => p.apply(row))
  const requests: string[] = []
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(String(url))
    return {ok:true,json:async () => ({snapshot:row.snapshot,revision:3,fingerprint:'fixed'})}
  })
  await page.submit()
  page.change('declaration_id',-1)
  assert.equal(dialogs[0].onPositiveClick(),false)
  assert.deepEqual(requests,['/api/publishing/publications/fixture/plan'])
})
test('稿件继承后端统一设置，不再出现账号默认入口；未保存资料阻止切换', async () => {
  const row=record();row.base_settings.watermark=true;row.base_sources={watermark:{kind:'series',name:'旅行系列'}}
  const {page,html}=await render(p=>p.apply(row))
  assert.equal(page.displayValues.watermark,true)
  assert.deepEqual({...page.displaySources.watermark},{kind:'series',name:'旅行系列'})
  assert.doesNotMatch(html,/账号默认设置|保存账号默认/)
  page.change('watermark',false)
  assert.equal(page.canSwitch(),false)
  assert.equal(page.displaySources.watermark.kind,'publication')
  page.inherit('watermark')
  assert.equal(page.displaySources.watermark.kind,'series')
})

test('项目设置独立保护未保存修改与生成中的候选，不与原BV编辑状态混用', async () => {
  const {page} = await render(p => { p.settingsDirty = true; p.tab = 'rules' })
  assert.equal(page.studioGuard, false)
  assert.equal(page.canSwitch(), false)
  page.switchWorkspace('drafts')
  assert.equal(page.tab, 'rules')
  let prevented = false
  const event: any = { preventDefault() { prevented = true }, returnValue: undefined }
  page.beforeUnload(event)
  assert.equal(prevented, true)
  assert.equal(event.returnValue, '')
  page.settingsDirty = false; page.settingsBusy = true
  assert.equal(page.canSwitch(), false)
  prevented = false; page.beforeUnload(event)
  assert.equal(prevented, true)
  page.settingsBusy = false
  page.switchWorkspace('drafts')
  assert.equal(page.tab, 'drafts')
})

test('项目保存或候选生成中拒绝离开；未保存修改需要明确选择才可离开', async () => {
  const {page, dialogs} = await render(p => { p.settingsBusy = true })
  assert.equal(page.confirmLeave(), false)
  assert.equal(dialogs.length, 0)
  page.settingsBusy = false; page.settingsDirty = true
  const pending = page.confirmLeave()
  assert.equal(dialogs.length, 1)
  dialogs[0].onNegativeClick()
  assert.equal(await pending, false)
  assert.equal(page.settingsDirty, true)
  const accepted = page.confirmLeave()
  dialogs[1].onPositiveClick()
  assert.equal(await accepted, true)
  page.settingsDirty = false
  assert.equal(page.confirmLeave(), true)
})

test('冻结发布资料沿用快照来源，缺失记录不猜测为全局或被当前草稿冒充', async () => {
  const row=record('published');row.snapshot.sources={watermark:{kind:'series',name:'原模板'}}
  const {page}=await render(p=>p.apply(row))
  assert.deepEqual({...page.displaySources.watermark},{kind:'series',name:'原模板'})
  assert.equal(page.displaySources.title.kind,'snapshot')
})

test('二维码使用独立白底与零内边距SVG，状态明确区分手机确认和本地保存', async () => {
  const {html} = await render(p => {
    p.tab = 'accounts'; p.loginId = 'fixture'
    p.loginState = {status:'waiting',url:'https://passport.bilibili.com/fixture'}
  })
  assert.match(html, /class="qr-frame"/)
  assert.match(html, /padding:0px/)
  assert.match(html, /<svg(?=[^>]*width="180")(?=[^>]*height="180")/)
  assert.match(html, /请使用B站App扫码/)
  for (const [status, text] of [['verifying','手机端已确认，正在校验账号'],['saving','账号已验证，正在安全保存'],['connected','账号已连接']]) {
    const result = await render(p => {p.tab = 'accounts'; p.loginState = {status}})
    assert.ok(result.html.includes(text))
    assert.doesNotMatch(result.html, /class="qr-frame"/)
  }
})

test('登录失败显示真实脱敏原因；迟到的旧轮询不覆盖新二维码', async t => {
  const {html,page} = await render(p => {
    p.tab = 'accounts'; p.loginId = 'old'
    p.loginState = {status:'failed',error:'手机端已确认，但网页连接未完成：HTTPS证书校验失败',code:'PUBLISH_TLS'}
  })
  assert.match(html, /HTTPS证书校验失败/)
  assert.match(html, /PUBLISH_TLS/)
  let release: (value: any) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => {release = resolve}))
  const pending = page.pollLogin()
  page.loginId = 'new'; page.loginState = {status:'generating'}
  release({ok:true,json:async () => ({status:'connected'})})
  await pending
  assert.equal(page.loginId, 'new')
  assert.equal(page.loginState.status, 'generating')
})

test('后端重启丢失会话后，清除旧二维码并解锁重新获取按钮', async t => {
  const {page} = await render(p => {p.loginId = 'old';p.loginState = {status:'waiting',url:'old-code'}})
  t.mock.method(globalThis, 'fetch', async () => ({ok:false,json:async () => ({code:'PUBLISH_LOGIN_NOT_FOUND',message:'登录会话已结束，请重新获取二维码'})}))
  await page.pollLogin()
  assert.equal(page.loginId, '')
  assert.equal(page.loginState.status, 'expired')
  assert.equal(page.loginState.url, undefined)
})

test('更多设置真实开关可更改；没有会激活继承按钮的外层label', async () => {
  const {html,page,switches} = await render(p => p.apply(record()))
  for (const label of html.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)) {
    assert.doesNotMatch(label[0], /n-switch|恢复继承/)
  }
  assert.equal(switches.length, 4)
  switches[1].handleClick()
  await nextTick()
  assert.equal(page.draft.overrides.close_comments, true)
  assert.equal(page.displayValues.close_comments, true)
  assert.equal(page.dirty, true)
  page.inherit('close_comments')
  assert.equal(page.displayValues.close_comments, false)
})

test('转载水印有明确限制说明，其他开关仍能使用', async () => {
  const {html,switches} = await render(p => { p.apply(record()); p.change('copyright', 2) })
  assert.match(html, /转载稿件不提交此选项/)
  assert.equal(switches[0].$props.disabled, true)
  assert.equal(switches[1].$props.disabled, false)
})

test('AI候选只在手动应用后修改文案，不调用保存上传投稿', async t => {
  const {page,assistant,html} = await render(p => p.apply(record()))
  assert.match(html, /AI 文案助手/)
  const calls: any[] = []
  const values = {title:'河内旅行新标题',description:'新简介',tags:['河内'],dynamic:'新动态'}
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push([url, JSON.parse(String(options?.body))])
    return {ok:true,json:async () => ({values,basis:['视频资料'],warnings:[]})}
  })
  await assistant.generate()
  assert.equal(page.displayValues.title, '河内美食')
  assert.equal(page.dirty, false)
  assert.equal(calls.length, 1)
  assert.match(calls[0][0], /\/suggest$/)
  assert.deepEqual(Object.keys(calls[0][1].current).sort(), ['description','dynamic','tags','title'])
  assistant.selected = ['title','tags']
  assistant.apply()
  assert.equal(page.displayValues.title, values.title)
  assert.equal(page.displayValues.description, '发布简介')
  assert.equal(page.dirty, true)
  assert.equal('rights_confirmed' in page.draft, false)
  assert.equal(page.displayValues.copyright, 3)
  assert.equal(calls.length, 1)
})

test('AI生成期间编辑标题，迟到结果不能覆盖', async t => {
  const {page,assistant,assistantProps} = await render(p => p.apply(record()))
  let release: (value:any) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => {release=resolve}))
  const pending = assistant.generate()
  page.change('title','人工改好的标题')
  // SSR 不运行更新渲染，显式模拟父组件刷新传入的文案属性。
  assistantProps.values = page.displayValues
  await nextTick()
  release({ok:true,json:async () => ({values:{title:'迟到的标题'},basis:[],warnings:[]})})
  await pending
  assert.equal(assistant.suggestion, null)
  assert.match(assistant.error, /稿件已变化/)
  assistant.apply()
  assert.equal(page.displayValues.title, '人工改好的标题')
  assert.equal(assistant.busy, false)
})

test('AI失败保留原稿且可以重试；只填空缺不覆盖已有标题', async t => {
  const {page,assistant} = await render(p => p.apply(record()))
  t.mock.method(globalThis, 'fetch', async () => ({ok:false,json:async () => ({message:'模拟服务失败'})}))
  await assistant.generate()
  assert.match(assistant.error, /模拟服务失败/)
  assert.equal(page.dirty, false)
  assert.equal(assistant.busy, false)
  t.mock.method(globalThis, 'fetch', async () => ({ok:true,json:async () => ({
    values:{title:'候选标题',description:'候选简介',tags:['河内'],dynamic:'空缺动态'},basis:[],warnings:[],
  })}))
  await assistant.generate()
  assistant.apply(true)
  assert.equal(page.displayValues.title, settings.title)
  assert.deepEqual(page.displayValues.tags, settings.tags)
  assert.equal(page.displayValues.dynamic, '空缺动态')
  assert.deepEqual(Object.keys(page.draft.overrides), ['dynamic'])
})

test('发布刷新文件列表保留已选分P和未保存的文案，恢复文件后重新可用', async t => {
  const {page} = await render(p => p.apply(record()))
  page.draft.parts = [{artifact_id:1,title:'配音版'}]
  page.change('title','未保存的标题')
  let exists = false
  const requests: string[] = []
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(String(url))
    return {ok:true,json:async () => ({files:[{id:1,exists}]})}
  })
  await page.refreshFiles()
  assert.equal(page.source.files[0].exists, false)
  assert.equal(page.draft.parts.length, 1)
  assert.equal(page.displayValues.title, '未保存的标题')
  exists = true
  await page.refreshFiles()
  assert.equal(page.source.files[0].exists, true)
  assert.equal(page.dirty, true)
  assert.ok(requests.every(url => url.endsWith('/candidates')))
})

test('设置平台封面只锁定封面操作，文案与开关不置灰，完成后保留同期编辑', async t => {
  const {page} = await render(p => p.apply(record()))
  let release: (value:any) => void = () => {}, requests = 0
  t.mock.method(globalThis, 'fetch', () => { requests++; return new Promise(resolve => {release=resolve}) })
  const pending = page.importCover()
  assert.equal(page.coverLoading, true)
  assert.equal(page.busy, false)
  assert.equal(page.metadataLocked, false)
  assert.equal(page.locked, false)
  page.change('title', '下载封面时修改标题')
  await page.importCover(); await page.save(); await page.upload()
  assert.equal(requests, 1)
  release({ok:true,json:async () => ({cover_id:'new.jpg'})}); await pending
  assert.equal(page.draft.cover_id, 'new.jpg')
  assert.equal(page.displayValues.title, '下载封面时修改标题')
  assert.equal(page.coverLoading, false)
})

test('封面失败保留旧图且可重试；迟到响应不能修改另一份或重新打开的稿件', async t => {
  const {page} = await render(p => {p.apply(record());p.draft.cover_id='old.jpg'})
  t.mock.method(globalThis, 'fetch', async () => ({ok:false,json:async () => ({message:'封面下载失败'})}))
  await page.importCover()
  assert.equal(page.draft.cover_id, 'old.jpg'); assert.match(page.coverError,/下载失败/)
  let release: (value:any) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => {release=resolve}))
  const pending = page.importCover()
  page.apply(record())
  release({ok:true,json:async () => ({cover_id:'late.jpg'})}); await pending
  assert.equal(page.draft.cover_id, '')
})

test('账号选项读取不锁整页、不改已选设置；合集只有一个小节时自动选择', async t => {
  const {page} = await render(p => {p.apply(record());p.change('human_type2',1029)})
  let release: (value:any) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => {release=resolve}))
  const pending = page.refreshMetadata()
  assert.equal(page.metadataLoading, true); assert.equal(page.metadataLocked, false)
  release({ok:true,json:async () => ({human_categories:[{id:1029,name:'vlog'}],declarations:[{value:1,label:'含AI生成内容'}],seasons:[]})})
  await pending
  assert.equal(page.metadata.human_categories[0].name,'vlog')
  assert.equal(page.displayValues.human_type2,1029)
  const {page:other,form} = await render(p => {
    p.apply(record())
    p.metadata = {seasons:[{id:10,title:'系列',sections:[{id:20,title:'正片'}]}]};p.metadataReady=true
  })
  form.select('season_id',10)
  assert.equal(other.draft.overrides.season_id,10)
  assert.equal(other.draft.overrides.section_id,20)
  form.select('season_id',null)
  assert.equal(other.draft.overrides.section_id,null)
})

test('合集选择旁可新建：打开仅查待核对记录，确认后创建，不保存或投稿', async t => {
  const {page,collection,html} = await render(p => {p.apply(record());p.draft.cover_id='fixture.jpg'})
  assert.match(html,/新建合集/)
  const calls: {url:string;body:any}[] = []
  let finish: (value:any) => void = () => {}
  t.mock.method(globalThis,'fetch',async (url,options) => {
    calls.push({url:String(url),body:options?.body ? JSON.parse(String(options.body)) : null})
    if (String(url).endsWith('/collection-requests')) return {ok:true,json:async () => []}
    if (String(url).endsWith('/collections')) return await new Promise(resolve => {finish=resolve})
    if (String(url).endsWith('/metadata')) return {ok:true,json:async () => ({human_categories:[],declarations:[],seasons:[{id:9,title:'旅行合集',sections:[{id:8,title:'正片'}]}]})}
    throw new Error('不应保存、上传视频或投稿')
  })
  await collection.open()
  assert.equal(calls.length,1)
  assert.equal(collection.target.account_id,'123')
  assert.equal(collection.cover,'fixture.jpg')
  collection.title='旅行合集';collection.description='简介'
  const creating = collection.create()
  await collection.create()
  assert.equal(calls.filter(c => c.url.endsWith('/collections')).length,1)
  assert.equal(page.busy,false)
  assert.equal(calls[1].body.confirmed,true)
  assert.match(calls[1].body.request_id,/^[a-f0-9]{32}$/)
  finish({ok:true,json:async () => ({status:'created',season_id:9,title:'旅行合集'})})
  await creating
  for(let i=0;i<12;i++) await nextTick()
  assert.equal(page.draft.overrides.season_id,9)
  assert.equal(page.draft.overrides.section_id,8)
  assert.equal(page.dirty,true)
  assert.equal(collection.show,false)
})

test('创建合集断网后禁止盲重试，重新开窗可核对；明确确认未创建才解除保护', async t => {
  const {collection,dialogs} = await render(p => p.apply(record()))
  let posts = 0, unresolved = false, resolutions = 0
  t.mock.method(globalThis,'fetch',async (url) => {
    if(String(url).endsWith('/collection-requests')) return {ok:true,json:async () => unresolved ? [{id:'a'.repeat(32),title:'旅行',busy:false}] : []}
    if(String(url).endsWith('/collections')) {posts++;unresolved=true;return {ok:false,json:async () => ({code:'PUBLISH_COLLECTION_UNKNOWN',message:'创建结果尚未确认'})}}
    if(String(url).endsWith('/resolve')) {resolutions++;unresolved=false;return {ok:true,json:async () => ({status:'failed'})}}
    return {ok:true,json:async () => ({human_categories:[],declarations:[],seasons:[]})}
  })
  await collection.open();collection.title='旅行';collection.cover='test.jpg'
  await collection.create();await collection.create()
  assert.equal(posts,1);assert.match(collection.error,/尚未确认/)
  await collection.open()
  assert.equal(collection.pending.length,1)
  collection.confirmResolution(false)
  assert.equal(resolutions,0)
  assert.match(dialogs[0].content,/不会自动创建/)
  await dialogs[0].onPositiveClick()
  assert.equal(resolutions,1);assert.equal(posts,1);assert.equal(collection.pending.length,0)
})

test('创建后的列表刷新失败或上下文变化，不清空选择、不覆盖其他草稿', async t => {
  const {page} = await render(p => p.apply(record()))
  t.mock.method(globalThis,'fetch',async () => ({ok:false,json:async () => ({message:'列表读取失败'})}))
  await page.collectionCreated({account_id:'123',context_key:page.collectionContext,previous_season:null,season_id:9,title:'新合集'})
  assert.equal(page.draft.overrides.season_id,undefined)
  let release: (value:any) => void = () => {}
  t.mock.method(globalThis,'fetch',() => new Promise(resolve => {release=resolve}))
  const pending = page.collectionCreated({account_id:'123',context_key:page.collectionContext,previous_season:null,season_id:9,title:'新合集'})
  for(let i=0;i<4;i++) await nextTick()
  page.apply({...record(),id:'different'})
  release({ok:true,json:async () => ({human_categories:[],declarations:[],seasons:[{id:9,title:'新合集',sections:[]}]})})
  await pending
  assert.equal(page.draft.overrides.season_id,undefined)
})
