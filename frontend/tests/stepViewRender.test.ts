import assert from 'node:assert/strict'
import test, { after, before, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

// 加载实际步骤页及 Naive UI，不能用组件桩绕过空 trigger 导致的渲染异常。
// 只运行内存中的组件渲染，不监听端口、不连接项目或外部服务。
let server: ViteDevServer
let StepView: any
let useNavStore: any
let STEP_LABELS: Record<string, string>
const keys = ['download', 'preprocess', 'transcribe', 'split', 'translate', 'tts', 'assemble']
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
const originalRaf = Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame')
const originalCancelRaf = Object.getOwnPropertyDescriptor(globalThis, 'cancelAnimationFrame')

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null },
  })
  ;({ default: StepView } = await server.ssrLoadModule('/src/views/StepView.vue'))
  ;({ useNavStore, STEP_LABELS } = await server.ssrLoadModule('/src/stores/nav.ts'))
  // SSR 不执行挂载钩子；初始化监听中的请求与动画也必须保持隔离。
  mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0,
    clearTimeout: () => {}, requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', { configurable: true, value: () => {} })
})

after(async () => {
  mock.restoreAll()
  for (const [key, descriptor] of [
    ['window', originalWindow], ['requestAnimationFrame', originalRaf],
    ['cancelAnimationFrame', originalCancelRaf],
  ] as const) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
  await server?.close()
})

const states = [
  { name: '已完成且最新', status: 'done', fresh: true, label: '已是最新' },
  { name: '已完成但过期', status: 'done', fresh: false, label: '需重跑' },
  { name: '尚未运行', status: 'pending', fresh: false, label: '待运行' },
]

for (const state of [
  { name: '当前阶段剩余时间', status: 'running', eta_seconds: 180, eta_scope: 'phase', eta: '当前阶段预计剩余约 3 分钟' },
  { name: '估算百分比和长时间', status: 'running', eta_seconds: 7200, progress_estimated: true, eta: '预计剩余约 120 分钟' },
  { name: '无剩余时间', status: 'running', eta_seconds: null },
  { name: '未知进度只显示阶段', status: 'running', progress_indeterminate: true },
  { name: '结束后隐藏运行进度', status: 'done', eta_seconds: 180 },
]) {
  test(`总览进度：${state.name}`, async () => {
    const { default: Pipeline } = await server.ssrLoadModule('/src/views/PipelineView.vue')
    const pinia = createPinia(), nav = useNavStore(pinia)
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/projects/:uid', component: Pipeline },
      { path: '/projects/:uid/publish', component: {render: () => null} },
      { path: '/projects/:uid/steps/:key', component: StepView },
    ] })
    await router.push('/projects/progress-fixture')
    nav.$patch({ uid: 'progress-fixture', title: '进度回归', stepsLoaded: true, steps: [{
      key: 'assemble', label: '合成成片', fresh: state.status === 'done', progress: 42.5,
      progress_phase: '视频编码', selected: true, cfg: {}, artifacts: [], unready_inputs: [], ...state,
    }] })
    const app = createSSRApp({ render: () => h(NMessageProvider, null, {
      default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
    }) })
    app.use(pinia); app.use(router); setup(app)
    app.mixin({ created() {
      if (this.$options.__name === 'PipelineView') this.$.setupState.smoothProgress = { assemble: 42.5 }
    } })
    try {
      const html = await renderToString(app)
      assert.match(html, /发布到B站/)
      assert.doesNotMatch(html, /独立确认，不随勾选步骤自动投稿/)
      assert.match(html, /仅运行勾选制作步骤/)
      assert.doesNotMatch(html, /aria-label="选择发布到B站"/)
      assert.ok(html.indexOf('publication-step') > html.indexOf('class="row"'))
      if (state.status === 'done') {
        assert.doesNotMatch(html, /class="pbar"/)
      } else if (state.progress_indeterminate) {
        assert.match(html, /class="pbar-eta"[^>]*>视频编码<\/span>/)
        assert.doesNotMatch(html, /class="pbar-inner"|class="pbar-value"/)
      } else {
        assert.match(html, /aria-label="合成成片进度"/)
        assert.match(html, state.progress_estimated ? />预计 42\.5%<\/span>/ : />42\.5%<\/span>/)
        if (state.eta) {
          assert.ok(html.includes(state.eta))
          // 时间必须在条形/百分比容器闭合后出现，不能再嵌在零行高轨道里。
          assert.match(html, /class="pbar-value"[^>]*>[^<]+<\/span><\/span>\s*<span class="pbar-eta"/)
        } else {
          assert.doesNotMatch(html, /class="pbar-eta"/)
        }
      }
    } finally { nav.reset(); disposePinia(pinia) }
  })
}

test('总览进度布局：时间正常占行，百分比不绝对定位，窄容器允许换行', async () => {
  const { descriptor } = parse(await readFile(new URL('../src/views/PipelineView.vue', import.meta.url), 'utf8'))
  const css = postcss.parse(descriptor.styles.map(style => style.content).join('\n'))
  const declarations = (selector: string) => {
    const result: Record<string, string> = {}
    css.walkRules(selector, rule => { rule.walkDecls(decl => { result[decl.prop] = decl.value }) })
    return result
  }
  assert.equal(declarations('.pbar').display, 'grid')
  assert.ok(Number(declarations('.pbar')['line-height']) >= 1)
  assert.equal(declarations('.pbar')['max-width'], '100%')
  assert.equal(declarations('.pbar-inner')['grid-template-columns'], 'minmax(0, 1fr) auto')
  assert.equal(declarations('.pbar-value').position, undefined)
  assert.notEqual(declarations('.pbar-eta')['white-space'], 'nowrap')
  assert.equal(declarations('.row')['flex-wrap'], 'wrap')
})

test('真实总览区分自动投稿策略和稿件状态，制作间隙不退回待准备或开放手动运行',async()=>{
  const {default:Pipeline}=await server.ssrLoadModule('/src/views/PipelineView.vue')
  const pinia=createPinia(),nav=useNavStore(pinia)
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:uid',component:Pipeline}]})
  await router.push('/projects/flow-ui')
  nav.$patch({uid:'flow-ui',title:'流程状态回归',stepsLoaded:true,steps:[{key:'assemble',label:'合成成片',status:'done',fresh:true,selected:true}],workflow:{mode:'publish',mode_source:{kind:'series',name:'旅行系列'},run:{id:'r',mode:'publish',status:'running',phase:'准备发布资料',total:2,cursor:2}}})
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(RouterView)})})})
  app.use(pinia);app.use(router);setup(app)
  try{
    const html=await renderToString(app)
    assert.match(html,/执行终点/);assert.match(html,/自动投稿/);assert.match(html,/准备发布资料/)
    assert.match(html,/data-source-kind="series"/);assert.match(html,/停止完整流程/)
    assert.doesNotMatch(html,/独立确认，不随勾选步骤自动投稿|待准备/)
    const manual=[...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(m=>m[0]).find(b=>b.includes('仅运行勾选制作步骤'))
    assert.ok(manual);assert.match(manual,/disabled/)
    assert.equal(nav.busy,true)
  }finally{nav.reset();disposePinia(pinia)}
})

async function renderVideoInfo(seed: (panel: any) => void = () => {}) {
  const { default: Panel } = await server.ssrLoadModule('/src/components/VideoInfoPanel.vue')
  let panel: any
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/info', component: Panel, props: { uid: 'info-fixture', available: false } }] })
  await router.push('/info')
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
  }) })
  app.use(router)
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'VideoInfoPanel') { panel = this.$.setupState; seed(panel) }
  } })
  return { html: await renderToString(app), panel }
}

test('视频资料：旧项目无产物仍有独立入口，不把资料更新称为视频重跑', async () => {
  const { html } = await renderVideoInfo(panel => { panel.loading = false; panel.canRefresh = true })
  assert.match(html, /刷新平台资料/)
  assert.match(html, /整理中文标题与概要/)
  // 解释收进悬停提示，不再抢占内容区。
  assert.doesNotMatch(html, /刷新只更新本区资料|展示标题（AI/)
})

test('视频资料：明确区分原文、AI、字幕语言和可选评论，安全显示外部内容', async () => {
  const { html, panel } = await renderVideoInfo(panel => {
    panel.canComments = true
    panel.info = { title_original: '<script>bad()</script>', summary_status: 'generated',
      summary_zh: '旧概要', summary_stale: true, description: '完整作者简介',
      source: { platform: 'YouTube', url: 'javascript:alert(1)', view_count: 0, comment_count: null },
      content: { thumbnail: 'https://i.ytimg.com/vi/abc/hqdefault.jpg', tags: ['测试标签'], chapters: [{ title: '第一章', start_time: 15 }],
        captions: { manual: [{ language: 'en', name: 'English', formats: ['vtt'] }] } },
      comments: { count: 1, requested_limit: 50, items: [{ id: 'a', text: '<img src=x onerror=bad()>', like_count: 0 }] },
    }
  })
  assert.match(html, /视频简介/)
  assert.match(html, /内容概要/)
  assert.match(html, /中文标题与概要待更新/)
  assert.match(html, /字幕语言/)
  assert.match(html, /评论精选/)
  assert.match(html, /<img[^>]+src="\/api\/projects\/[^/]+\/steps\/download\/video-info\/cover\?v=/)
  assert.match(html, /href="https:\/\/i.ytimg.com\/vi\/abc\/hqdefault.jpg"/)
  assert.equal(panel.timecode(15), '0:15')
  assert.doesNotMatch(html, /href="javascript:|<script>bad|<img src=x/)
  assert.equal(panel.sourceRows.find((row: any) => row.label === '播放量').value, '0')
  assert.equal(panel.sourceRows.find((row: any) => row.label === '评论数').value, '未获取')
})

test('视频资料：评论请求提交精确数量，其他操作不偷偷采集评论', async t => {
  const { panel } = await renderVideoInfo()
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    if (options?.method !== 'POST') return { ok: true, json: async () => ({ info: null }) }
    requests.push({ url, body: JSON.parse(options.body) })
    return { ok: true, json: async () => ({ info: { comments: { count: 2 } } }) }
  })
  panel.commentLimit = 27
  await panel.runAction('comments')
  await panel.runAction('refresh')
  await panel.runAction('summarize')
  assert.deepEqual(requests.map(row => row.body), [{ comment_limit: 27 }, {}, {}])
  assert.ok(requests[0].url.endsWith('/video-info/comments'))
  assert.ok(requests[1].url.endsWith('/video-info/refresh'))
  panel.commentLimit = null
  await panel.runAction('comments')
  assert.equal(requests.length, 3)
  panel.loader.cancel()
})

test('视频资料：失败保留旧资料，重复点击不会创建第二个请求', async t => {
  const { panel } = await renderVideoInfo()
  panel.info = { summary_zh: '保留的概要' }
  let finish: any, count = 0
  t.mock.method(globalThis, 'fetch', (url: string, options: any) => {
    if (options?.method !== 'POST') return new Promise(() => {})
    count++
    return new Promise(resolve => { finish = resolve })
  })
  const first = panel.runAction('refresh')
  await panel.runAction('comments')
  assert.equal(count, 1)
  finish({ ok: false, json: async () => ({ message: '平台访问失败', suggestion: '旧资料已保留' }) })
  await first
  assert.equal(panel.info.summary_zh, '保留的概要')
  assert.match(panel.error, /平台访问失败/)
  assert.equal(panel.active, '')
  panel.loader.cancel()
})

for (const key of keys) {
  for (const state of states) {
    test(`${key}：${state.name}的实际步骤页可渲染`, async () => {
      const pinia = createPinia()
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: '/projects/:uid/steps/:key', component: StepView }],
      })
      await router.push(`/projects/render-fixture/steps/${key}`)
      const app = createSSRApp({ render: () => h(NMessageProvider, null, {
        default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
      }) })
      app.use(pinia)
      app.use(router)
      setup(app) // Naive UI 样式使用官方 SSR 适配，不依赖真实 document。
      const errors: string[] = []
      app.config.errorHandler = error => { errors.push(String(error)) }
      const nav = useNavStore(pinia)
      nav.$patch({
        uid: 'render-fixture', title: '旧项目回归样例', stepsLoaded: true,
        steps: [...keys, 'subtitles'].map(stepKey => ({
          key: stepKey, label: STEP_LABELS[stepKey], description: '步骤页面回归验证',
          status: state.status, fresh: state.fresh, progress: state.status === 'done' ? 100 : 0,
          selected: true, cfg: {}, artifacts: [], inputs: [], outputs: [],
          unready_inputs: [], affected_steps: [],
        })),
      })
      try {
        const html = await renderToString(app)
        assert.deepEqual(errors, [], '步骤页面不能在组件初始化或渲染时抛异常')
        assert.ok(html.includes(STEP_LABELS[key]), '必须显示当前步骤标题')
        const label = key === 'tts' && state.status === 'done' && !state.fresh
          ? '仍有待配音句' : state.label
        assert.ok(html.includes(label), `必须显示正确状态：${label}`)
        if (key === 'assemble') {
          assert.match(html, /选择成品方案/)
          assert.match(html, /成片版本/)
          assert.match(html, /继承规则/)
          assert.match(html, /成品文件/)
          assert.match(html, /同版本再次生成将替换自身/)
          assert.doesNotMatch(html, /当前字幕方案|加入本次清单|生成清单成品/)
          assert.doesNotMatch(html, /重做本步产物|n-checkbox--checked/)
        }
      } finally {
        nav.reset()
        disposePinia(pinia)
      }
    })
  }
}

test('下载失败页展示具体超时原因，不笼统承诺历史文件一定存在', async () => {
  const pinia = createPinia(), nav = useNavStore(pinia)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/projects/:uid/steps/:key', component: StepView },
  ] })
  await router.push('/projects/download-error/steps/download')
  nav.$patch({ uid: 'download-error', title: '下载失败回归', stepsLoaded: true, steps: [{
    key: 'download', label: '获取视频', status: 'failed', fresh: false, progress: 5,
    cfg: {}, artifacts: [], inputs: [], outputs: [], unready_inputs: [], affected_steps: [],
    error: { code: 'DOWNLOAD_FAIL', message: '视频下载超时，自动重试后仍未完成',
      suggestion: '请检查网络或代理连接后重试；这不是整步运行时限。' },
  }] })
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
  }) })
  app.use(pinia); app.use(router); setup(app)
  try {
    const html = await renderToString(app)
    assert.match(html, /视频下载超时，自动重试后仍未完成/)
    assert.match(html, /网络或代理/)
    assert.match(html, /DOWNLOAD_FAIL/)
    assert.match(html, /步骤日志中的具体原因与文件保留、清理记录/)
    assert.doesNotMatch(html, /正式产物会保留/)
  } finally { nav.reset(); disposePinia(pinia) }
})

test('实际成品组件：空选无请求，取消不执行，确认严格提交勾选方案', async t => {
  const { default: Panel } = await server.ssrLoadModule('/src/components/AssembleOutputsPanel.vue')
  const pinia = createPinia(), nav = useNavStore(pinia)
  let panel: any, shown: any
  const bridge = { setup() {
    const dialog = useDialog()
    t.mock.method(dialog, 'info', (options: any) => { shown = options; return {} })
    return () => h(Panel, { uid: 'fixture', artifacts: [
      {name:'final_src_hardsub_20260927_120000_123456.mp4',size:123,path:'fixture.mp4'},
    ], sourcePath: '', subtitlePath: '', subtitleName: '' })
  } }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(bridge) }),
  }) })
  app.use(pinia)
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssembleOutputsPanel') panel = this.$.setupState } })
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    if (!options?.method) return { ok: true, json: async () => ({ overlay: { items: [{ id: 'a' }] }, overlay_revision: 'rev-1' }) }
    requests.push({ url, body: JSON.parse(options.body) })
    return { ok: true, json: async () => ({ blockers: [], replacements:['src_hardsub'], comments_count: JSON.parse(options.body).outputs.some((o: any) => o.include_comments) ? 1 : 0 }) }
  })
  t.mock.method(nav, 'poll', async () => {})
  try {
    const html = await renderToString(app)
    assert.match(html, /尚未加入方案/)
    assert.match(html, /aria-label="已有组合成品"/)
    assert.match(html, /加入重生成/)
    assert.ok(panel.existingKeys.has('src_hardsub'))
    assert.equal(panel.currentKey, 'src')
    panel.addCurrent()
    assert.equal(panel.selected.length, 0) // 全关是原片，不隐式加入配音版。
    assert.ok(panel)
    await panel.submit()
    assert.deepEqual(requests, [])
    panel.toggle('src_hardsub', true)
    await panel.submit()
    assert.match(shown.content, /本次仅生成：原声 · 烧录字幕/)
    assert.match(shown.content, /将覆盖已有成品：原声 · 烧录字幕/)
    assert.doesNotMatch(shown.content, /新增一个版本|不覆盖已有/)
    shown.onNegativeClick()
    assert.equal(requests.length, 1)
    assert.ok(requests[0].url.endsWith('/output_plan'))
    await panel.submit()
    // 即便确认后组件内选择发生变化，也不能扩大已展示的请求范围。
    panel.toggle('dub', true)
    await shown.onPositiveClick()
    assert.ok(requests[2].url.endsWith('/outputs'))
    assert.deepEqual(requests[2].body, { outputs: [{ dubbing: false, burn_subtitles: true, include_comments: false }] })
    nav.$patch({ steps: [{ key: 'assemble', status: 'running',
      assembly_task: { scope: 'outputs', variants: ['src_hardsub'] } }] })
    assert.match(panel.taskSummary, /当前正式合成任务：原声 · 烧录字幕/)
    assert.match(panel.taskSummary, /不由页面勾选控制/)
    assert.equal(panel.choice.include_comments, false) // 有已确认方案也不能自动启用。
    nav.$patch({ steps: [] }); panel.selected = []
    panel.setDimension('include_comments', true); panel.addCurrent()
    panel.setDimension('dubbing', true); panel.setDimension('include_comments', false); panel.addCurrent()
    await nextTick(); await panel.submit()
    assert.match(shown.content, /叠加 1 条已确认评论/)
    panel.setDimension('dubbing', false); await nextTick()
    assert.deepEqual([...panel.selected], ['src_comments', 'dub']) // 三个选择只修改当前组合，清单独立保存。
    panel.toggle('src_comments', false)
    await shown.onPositiveClick()
    assert.deepEqual(requests.at(-1).body, { outputs: [
      { dubbing: false, burn_subtitles: false, include_comments: true },
      { dubbing: true, burn_subtitles: false, include_comments: false },
    ], overlay_revision: 'rev-1' })
  } finally { nav.reset(); disposePinia(pinia) }
})

test('成品回收需单独确认，使用确认时的文件版本；刷新不改生成清单', async t => {
  const { default: Panel } = await server.ssrLoadModule('/src/components/AssembleOutputsPanel.vue')
  const pinia = createPinia(), nav = useNavStore(pinia)
  const artifacts = reactive([
    {id:1,name:'final_dub_20260927_120000_123456.mp4',size:123,exists:true,file_revision:'old-version'},
    {id:2,name:'final_src_hardsub_20260927_120000_123456.mp4',size:123,exists:false,file_revision:'',record_revision:'missing-record'},
  ])
  let panel: any, shown: any, polls = 0
  const requests: any[] = [], events: boolean[] = []
  t.mock.method(nav, 'refreshSteps', async () => { polls++ })
  t.mock.method(globalThis, 'fetch', async (url:string, options:any) => {
    if (!options?.method) return {ok:true,json:async () => ({overlay:{items:[]}})}
    requests.push({url,body:JSON.parse(options.body)})
    return {ok:true,json:async () => ({status:'recycled',message:'已移到回收站'})}
  })
  const bridge = { setup() {
    const dialog = useDialog()
    t.mock.method(dialog, 'warning', (options:any) => {shown=options; return {}})
    return () => h(Panel,{uid:'fixture',artifacts,sourcePath:'',subtitlePath:'',subtitleName:'',onRecycleActive:(value:boolean) => events.push(value)})
  }}
  const app = createSSRApp({render:() => h(NMessageProvider,null,{default:() => h(NDialogProvider,null,{default:() => h(bridge)})})})
  app.use(pinia);app.use(createRouter({history:createMemoryHistory(),routes:[]}));setup(app)
  app.mixin({created() {if(this.$options.__name === 'AssembleOutputsPanel') panel=this.$.setupState}})
  try {
    const html = await renderToString(app)
    assert.match(html,/刷新列表|移到回收站/)
    assert.match(html,/磁盘上缺失/)
    assert.match(html,/删除记录/)
    assert.doesNotMatch(html,/missing-outputs/)
    assert.equal(panel.history.length,2)
    assert.deepEqual(panel.history.map((group:any) => group.key),['dub','src_hardsub'])
    panel.toggle('dub',true)
    await panel.refreshFiles()
    assert.equal(polls,1)
    assert.deepEqual([...panel.selected],['dub'])
    panel.recycle(artifacts[0])
    assert.equal(requests.length,0)
    assert.match(shown.content,/仅回收这一个文件/)
    shown.onNegativeClick();shown.onAfterLeave()
    assert.equal(requests.length,0)
    panel.recycle(artifacts[0])
    artifacts[0].file_revision = 'replaced-after-dialog'
    await shown.onPositiveClick();shown.onAfterLeave()
    assert.equal(requests.length,1)
    assert.ok(requests[0].url.endsWith('/recycle'))
    assert.deepEqual(requests[0].body,{artifact_id:1,file_revision:'old-version',confirmed:true})
    assert.deepEqual(events,[true,false])
    artifacts[0].exists=false
    assert.equal(panel.history.length,2)
    assert.deepEqual(panel.history.map((group:any) => group.key),['dub','src_hardsub'])
    assert.equal(panel.existingKeys.has('dub'),false)
    artifacts[0].exists=true
    assert.equal(panel.history.length,2)
    panel.forget(artifacts[1])
    assert.equal(requests.length,1)
    assert.match(shown.content,/仅删除本地成品记录/)
    artifacts[1].record_revision = 'updated-record'
    await shown.onPositiveClick();shown.onAfterLeave()
    assert.ok(requests[1].url.endsWith('/forget'))
    assert.equal(requests[1].body.record_revision,'missing-record')
    nav.$patch({publication:{status:'uploading'}})
    const previous = shown
    panel.recycle(artifacts[0])
    assert.equal(shown,previous)
    assert.equal(requests.length,2)
  } finally {nav.reset();disposePinia(pinia)}
})

test('删除缺失记录只等待写回执，慢刷新不拖住弹窗；写失败保留记录并允许重试', async t => {
  const { default: Panel } = await server.ssrLoadModule('/src/components/AssembleOutputsPanel.vue')
  const pinia = createPinia(), nav = useNavStore(pinia)
  const file = {id:1,name:'final_dub_20260927_120000_123456.mp4',size:123,exists:false,record_revision:'record-v1'}
  nav.$patch({uid:'fixture',steps:[{key:'assemble',status:'done',artifacts:[file]}]})
  let panel:any, shown:any, finishWrite:any, finishRefresh:any, posts=0
  const handle:any = {}, events:boolean[] = []
  const slowRefresh = new Promise<void>(resolve => {finishRefresh=resolve})
  t.mock.method(nav,'refreshSteps',() => slowRefresh)
  t.mock.method(globalThis,'fetch',async (_url,options) => {
    if (!options?.method) return {ok:true,json:async()=>({overlay:{items:[]}})}
    posts++
    return new Promise(resolve => {finishWrite=resolve})
  })
  const bridge = {setup() {
    t.mock.method(useDialog(),'warning',(options:any) => {shown=options;return handle})
    return () => h(Panel,{uid:'fixture',artifacts:nav.stepMap.assemble.artifacts,sourcePath:'',subtitlePath:'',subtitleName:'',onRecycleActive:(value:boolean)=>events.push(value)})
  }}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(bridge)})})})
  app.use(pinia);app.use(createRouter({history:createMemoryHistory(),routes:[]}));setup(app)
  app.mixin({created(){if(this.$options.__name==='AssembleOutputsPanel')panel=this.$.setupState}})
  const flush=async()=>{for(let i=0;i<24;i++)await Promise.resolve()}
  try {
    await renderToString(app)
    panel.toggle('dub',true);panel.forget(file)
    let settled=false
    const failure=shown.onPositiveClick().then((value:any)=>{settled=true;return value})
    await flush()
    assert.equal(settled,false);assert.equal(handle.loading,true);assert.equal(handle.closable,false)
    await shown.onPositiveClick();assert.equal(posts,1,'写操作仍在执行时不能重复删除')
    finishWrite({ok:false,json:async()=>({message:'文件状态变化，请刷新后重试'})})
    await flush()
    assert.equal(settled,true,'失败提示也不应等待刷新')
    assert.equal(await failure,false,'失败保持确认框，允许重试')
    assert.equal(nav.stepMap.assemble.artifacts.length,1)
    assert.equal(handle.loading,false);assert.equal(handle.negativeButtonProps.disabled,false)
    settled=false
    const success=shown.onPositiveClick().then(()=>{settled=true})
    await flush();assert.equal(posts,2);assert.equal(settled,false)
    finishWrite({ok:true,json:async()=>({status:'forgotten',message:'已删除缺失成品记录'})})
    await flush()
    assert.equal(settled,true,'成功弹窗不等待后续刷新完成')
    await success
    assert.equal(nav.stepMap.assemble.artifacts.length,0)
    assert.deepEqual([...panel.selected],['dub'])
    await shown.onPositiveClick();assert.equal(posts,2,'成功的确认不能重复提交')
    assert.deepEqual(events,[true,false,true,false])
    shown.onAfterLeave();assert.equal(panel.recycleConfirming,false)
  } finally {finishRefresh();await flush();nav.reset();disposePinia(pinia)}
})

test('全工程文件链接不会自动打开，点击防重复且失败后可重试', async t => {
  const { default: Link } = await server.ssrLoadModule('/src/components/FilePathLink.vue')
  let link:any, finish:any
  const requests:any[] = []
  t.mock.method(globalThis,'fetch',async (url,options) => {
    requests.push({url,body:JSON.parse(String(options?.body))})
    return new Promise(resolve => { finish = resolve })
  })
  const path = 'D:\\旅行 项目\\原片 & 1.mp4'
  const app = createSSRApp({render:() => h(NMessageProvider,null,{default:() => h(Link,{path,label:'原片'})})})
  setup(app)
  app.mixin({created() {if(this.$options.__name === 'FilePathLink')link=this.$.setupState}})
  const html = await renderToString(app)
  assert.match(html,/button[^>]*type="button"/)
  assert.match(html,/在资源管理器中定位/)
  assert.equal(requests.length,0)
  const first = link.reveal();await link.reveal()
  assert.equal(requests.length,1)
  assert.deepEqual(requests[0],{url:'/api/files/reveal',body:{path}})
  finish({ok:false,json:async () => ({message:'磁盘不可用'})});await first
  assert.equal(link.busy,false)
  const retry = link.reveal()
  finish({ok:true,json:async () => ({missing:true,message:'文件已缺失，已打开最近的现存目录'})});await retry
  assert.equal(requests.length,2)
  assert.equal(link.busy,false)
})

test('总览文件清单给出用途说明与可定位路径', async () => {
  const { default: Pipeline } = await server.ssrLoadModule('/src/views/PipelineView.vue')
  const pinia = createPinia(), nav = useNavStore(pinia)
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:uid',component:Pipeline}]})
  await router.push('/projects/fixture')
  nav.$patch({uid:'fixture',steps:[],stepsLoaded:true})
  const app = createSSRApp({render:() => h(NMessageProvider,null,{default:() => h(NDialogProvider,null,{default:() => h(RouterView)})})})
  app.use(pinia);app.use(router);setup(app)
  app.mixin({created() {if(this.$options.__name === 'PipelineView') {
    this.$.setupState.files = [{file:'srt/trans.srt',path:'D:\\项目\\srt\\trans.srt',description:'目标语言字幕',size:120}]
    this.$.setupState.fileDir = 'D:\\项目'
  }}})
  try {
    const html = await renderToString(app)
    assert.match(html,/>说明<\/th>/)
    assert.match(html,/目标语言字幕/)
    assert.match(html,/aria-label="定位 srt\/trans.srt"/)
    assert.match(html,/aria-label="定位 D:\\项目"/)
  } finally {nav.reset();disposePinia(pinia)}
})

test('试片跟随当前三维组合，原声无需配音依赖；检查期间修改选项不改变本次快照', async t => {
  const { default: Panel } = await server.ssrLoadModule('/src/components/AssemblePreviewPanel.vue')
  const pinia = createPinia(), nav = useNavStore(pinia), requests: any[] = []
  const choice = reactive({ dubbing: false, burn_subtitles: true, include_comments: true })
  let panel: any, release: any
  t.mock.method(nav, 'poll', async () => {})
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    if (!options?.method) return new Response(JSON.stringify({ overlay: { items: [{ id: 'a' }] }, overlay_revision: 'revision' }))
    requests.push({ url, body: JSON.parse(options.body) })
    if (url.endsWith('/output_plan')) return new Promise(resolve => { release = () => resolve(new Response('{"blockers":[]}')) })
    return new Response('{}')
  })
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Panel, {
    uid: 'fixture', artifacts: [], sourceUrl: '/isolated-video.mp4', choice,
  }) }) })
  app.use(pinia); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssemblePreviewPanel') panel = this.$.setupState } })
  try {
    const html = await renderToString(app)
    assert.match(html, /跟随上方“当前组合”/)
    assert.doesNotMatch(html, /默认方案|本次试片叠加已确认评论/)
    for (let i = 0; i < 10; i++) await Promise.resolve()
    panel.onMeta({ target: { duration: 200 } }); panel.setStart(1); panel.setEnd(3)
    assert.equal(panel.fmt(124.5), '2:04.5'); assert.equal(panel.canSubmit, true)
    const work = panel.submit(); await nextTick()
    choice.dubbing = true; choice.include_comments = false
    release(); await work
    assert.deepEqual(requests[0].body, { outputs: [{ dubbing: false, burn_subtitles: true, include_comments: true }], overlay_revision: 'revision' })
    assert.deepEqual(requests[1].body, { start: 1, end: 3, output: { dubbing: false, burn_subtitles: true, include_comments: true }, overlay_revision: 'revision' })
    choice.dubbing = false; choice.burn_subtitles = false
    assert.equal(panel.canSubmit, false)
  } finally { nav.reset(); disposePinia(pinia) }
})

test('字幕工作区直达页无配音运行入口，草稿离开确认合并且可取消', async t => {
  const pinia = createPinia(), nav = useNavStore(pinia)
  const router = createRouter({ history: createMemoryHistory(),
    routes: [{ path: '/projects/:uid/steps/:key', component: StepView }] })
  await router.push('/projects/fixture/steps/tts?panel=subtitles')
  let panel: any
  const prompts: any[] = []
  const bridge = { setup() {
    const dialog = useDialog()
    t.mock.method(dialog, 'warning', (options: any) => { prompts.push(options); return {} })
    return () => h(RouterView)
  } }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(bridge) }),
  }) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'DubbingPanel') panel = this.$.setupState } })
  nav.$patch({ uid: 'fixture', stepsLoaded: true, steps: [...keys, 'subtitles'].map(key => ({
    key, label: STEP_LABELS[key], status: 'done', fresh: true, cfg: {}, artifacts: [], inputs: [],
    outputs: [], unready_inputs: [], affected_steps: [],
  })) })
  try {
    const html = await renderToString(app)
    assert.match(html, /字幕工作区/)
    assert.match(html, /烧录效果预览/)
    assert.match(html, /拖动定位画面/)
    assert.match(html, /字幕预览进度/)
    assert.match(html, /精确到秒/)
    assert.match(html, /字幕设置/)
    assert.match(html, /字幕文件/)
    assert.match(html, /返回校对与配音/)
    assert.doesNotMatch(html, /重做全项目配音…/)
    assert.equal(panel.activePane, 'subtitles')
    panel.subtitleDirty = true
    // 连续点击导航只出现一次对话框，取消不能清除草稿。
    const first = panel.confirmLeave(), second = panel.confirmLeave()
    assert.equal(first, second)
    assert.equal(prompts.length, 1)
    prompts[0].onNegativeClick()
    assert.equal(await first, false)
    assert.equal(panel.subtitleDirty, true)
    const leave = panel.confirmLeave()
    assert.equal(prompts.length, 2)
    prompts[1].onPositiveClick()
    assert.equal(await leave, true)
    panel.subtitleDirty = false
    assert.equal(panel.confirmLeave(), true)
    // 标签切换不离开当前步骤，保留已访问的工作区实例。
    panel.changePane('editor')
    await nextTick()
    assert.equal(panel.subtitlesVisited, true)
    assert.equal(panel.activePane, 'editor')
  } finally { nav.reset(); disposePinia(pinia) }
})

function commentsFixture() {
  return { rows: [
    { id: 'a', source_hash: 'a1', text: 'Original A <script>bad()</script>', text_zh: '中文评论 A', author: '观众 A' },
    { id: 'b', source_hash: 'b1', text: 'Original B', text_zh: '', author: '观众 B' },
  ], filter: { items: [{ id: 'a', reason: '具体体验', score: 90 }] }, filter_stale: false,
  defaults: { start: 5, interval: 15, duration: 6, position: 'top_left', font_size: 36, margin: 60, show_author: false,
    language_mode: 'translated', source_first: false, background_color: '#000000', background_opacity: 50 },
  fields:[{path:'comments.background_color',label:'文字底色',type:'color'}, {path:'comments.background_opacity',label:'底色不透明度',type:'int'}, {path:'comments.outline_color',label:'描边颜色',type:'color'}, {path:'comments.outline',label:'描边粗细',type:'float'}],
  overlay: null, overlay_revision: 'rev-0', job: null }
}

async function renderComments(t: any, seed: (panel: any) => void = () => {}) {
  const { default: Panel } = await server.ssrLoadModule('/src/components/CommentStudioPanel.vue')
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/projects/:uid/steps/:key', component: Panel, props: route => ({ uid: route.params.uid }) },
  ] })
  await router.push('/projects/comments-fixture/steps/download')
  let panel: any
  const prompts: any[] = []
  const bridge = { setup() {
    const dialog = useDialog()
    for (const key of ['info', 'warning']) t.mock.method(dialog, key, (options: any) => { prompts.push(options); return {} })
    return () => h(RouterView)
  } }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(bridge) }),
  }) })
  app.use(router); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'CommentStudioPanel') {
      panel = this.$.setupState; const value = commentsFixture(); panel.state = value; panel.hydrate(value); seed(panel)
    }
  } })
  return { html: await renderToString(app), panel, prompts, router }
}

test('评论 UI 中文优先、原文对照安全显示，AI 建议不自动勾选', async t => {
  const { html, panel } = await renderComments(t, panel => { panel.compare = true })
  assert.match(html, /中文评论 A/)
  assert.match(html, /Original A &lt;script&gt;/)
  assert.doesNotMatch(html, /<script>bad/)
  assert.match(html, /全部翻译成中文/)
  assert.equal(panel.items.length, 0)
  assert.equal(panel.dirty, false)
  panel.useCandidates()
  assert.deepEqual(panel.items.map((row: any) => row.id), ['a'])
  assert.equal(panel.items[0].display_text, '中文评论 A')
  assert.equal(panel.dirty, true)
  panel.onlySuggested = true
  panel.toggle(panel.rows[1], true)
  assert.equal(panel.items.length, 2) // 隐藏项仍被选中，不能静默丢弃。
  panel.move(1, -1)
  assert.equal(panel.items[0].id, 'b')
})

test('评论来源与统一标识一致；修改立即标项目，恢复继承回到真实模板', async t => {
  const {html,panel} = await renderComments(t, panel => {
    panel.state.configuration = { sources:{'comments.outline':{kind:'series',name:'项目设置'}},
      parent_sources:{'comments.outline':{kind:'series',name:'项目设置'}}, defaults:{'comments.outline':2}, overrides:{} }
  })
  assert.match(html,/data-source-kind="series"/)
  assert.equal(panel.styleSource('outline').kind,'series')
  panel.setStyle('outline',0)
  assert.equal(panel.styleSource('outline').kind,'project')
  panel.inheritStyle('outline')
  assert.equal(panel.styleSource('outline').kind,'series')
  assert.equal(panel.settings.outline,2)
})

test('评论翻译只发显式请求，重复点击合并，译文更新不覆盖人工上屏草稿', async t => {
  const { panel } = await renderComments(t)
  panel.toggle(panel.rows[0], true); panel.items[0].display_text = '人工核对文字'
  const value = commentsFixture(); value.rows[1].text_zh = '中文 B'
  let resolve: any, posts = 0
  t.mock.method(globalThis, 'fetch', (url: string, options: any) => {
    if (!options?.method) return Promise.resolve({ ok: true, json: async () => value })
    posts++; assert.ok(url.endsWith('/comments/translate'))
    return new Promise(done => { resolve = done })
  })
  const task = panel.process('translate')
  await panel.process('translate')
  assert.equal(posts, 1)
  resolve({ ok: true, json: async () => value }); await task
  assert.equal(panel.rows[1].text_zh, '中文 B')
  assert.equal(panel.items[0].display_text, '人工核对文字')
  assert.equal(panel.dirty, true)
})

test('评论确认可取消；保存是独立快照，不触发合成，失败保留草稿', async t => {
  const { panel, prompts } = await renderComments(t)
  panel.toggle(panel.rows[0], true)
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    const body = JSON.parse(options.body); requests.push({ url, body })
    return { ok: false, json: async () => ({ message: '方案已在其他页面修改' }) }
  })
  panel.confirmPlan()
  assert.match(prompts[0].content, /不会自动合成/)
  prompts[0].onNegativeClick(); assert.equal(requests.length, 0)
  panel.confirmPlan(); panel.items[0].display_text = '确认后模拟变化'
  await prompts[1].onPositiveClick()
  assert.equal(requests.length, 1)
  assert.ok(requests[0].url.endsWith('/comments/overlay'))
  assert.equal(requests[0].body.items[0].display_text, '中文评论 A')
  assert.equal(requests[0].body.revision, 'rev-0')
  assert.match(panel.error, /其他页面修改/)
  assert.equal(panel.items[0].display_text, '确认后模拟变化')
  assert.equal(panel.dirty, true)
})

test('评论草稿只在修改后拦截离开，确认合并；超长文本不静默截断', async t => {
  const { panel, prompts } = await renderComments(t)
  assert.equal(panel.beforeLeave(), true)
  panel.rows[0].text_zh = '长'.repeat(241)
  panel.toggle(panel.rows[0], true)
  assert.equal(panel.items[0].display_text.length, 241)
  assert.equal(panel.invalid, true)
  panel.confirmPlan(); assert.equal(prompts.length, 0)
  const first = panel.beforeLeave(), second = panel.beforeLeave()
  assert.equal(first, second); assert.equal(prompts.length, 1)
  prompts[0].onNegativeClick(); assert.equal(await first, false)
  assert.equal(panel.dirty, true)
  const leave = panel.beforeLeave(); prompts[1].onPositiveClick(); assert.equal(await leave, true)
})

test('评论旧轮询不能覆盖新读取；外部方案变化不覆盖本地草稿', async t => {
  const { panel } = await renderComments(t)
  const old = commentsFixture(), fresh = commentsFixture()
  fresh.overlay_revision = 'rev-1'; fresh.rows[0].text_zh = '新译文'
  let resolve: any, count = 0
  t.mock.method(globalThis, 'fetch', () => ++count === 1 ? new Promise(done => { resolve = done })
    : Promise.resolve({ ok: true, json: async () => fresh }))
  panel.toggle(panel.rows[0], true)
  const pending = panel.load(); await panel.load()
  resolve({ ok: true, json: async () => old }); await pending
  assert.equal(panel.rows[0].text_zh, '新译文')
  assert.equal(panel.items[0].display_text, '中文评论 A')
  assert.equal(panel.revision, 'rev-0')
  assert.equal(panel.planChanged, true)
})

test('字幕与评论共用真实刻度控件：完整起点、安全提示与禁用状态', async () => {
  const { default: Timeline } = await server.ssrLoadModule('/src/components/PreviewTimeline.vue')
  for (const kind of ['字幕', '评论']) {
    const cues = Array.from({ length: 200 }, (_, index) => ({ start: index, end: index + .8, label: `第 ${index + 1} 条 <script>外部内容</script>` }))
    cues.push({ start: 301, end: 302, label: '越界内容' })
    const app = createSSRApp({ render: () => h(Timeline, { value: 5.1, max: 300, cues, kind, label: `${kind}预览进度`, disabled: true }) })
    setup(app)
    const html = await renderToString(app)
    assert.equal((html.match(/class="cue-marker/g) || []).length, 200)
    assert.equal((html.match(/aria-label="定位到/g) || []).length, 200)
    assert.match(html, /&lt;script&gt;外部内容/)
    assert.doesNotMatch(html, /<script>|越界内容/)
    assert.match(html, /disabled tabindex="-1"/)
    assert.match(html, new RegExp(`上一处${kind}`))
  }
})

test('双语评论直接编辑两种文字，共用底色与透明控件，不再提供核对原文按钮', async t => {
  const { html, panel } = await renderComments(t, panel => {
    panel.toggle(panel.rows[0], true); panel.settings.language_mode = 'bilingual'
  })
  assert.match(html, /第 1 条中文上屏文字/)
  assert.match(html, /第 1 条原文上屏文字/)
  assert.match(html, /文字底色/)
  assert.match(html, /底色不透明度/)
  assert.match(html, /描边颜色/)
  assert.match(html, /描边粗细/)
  assert.match(html, /评论配色预设/)
  assert.match(html, /刻度为评论开始点/)
  assert.match(html, /定位到 00:05.0/)
  assert.match(html, /紧凑评论列表/)
  assert.doesNotMatch(html, /核对原文|采用当前译文/)
  assert.equal(panel.settings.position, 'top_left')
  assert.equal(panel.settings.outline_color, '#000000')
  assert.equal(panel.settings.outline, 2)
  assert.equal(panel.items[0].source_text, panel.rows[0].text)
  panel.settings.background_opacity = 0
  assert.equal(panel.invalid, false)
  panel.items[0].display_text = ''
  assert.equal(panel.invalid, true)
  panel.settings.language_mode = 'original'
  assert.equal(panel.invalid, false)
})

test('评论切换预设不改时间、底色、粗细或入选文字，只在确认时保存两色', async t => {
  const { panel, prompts } = await renderComments(t, p => {
    p.toggle(p.rows[0], true); p.settings.color = '#F7F3E8'; p.settings.outline_color = '#202631'
    p.baseline = p.snapshot
  })
  const items = JSON.stringify(panel.items), before = { ...panel.settings }, requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    requests.push(JSON.parse(init.body)); return { ok: false, json: async () => ({ message: '模拟保存冲突' }) }
  })
  assert.equal(panel.dirty, false)
  panel.applyTextColors('sunshine')
  assert.equal(panel.dirty, true); assert.equal(requests.length, 0)
  assert.deepEqual({ ...panel.settings }, { ...before, color: '#FFD65A', outline_color: '#30213A' })
  assert.equal(JSON.stringify(panel.items), items)
  panel.applyTextColors('ivory'); assert.equal(panel.dirty, true, '再次明确选择同值也会固定本项目配色')
  panel.applyTextColors('sunshine'); panel.confirmPlan(); await prompts[0].onPositiveClick()
  assert.equal(requests[0].settings.color, '#FFD65A'); assert.equal(requests[0].settings.outline_color, '#30213A')
  assert.equal(panel.settings.color, '#FFD65A', '保存失败保留草稿')
  panel.saving = true; panel.applyTextColors('rose'); assert.equal(panel.settings.color, '#FFD65A')
})

test('真实配色选择器显示匹配名称、自定义、禁用与零描边提醒', async () => {
  const { default: Picker } = await server.ssrLoadModule('/src/components/TextColorPresets.vue')
  for (const [color, outlineColor, label] of [['#F7F3E8', '#202631', '暖象牙'], ['#FFD65A', '#30213A', '晴日金'], ['#112233', '#000000', '自定义']]) {
    const app = createSSRApp({ render: () => h(Picker, { kind: '字幕', color, outlineColor, outline: 0, disabled: true }) })
    setup(app)
    const html = await renderToString(app)
    assert.match(html, new RegExp(`字幕配色预设：${label}`))
    assert.match(html, /描边已关闭/); assert.match(html, /disabled/)
    assert.match(html, /只替换文字与描边颜色/)
  }
})

test('打开的真实配色弹层完整显示 16 色，分类后仅展示对应卡片', async () => {
  const { default: Picker } = await server.ssrLoadModule('/src/components/TextColorPresets.vue')
  for (const [group, count] of [['all', 16], ['warm', 5], ['fresh', 5], ['ink', 5], ['soft', 1]] as const) {
    const app = createSSRApp({ render: () => h(Picker, { kind: '字幕', color: '#F7F3E8', outlineColor: '#202631', outline: 2 }) })
    setup(app)
    app.mixin({ created() {
      if (this.$options.__name === 'TextColorPresets') { this.$.setupState.open = true; this.$.setupState.group = group }
    } })
    const context: any = {}
    const html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
    assert.equal((html.match(/class="palette-option/g) || []).length, count)
    assert.match(html, /配色依据与使用提示/)
    if (group === 'all') { assert.match(html, /晴日金/); assert.match(html, /暖象牙/); assert.match(html, /靛蓝瓷/) }
  }
})

test('评分缓存可即时改变前 N 条且不调用 AI，应用数量保留编辑并确认移出', async t => {
  const { panel, prompts } = await renderComments(t, panel => {
    panel.state.rows[1].text_zh = '中文评论 B' // 排序测试使用可上屏评论，缺译文应另行自动排除。
    panel.state.filter = { items: [], assessments: [
      { id: 'a', score: 80, eligible: true, reason: '共鸣', outcome: 'selected' },
      { id: 'b', score: 90, eligible: true, reason: '幽默', outcome: 'limit' },
    ] }
  })
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; throw new Error('本地调数量不能请求接口') })
  panel.limit = 1
  assert.deepEqual(panel.suggestions.map((r: any) => r.id), ['b'])
  assert.equal(panel.assessmentLabel('b'), '90 分')
  assert.match(panel.assessmentReason('a'), /不在当前前 N 条/)
  panel.toggle(panel.rows[0], true); panel.items[0].display_text = '保留人工改写'
  panel.limit = 2
  assert.equal(panel.items.length, 1) // 仅调数量不改变待上屏清单。
  panel.useCandidates()
  assert.deepEqual(panel.items.map((r: any) => r.id), ['b', 'a'])
  assert.equal(panel.items[1].display_text, '保留人工改写')
  panel.limit = 1; panel.useCandidates()
  assert.equal(panel.items.length, 2)
  prompts[0].onNegativeClick?.()
  assert.equal(panel.items.length, 2)
  panel.useCandidates(); prompts[1].onPositiveClick()
  assert.deepEqual(panel.items.map((r: any) => r.id), ['b'])
  panel.items[0].display_text = '原文更新前的人工改写'
  panel.rows.find((row: any) => row.id === 'b').source_hash = 'updated-source'
  panel.useCandidates()
  assert.match(prompts[2].content, /替换 1 条原文已变化/)
  assert.equal(panel.items[0].display_text, '原文更新前的人工改写')
  prompts[2].onPositiveClick()
  assert.equal(panel.items[0].source_hash, 'updated-source')
  assert.equal(requests, 0)
})

test('评分失败或未更新时不编造淘汰理由；修改标准不悄悄触发新评分', async t => {
  const { panel } = await renderComments(t)
  assert.match(panel.assessmentReason('b'), /旧版筛选未记录/)
  let requests = 0
  t.mock.method(globalThis, 'fetch', () => { requests++; return new Promise(() => {}) })
  panel.prompt = '只保留让人有共鸣的反馈'
  panel.limit = 20
  assert.equal(requests, 0)
  panel.state.filter_stale = true
  assert.equal(panel.suggestions.length, 0)
})
