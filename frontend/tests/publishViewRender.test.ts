import assert from 'node:assert/strict'
import test, { before, after, afterEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, PublishView: any, setNavigationExecution: any
before(async () => {
  server = await createServer({ root:fileURLToPath(new URL('..', import.meta.url)),
    configFile:false, plugins:[vue()], appType:'custom', server:{middlewareMode:true,hmr:false,watch:null,ws:false} })
  ;({default:PublishView} = await server.ssrLoadModule('/src/views/PublishView.vue'))
  ;({setNavigationExecution}=await server.ssrLoadModule('/src/utils/configurationEditability.ts'))
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })
afterEach(()=>setNavigationExecution('',false,true))

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
test('章节失败需明确确认，专用重试不会重新上传或投稿且双击只发一次',async t=>{
  const row:any=record('published');row.receipt={bvid:'BV1234567890'}
  row.chapter_sync={status:'failed',enabled:true,error:'章节设置失败'}
  const {page,dialogs}=await render(p=>p.apply(row)),requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),method:options?.method||'GET',body:options?.body?JSON.parse(String(options.body)):null})
    return {ok:true,json:async()=>String(url).includes('/chapters/retry')?{...row,chapter_sync:{status:'completed',enabled:true}}:[row]}
  })
  await page.chapterAction('retry');assert.equal(requests.length,0);assert.equal(dialogs.length,1)
  assert.match(dialogs[0].content,/不会重新上传视频或重新投稿/)
  const first=dialogs[0].onPositiveClick();await dialogs[0].onPositiveClick();await first
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {path:'/api/publishing/publications/fixture/chapters/retry',method:'POST',body:{confirmed:true}},
  ])
  assert.equal(page.current.status,'published');assert.equal(page.current.chapter_sync.status,'completed')
})

test('章节未知结果只能发送reconcile_only核对，不能借enable重写',async t=>{
  const row:any=record('published');row.receipt={bvid:'BV1234567890'};row.chapter_sync={status:'unknown',enabled:true}
  const {page,dialogs}=await render(p=>p.apply(row)),requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),method:options?.method||'GET',body:options?.body?JSON.parse(String(options.body)):null})
    return {ok:true,json:async()=>String(url).endsWith('/chapters/retry')?row:[row]}
  })
  await page.chapterAction('retry',true);assert.equal(dialogs.length,0);assert.equal(requests.length,0)
  await page.chapterAction('reconcile')
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {path:'/api/publishing/publications/fixture/chapters/retry',method:'POST',body:{confirmed:true,reconcile_only:true}},
  ])
})

test('历史关闭记录仅在补充确认后enable，普通刷新只读取本地发布记录',async t=>{
  const row:any=record('published');row.receipt={bvid:'BV1234567890'}
  row.snapshot.chapter_options={enabled:false,translate:true,permanent:true,items:Array.from({length:21},(_,i)=>({title:'章节'+i,start_time:i*10}))}
  row.snapshot.parts=[{title:'原声版'},{title:'配音版'}]
  const {page,dialogs}=await render(p=>p.apply(row)),requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),method:options?.method||'GET',body:options?.body?JSON.parse(String(options.body)):null})
    return {ok:true,json:async()=>String(url).includes('?project_uid=')?[row]:row}
  })
  await page.chapterAction('refresh')
  assert.ok(requests.every(request=>request.method==='GET'));assert.equal(dialogs.length,0)
  await page.chapterAction('retry');assert.equal(dialogs.length,0)
  await page.chapterAction('retry',true);assert.equal(dialogs.length,1)
  assert.match(dialogs[0].content,/BV1234567890.*21 条源章节.*2 个分P.*自动翻译并精简中文标题.*App默认展示章节/)
  await dialogs[0].onPositiveClick()
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {path:'/api/publishing/publications/fixture/chapters/retry',method:'POST',body:{confirmed:true,enable:true}},
  ])
})

function record(status = 'draft') {
  return {id:'fixture',project_uid:'fixture-project',revision:3,status,receipt:{},progress:0,error:'',
    base_settings:{...settings},base_origins:{},
    draft:{account_id:'123',template_id:'',parts:[],overrides:{},cover_id:'',content:{}},
    snapshot:status === 'draft' ? {} : {settings:{...settings},origins:{},base_settings:{...settings},base_origins:{},parts:[{artifact_id:1,title:'配音版'}],account_id:'123',account_name:'测试账号'},
  }
}
async function render(seed: (page:any) => void = () => {}, path='/projects/fixture-project/publish') {
  let page: any, assistant: any, assistantProps: any, form: any, collection: any, partsEditor: any, partsProps: any
  const switches: any[] = []
  const dialogs: any[] = []
  const router = createRouter({history:createMemoryHistory(),routes:[
    {path:'/publish',component:PublishView},{path:'/projects/:uid/publish',component:PublishView},
    {path:'/publications/:id',component:PublishView,props:{recordOnly:true}},
    {path:'/publications',component:{render:()=>null}},
    {path:'/settings',component:{render:()=>null}},{path:'/projects/:uid/steps/assemble',component:{render:() => null}},
    {path:'/projects/:uid',component:{render:()=>null}},
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
    if (this.$options.__name === 'PublishPartsEditor') { partsEditor = this.$.setupState; partsProps = this.$.props }
    if (this.$options.__name === 'PublishCollectionCreate') collection = this.$.setupState
    if (this.$options.name === 'Switch') switches.push(this)
    if (this.$options.__name === 'PublishView') {
      page = this.$.setupState
      page.catalog = {defaults:{...settings},accounts:[{id:'123',name:'测试账号',defaults:{}}],templates:[],worker_ready:true}
      page.catalogReady = true; page.catalogLoading = false; page.recordsReady = true
      page.source = {files:[]};seed(page)
    }
  }})
  return {html:await renderToString(app),page,dialogs,assistant,switches,assistantProps,form,collection,router,partsEditor,partsProps,
    dispose:()=>page.disposeWorkspace()}
}
function actionButtons(html:string) {
  return [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match=>match[0]).filter(button=>/class="[^"]*\bn-button\b/.test(button))
}
function buttonText(button:string) { return button.replace(/<[^>]*>/g,'').trim() }
function emptyEditor(html:string) { return html.slice(html.indexOf('class="editor-empty"')) }

test('空列表或只有终态记录时，明确后台执行补读本地列表并打开新记录',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  for(const previous of [[],[{...record('published'),id:'old'}]]) {
    setNavigationExecution('fixture-project',true)
    const {page,html,router}=await render(p=>{p.records=previous}),requests:string[]=[]
    assert.match(emptyEditor(html),/项目正在后台执行.*发布记录会自动刷新/s)
    assert.match(emptyEditor(html),/查看项目总览/);assert.doesNotMatch(emptyEditor(html),/为所选项目新建草稿/)
    const created={...record('published'),id:'automatic-new',receipt:{bvid:'BVnew'}}
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      assert.equal(options?.method,'GET','发现过程不创建、上传或查询平台')
      requests.push(String(url));return {ok:true,json:async()=>String(url).includes('?project_uid=')?[created,...previous]:created}
    })
    await page.poll()
    assert.equal(page.current.id,'automatic-new');assert.equal(router.currentRoute.value.query.publication,'automatic-new')
    assert.deepEqual(requests,['/api/publishing/publications?project_uid=fixture-project','/api/publishing/publications/automatic-new'])
  }
})

test('未知、空闲、归档、别的项目和已挂起导航不会触发发现读取',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  const requests:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{requests.push(String(url));throw new Error('不应发起请求')})
  for(const state of [
    ['',false,true,false],['fixture-project',false,false,false],['fixture-project',true,false,true],
    ['other',true,false,false],['fixture-project',true,true,false],
  ]) {
    setNavigationExecution(...state)
    const {page,html}=await render()
    assert.equal(page.knownProjectExecution,false);assert.doesNotMatch(emptyEditor(html),/项目正在后台执行/)
    await page.poll();assert.equal(page.current,null)
  }
  assert.deepEqual(requests,[])
})

test('执行结束与列表返回交叉时保留成功尾读，失败可重试，成功后不持续扫描',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  setNavigationExecution('fixture-project',true)
  const {page}=await render(),requests:string[]=[]
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async url=>{
    requests.push(String(url))
    if(requests.length===1)return new Promise(resolve=>{finish=resolve})
    if(requests.length===2)return {ok:false,json:async()=>({message:'暂时读取失败'})}
    const created={...record('published'),id:'tail-new'}
    return {ok:true,json:async()=>String(url).includes('?project_uid=')?[created]:created}
  })
  const activeRead=page.poll();setNavigationExecution('fixture-project',false)
  finish({ok:true,json:async()=>[]});await activeRead
  await page.poll();assert.equal(page.current,null);assert.match(page.recordsError,/暂时读取失败/);assert.equal(page.error,'')
  await page.poll();assert.equal(page.current.id,'tail-new');assert.equal(page.recordsError,'');assert.equal(page.error,'')
  await page.poll();assert.equal(requests.length,4,'成功尾读后已公开记录不再追加扫描')
})

test('后台新记录不替换已有选择、草稿、未保存设置、规则页或显式记录身份',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  const created={...record('published'),id:'new'},requests:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{
    requests.push(String(url));assert.match(String(url),/publications\?project_uid=/)
    return {ok:true,json:async()=>[created]}
  })
  for(const state of [
    {seed:(p:any)=>{const row=record();row.draft.account_id='';p.apply(row);p.draft.overrides.title='未保存标题'}},
    {seed:(p:any)=>{p.draft={...record().draft,account_id:''}}},
    {seed:(p:any)=>{p.settingsDirty=true}},
    {seed:(p:any)=>{p.reviewDirty=true}},
    {seed:(p:any)=>{p.originalUpdateDirty=true}},
    {seed:(p:any)=>{p.remoteCopyFields=['title']}},
    {seed:(p:any)=>{p.tab='rules'}},
    {seed:()=>{},path:'/projects/fixture-project/publish?publication=explicit'},
  ]) {
    setNavigationExecution('fixture-project',true)
    const {page}=await render(state.seed,state.path)
    const before=page.current,draft=page.draft,tab=page.tab,count=requests.length
    await page.poll()
    assert.equal(page.current,before);assert.equal(page.draft,draft);assert.equal(page.tab,tab)
    assert.equal(requests.length,count+1)
  }
})

test('列表或自动详情迟到时保留手动选稿、规则编辑及新项目上下文',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  const created={...record('published'),id:'new'}
  for(const change of ['manual','manual-after-detail','manual-after-detail-failure','rules','project','unmount']) {
    setNavigationExecution('fixture-project',true)
    const {page,router,dispose}=await render()
    let finishList:(value:any)=>void=()=>{},finishDetail:(value:any)=>void=()=>{}
    const requests:string[]=[]
    t.mock.method(globalThis,'fetch',async url=>{
      requests.push(String(url))
      if(String(url).includes('?project_uid='))return new Promise(resolve=>{finishList=resolve})
      if(String(url).endsWith('/manual'))return {ok:true,json:async()=>({...created,id:'manual'})}
      return new Promise(resolve=>{finishDetail=resolve})
    })
    const pending=page.poll()
    if(change==='manual')await page.open('manual')
    if(change==='project')await router.push('/projects/second/publish')
    if(change==='unmount') {
      const previous=Object.getOwnPropertyDescriptor(globalThis,'window')
      Object.defineProperty(globalThis,'window',{configurable:true,value:{removeEventListener(){}}})
      try {dispose()} finally {
        if(previous)Object.defineProperty(globalThis,'window',previous)
        else delete (globalThis as any).window
      }
    }
    finishList({ok:true,json:async()=>[created]})
    if(change==='rules'||change.startsWith('manual-after-detail')) {
      for(let i=0;i<20;i++)await Promise.resolve()
      assert.equal(requests.length,2)
      if(change==='rules') {page.tab='rules';page.settingsDirty=true}
      else {
        assert.equal(page.busy,false,'自动发现不占全页锁，手动选择能够立即接管')
        await page.open('manual')
      }
      finishDetail(change==='manual-after-detail-failure'
        ?{ok:false,json:async()=>({message:'旧自动详情读取失败'})}:{ok:true,json:async()=>created})
    }
    await pending
    assert.equal(page.current?.id||null,change.startsWith('manual')?'manual':null)
    assert.doesNotMatch(page.error,/旧自动详情读取失败/)
    if(change==='rules') {assert.equal(page.tab,'rules');assert.equal(page.settingsDirty,true)}
    if(change==='project'||change==='unmount')assert.deepEqual(page.records,[])
  }
})

test('自动发现创建瞬间的draft继续跟随后台同一记录，未保存编辑不被刷新重置',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  setNavigationExecution('fixture-project',true)
  const {page}=await render(),requests:string[]=[]
  const created={...record(),id:'automatic-draft',draft:{...record().draft,account_id:''}}
  let latest:any=created
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.equal(options?.method,'GET');requests.push(String(url))
    return {ok:true,json:async()=>String(url).includes('?project_uid=')?[latest]:String(url).endsWith('/candidates')?{files:[]}:latest}
  })
  await page.poll();assert.equal(page.current.status,'draft');assert.equal(page.current.id,created.id)
  page.draft.overrides.title='用户保留的编辑'
  latest={...record('submitted'),id:created.id}
  const before=page.current,count=requests.length
  await page.poll()
  assert.equal(page.current,before);assert.equal(page.draft.overrides.title,'用户保留的编辑')
  assert.equal(requests.length,count+1,'有未保存修改时只更新目录')
  // 恢复测试基线，继续验证回读不被创建瞬间的draft状态卡住。
  page.draft.overrides={}
  page.saved=JSON.stringify(page.draft)
  await page.poll();assert.equal(page.current.status,'submitted');assert.equal(page.current.id,created.id)
  assert.equal(requests.filter(url=>url.endsWith('/candidates')).length,1,'候选只在首次打开时读取一次')
})

test('手动选中后台draft后，目录的同身份新状态触发一次详情核对且不改选择',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  setNavigationExecution('fixture-project',true)
  const {page,router}=await render(),requests:string[]=[]
  const draft={...record(),id:'manual-draft',draft:{...record().draft,account_id:''},updated_at:'2026-10-20T00:00:00'}
  let latest:any=draft
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.equal(options?.method,'GET');requests.push(String(url))
    return {ok:true,json:async()=>String(url).includes('?project_uid=')?[latest]:String(url).endsWith('/candidates')?{files:[]}:latest}
  })
  await page.open(draft.id);assert.equal(page.current.status,'draft')
  latest={...record('published'),id:draft.id,updated_at:'2026-10-20T00:01:00Z'}
  await page.poll();assert.equal(page.current.status,'published');assert.equal(page.current.id,draft.id)
  assert.equal(router.currentRoute.value.query.publication,draft.id)
  await page.poll()
  assert.equal(requests.filter(url=>url.endsWith('/manual-draft')).length,2,'只在实际状态变化时补一次详情')
  assert.equal(requests.filter(url=>url.endsWith('/candidates')).length,1)
})

test('投稿后等待审核与合集时只读轮询直到成功，未扩大发布操作锁',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  for(const status of ['submitted','collection_pending']) {
    const {page}=await render(p=>p.apply(record(status))),requests:any[]=[]
    assert.equal(page.active,false)
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      requests.push({url:String(url),method:options?.method||'GET'})
      return {ok:true,json:async()=>({...record('published'),receipt:{bvid:'BVfixture',collection_joined:true}})}
    })
    await page.poll()
    assert.equal(page.current.status,'published');assert.equal(page.records[0].status,'published')
    assert.deepEqual(requests,[{url:'/api/publishing/publications/fixture',method:'GET'}])
  }
})

test('其他稿件待审核仍刷新列表，当前未保存草稿不被回读重置',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  const {page}=await render(p=>p.apply(record())),requests:string[]=[]
  page.draft.overrides.title='未保存的本地标题'
  const pending={...record('submitted'),id:'other'}
  page.records=[page.current,pending]
  t.mock.method(globalThis,'fetch',async(url)=>{
    requests.push(String(url))
    return {ok:true,json:async()=>[record(),{...pending,status:'published'}]}
  })
  await page.poll()
  assert.deepEqual(requests,['/api/publishing/publications?project_uid=fixture-project'])
  assert.equal(page.draft.overrides.title,'未保存的本地标题');assert.equal(page.dirty,true)
  assert.equal(page.current.status,'draft');assert.equal(page.records[1].status,'published')
})

test('轮询迟到结果不能倒退同一稿件的新写回回执',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  for(const failed of [false,true]) {
  const {page}=await render(p=>p.apply(record('submitted')))
  let finish:(value:any)=>void=()=>{}
  const response=new Promise(resolve=>{finish=resolve})
  t.mock.method(globalThis,'fetch',async()=>({ok:!failed,json:async()=>await response}))
  const polling=page.poll()
  page.apply({...record('published'),receipt:{bvid:'BVnew'}})
  finish(failed?{message:'旧回执读取失败'}:record('submitted'));await polling
  assert.equal(page.current.status,'published');assert.equal(page.current.receipt.bvid,'BVnew')
  assert.equal(page.records[0].status,'published')
  assert.doesNotMatch(page.error,/旧回执读取失败/)
  }
})

test('等待审核的回读保留原稿更新、审查和设置中未保存的调整',async t=>{
  t.mock.timers.enable({apis:['setTimeout']})
  for(const flag of ['originalUpdateDirty','originalUpdateBusy','reviewDirty','reviewBusy','settingsDirty','settingsBusy','remoteCopyBusy','remoteSyncBusy']) {
    const {page}=await render(p=>p.apply(record('submitted'))),requests:string[]=[]
    page[flag]=true
    t.mock.method(globalThis,'fetch',async(url)=>{
      requests.push(String(url));return {ok:true,json:async()=>[record('published')]}
    })
    const before=page.current;await page.poll()
    assert.equal(page.current,before);assert.equal(page[flag],true)
    assert.deepEqual(requests,['/api/publishing/publications?project_uid=fixture-project'])
  }
})

test('项目发布末步已有账号时引导新建本项目草稿，不默认创建或选择账号',async()=>{
  const {html,page}=await render()
  const empty=emptyEditor(html)
  assert.match(empty,/已保存账号：测试账号/)
  assert.match(empty,/为所选项目新建草稿/);assert.match(empty,/管理账号/)
  assert.doesNotMatch(empty,/连接B站账号|已连接/)
  assert.doesNotMatch(html,/id="source-project"/)
  assert.ok(actionButtons(empty).some(button=>buttonText(button)==='新建发布草稿'))
  assert.equal(page.current,null);assert.equal(page.draft,null);assert.equal(page.projectUid,'fixture-project')
})

test('已有发布记录优先引导打开记录，保持记录与账号选择不变',async()=>{
  const row=record('published')
  const {html,page}=await render(p=>{p.records=[row];p.projectUid='fixture-project'})
  assert.match(emptyEditor(html),/选择发布记录/)
  assert.ok(actionButtons(emptyEditor(html)).some(button=>buttonText(button)==='选择发布记录'))
  assert.equal(page.current,null);assert.equal(page.draft,null);assert.deepEqual(page.records,[row])
})

test('账号加载中、尚未读取、读取失败都不误判未连接或发布组件未就绪',async()=>{
  for(const mode of ['loading','unread','failed']) {
    const {html,page}=await render(p=>{
      p.catalog={accounts:[],templates:[],defaults:{}}
      p.catalogReady=false;p.catalogLoading=mode==='loading';p.catalogError=mode==='failed'?'网络不可用':''
    })
    assert.doesNotMatch(html,/尚未保存B站账号|先连接B站账号|发布组件尚未就绪/)
    assert.match(emptyEditor(html),/重新读取账号/)
    if(mode==='failed')assert.match(emptyEditor(html),/读取失败不代表账号已退出登录/)
    else assert.match(emptyEditor(html),/正在读取发布资料/)
    assert.equal(page.current,null)
  }
  const stale=await render(p=>{p.catalogError='读取超时'})
  assert.match(emptyEditor(stale.html),/已保存账号：测试账号/)
  assert.doesNotMatch(emptyEditor(stale.html),/连接B站账号/)
})

test('成功读取零账号才引导连接；前往设置账号管理仍受未保存与忙态保护',async()=>{
  const {html,page,router}=await render(p=>{p.catalog.accounts=[]})
  assert.match(emptyEditor(html),/先连接B站账号/)
  assert.ok(actionButtons(emptyEditor(html)).some(button=>buttonText(button)==='连接B站账号'))
  page.settingsDirty=true;await page.manageAccount();assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  page.settingsDirty=false;page.busy=true;await page.manageAccount();assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  page.busy=false;await page.manageAccount()
  assert.equal(router.currentRoute.value.path,'/settings');assert.equal(router.currentRoute.value.query.section,'bilibili')
  assert.equal(page.current,null)
})

test('发布记录加载与读取失败单独呈现，不声称还没有记录也不要求重新连接账号',async()=>{
  for(const mode of ['loading','failed']) {
    const {html}=await render(p=>{p.recordsReady=false;p.recordsLoading=mode==='loading';p.recordsError=mode==='failed'?'本地资料读取失败':''})
    assert.doesNotMatch(html,/还没有发布记录/)
    assert.doesNotMatch(emptyEditor(html),/连接B站账号/)
    assert.match(emptyEditor(html),mode==='failed'?/发布记录读取失败/:/正在读取发布记录/)
  }
})

test('已读取的发布记录后台刷新时保留列表与选择引导，不插入加载占位',async()=>{
  const {html,page}=await render(p=>{p.records=[record('uploading')];p.recordsLoading=true})
  const list=html.slice(html.indexOf('id="publication-records"'),html.indexOf('class="editor-empty"'))
  assert.equal(page.recordsLoading,true);assert.equal(page.recordsInitialLoading,false)
  assert.match(list,/publication-item/);assert.doesNotMatch(list,/正在读取发布记录/)
  assert.match(emptyEditor(html),/选择发布记录/)
  assert.doesNotMatch(emptyEditor(html),/正在读取发布记录/)
})

test('已确认没有记录时后台刷新保留空态和新建草稿引导',async()=>{
  const {html,page}=await render(p=>{p.recordsLoading=true})
  assert.equal(page.recordsInitialLoading,false)
  assert.match(html,/还没有发布记录。新建草稿不会上传视频。/)
  assert.match(emptyEditor(html),/为所选项目新建草稿/)
  assert.doesNotMatch(html,/正在读取发布记录/)
})

test('原稿进度刷新保留旧卡片直到新数据到达，失败仍可重试且不丢选择',async t=>{
  const row:any=record('published');row.receipt={bvid:'BV1234567890'}
  row.latest_update={id:'update-fixture',status:'uploading'}
  const {page}=await render(p=>p.apply(row))
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    assert.equal(String(url),'/api/publishing/publications?project_uid=fixture-project')
    assert.equal(options?.method,'GET')
    return new Promise((resolve,reject)=>requests.push({resolve,reject}))
  })
  const failed=page.originalUpdateChanged()
  assert.equal(page.recordsLoading,true);assert.equal(page.recordsInitialLoading,false)
  assert.equal(page.records[0].latest_update.status,'uploading');assert.equal(page.current.id,row.id)
  requests[0].reject(new Error('网络暂时不可用'));await failed
  assert.equal(page.recordsLoading,false);assert.equal(page.recordsReady,true)
  assert.equal(page.records[0].latest_update.status,'uploading');assert.match(page.recordsError,/网络暂时不可用/)
  const refreshed=page.originalUpdateChanged()
  assert.equal(page.recordsLoading,true);assert.equal(page.recordsInitialLoading,false)
  requests[1].resolve({ok:true,json:async()=>[{...row,revision:4,latest_update:{...row.latest_update,status:'completed'}}]})
  await refreshed
  assert.equal(page.records[0].latest_update.status,'completed');assert.equal(page.current.latest_update.status,'completed')
  assert.equal(page.current.id,row.id);assert.equal(page.recordsError,'');assert.equal(page.recordsLoading,false)
})

test('草稿账号选择正确引导已有账号，前往账号设置保持未保存保护',async()=>{
  const row=record();row.draft.account_id=''
  const {html,page,router}=await render(p=>p.apply(row))
  assert.match(html,/选择发布账号/);assert.doesNotMatch(html,/先在账号页连接B站/)
  assert.equal(page.draft.account_id,'');assert.equal(page.dirty,false)
  page.change('title','保留未保存文案');await page.manageAccount()
  assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  assert.equal(page.tab,'drafts');assert.equal(page.draft.account_id,'')
})

test('重新读取账号去重，失败保留旧列表与草稿，成功清除读取错误不擅自选账号',async t=>{
  const row=record();row.draft.account_id=''
  const {page}=await render(p=>p.apply(row))
  let finish:(value:any)=>void=()=>{};const calls:string[]=[]
  t.mock.method(globalThis,'fetch',url=>{calls.push(String(url));return new Promise(resolve=>{finish=resolve})})
  const first=page.refreshCatalog();assert.equal(page.catalogLoading,true)
  await page.refreshCatalog();assert.equal(calls.length,1)
  finish({ok:false,json:async()=>({message:'账号读取超时'})});await first
  assert.equal(page.catalogLoading,false);assert.equal(page.catalogReady,true)
  assert.equal(page.catalogError,'账号读取超时');assert.equal(page.catalog.accounts[0].name,'测试账号')
  assert.equal(page.draft.account_id,'');assert.equal(page.dirty,false);assert.equal(page.error,'')
  const second=page.refreshCatalog()
  finish({ok:true,json:async()=>({accounts:[{id:'456',name:'另一个账号'}],templates:[],defaults:{},worker_ready:true})});await second
  assert.equal(page.catalogError,'');assert.equal(page.catalog.accounts[0].id,'456')
  assert.equal(page.draft.account_id,'');assert.equal(page.current.id,'fixture')
  assert.deepEqual(calls,['/api/publishing/catalog','/api/publishing/catalog'])
})

test('读取失败不当成零账号，重新读取发布记录保留当前编辑且不触发创建上传',async t=>{
  const {page}=await render(p=>p.apply(record()))
  page.change('title','保留待保存标题')
  const calls:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{calls.push(String(url));return {ok:true,json:async()=>({})}})
  await page.refreshCatalog()
  assert.match(page.catalogError,/账号列表格式无效/);assert.equal(page.catalog.accounts[0].id,'123')
  assert.equal(page.draft.overrides.title,'保留待保存标题');assert.equal(page.dirty,true)
  t.mock.method(globalThis,'fetch',async url=>{calls.push(String(url));return {ok:false,json:async()=>({message:'本地列表暂时不可用'})}})
  await page.rereadRecords();assert.equal(page.recordsError,'本地列表暂时不可用')
  t.mock.method(globalThis,'fetch',async url=>{calls.push(String(url));return {ok:true,json:async()=>[record()]}})
  await page.rereadRecords();assert.equal(page.recordsError,'');assert.equal(page.recordsReady,true)
  assert.equal(page.draft.overrides.title,'保留待保存标题');assert.equal(page.dirty,true)
  assert.ok(calls.every(path=>path.endsWith('/catalog')||path.split('?')[0].endsWith('/publications')))
})

test('新空状态复用本地草稿创建，忙态与未保存保护阻止重复或迟到修改',async t=>{
  const {page,router}=await render(p=>{p.projectUid='fixture-project'})
  const calls:{url:string;method:any}[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);calls.push({url:path,method:options?.method})
    if(path.endsWith('/drafts'))return {ok:true,json:async()=>({id:'fixture'})}
    if(path.endsWith('/candidates'))return {ok:true,json:async()=>({files:[]})}
    if(path.endsWith('/publications/fixture'))return {ok:true,json:async()=>record()}
    if(path.split('?')[0].endsWith('/publications'))return {ok:true,json:async()=>[record()]}
    throw new Error(`不应调用上传或投稿：${path}`)
  })
  page.settingsDirty=true;await page.create();assert.equal(calls.length,0)
  page.settingsDirty=false;page.busy=true;await page.create();assert.equal(calls.length,0)
  page.busy=false;await page.create();await flush()
  assert.equal(page.current.id,'fixture');assert.equal(page.current.status,'draft')
  assert.equal(router.currentRoute.value.query.publication,'fixture')
  assert.deepEqual(calls.filter(call=>call.method==='POST'),[{url:'/api/publishing/projects/fixture-project/drafts',method:'POST'}])
})

test('实际发布页面渲染：未选任何成片，上传按钮禁用且没有隐式默认', async () => {
  const {html,page} = await render(p => p.apply(record()))
  assert.match(html, /尚未加入视频。从下方选择一个文件，即成为 P1/)
  assert.deepEqual(page.draft.parts, [])
  const buttons = actionButtons(html)
  for (const text of ['仅上传文件', '上传并投稿']) {
    const button = buttons.find(b => buttonText(b)===text)
    assert.ok(button)
    assert.match(button, /disabled/)
  }
  assert.ok(!buttons.some(button=>buttonText(button)==='确认投稿'))
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

test('修订失效展示具体变更并提供合成入口，重新检查文字与生成成片明确区分', async () => {
  const {html,page}=await render(p=>{
    const row=record();row.draft.parts=[{artifact_id:11,title:'审查修订版'}];row.draft.cover_id='fixture-cover';p.apply(row)
    p.source={files:[{id:11,name:'final_version_review.mp4',exists:true,size:4096,review_ready:false,review_status:'审查修订已失效',
      review_reason:'旧通用原因',review_revision_reason:'人工屏蔽时段已变化',
      review_revision_reasons:[{code:'review_config_changed',message:'人工屏蔽时段已变化',
        changes:[{label:'人工屏蔽时段',before:'96.48–133.00 秒',after:'无人工时段'},
          {label:'模式说明',before:'<script>旧设置</script>',after:'仅屏蔽语音，保留背景'}]}]}]}
  })
  assert.match(html,/审查修订已失效/);assert.match(html,/查看修订失效原因/)
  assert.match(html,/生成时：96.48–133.00 秒/);assert.match(html,/当前：无人工时段/)
  assert.match(html,/&lt;script&gt;旧设置&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>旧设置|旧通用原因/)
  assert.match(html,/href="\/projects\/fixture-project\/steps\/assemble"[^>]*>去合成重新生成/)
  assert.match(html,/「重新检查文字」只更新报告/)
  assert.match(html,/合成页勾选需要发布的版本并生成成片/)
  assert.match(html,/完成后返回「刷新视频列表」，核对并选用有效文件/)
  assert.match(html,/自动发布遇到选中的失效文件会停止并提示/)
  assert.equal(page.selectedNeedsReview,true);assert.equal(page.submissionBlocked,true)
  for(const text of ['仅上传文件','上传并投稿'])assert.match(actionButtons(html).find(button=>buttonText(button)===text)!,/disabled/)
  assert.deepEqual(page.draft.parts,[{artifact_id:11,title:'审查修订版'}])
})

test('新成片出现后刷新保留旧分P引用，仍须明确选用有效文件', async t => {
  const old={id:11,name:'final_version_review.mp4',exists:true,review_ready:false,review_status:'审查修订已失效'}
  const fresh={id:12,name:'final_version_current.mp4',exists:true,review_ready:true,version_name:'当前规则成片'}
  const {page}=await render(p=>{
    const row=record();row.draft.parts=[{artifact_id:11,title:'审查修订版'}];row.draft.cover_id='fixture-cover';p.apply(row)
    p.source={files:[old]}
  })
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.match(String(url),/\/projects\/fixture-project\/candidates$/);assert.equal(options?.method,'GET')
    return{ok:true,json:async()=>({files:[old,fresh]})}
  })
  await page.refreshFiles()
  assert.equal(page.selectedNeedsReview,true);assert.equal(page.submissionBlocked,true)
  assert.deepEqual(page.draft.parts,[{artifact_id:11,title:'审查修订版'}])
  page.removePart(0);page.addPart(fresh,true)
  assert.equal(page.selectedNeedsReview,false);assert.equal(page.submissionBlocked,false)
  assert.deepEqual(page.draft.parts,[{artifact_id:12,title:'当前规则成片'}])
  assert.equal(page.dirty,true)
})

test('候选列表区分已处理、零命中和关闭审查，旧接口缺字段保持可用', async () => {
  const {html}=await render(p=>{
    p.apply(record())
    p.source={files:[
      {id:1,name:'final_dub_sub.mp4',exists:true,review_ready:true,review_status:'字幕已处理·声音未静音'},
      {id:2,name:'final_src_sub.mp4',exists:true,review_ready:null,review_status:'未命中本地规则',review_reason:'文本规则未命中，仍需核对画面'},
      {id:3,name:'final_src.mp4',exists:true,review_ready:null,review_status:'审查关闭',review_reason:'本项目关闭了自动屏蔽'},
      {id:4,name:'final_dub.mp4',exists:true},
    ]}
  })
  assert.match(html,/字幕已处理·声音未静音/);assert.match(html,/未命中本地规则/);assert.match(html,/审查关闭/)
  assert.doesNotMatch(html,/已按当前规则处理/)
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
    if (mode === 'locked') {
      assert.equal(form, undefined)
      page.change('description', '冻结稿件不得补入日期')
    } else form.insertPublishedDate()
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
  const {html,page,router}=await render(p=>p.apply(row))
  const stages=['publish-video','publish-metadata','publish-confirm','publish-receipt'].map(id=>html.indexOf(`id="${id}"`))
  assert.ok(stages.every((position,index)=>position>=0 && (!index || position>stages[index-1])))
  const buttons=actionButtons(html),main=buttons.find(button=>buttonText(button)==='上传并投稿')
  assert.ok(main);assert.match(main,/n-button--primary-type/);assert.match(main,/n-button--large-type/)
  assert.ok(buttons.findIndex(button=>buttonText(button)==='仅上传文件')>buttons.indexOf(main))
  assert.ok(buttons.some(button=>buttonText(button)==='保存资料'))
  assert.match(html,/确认一次后自动上传视频、封面并投稿/)
  assert.ok(html.indexOf('请确保素材可用于本次发布')<html.indexOf('class="primary-publish-action"'))
  page.change('title','未保存标题');await page.manageAccount(true)
  assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  page.apply(row);await page.manageAccount(true)
  assert.equal(router.currentRoute.value.path,'/settings')
  assert.equal(router.currentRoute.value.query.section,'bilibili')
  assert.equal(router.currentRoute.value.query.account,'123')
  assert.equal(router.currentRoute.value.query.manage,'studio')
})

test('已有BV把更新原稿与另发新稿放入独立任务，未知和活动记录不提供更新入口',async()=>{
  for(const status of ['rejected','published']) {
    const {html,page}=await render(p=>p.apply(reviewRecord(status)))
    assert.equal(actionButtons(html).filter(button=>buttonText(button)==='使用审查修订版新建草稿').length,1)
    assert.match(html,/发布后得到新BV/);assert.match(html,/到官网替换原BV视频/)
    assert.match(html,/https:\/\/member.bilibili.com\/platform\/home/)
    assert.ok(page.workspace.tasks.some((task:any)=>task.key==='update'))
    assert.ok(page.workspace.tasks.some((task:any)=>task.key==='more'))
    assert.equal(page.selectedTask,status==='rejected'?'update':'status')
    assert.match(html,/data-result-surface="more"/)
    assert.match(html, /aria-label="更新原稿件（保留原BV）"/)
    assert.ok(actionButtons(html).some(button=>buttonText(button)==='设置项目交付目标…'))
    assert.ok(actionButtons(html).some(button=>buttonText(button)==='选择现有成片与资料'))
  }
  for(const status of ['unknown','uploading','draft']) {
    const {html,page}=await render(p=>p.apply(reviewRecord(status)))
    assert.ok(!actionButtons(html).some(button=>buttonText(button)==='使用审查修订版新建草稿'))
    assert.ok(!page.workspace.tasks.some((task:any)=>task.key==='update'))
  }
  const {html:live,page}=await render(p=>p.apply({...reviewRecord('uploading'),progress:25,progress_detail:{phase:'uploading',loaded:250,total:1000,current_part:1,completed_parts:0,total_parts:2}}))
  assert.equal(page.selectedTask,'status')
  assert.equal((live.match(/data-publication-progress/g)||[]).length,1)
  assert.ok(actionButtons(live).some(button=>buttonText(button)==='暂停上传'))
})

test('已有空草稿保持原状，退回记录须由用户打开，账号与原BV编辑迁至设置',async()=>{
  const current:any=record();current.id='existing-user-empty'
  const old=reviewRecord()
  const {page,html}=await render(p=>{p.apply(current);p.records=[current,old]})
  assert.equal(page.current.id,'existing-user-empty');assert.deepEqual(page.draft.parts,[])
  assert.match(html,/平台稿件与回执保留/);assert.match(html,/打开原记录 → 更新原稿件（保留原BV）/)
  assert.ok(!actionButtons(html).some(button=>buttonText(button)==='使用审查修订版新建草稿'))
  assert.doesNotMatch(html,/tab="管理账号"|tab="编辑原BV"/)
  assert.ok(actionButtons(html).some(button=>buttonText(button)==='管理账号'))
})
test('退回稿件章节pending允许更新原稿件，只有正在执行或未知结果才禁用并说明原因', async () => {
  for (const status of ['pending', 'running', 'unknown']) {
    const row = reviewRecord(); row.chapter_sync = { status, enabled: true, phase: 'waiting_review' }
    const { page, html } = await render(p => p.apply(row))
    const button = actionButtons(html).find(item => buttonText(item) === '选择现有成片与资料') || ''
    assert.ok(button)
    if (status === 'pending') {
      assert.doesNotMatch(button, /\sdisabled(?:=|\s|>)/)
      assert.equal(page.originalUpdateBlockReason, '')
    } else {
      assert.match(button, /\sdisabled(?:=|\s|>)/)
      assert.match(page.originalUpdateBlockReason, status === 'running' ? /原视频章节正在处理/ : /上次章节写入结果待核对/)
      assert.ok(html.includes(page.originalUpdateBlockReason))
    }
  }
})
test('单独文案与章节作为独立任务，原同步锚点切到该任务而不丢弃更新选择', async t => {
  const { page, html } = await render(p => p.apply(reviewRecord()))
  assert.equal(page.selectedTask,'update')
  assert.ok(page.workspace.tasks.some((task:any)=>task.key==='sync'))
  assert.match(html,/id="publish-remote-sync"/); assert.match(html,/载入已保存项目文案/)
  assert.match(html,/aria-label="B站视频章节"/)
  const oldDocument=(globalThis as any).document, positions:string[]=[]
  ;(globalThis as any).document={querySelector:()=>null,getElementById:(id:string)=>({scrollIntoView:()=>positions.push(id)})}
  t.after(async()=>{
    // 任务选择会在下一次渲染后定位，清空异步回调再撤销完整的 DOM 替身。
    await nextTick();await flush()
    if(oldDocument===undefined)delete (globalThis as any).document
    else (globalThis as any).document=oldDocument
  })
  page.originalUpdateDirty=true
  let prevented=false
  page.revealPublicationAnchor({target:{closest:(selector:string)=>selector==='a[href]'?{getAttribute:()=> '#publish-remote-sync'}:null},preventDefault:()=>{prevented=true}})
  await nextTick()
  assert.equal(page.selectedTask,'sync');assert.equal(page.originalUpdateDirty,true)
  assert.equal(prevented,true);assert.deepEqual(positions,['publish-remote-sync'])
  page.selectTask('update');assert.equal(page.selectedTask,'update')
  const direct = await render(p=>p.apply(reviewRecord()),'/projects/fixture-project/publish#publish-remote-sync')
  assert.equal(direct.page.selectedTask,'sync')
})
test('原稿更新的准备修改需要离开确认，已交后台执行的任务允许离开且不会误锁整页', async () => {
  const {page,dialogs} = await render(p => p.apply(reviewRecord()))
  page.originalUpdateBusy = true
  assert.equal(page.canSwitch(), false); assert.equal(page.confirmLeave(), false)
  page.originalUpdateBusy = false; page.originalUpdateDirty = true
  assert.equal(page.canSwitch(), false)
  const leave = page.confirmLeave(); assert.equal(dialogs.length, 1)
  dialogs[0].onPositiveClick(); assert.equal(await leave, true)
  page.originalUpdateDirty = false; page.originalUpdateActive = true
  assert.equal(page.canSwitch(), true); assert.equal(page.confirmLeave(), true)
  page.applyRemoteCopy({title:'迟到的候选'}); assert.deepEqual(page.remoteCopyFields, [])
  await page.loadSavedProjectCopy(); assert.equal(page.remoteProjectCopyLoading, false)
})

test('发布记录的项目交付入口回本页发布设置，未保存或活动更新不能绕过',async()=>{
  const {page,router}=await render(p=>p.apply(reviewRecord()))
  page.originalUpdateActive=true;await page.openAutomaticOriginalUpdate();assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  page.originalUpdateActive=false;page.originalUpdateDirty=true;await page.openAutomaticOriginalUpdate();assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish')
  page.originalUpdateDirty=false;await page.openAutomaticOriginalUpdate()
  assert.equal(router.currentRoute.value.path,'/projects/fixture-project/publish');assert.equal(router.currentRoute.value.hash,'#project-rules')
  assert.equal(router.currentRoute.value.query.run,undefined);assert.equal(page.tab,'rules')
  assert.equal(router.currentRoute.value.query.publication,'fixture')
})
test('原稿更新状态刷新只读取本地记录并更新平台观察值，保留历史冻结资料', async t => {
  const source = reviewRecord(), updated = {...source, status:'submitted', revision:source.revision+1,
    receipt:{...source.receipt,state_desc:'正在审核'}, latest_update:{id:'update-1',status:'waiting'}}
  const {page} = await render(p => { p.apply(source); p.records=[source] }), requests:any[]=[]
  const historical = JSON.stringify(page.current.snapshot)
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),method:options?.method || 'GET'})
    return {ok:true,json:async()=>[updated]}
  })
  await page.originalUpdateChanged()
  assert.equal(page.current.status,'submitted'); assert.equal(page.current.receipt.state_desc,'正在审核')
  assert.equal(JSON.stringify(page.current.snapshot),historical)
  assert.ok(requests.length===1 && requests.every(request=>request.method==='GET'))
})
test('回收与归档详情只读，恢复入口保留，不能继续发布或修改视频',async()=>{
  for(const lifecycle of ['deleted','archived']) {
    const current:any=record();current.lifecycle=lifecycle;current.lifecycle_actions={restore:{allowed:true,reason:''}}
    const {page,html}=await render(p=>p.apply(current))
    assert.equal(page.recordInactive,true);assert.equal(page.locked,true);assert.equal(page.editableMetadata,false);assert.equal(page.actionable,false)
    assert.match(html,/恢复记录/);assert.match(html,/B站稿件没有删除/);assert.match(html,/已保存的草稿内容/)
    assert.ok(!actionButtons(html).some(button=>['上传并投稿','保存资料','继续上传并投稿'].includes(buttonText(button))))
    assert.doesNotMatch(html,/id="publish-confirm"|class="review-workspace"/)
  }
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
  assert.match(confirmation,/保留原BV替换视频，请取消本次新稿准备，使用本页「更新原稿件」/)
  assert.doesNotMatch(confirmation,/保留原BV替换视频请在B站创作中心操作/)
  assert.doesNotMatch(confirmation,/这会创建新投稿并获得新BV|创建草稿只保存到本地|本版创作管理可修改文案/)
  assert.match(confirmation,/需要另行核对画面，不能保证平台审核通过/)
  assert.equal((confirmation.match(/<p>/g)||[]).length,3,'摘要一段，并保留画面局限和新增警告')
  assert.match(confirmation,/P1 · 原声版[\s\S]*old-src\.mp4[\s\S]*原声修订版[\s\S]*P2 · 配音版[\s\S]*old-dub\.mp4[\s\S]*配音修订版/)
  assert.match(confirmation,/&lt;script&gt;提醒&lt;\/script&gt;/);assert.doesNotMatch(confirmation,/<script>提醒/)
  dialogs[0].onAfterLeave();assert.equal(page.confirming,false);assert.equal(page.current.id,'fixture')
})

test('确认修订草稿只发送一次版本化本地创建，保留旧记录且不调用任何上传动作',async t=>{
  const {page,dialogs,router}=await render(p=>p.apply(reviewRecord()))
  const calls:any[]=[]
  let release:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);calls.push({url:path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):null})
    if(path.endsWith('/review-draft/plan'))return {ok:true,json:async()=>reviewPlan()}
    if(path.endsWith('/review-draft'))return new Promise(resolve=>{release=resolve})
    if(path.split('?')[0].endsWith('/publications'))return {ok:true,json:async()=>[reviewDraft(),reviewRecord()]}
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
  assert.equal(router.currentRoute.value.query.publication,'review-local')
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
  // 渲染已有BV时章节区会执行只读核对；它不属于创建或上传动作。
  assert.ok(calls.every(url=>url.includes('/review-draft')||url==='/api/publishing/accounts/123/studio/read/chapters'),JSON.stringify(calls))
})

test('发布页的成片编辑入口始终指向当前记录，不随其他来源选择改变',async()=>{
  const {html}=await render(p=>{p.apply(record());p.projectUid='different-new-project'})
  assert.match(html,/<a href="\/projects\/fixture-project\/steps\/assemble"[^>]*>修改成片版本效果/)
  assert.match(html,/<nav[^>]*aria-label="工作流阶段导航"[^>]*>[\s\S]*?<a href="\/projects\/fixture-project\/steps\/assemble"[^>]*>上一步 · 合成成片<\/a>/)
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
    if (String(url).split('?')[0].endsWith('/publications')) return {ok:true,json:async () => []}
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
    if(path.split('?')[0].endsWith('/publications'))return new Promise((resolve,reject)=>lists.push({resolve,reject}))
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
  const {page,router} = await render(p => p.apply(record('uploaded')))
  const source = page.source
  const requests:any[] = []
  let completeCopy:any, completeList:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url)
    requests.push({path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):undefined})
    if(path.endsWith('/copy')) return new Promise(resolve=>{completeCopy=resolve})
    if(path.split('?')[0].endsWith('/publications')) return new Promise(resolve=>{completeList=resolve})
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
  // 地址同步也会经过路由异步队列；让本轮微任务走完，但列表请求仍保持未返回。
  await new Promise<void>(resolve=>setImmediate(resolve))
  try {
    assert.equal(settled,true,'后台队列列表慢不拖住复制操作')
    assert.equal(page.busy,false);assert.equal(page.copying,false)
    assert.equal(page.current.id,'copied-fixture')
    assert.equal(router.currentRoute.value.query.publication,'copied-fixture')
    assert.equal(page.current.status,'draft');assert.equal(page.dirty,false)
    assert.deepEqual(page.resolved.values,settings)
    assert.equal(page.source,source,'同项目候选文件不重复扫描')
    assert.deepEqual(requests.map(r=>r.path),['/api/publishing/publications/fixture/copy','/api/publishing/publications?project_uid=fixture-project'])
    page.change('title','复制后继续编辑的标题')
    assert.equal(page.dirty,true)
    completeList({ok:false,json:async()=>({message:'隔离慢列表失败'})})
    for(let i=0;i<24;i++) await Promise.resolve()
    assert.equal(page.error,'','刷新失败不误报为复制失败')
    assert.equal(page.current.id,'copied-fixture')
    assert.equal(page.displayValues.title,'复制后继续编辑的标题')
    assert.equal(page.dirty,true)
  } finally {
    completeList({ok:true,json:async()=>[copied]})
    await finished
  }
})

test('复制响应丢失后重试复用请求标识，源资料变化时建立新请求', async t => {
  const {page} = await render(p=>p.apply(record('uploaded')))
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).split('?')[0].endsWith('/publications'))return {ok:true,json:async()=>[page.current]}
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
    if (String(url).split('?')[0].endsWith('/publications')) return {ok:true,json:async () => []}
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
    if (String(url).split('?')[0].endsWith('/publications')) return {ok:true,json:async () => []}
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
      if (String(url).split('?')[0].endsWith('/publications')) return {ok:true,json:async () => []}
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

test('项目设置独立保护未保存修改与生成中的候选', async () => {
  const {page} = await render(p => { p.settingsDirty = true; p.tab = 'rules' })
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

test('稿件候选生成立即保护保存发布与离开，SSR不额外读取权限', async t => {
  const {page,assistantProps} = await render(p => p.apply(record()))
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async url => {
    calls.push(String(url)); return {ok:true,json:async () => ({locked:false})}
  })
  page.copyBusyChanged(false); assert.equal(calls.length, 0)
  page.copyBusyChanged(true)
  assert.equal(page.canSwitch(), false); assert.equal(await page.confirmLeave(), false)
  assert.equal(assistantProps.disabled, false, '助手自身不会因copyBusy而自锁')
  page.change('title', '生成期间人工修改')
  await page.save(); await page.upload(); await page.submit()
  assert.equal(calls.length, 0)
  let blocked = false
  const event = {preventDefault() { blocked = true }, returnValue: undefined as any}
  page.beforeUnload(event); assert.equal(blocked, true); assert.equal(event.returnValue, '')
  page.copyBusyChanged(false); assert.equal(calls.length, 0, 'SSR不启动状态网络')
})

test('稿件候选结束只在busy真到假时刷新权限，取消后仍服从后台锁', async t => {
  const {page} = await render(p => p.apply(record()))
  const reads: string[] = []
  t.mock.method(globalThis, 'fetch', async url => {
    reads.push(String(url)); return {ok:true,json:async () => ({locked:true,reason:'稿件发布文案正在生成'})}
  })
  // 模拟客户端已挂载观察权限，直接调用真实父页面事件处理函数。
  page.editorObserving = true
  try {
    page.copyBusyChanged(false); page.copyBusyChanged(true); assert.equal(reads.length, 0)
    page.copyBusyChanged(false)
    for (let i = 0; i < 16; i++) await Promise.resolve()
    assert.deepEqual(reads, ['/api/configuration/project%3Afixture-project/editability'])
    assert.equal(page.projectLocked, true); assert.equal(page.metadataLocked, true)
    page.copyBusyChanged(false); assert.equal(reads.length, 1)
  } finally { page.editorObserving = false }
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
  assert.match(page.coverOperation,/正在读取并检查平台封面/)
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

test('确认发布前具名显示预检等待，重复点击不重复检查或提前投稿',async t=>{
  const {page,dialogs}=await render(p=>p.apply(record()))
  let release:(value:any)=>void=()=>{}
  const requests:string[]=[]
  t.mock.method(globalThis,'fetch',(url:any)=>{requests.push(String(url));return new Promise(resolve=>{release=resolve})})
  const pending=page.upload(true)
  assert.equal(page.preflighting,true);assert.match(page.preparationHint,/正在执行发布预检.*账号.*视频文件.*投稿资料/)
  await page.upload(true);assert.equal(requests.length,1)
  release({ok:true,json:async()=>({snapshot:record('uploaded').snapshot,revision:3,fingerprint:'fixed-plan'})})
  await pending
  assert.equal(page.preflighting,false);assert.equal(dialogs.length,1)
  assert.deepEqual(requests,['/api/publishing/publications/fixture/plan'])
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

test('从全部记录打开指定历史稿件，优先读取 URL 所选记录而非最新一条',async t=>{
  const latest={...record(),id:'latest-local'}
  const selected={...record('published'),id:'older-local',receipt:{bvid:'BV1234567890'}}
  const {page}=await render(p=>{p.records=[latest]},'/projects/fixture-project/publish?publication=older-local')
  const requests:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{
    const path=String(url);requests.push(path)
    if(path==='/api/publishing/publications/older-local')return {ok:true,json:async()=>selected}
    throw new Error(`不应读取其他稿件或执行发布：${path}`)
  })
  await page.openRequestedRecord()
  assert.equal(page.current.id,'older-local');assert.equal(page.current.receipt.bvid,'BV1234567890')
  assert.deepEqual(requests,['/api/publishing/publications/older-local'])
  assert.equal(page.source,null)
  assert.equal(page.records.find((row:any)=>row.id==='latest-local').status,'draft')
})

test('项目发布入口拒绝其他项目的指定记录，避免把稿件或成片绑定错项目',async t=>{
  const {page}=await render(()=>{},'/projects/fixture-project/publish?publication=foreign-local')
  const requests:string[]=[]
  t.mock.method(globalThis,'fetch',async url=>{
    requests.push(String(url));return {ok:true,json:async()=>({...record(),id:'foreign-local',project_uid:'another-project'})}
  })
  await page.openRequestedRecord()
  assert.equal(page.current,null);assert.equal(page.draft,null);assert.deepEqual(page.records,[])
  assert.match(page.error,/属于其他项目/)
  assert.deepEqual(requests,['/api/publishing/publications/foreign-local'])
})

test('已删除项目保留的记录展示快照与回执，不提供新稿编辑上传或复制，仅允许主动核对',async t=>{
  const archived=reviewRecord('published')
  const {page,html,dialogs}=await render(p=>p.apply(archived),'/publications/fixture')
  assert.match(html,/class="frozen-content"/);assert.match(html,/河内美食/);assert.match(html,/发布简介/)
  assert.match(html,/BV1234567890/);assert.match(html,/来源项目暂不可用/)
  assert.match(html,/href="\/settings\?section=bilibili&amp;account=123&amp;manage=studio"/)
  assert.doesNotMatch(html,/id="publish-confirm"|aria-label="发布准备步骤"|<input\b|<textarea\b/)
  assert.ok(html.indexOf('id="publish-receipt"')<html.indexOf('id="confirmed-content"'))
  assert.ok(!actionButtons(html).some(button=>/新建发布草稿|上传并投稿|复制为新草稿|保存资料|使用审查修订版新建草稿/.test(buttonText(button))))
  const draftBefore=JSON.stringify(page.draft),requests:{url:string;method:any;body:any}[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);requests.push({url:path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):undefined})
    if(path==='/api/publishing/publications/fixture/actions')return {ok:true,json:async()=>({...archived,revision:4})}
    if(path==='/api/publishing/publications/fixture')return {ok:true,json:async()=>({...archived,revision:4})}
    throw new Error(`保留记录不应发起制作或重新发布：${path}`)
  })
  page.projectUid='fixture-project';page.change('title','不应修改');page.inherit('title')
  page.addPart({id:99,name:'other.mp4'},true);page.selectAccount('456')
  await page.create();await page.save();await page.upload(true);await page.submit();await page.copy()
  await page.createReviewDraft();await page.importCover();await page.action('publish');await page.action('collection')
  assert.equal(JSON.stringify(page.draft),draftBefore);assert.deepEqual(requests,[]);assert.deepEqual(dialogs,[])
  await page.action('check');await flush()
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {url:'/api/publishing/publications/fixture/actions',method:'POST',body:{action:'check',revision:3}},
  ])
  assert.equal(page.current.revision,4);assert.equal(page.current.receipt.bvid,'BV1234567890')
})

test('本地草稿创建成功但详情和列表读取失败时保留记录，可直接重新打开且不再创建',async t=>{
  const created={...record(),id:'created-draft'}
  const {page}=await render()
  const requests:{url:string;method:any}[]=[]
  let detailsAvailable=false
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);requests.push({url:path,method:options?.method})
    if(path==='/api/publishing/projects/fixture-project/drafts')return {ok:true,json:async()=>created}
    if(path==='/api/publishing/publications?project_uid=fixture-project')return {ok:false,json:async()=>({message:'记录列表读取暂不可用'})}
    if(path==='/api/publishing/publications/created-draft')return detailsAvailable
      ?{ok:true,json:async()=>created}:{ok:false,json:async()=>({message:'草稿详情读取暂不可用'})}
    if(path==='/api/publishing/projects/fixture-project/candidates')return {ok:true,json:async()=>({files:[]})}
    throw new Error(`创建本地草稿不应触发其他写入：${path}`)
  })
  await page.create();await flush()
  assert.equal(page.current,null);assert.equal(page.recordsReady,true);assert.equal(page.recordsLoading,false)
  assert.equal(page.records.length,1);assert.equal(page.records[0].id,'created-draft')
  assert.match(page.error,/草稿已创建.*从左侧记录重新打开.*草稿详情读取暂不可用/)
  assert.match(page.recordsError,/记录列表读取暂不可用/)
  assert.equal(page.busy,false)
  detailsAvailable=true;await page.open('created-draft')
  assert.equal(page.current.id,'created-draft');assert.equal(page.draft.account_id,'123');assert.equal(page.dirty,false)
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {url:'/api/publishing/projects/fixture-project/drafts',method:'POST'},
  ])
})

test('新草稿成片列表读取失败仍保留编辑，重复点击只创建一次，刷新文件后保留用户修改',async t=>{
  const created={...record(),id:'created-draft'}
  const {page,router}=await render()
  const requests:{url:string;method:any}[]=[]
  let releaseCandidates:(value:any)=>void=()=>{}, candidateReady=false
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);requests.push({url:path,method:options?.method})
    if(path==='/api/publishing/projects/fixture-project/drafts')return {ok:true,json:async()=>created}
    if(path==='/api/publishing/publications/created-draft')return {ok:true,json:async()=>created}
    if(path==='/api/publishing/publications?project_uid=fixture-project')return {ok:true,json:async()=>[created]}
    if(path==='/api/publishing/projects/fixture-project/candidates')return candidateReady
      ?{ok:true,json:async()=>({files:[{id:11,name:'final_dub.mp4',exists:true}]})}
      :new Promise(resolve=>{releaseCandidates=resolve})
    throw new Error(`准备本地稿件不应上传或投稿：${path}`)
  })
  const creating=page.create();await flush()
  assert.equal(page.current.id,'created-draft');assert.equal(page.busy,true)
  await page.create()
  releaseCandidates({ok:false,json:async()=>({message:'成片列表读取暂不可用'})});await creating;await flush()
  assert.equal(page.current.id,'created-draft');assert.equal(page.current.status,'draft')
  assert.equal(router.currentRoute.value.query.publication,'created-draft')
  assert.equal(page.busy,false);assert.equal(page.source,null);assert.equal(page.metadataLocked,false)
  assert.match(page.error,/草稿已创建.*成片列表暂未读取成功.*刷新文件列表/)
  page.change('title','人工修正标题');assert.equal(page.dirty,true)
  candidateReady=true;await page.refreshFiles()
  assert.equal(page.source.files[0].id,11);assert.equal(page.displayValues.title,'人工修正标题')
  assert.equal(page.dirty,true);assert.equal(page.current.id,'created-draft')
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[
    {url:'/api/publishing/projects/fixture-project/drafts',method:'POST'},
  ])
})

test('发布资料请求、确认窗口或封面处理中拒绝离开，保留当前记录并不显示放弃修改弹窗',async()=>{
  for(const state of ['busy','confirming','coverLoading']){
    const {page,dialogs}=await render(p=>p.apply(record()))
    const before=JSON.stringify(page.draft)
    page[state]=true
    assert.equal(page.confirmLeave(),false)
    assert.equal(page.current.id,'fixture');assert.equal(JSON.stringify(page.draft),before)
    assert.deepEqual(dialogs,[])
    page[state]=false;assert.equal(page.confirmLeave(),true)
  }
})

test('读取发布资料迟迟未返回时允许离开，不把只读请求误判为投稿写入', async () => {
  const {page} = await render(p => p.apply(record()))
  page.busy = true
  page.busyKind = 'read'
  assert.equal(page.confirmLeave(), true)
  assert.equal(page.canSwitch(), true)
  page.busy = false
  page.busyKind = ''
})

test('确认对话框的取消、遮罩和关闭回调都会清理确认锁，正向异步操作仍保留锁', async () => {
  const {page} = await render(p => p.apply(record()))
  let options: any
  const factory = (value: any) => { options = value; return {} }
  for (const callback of ['onAfterLeave', 'onClose', 'onNegativeClick', 'onMaskClick']) {
    page.confirming = true
    page.openConfirmation(factory, {})
    options[callback]?.()
    assert.equal(page.confirming, false, callback)
  }
  page.confirming = true
  page.openConfirmation(factory, {onPositiveClick: () => Promise.resolve()})
  options.onPositiveClick()
  assert.equal(page.confirming, true)
  options.onClose()
  assert.equal(page.confirming, false)
  page.confirming = true
  page.openConfirmation(factory, {onNegativeClick: () => false})
  options.onNegativeClick()
  assert.equal(page.confirming, true)
  options.onAfterLeave()
  assert.equal(page.confirming, false)
})

const publishFiles = () => [
  {id:11,name:'final_original_11.mp4',path:'D:/fixture/final_original_11.mp4',version_name:'原声字幕版',version_summary:'原声 · 双语字幕',exists:true,size:4096,duration:60},
  {id:12,name:'final_dubbing_12.mp4',path:'D:/fixture/final_dubbing_12.mp4',version_name:'中文配音版',version_summary:'配音 · 中文字幕',exists:true,size:8192,duration:60,has_dubbing:true},
]
function preparedRecord(status='draft'):any {
  const row:any=record(status)
  row.draft.parts=[{artifact_id:11,title:'自定义原声名称'},{artifact_id:12,title:'自定义配音名称'}]
  row.draft.cover_id='fixture-cover.jpg'
  if(status!=='draft')row.snapshot.parts=publishFiles().map((file,index)=>({...file,artifact_id:file.id,title:row.draft.parts[index].title}))
  return row
}
function assertDisabled(button:string|undefined,disabled=true) {
  assert.ok(button)
  assert.equal(/\sdisabled(?:=|\s|>)/.test(button),disabled)
}

test('真实分P组件添加、改名、排序与移除写回草稿，文件按ID对应且不会重复加入',async()=>{
  const files=publishFiles()
  const {page,partsEditor}=await render(p=>{p.apply(record());p.source={files}})
  partsEditor.add(files[0]);partsEditor.add(files[0]);partsEditor.add(files[1])
  assert.deepEqual(page.draft.parts.map((part:any)=>part.artifact_id),[11,12])
  partsEditor.rename(0,'给观众看的原声版')
  assert.equal(page.draft.parts[0].title,'给观众看的原声版')
  assert.equal(files[0].name,'final_original_11.mp4')
  partsEditor.move(1,-1)
  assert.deepEqual(page.draft.parts.map((part:any)=>part.artifact_id),[12,11])
  const reordered=preparedRecord();reordered.draft=JSON.parse(JSON.stringify(page.draft))
  const next=await render(p=>{p.apply(reordered);p.source={files}})
  const cards=[...next.html.matchAll(/<li\b[^>]*class="part-card"[^>]*>([\s\S]*?)<\/li>/g)].map(match=>match[1])
  assert.equal(cards.length,2)
  assert.match(cards[0],/P1[\s\S]*中文配音版/)
  assert.match(cards[0],/final_dubbing_12\.mp4/);assert.doesNotMatch(cards[0],/final_original_11\.mp4/)
  assert.match(cards[1],/给观众看的原声版/);assert.match(cards[1],/final_original_11\.mp4/)
  assert.match(next.html,/已加入 P1/);assert.match(next.html,/已加入 P2/)
  next.partsEditor.remove(0)
  assert.deepEqual(next.page.draft.parts,[{artifact_id:11,title:'给观众看的原声版'}])
  assert.equal(next.page.dirty,true)
})

test('草稿视频清单包含审查，账号封面与分组文案集中在资料区，保存紧邻编辑资料',async()=>{
  const {html}=await render(p=>{p.apply(preparedRecord());p.source={files:publishFiles()}})
  const video=html.slice(html.indexOf('id="publish-video"'),html.indexOf('id="publish-metadata"'))
  const metadata=html.slice(html.indexOf('id="publish-metadata"'),html.indexOf('id="publish-confirm"'))
  assert.match(video,/B站分P与视频文件对应清单/)
  assert.match(video,/字幕、配音与评论审查/)
  assert.doesNotMatch(video,/发布账号|上传封面/)
  assert.match(metadata,/发布账号/);assert.match(metadata,/上传封面/)
  assert.match(metadata,/aria-label="稿件文案"/);assert.match(metadata,/aria-label="投稿属性"/);assert.match(metadata,/aria-label="发布安排"/)
  assert.ok(actionButtons(metadata).some(button=>buttonText(button)==='保存资料'))
  assert.ok(actionButtons(metadata).some(button=>buttonText(button)==='放弃修改'))
  assert.match(metadata,/保存到本地草稿，不会开始上传/)
})

test('已选文件缺失或尚未读取时保留P名称和清单，分别提示状态并阻止草稿上传',async()=>{
  for(const mode of ['missing','unread']) {
    const row=preparedRecord()
    const {html,page}=await render(p=>{
      p.apply(row)
      p.source=mode==='unread'?null:{files:publishFiles().map(file=>({...file,exists:false}))}
    })
    assert.deepEqual(page.draft.parts,row.draft.parts)
    assert.equal(page.publishingStep,1);assert.equal(page.submissionBlocked,true)
    for(const label of ['上传并投稿','仅上传文件'])assertDisabled(actionButtons(html).find(button=>buttonText(button)===label))
    if(mode==='missing') {
      assert.match(html,/本地文件缺失，已保留此分P/)
      assert.match(html,/final_original_11\.mp4/)
    } else {
      assert.match(html,/对应文件信息尚未读取|视频列表尚未读取/)
      assert.match(html,/文件状态待读取/)
      assert.doesNotMatch(html,/本地文件缺失，已保留此分P/)
    }
  }
})

test('草稿待办阶段正确，任务导航只标记一个当前工作区且不使用第二排锚点步骤',async()=>{
  for(const mode of ['ready','missing-account','missing-cover','missing-title','needs-review','unsaved']) {
    const {html,page}=await render(p=>{
      const row=preparedRecord()
      if(mode==='missing-account')row.draft.account_id=''
      if(mode==='missing-cover')row.draft.cover_id=''
      if(mode==='missing-title')row.base_settings.title='  '
      p.apply(row);p.source={files:publishFiles().map(file=>({...file,review_ready:mode!=='needs-review'}))}
      if(mode==='unsaved')p.change('description','需要保存的简介')
    })
    const expected=mode==='ready'?3:mode==='needs-review'?1:2
    assert.equal(page.publishingStep,expected)
    assert.deepEqual(page.workspace.tasks.slice(0,4).map((task:any)=>task.key),['video','metadata','confirm','status'])
    assert.equal((html.match(/aria-current="page"/g)||[]).length,1)
    assert.match(html,new RegExp(`aria-current="page" data-task="${page.selectedTask}"`))
    assert.doesNotMatch(html,/aria-label="发布准备步骤"/)
    if(mode==='ready')assertDisabled(actionButtons(html).find(button=>buttonText(button)==='上传并投稿'),false)
  }
})

test('上传和最终记录提供单一状态卡，冻结快照独立于当前视频名称与编辑工具',async()=>{
  for(const status of ['queued','uploading','submitting','joining','checking','submitted','published','private','reviewed','rejected','collection_pending','unknown']) {
    const row=preparedRecord(status)
    if(['submitted','published','private','reviewed','rejected','collection_pending','checking'].includes(status))row.receipt={bvid:'BV1234567890',state_desc:'测试平台状态'}
    row.snapshot.settings.description='已确认简介<script>仅作为文字</script>'
    const {html,page,form,assistant,partsProps}=await render(p=>{
      p.apply(row);p.source={files:publishFiles().map(file=>({...file,name:'today_new.mp4',version_name:'今天的新版本'}))}
    })
    assert.equal((html.match(/id="publish-receipt"/g)||[]).length,1)
    const frozen=html.match(/<details\b[^>]*id="confirmed-content"[^>]*>/)?.[0]
    assert.ok(frozen)
    assert.ok(page.workspace.tasks.some((task:any)=>task.key==='snapshot'))
    assert.match(html,/final_original_11\.mp4/);assert.match(html,/final_dubbing_12\.mp4/)
    assert.doesNotMatch(html,/today_new\.mp4|今天的新版本|<script>仅作为文字<\/script>/)
    assert.match(html,/已确认简介&lt;script&gt;仅作为文字&lt;\/script&gt;/)
    assert.equal(partsProps.frozen,true);assert.equal(partsProps.disabled,true)
    assert.equal(form,undefined)
    // 已有BVID且状态已结束时，发布页内提供远端文案候选与同步入口；
    // 上传/检查中的记录仍只展示进度，不提前打开远端编辑器。
    const remoteEditable = ['submitted','published','private','reviewed','rejected','collection_pending'].includes(status)
    assert.equal(!!assistant, remoteEditable)
    if (remoteEditable) assert.match(html,/id="publish-remote-sync"/)
    assert.equal(page.publishingStep,4)
    assert.ok(!actionButtons(html).some(button=>['保存资料','上传并投稿','确认投稿'].includes(buttonText(button))))
  }
})

test('上传完成后视频与账号仍冻结，资料可以修改；本地文件不可用也不误判必须重新上传',async()=>{
  const row=preparedRecord('uploaded')
  const {html,page,partsEditor,form,partsProps}=await render(p=>{p.apply(row);p.source=null})
  assert.equal(page.workspace.mode,'resume')
  assert.ok(page.workspace.tasks.some((task:any)=>task.key==='metadata'))
  assert.ok(form);assert.equal(page.metadataLocked,false);assert.equal(page.submissionBlocked,false)
  assert.equal(partsProps.frozen,true)
  const frozen=html.match(/<details\b[^>]*id="publish-video"[^>]*>/)?.[0]
  assert.ok(frozen)
  partsEditor.rename(0,'不能变更');partsEditor.move(0,1);partsEditor.remove(0);partsEditor.add(publishFiles()[0])
  page.selectAccount('456');page.renamePart(0,'不能变更');page.removePart(0);page.reorderPart(0,1)
  assert.deepEqual(page.draft.parts,row.draft.parts);assert.equal(page.draft.account_id,'123')
  page.change('title','上传后修改标题')
  assert.equal(page.displayValues.title,'上传后修改标题');assert.equal(page.dirty,true)
  assert.equal(page.draft.cover_id,'fixture-cover.jpg')
})

test('暂停和失败保留原确认清单，步骤指向继续操作，不要求再次读取本地候选列表',async()=>{
  for(const status of ['paused','failed']) {
    const row=preparedRecord(status);row.error='本次上传没有完成'
    row.progress_detail={phase:'uploading',loaded:1000,total:2000,current_part:2,completed_parts:1,total_parts:2}
    const {html,page,form}=await render(p=>{p.apply(row);p.source=null})
    assert.equal(page.publishingStep,3);assert.equal(page.submissionBlocked,false)
    assert.equal(page.selectedTask,'status')
    assert.ok(page.workspace.tasks.some((task:any)=>task.key==='confirm'))
    assert.match(html,/本次上传没有完成/);assert.match(html,/复用已完成分P/)
    assertDisabled(actionButtons(html).find(button=>buttonText(button)==='继续上传并投稿'),false)
    assertDisabled(actionButtons(html).find(button=>buttonText(button)==='仅继续上传'),false)
    assert.equal(form,undefined)
    assert.deepEqual(page.draft.parts,row.draft.parts)
  }
})

test('放弃资料修改仅恢复当前已保存草稿，忙态不丢弃修改，也不请求保存上传或投稿',async()=>{
  const row=preparedRecord()
  const {page}=await render(p=>{p.apply(row);p.source={files:publishFiles()}})
  page.change('title','未保存的标题');page.renamePart(0,'未保存的P名')
  for(const state of ['busy','copyBusy','confirming','coverLoading']) {
    page[state]=true;page.discardDraft()
    assert.equal(page.draft.overrides.title,'未保存的标题');assert.equal(page.draft.parts[0].title,'未保存的P名')
    page[state]=false
  }
  page.discardDraft()
  assert.deepEqual(page.draft,row.draft);assert.equal(page.dirty,false)
  assert.equal(page.current.id,'fixture');assert.equal(page.current.status,'draft')
})

test('已删除项目的草稿仅显示当时保存的文案与覆盖项，不用今日继承值补成已确认投稿',async()=>{
  const row=preparedRecord()
  row.base_settings.title='今天继承来的标题';row.base_settings.description='今天继承来的简介'
  row.draft.content={title:'当时的基础标题',description:'当时保存的简介',tags:['当时标签']}
  row.draft.overrides={title:'当时覆盖后的标题',close_comments:true}
  const {html,page,form,assistant}=await render(p=>p.apply(row),'/publications/fixture')
  assert.match(html,/当时覆盖后的标题/);assert.match(html,/当时保存的简介/);assert.match(html,/当时标签/)
  assert.match(html,/已保存的稿件资料/)
  assert.doesNotMatch(html,/今天继承来的标题|今天继承来的简介|已确认视频清单|已确认分P|已确认文件|以上为确认时/)
  assert.match(html,/<dt[^>]*>稿件类型<\/dt><dd[^>]*>未记录/)
  assert.match(html,/<dt[^>]*>评论<\/dt><dd[^>]*>关闭/)
  assert.ok(html.indexOf('id="publish-receipt"')<html.indexOf('id="confirmed-content"'))
  assert.equal(form,undefined);assert.equal(assistant,undefined)
  assert.equal(page.metadataLocked,true);assert.equal(page.canCreateReviewDraft,false)
  assert.ok(!actionButtons(html).some(button=>/保存资料|上传并投稿|复制为新草稿|确认投稿/.test(buttonText(button))))
  page.change('title','不允许编辑')
  assert.deepEqual(page.draft.overrides,row.draft.overrides);assert.equal(page.dirty,false)
})

test('冻结快照包含原封面及水印互动安排，未记录选项如实显示，不请求今日平台信息',async()=>{
  const row=preparedRecord('published')
  row.snapshot.cover_path='D:\\fixture\\covers\\original-cover.jpg'
  row.snapshot.settings={...settings,watermark:true,close_comments:false,close_danmaku:true,selected_comments:true,topic_name:'当时话题<script>',topic_id:7}
  const {html}=await render(p=>{p.apply(row);p.source={files:[],thumbnail_preview:'/today-cover.jpg'}})
  assert.match(html,/src="\/api\/publishing\/covers\/original-cover\.jpg"/)
  assert.doesNotMatch(html,/today-cover\.jpg/)
  for(const [label,value] of [['B站水印','开启'],['评论','开放'],['弹幕','关闭'],['精选评论','开启']])
    assert.match(html,new RegExp(`<dt[^>]*>${label}<\\/dt><dd[^>]*>${value}`))
  assert.match(html,/当时话题&lt;script&gt;/);assert.doesNotMatch(html,/<script>/)
})

test('追加已有BV的局部上传记录不能当作完整稿件准备审查修订新稿',async()=>{
  for(const marker of ['update-target','append-receipt']) {
    const row=preparedRecord('published');row.receipt={bvid:'BV1234567890'}
    if(marker==='update-target')row.snapshot.update_target={bvid:'BV1234567890'}
    else row.receipt.update_kind='append'
    const {html,page,dialogs}=await render(p=>p.apply(row))
    assert.equal(page.canCreateReviewDraft,false)
    assert.ok(!actionButtons(html).some(button=>buttonText(button)==='使用审查修订版新建草稿'))
    await page.createReviewDraft();assert.deepEqual(dialogs,[])
    assert.match(html,/到官网替换原BV视频/)
    assert.ok(actionButtons(html).some(button=>buttonText(button)==='复制为新草稿'))
  }
})

test('保留的未知记录仍可主动关联BV，空值和重复点击受保护，核对成功后不能再次关联',async t=>{
  const row=preparedRecord('unknown'),resolved={...row,status:'published',revision:4,receipt:{bvid:'BV1234567890'}}
  const {html,page}=await render(p=>p.apply(row),'/publications/fixture')
  assertDisabled(actionButtons(html).find(button=>buttonText(button)==='核对并关联'))
  const requests:any[]=[];let finish:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url);requests.push({path,method:options?.method,body:options?.body?JSON.parse(String(options.body)):null})
    if(path.endsWith('/receipt'))return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>resolved})})
    if(path==='/api/publishing/publications/fixture')return {ok:true,json:async()=>resolved}
    throw new Error(`保留记录不得执行其他操作：${path}`)
  })
  page.receiptBV='  ';await page.attachReceipt();assert.deepEqual(requests,[])
  page.receiptBV='  BV1234567890  '
  page.confirming=true;await page.attachReceipt();assert.deepEqual(requests,[]);page.confirming=false
  const first=page.attachReceipt();await page.attachReceipt()
  assert.equal(requests.length,1);assert.equal(page.busy,true)
  finish();await first;await flush()
  assert.deepEqual(requests.filter(request=>request.method==='POST'),[{path:'/api/publishing/publications/fixture/receipt',method:'POST',body:{bvid:'BV1234567890'}}])
  assert.equal(page.current.status,'published');assert.equal(page.current.receipt.bvid,'BV1234567890')
  await page.attachReceipt()
  assert.equal(requests.filter(request=>request.method==='POST').length,1)
})

test('同一草稿任务切换保留分P、封面、未保存文字和已挂载表单，不需要先保存或发起网络',async()=>{
  const {page,form,assistant,partsEditor}=await render(p=>{p.apply(preparedRecord());p.source={files:publishFiles()}})
  const draftIdentity=page.draft
  page.change('title','尚未保存的当前标题')
  page.renamePart(0,'尚未保存的分P名称')
  assistant.instruction='这份写作要求暂不应用'
  const before=JSON.stringify(page.draft)
  assert.equal(page.canSwitch(),false)
  for(const task of ['metadata','confirm','status','video']) {
    page.selectTask(task)
    assert.equal(page.selectedTask,task)
    assert.equal(page.draft,draftIdentity)
    assert.equal(JSON.stringify(page.draft),before)
    assert.equal(page.dirty,true)
    assert.equal(assistant.instruction,'这份写作要求暂不应用')
  }
  assert.ok(form);assert.ok(partsEditor)
  page.selectTask('metadata')
  page.removePart(0)
  assert.equal(page.publishingStep,2)
  assert.equal(page.selectedTask,'metadata','编辑不应抢走用户选择的工作区')
})

test('同记录的原稿更新、文案与状态可以来回查看，独立未保存状态与防冲突门禁继续生效',async()=>{
  const {page,assistant}=await render(p=>p.apply(reviewRecord('published')))
  page.applyRemoteCopy({title:'待核对的新文案'})
  page.originalUpdateDirty=true
  const title=page.remoteCopyValues.title,recordIdentity=page.current
  for(const task of ['sync','update','snapshot','status','more','update']) {
    page.selectTask(task)
    assert.equal(page.selectedTask,task)
    assert.equal(page.remoteCopyValues.title,title)
    assert.equal(page.remoteCopyDirty,true)
    assert.equal(page.originalUpdateDirty,true)
    assert.equal(page.current,recordIdentity)
  }
  assert.ok(assistant)
  assert.equal(page.canSwitch(),false)
  page.applyRemoteCopy({title:'不应穿透原稿更新锁的候选'})
  assert.equal(page.remoteCopyValues.title,title)
})

test('旧任务锚点在新草稿上安全回退，后续编辑不会随准备阶段自动跳页',async()=>{
  const {page}=await render(p=>{
    p.apply(preparedRecord());p.source={files:publishFiles()}
  },'/projects/fixture-project/publish#publication-original-update')
  assert.equal(page.selectedTask,'video')
  page.change('title','继续修改当前草稿')
  assert.equal(page.publishingStep,2)
  assert.equal(page.selectedTask,'video')
})

test('状态刷新保留仍合法的主动任务，状态变更后不保留已失效的新稿操作',async()=>{
  const {page}=await render(p=>p.apply(reviewRecord('published')))
  page.selectTask('snapshot')
  page.apply({...reviewRecord('published'),progress:42})
  assert.equal(page.selectedTask,'snapshot')
  const unknown=reviewRecord('unknown')
  page.apply(unknown)
  assert.equal(page.selectedTask,'snapshot')
  page.selectTask('update')
  assert.equal(page.selectedTask,'status')
  assert.ok(!page.workspace.tasks.some((task:any)=>['update','sync','confirm'].includes(task.key)))
  const other=preparedRecord();other.id='new-record'
  page.apply(other)
  assert.equal(page.selectedTask,'video')
})

test('只读与归档记录的任务切换无法获得投稿或原稿更新入口',async()=>{
  const cases:[any,string][]=[
    [reviewRecord('published'),'/publications/fixture'],
    [{...reviewRecord('published'),lifecycle:'archived'},'/projects/fixture-project/publish'],
  ]
  for(const [row,path] of cases) {
    const {page}=await render(p=>p.apply(row),path)
    for(const task of ['video','metadata','confirm','update','sync']) {
      page.selectTask(task)
      assert.equal(page.selectedTask,'status')
    }
    page.selectTask('snapshot');assert.equal(page.selectedTask,'snapshot')
    assert.equal(page.metadataLocked,true)
  }
})

test('任务锚点只接管当前稿件页，其他项目或外部链接保持正常路由离开保护',async()=>{
  const {page}=await render(p=>p.apply(reviewRecord()))
  for(const href of ['https://member.bilibili.com/platform/home#publish-remote-sync','/projects/other-project/publish#publish-remote-sync','/projects/fixture-project/publish?publication=other#publish-remote-sync']) {
    let prevented=false
    page.revealPublicationAnchor({target:{closest:()=>({getAttribute:()=>href})},preventDefault:()=>{prevented=true}})
    assert.equal(prevented,false)
    assert.equal(page.selectedTask,'update')
  }
})

test('切换发布记录同步地址与选中身份，只读取新记录一次并清除上一稿的任务锚点',async t=>{
  const older={...reviewRecord('published'),id:'older-record'},newer={...reviewRecord('rejected'),id:'newer-record'}
  const {page,router}=await render(p=>{p.apply(older);p.records=[older,newer]},
    '/projects/fixture-project/publish?publication=older-record#publish-remote-sync')
  assert.equal(page.selectedTask,'sync')
  const calls:string[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.ok(!options?.method || options.method==='GET')
    calls.push(String(url))
    assert.equal(String(url),'/api/publishing/publications/newer-record')
    return {ok:true,json:async()=>newer}
  })
  await page.open('newer-record');await nextTick();await flush()
  assert.equal(page.current.id,'newer-record')
  assert.equal(router.currentRoute.value.query.publication,'newer-record')
  assert.equal(router.currentRoute.value.hash,'')
  assert.equal(page.selectedTask,'update')
  assert.deepEqual(calls,['/api/publishing/publications/newer-record'])
})

test('地址补齐当前记录身份不会重复读取或清空该稿的未保存文案与任务',async()=>{
  const {page,router}=await render(p=>p.apply(reviewRecord('published')),
    '/projects/fixture-project/publish?publication=previous-record')
  page.applyRemoteCopy({title:'待同步标题'})
  page.selectTask('sync')
  const current=page.current,draft=page.draft
  await router.replace({query:{publication:'fixture'}});await nextTick();await flush()
  assert.equal(page.current,current);assert.equal(page.draft,draft)
  assert.equal(page.remoteCopyValues.title,'待同步标题')
  assert.equal(page.remoteCopyDirty,true);assert.equal(page.selectedTask,'sync')
})

test('空状态选择记录在窄屏打开可见选择器，桌面定位记录列表，不改变当前稿件',async t=>{
  const {page}=await render(p=>{p.records=[reviewRecord('published')]})
  const previous=(globalThis as any).document,actions:string[]=[]
  t.after(async()=>{await nextTick();await flush();if(previous===undefined)delete (globalThis as any).document;else (globalThis as any).document=previous})
  ;(globalThis as any).document={
    querySelector:()=>({getClientRects:()=>[{}],focus:()=>actions.push('选择器聚焦'),click:()=>actions.push('选择器打开')}),
    getElementById:()=>{throw new Error('窄屏不应跳到隐藏侧栏')},
  }
  page.focusPublicationRecords()
  assert.deepEqual(actions,['选择器聚焦','选择器打开'])
  ;(globalThis as any).document={querySelector:()=>null,getElementById:()=>({
    scrollIntoView:()=>actions.push('定位列表'),querySelector:()=>({focus:()=>actions.push('聚焦记录')}),
  })}
  page.focusPublicationRecords()
  assert.deepEqual(actions.slice(2),['定位列表','聚焦记录'])
  assert.equal(page.current,null);assert.equal(page.draft,null)
})
