import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, View: any, Info: any, navModule: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  View = (await server.ssrLoadModule('/src/views/CommentsView.vue')).default
  Info = (await server.ssrLoadModule('/src/components/VideoInfoPanel.vue')).default
  navModule = await server.ssrLoadModule('/src/stores/nav.ts')
})
after(() => server.close())

const commentsFixture = () => ({ rows: [], overlay: null, overlay_revision: 'revision', defaults: {}, fields: [],
  available_assessments: [], rating_progress: { saved: 0, total: 0, remaining: 0 },
  configuration: { values: { 'comments.limit': 180, 'comments.count': 10, 'comments.prompt': '保留有用评论' },
    defaults: {}, overrides: {}, sources: {} }, job: null })
const infoFixture = () => ({ title_original: '原视频标题', summary_zh: '视频概要', source: { duration: 120 }, content: {},
  comments: { count: 2, collected_at: '2026-10-17T01:00:00Z', items: [{ id: 'a' }, { id: 'b' }] } })

async function render(t: any, options: { mode?: 'video' | 'comments' | 'all'; page?: boolean; path?: string;
  data?: any; canCollect?: boolean; seedInfo?: (page: any) => void; seedStudio?: (page: any) => void } = {}) {
  const requests: { url: string; options: any }[] = []
  const data = options.data || commentsFixture()
  t.mock.method(globalThis, 'fetch', async (url: any, request: any = {}) => {
    requests.push({ url: String(url), options: request })
    assert.ok(!request.method || request.method === 'GET', '展示工作区不得请求采集、AI 或保存')
    return { ok: true, json: async () => String(url).includes('/api/configuration/')
      ? { name: '本项目', overrides: {}, defaults: {}, sections: {}, sources: {}, parent_sources: {} }
      : String(url).endsWith('/video-info') ? { info: infoFixture(), can_collect_comments: options.canCollect !== false }
      : data } as any
  })
  const pinia = createPinia(), nav = navModule.useNavStore(pinia)
  nav.uid = 'fixture'; nav.title = '测试项目'
  nav.steps = [{ key: 'download', label: '获取视频', status: 'done', artifacts: [{ name: 'video_info.json', sha256: 'source-1', job_id: 12, size: 256 }] }]
  const component = options.page ? View : { render: () => h(Info, { uid: 'fixture', available: true, ...(options.mode ? { mode: options.mode } : {}) }) }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/projects/:uid/comments', component }, { path: '/outside', component: { render: () => h('p', '外部页面') } },
    { path: '/:pathMatch(.*)*', component: { render: () => null } },
  ] })
  await router.push(options.path || '/projects/fixture/comments'); await router.isReady()
  const instances: any[] = [], pages: Record<string, any> = {}, dialogs: any[] = [], events: Record<string, any[]> = {}
  const Bridge = { setup() {
    const dialog = useDialog()
    for (const method of ['warning', 'info']) t.mock.method(dialog, method, (value: any) => { dialogs.push(value); return {} })
    return () => h(RouterView)
  } }
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Bridge))) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() {
    instances.push(this)
    const name = this.$options.__name
    if (['VideoInfoPanel', 'CommentsView', 'CommentStudioPanel'].includes(name)) pages[name] = this.$.setupState
    if (name === 'VideoInfoPanel') {
      const page = this.$.setupState
      page.info = infoFixture(); page.canComments = options.canCollect !== false; page.loading = false
      options.seedInfo?.(page)
    }
    if (name === 'CommentStudioPanel') {
      const page = this.$.setupState
      page.state = data; page.hydrate(data); page.prompt = '保留有用评论'
      const emit = this.$.emit
      this.$.emit = (event: string, value: any) => { (events[event] ||= []).push(value); return emit(event, value) }
      options.seedStudio?.(page)
    }
  } })
  const html = await renderToString(app)
  t.after(() => { pages.VideoInfoPanel?.loader.cancel(); nav.reset(); disposePinia(pinia) })
  return { html, pages, instances, requests, router, dialogs, events }
}

test('下载资料 video 模式只显示评论概览入口，不挂载评论工作区', async t => {
  const result = await render(t, { mode: 'video' })
  assert.match(result.html, /视频资料/); assert.match(result.html, /视频概要/)
  assert.match(result.html, /已获取 2 条评论原文/)
  assert.match(result.html, /href="\/projects\/fixture\/comments"/)
  assert.doesNotMatch(result.html, /热门评论采集数量|获取热门评论|确认上屏方案|评论处理流程/)
  assert.equal(result.pages.CommentStudioPanel, undefined)
})

test('comments 模式仅显示采集与工作区，手工来源无采集能力也能进入设置', async t => {
  const result = await render(t, { mode: 'comments', canCollect: false, seedInfo: page => { page.info = null } })
  assert.match(result.html, /评论原文/); assert.match(result.html, /此来源暂不支持在线获取评论/)
  assert.match(result.html, /评论处理流程/); assert.match(result.html, /自动化与评分设置/)
  assert.doesNotMatch(result.html, /刷新平台资料|整理中文标题与概要|视频简介|视频概要/)
  assert.ok(result.pages.CommentStudioPanel)
  const collect = result.instances.find(instance => instance.$options.name === 'Button' && /热门评论/.test(instance.$attrs['aria-label'] || ''))
  assert.ok(collect); assert.equal(collect.$props.disabled, true)
})

test('默认 all 模式保留视频资料和原有评论精选，初始化只读项目采集上限', async t => {
  const result = await render(t)
  assert.match(result.html, /视频资料/); assert.match(result.html, /评论精选/)
  assert.equal(result.pages.VideoInfoPanel.commentLimit, 180)
  result.pages.VideoInfoPanel.applyCollectionLimit(240)
  assert.equal(result.pages.VideoInfoPanel.commentLimit, 240)
  result.pages.VideoInfoPanel.setCommentLimit(73)
  result.pages.VideoInfoPanel.applyCollectionLimit(300)
  assert.equal(result.pages.VideoInfoPanel.commentLimit, 73, '用户本次输入不能被后端轮询覆盖')
  assert.ok(result.requests.every(request => !request.options.method || request.options.method === 'GET'))
})

test('评论专属采集条集中展示来源时间、数量和图标动作，默认 all 与 video 保持原呈现',async t=>{
  const result=await render(t,{mode:'comments'})
  const toolbar=result.html.match(/<header\b[^>]*class="collection-toolbar"[^>]*>([\s\S]*?)<\/header>/)?.[1]
  assert.ok(toolbar);assert.match(toolbar,/评论原文/);assert.match(toolbar,/更新于/)
  assert.match(toolbar,/本次获取/);assert.match(toolbar,/热门评论采集数量/)
  assert.match(toolbar,/aria-label="热门评论采集说明"/)
  assert.match(toolbar,/<svg[^>]*aria-hidden="true"/)
  const collect=result.instances.find(instance=>instance.$options.name==='Button'&&/热门评论/.test(instance.$attrs['aria-label']||''))
  assert.equal(collect.$props.type,'info');assert.equal(collect.$props.secondary,true)
  assert.equal(collect.$props.loading,false);assert.equal(collect.$props.disabled,false)
  const input=result.instances.find(instance=>instance.$options.name==='InputNumber'&&instance.$props.inputProps?.['aria-label']==='热门评论采集数量')
  assert.equal(input.$props.min,1);assert.equal(input.$props.max,500);assert.equal(input.$props.precision,0)
  input.$emit('update:value',240)
  assert.equal(result.pages.VideoInfoPanel.commentLimit,240)
  assert.ok(result.requests.every(request=>!request.options.method||request.options.method==='GET'))
  const legacy=await render(t)
  assert.doesNotMatch(legacy.html,/class="collection-toolbar"|aria-label="热门评论采集说明"/)
  const original=legacy.instances.find(instance=>instance.$options.name==='Button'&&/热门评论/.test(instance.$attrs['aria-label']||''))
  assert.equal(original.$props.secondary,false);assert.equal(original.$props.type,'default')
  const video=await render(t,{mode:'video'})
  assert.doesNotMatch(video.html,/class="collection-toolbar"|热门评论采集数量/)
})

test('独立页以图标工具和明确日志折叠区区分操作，保留可访问展开状态与惰性诊断',async t=>{
  const result=await render(t,{page:true})
  assert.match(result.html,/role="group" aria-label="评论工作区工具"/)
  assert.match(result.html,/class="diagnostics-identity"/);assert.match(result.html,/class="diagnostics-toggle"/)
  assert.match(result.html,/LLM 对话与运行日志/);assert.match(result.html,/评论评分与评论翻译/)
  const buttons=result.instances.filter(instance=>instance.$options.name==='Button')
  const settings=buttons.find(button=>button.$attrs.title==='打开本项目的自动化与评分设置')
  const diagnostics=buttons.find(button=>button.$attrs['aria-controls']==='llm-conversation')
  assert.ok(settings);assert.equal(settings.$props.quaternary,true)
  assert.ok(diagnostics);assert.equal(diagnostics.$props.secondary,true)
  assert.equal(diagnostics.$attrs['aria-expanded'],false)
  assert.equal(result.pages.CommentsView.diagnosticsLoaded,false)
  diagnostics.$props.onClick()
  await nextTick()
  assert.equal(result.pages.CommentsView.diagnosticsLoaded,true);assert.equal(result.pages.CommentsView.diagnosticsOpen,true)
  assert.ok(result.requests.every(request=>!request.options.method||request.options.method==='GET'))
})

test('没有有效项目采集上限时回退 50，修改输入不保存配置', async t => {
  const data = commentsFixture(); delete (data.configuration.values as any)['comments.limit']
  const result = await render(t, { mode: 'comments', data })
  assert.equal(result.pages.VideoInfoPanel.commentLimit, 50)
  result.pages.VideoInfoPanel.setCommentLimit(125)
  assert.equal(result.pages.VideoInfoPanel.commentLimit, 125)
  assert.ok(result.requests.every(request => !request.options.method || request.options.method === 'GET'))
})

test('评论草稿独立回传并阻止刷新原文，仍允许继续编辑，迟到事件也不启动采集', async t => {
  const result = await render(t, { mode: 'comments', seedStudio: page => page.setStyle('duration', 7) })
  const info = result.pages.VideoInfoPanel, studio = result.pages.CommentStudioPanel
  assert.equal(studio.dirty, true); assert.equal(info.commentDirty, true)
  assert.equal(info.collectionBlocked, true); assert.equal(studio.editLocked, false)
  assert.match(info.collectionBlockReason, /确认或重置上屏草稿/)
  const before = result.requests.length
  await info.runAction('comments')
  assert.equal(result.requests.length, before)
  studio.setStyle('duration', 8); assert.equal(studio.settings.duration, 8)
  studio.hydrate(commentsFixture()); await nextTick()
  // SSR 结束会主动停止同步 watcher；通过真实组件事件验证父子状态回传。
  result.instances.find(instance => instance.$options.__name === 'CommentStudioPanel').$emit('dirty-change', studio.dirty)
  assert.equal(info.commentDirty, false); assert.equal(info.collectionBlocked, false)
})

test('确认弹窗打开到保存结束期间与采集互斥，取消后解除并保留草稿', async t => {
  const result = await render(t, { mode: 'comments', seedStudio: page => page.setStyle('duration', 7) })
  const studio = result.pages.CommentStudioPanel, info = result.pages.VideoInfoPanel
  studio.confirmPlan()
  const studioComponent = result.instances.find(instance => instance.$options.__name === 'CommentStudioPanel')
  studioComponent.$emit('saving-change', studio.saving)
  assert.equal(result.dialogs.length, 1); assert.equal(info.commentSaving, true)
  assert.match(info.collectionBlockReason, /正在保存评论设置或确认上屏方案/)
  await info.runAction('comments')
  assert.ok(result.requests.every(request => !request.options.method || request.options.method === 'GET'))
  result.dialogs[0].onNegativeClick()
  studioComponent.$emit('saving-change', studio.saving)
  assert.equal(info.commentSaving, false); assert.equal(info.commentDirty, true)
  assert.equal(studio.settings.duration, 7)
})

test('独立页有统一阶段导航、配置与日志入口，打开收起日志不改变工作区草稿', async t => {
  const result = await render(t, { page: true, seedStudio: page => page.setStyle('duration', 7) })
  assert.match(result.html, /<h1\b[^>]*>上屏评论<\/h1>/)
  assert.match(result.html, /评论设置/); assert.match(result.html, /LLM 对话与日志/)
  assert.match(result.html, /第 7 \/ 9 阶段/)
  assert.match(result.html, /上一步 · 字幕与配音/); assert.match(result.html, /下一步 · 合成成片/)
  assert.match(result.html, /此阶段默认设置/)
  const view = result.pages.CommentsView, studio = result.pages.CommentStudioPanel
  const draft = JSON.stringify({ items: studio.items, settings: studio.settings })
  await view.showDiagnostics('translate', false)
  assert.equal(view.diagnosticsAction, 'translate'); assert.equal(view.diagnosticsLoaded, true); assert.equal(view.diagnosticsOpen, true)
  view.diagnosticsToggled({ target: { open: false } })
  assert.equal(view.diagnosticsOpen, false); assert.equal(view.diagnosticsLoaded, true)
  assert.equal(JSON.stringify({ items: studio.items, settings: studio.settings }), draft)
  assert.equal(studio.dirty, true)
})

test('直接诊断深链默认打开对应动作，手动展开也会载入诊断，配置跳转保留页面实例', async t => {
  const result = await render(t, { page: true, path: '/projects/fixture/comments?comment_action=translate#llm-conversation' })
  const view = result.pages.CommentsView
  assert.equal(view.diagnosticsLoaded, true); assert.equal(view.diagnosticsOpen, true); assert.equal(view.diagnosticsAction, 'translate')
  assert.match(result.html, /评论 LLM 对话与日志/)
  view.diagnosticsLoaded = false; view.diagnosticsToggled({ target: { open: true } })
  assert.equal(view.diagnosticsLoaded, true)
  view.showSettings(); await nextTick(); await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(result.router.currentRoute.value.hash, '#comment-settings')
  assert.equal(result.router.currentRoute.value.path, '/projects/fixture/comments')
})

test('独立页保留现有路由离开保护：普通草稿确认离开，确认保存期间禁止离开', async t => {
  const result = await render(t, { page: true, seedStudio: page => page.setStyle('duration', 7) })
  const leaving = result.router.push('/outside')
  await nextTick(); await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(result.dialogs.length, 1); assert.match(result.dialogs[0].title, /草稿尚未确认/)
  result.dialogs[0].onNegativeClick(); await leaving
  assert.equal(result.router.currentRoute.value.path, '/projects/fixture/comments')
  result.pages.CommentStudioPanel.confirmPlan()
  result.instances.find(instance => instance.$options.__name === 'CommentStudioPanel').$emit('saving-change', result.pages.CommentStudioPanel.saving)
  assert.equal(result.pages.VideoInfoPanel.commentSaving, true)
  await result.router.push('/outside')
  assert.equal(result.router.currentRoute.value.path, '/projects/fixture/comments')
  assert.equal(result.dialogs.length, 2, '保存期间不会再弹放弃草稿确认')
  result.dialogs[1].onNegativeClick()
})

function preparableFixture() {
  const data:any=commentsFixture()
  data.rows=[{id:'a',source_hash:'a',text:'Original comment',text_zh:'一条可用评论',author:'观众'}]
  data.configuration.values['comments.count']=1
  return data
}
function draftResponse(data:any,page:any) {
  return {...data,prepare_job:{id:'prepare-fixture',status:'done'},preparation:{
    draft:{items:[{...data.rows[0],display_text:data.rows[0].text_zh,source_text:data.rows[0].text,selection_origin:'automatic'}],
      settings:{...page.settings},excluded_ids:[],order_mode:'chronological'},summary:{ready:1},warnings:[]}}
}

test('没有旧草稿时准备中的导航和刷新也受保护，收到结果后改用正常草稿确认',async t=>{
  const data=preparableFixture(),result=await render(t,{page:true,data}),studio=result.pages.CommentStudioPanel
  assert.equal(studio.dirty,false)
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async(url,options:any={})=>{
    if(String(url).endsWith('/comments/prepare'))return await new Promise(resolve=>{finish=resolve})
    return {ok:true,json:async()=>data} as any
  })
  const warnings:string[]=[]
  t.mock.method(studio.message,'warning',(value:string)=>{warnings.push(value)})
  const task=studio.prepareTarget()
  assert.equal(studio.pending,'prepare');assert.equal(studio.dirty,false)
  await result.router.push('/outside')
  assert.equal(result.router.currentRoute.value.path,'/projects/fixture/comments')
  assert.equal(result.dialogs.length,0)
  assert.match(warnings[0],/正在准备上屏草稿，请等待结果返回或停止本次准备后再离开/)
  let prevented=0
  const unloading:any={preventDefault:()=>{prevented++}}
  studio.unload(unloading);assert.equal(prevented,1);assert.equal(unloading.returnValue,'')
  finish({ok:true,json:async()=>draftResponse(data,studio)});await task
  assert.equal(studio.pending,'');assert.equal(studio.dirty,true);assert.equal(studio.items.length,1)
  const leaving=result.router.push('/outside')
  await nextTick();await new Promise(resolve=>setTimeout(resolve,0))
  assert.equal(result.dialogs.length,1);assert.match(result.dialogs[0].title,/草稿尚未确认/)
  result.dialogs[0].onNegativeClick();await leaving
  assert.equal(result.router.currentRoute.value.path,'/projects/fixture/comments')
})

test('准备取消实际结束后解除离开保护，未保存的草稿不会凭空产生',async t=>{
  const data=preparableFixture(),result=await render(t,{page:true,data}),studio=result.pages.CommentStudioPanel
  let finish:(value:any)=>void=()=>{},cancelled=false
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options:any={})=>{
    const path=String(url);requests.push({path,options})
    if(path.endsWith('/comments/prepare'))return await new Promise(resolve=>{finish=resolve})
    if(path.endsWith('/comments/cancel')){cancelled=true;return {ok:true,json:async()=>({ok:true})} as any}
    const state={...data,prepare_job:{id:'prepare-fixture',status:cancelled?'cancelled':'running'}}
    return {ok:true,json:async()=>state} as any
  })
  const task=studio.prepareTarget()
  await nextTick();await new Promise(resolve=>setTimeout(resolve,0))
  studio.state={...data,prepare_job:{id:'prepare-fixture',status:'running'}}
  await studio.stop()
  assert.deepEqual(JSON.parse(requests.find(row=>row.path.endsWith('/comments/cancel')).options.body),{job_id:'prepare-fixture'})
  assert.equal(studio.beforeLeave(),false,'取消信号尚未结束原请求时继续保护返回结果')
  finish({ok:false,json:async()=>({message:'本次准备已停止，已完成评分与译文保留'})});await task
  assert.equal(studio.pending,'');assert.equal(studio.dirty,false);assert.equal(studio.items.length,0)
  let prevented=false
  studio.unload({preventDefault:()=>{prevented=true}})
  assert.equal(prevented,false)
  await result.router.push('/outside')
  assert.equal(result.router.currentRoute.value.path,'/outside');assert.equal(result.dialogs.length,0)
})

test('普通评分或翻译没有草稿时仍允许后台继续，不额外阻止离开和刷新',async t=>{
  const result=await render(t,{page:true}),studio=result.pages.CommentStudioPanel
  for(const action of ['filter','translate']){
    studio.pending=action
    assert.equal(studio.beforeLeave(),true)
    let prevented=false
    studio.unload({preventDefault:()=>{prevented=true}})
    assert.equal(prevented,false)
  }
  studio.pending=''
})

test('项目边界切换后迟到的准备结果不能覆盖新项目状态或清除其待执行操作',async t=>{
  const data=preparableFixture(),result=await render(t,{page:true,data}),studio=result.pages.CommentStudioPanel
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async(url,options:any={})=>{
    if(String(url).endsWith('/comments/prepare'))return await new Promise(resolve=>{finish=resolve})
    return {ok:true,json:async()=>data} as any
  })
  const task=studio.prepareTarget(),response=draftResponse(data,studio)
  // SSR 已停止 props watcher，显式重现切项目时同一边界会执行的 epoch 重置。
  const nextData={...data,overlay_revision:'new-project',rows:[{id:'next',source_hash:'next',text:'New source',text_zh:'新项目原文'}]}
  studio.epoch++;studio.state=nextData;studio.hydrate(nextData);studio.pending='translate';studio.error='新项目状态'
  const current=JSON.stringify({items:studio.items,settings:studio.settings,revision:studio.revision})
  finish({ok:true,json:async()=>response});await task
  assert.equal(JSON.stringify({items:studio.items,settings:studio.settings,revision:studio.revision}),current)
  assert.equal(studio.state.overlay_revision,'new-project');assert.equal(studio.pending,'translate');assert.equal(studio.error,'新项目状态')
})
