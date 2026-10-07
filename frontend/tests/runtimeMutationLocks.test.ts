import assert from 'node:assert/strict'
import test, { before, after, afterEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, execution: any, navModule: any
const components: Record<string, any> = {}
const animationProperties = ['window', 'requestAnimationFrame', 'cancelAnimationFrame'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  for (const [name, path] of Object.entries({ CommentStudioPanel: 'components', VideoInfoPanel: 'components',
    DubbingPanel: 'components', BiliContentImport: 'components', PublishView: 'views', ProjectsView: 'views', StepView: 'views', PipelineView: 'views' })) {
    components[name] = (await server.ssrLoadModule(`/src/${path}/${name}.vue`)).default
  }
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
  navModule = await server.ssrLoadModule('/src/stores/nav.ts')
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明网络请求') })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', { configurable: true, value: () => {} })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  } })
})
afterEach(() => execution.setNavigationExecution('', false, true))
after(async () => {
  mock.restoreAll()
  for (const [key, descriptor] of animationProperties) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
  await server.close()
})

async function render(name: string, props: any = {}, seed: (page: any) => void = () => {}, routePath = '/projects/fixture/steps/download') {
  const component = components[name], pinia = createPinia(), nav = navModule.useNavStore(pinia)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: name === 'StepView' ? '/projects/:uid/steps/:key' : name === 'PipelineView' ? '/projects/:uid' : '/:pathMatch(.*)*', component: defineComponent({ render: () => h(component, props) }) }] })
  await router.push(routePath)
  let page: any
  const dialogs: any[] = []
  const Probe = defineComponent({ setup() {
    const dialog = useDialog()
    dialog.warning = dialog.info = ((options: any) => { dialogs.push(options); return { destroy() {} } }) as any
    return () => h(RouterView)
  } })
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Probe))) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === name) { page = this.$.setupState; seed(page) } } })
  return { page: await (async () => { const html = await renderToString(app); return { state: page, html } })(), dialogs, nav,
    cleanup: () => { nav.reset(); disposePinia(pinia) } }
}
const comments = () => ({ rows: [{ id: 'a', author: '甲', text: 'Original A', text_zh: '中文 A', source_hash: 'a' },
  { id: 'b', author: '乙', text: 'Original B', text_zh: '中文 B', source_hash: 'b' }],
  available_assessments: [{ id: 'a', eligible: true, score: 90, reason: '旅行体验' }],
  overlay: { items: [{ id: 'a', text: 'Original A', source_text: 'Original A', display_text: '当前中文', source_hash: 'a' }] },
  overlay_revision: 'rev', defaults: {}, configuration: { values: {}, defaults: {}, overrides: {}, sources: {}, fingerprint: 'cfg' }, fields: [], job: null })
function seedComments(page: any) { const data = comments(); page.state = data; page.hydrate(data) }
const settings = { title: '原标题', description: '简介', tags: ['旅行'], dynamic: '', copyright: 3, source: 'https://example.com/video' }
function record(status = 'draft') { return { id: 'pub', project_uid: 'fixture', revision: 2, status, receipt: {},
  base_settings: settings, base_origins: {}, snapshot: status === 'draft' ? {} : { settings, base_settings: settings, origins: {}, parts: [] },
  draft: { account_id: '', template_id: '', cover_id: '', parts: [], overrides: {}, content: {} } } }
function seedPublication(page: any) { page.catalog = { accounts: [], defaults: settings, worker_ready: true }; page.source = { files: [] }; page.apply(record()) }
function buttons(html: string) { return [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(row => row[0]) }
function button(html: string, label: string) { const found = buttons(html).find(row => row.replace(/<[^>]*>/g, '').trim() === label); assert.ok(found, label); return found }

test('评论方案在项目运行时不可改动，但原文、对照与只读重读仍可用', async t => {
  execution.setNavigationExecution('fixture', true)
  const result = await render('CommentStudioPanel', { uid: 'fixture' }, seedComments)
  const page = result.page.state, before = JSON.stringify({ items: page.items, settings: page.settings }), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => comments() } as any })
  try {
    page.toggle(comments().rows[1], true); page.setStyle('font_size', 72); page.inheritStyle('font_size'); page.useCandidates(); page.move(0, 1)
    page.confirmPlan(); await page.process('filter'); await page.process('translate')
    assert.equal(JSON.stringify({ items: page.items, settings: page.settings }), before)
    assert.equal(result.dialogs.length, 0); assert.equal(calls.length, 0)
    assert.match(button(result.page.html, '确认上屏方案（1 条）'), /disabled/)
    assert.match(result.page.html, /原文、评分和日志仍可查看/)
    page.compare = true; page.onlySuggested = true; await page.load()
    assert.equal(page.compare, true); assert.equal(calls.length, 1); assert.equal(calls[0].options, undefined)
  } finally { result.cleanup() }
})

test('评论确认窗口打开后任务启动，保存前重新拦截并保留草稿', async () => {
  const result = await render('CommentStudioPanel', { uid: 'fixture' }, seedComments), page = result.page.state
  try {
    page.confirmPlan(); assert.equal(result.dialogs.length, 1)
    execution.setNavigationExecution('fixture', true)
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(page.saving, false); assert.equal(page.items[0].display_text, '当前中文')
    assert.match(page.error, /正在执行|正在运行/)
  } finally { result.cleanup() }
})

test('发布草稿运行锁覆盖文案、账号、分P、封面和保存，刷新成片保持只读', async t => {
  execution.setNavigationExecution('fixture', true)
  const result = await render('PublishView', {}, seedPublication, '/publish'), page = result.page.state, before = JSON.stringify(page.draft), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ files: [] }) } as any })
  try {
    page.change('title', '不能覆盖'); page.inherit('title'); page.selectAccount('123'); page.addPart({ id: 1, version_name: '版本' }, true)
    await page.setCover(() => { throw new Error('不应上传封面') }); await page.save(); await page.upload(true); await page.submit()
    assert.equal(JSON.stringify(page.draft), before); assert.equal(calls.length, 0)
    assert.match(button(result.page.html, '保存资料'), /disabled/)
    await page.refreshFiles(); assert.equal(calls.length, 1); assert.equal(calls[0].options.method, 'GET')
    assert.match(result.page.html, /稿件、成片和回执仍可查看/)
  } finally { result.cleanup() }
})

test('当前来源运行中不能新建或复制草稿，也不阻止查看发布记录', async () => {
  execution.setNavigationExecution('fixture', true)
  const result = await render('PublishView', {}, page => { seedPublication(page); page.projectUid = 'fixture'; page.apply(record('rejected')) }, '/publish')
  try {
    await result.page.state.create(); await result.page.state.copy(); await result.page.state.createReviewDraft()
    assert.equal(result.page.state.current.id, 'pub'); assert.equal(result.dialogs.length, 0)
    assert.match(button(result.page.html, '复制为新草稿'), /disabled/)
    assert.match(button(result.page.html, '新建发布草稿'), /disabled/)
  } finally { result.cleanup() }
})

test('平台导入只锁保存到目标项目，读取候选仍可用，确认后的运行变化会阻断', async t => {
  const result = await render('BiliContentImport', { accountId: '123', projects: [{ uid: 'fixture', title: '目标' }], defaultProject: 'fixture' }), page = result.page.state
  const caption = { import_key: 'candidate', language: '中文', lines: [{ start: 0, end: 1, text: '字幕' }] }, calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ items: [], total: 0 }) } as any })
  try {
    page.importData(caption, 'subtitle'); assert.equal(result.dialogs.length, 1)
    execution.setNavigationExecution('fixture', true)
    await result.dialogs[0].onPositiveClick(); assert.equal(calls.length, 0)
    page.importData(caption, 'subtitle'); assert.equal(result.dialogs.length, 1)
    page.bvid = 'BV1234567890'; await page.fetchContent('comments')
    assert.equal(calls.length, 1); assert.match(String(calls[0].url), /\/studio\/read\/comments$/)
  } finally { result.cleanup() }
})

test('已拖动的混音在任务启动后不发迟到PUT，保留试听值和待保存状态', async t => {
  const result = await render('DubbingPanel', { uid: 'fixture' }), page = result.page.state, calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({}) } as any })
  try {
    page.mixEdits.change('voice', .35)
    execution.setNavigationExecution('fixture', true)
    await page.mixEdits.save()
    assert.equal(calls.length, 0); assert.equal(page.volDub, .35); assert.equal(page.mixEdits.pending.value, true)
    assert.match(page.mixEdits.error.value, /正在执行|正在运行/)
    assert.equal(page.playbackDisabled, false)
  } finally { await page.mixEdits.dispose(); result.cleanup() }
})

test('项目改名在运行时不能打开或提交，已有编辑值保持不变', async () => {
  const entry = { uid: 'fixture', title: '当前标题', status: 'processing', running_step: 'split', steps: [], dir_exists: true }
  const result = await render('ProjectsView', {}, page => { page.projects = [entry]; page.loading = false }, '/projects'), page = result.page.state
  try {
    page.openRename(entry); assert.equal(page.renaming, null)
    page.renaming = { ...entry, running_step: null }; page.renameTitle = '保留的手动标题'
    await page.applyRename()
    assert.equal(page.renaming.title, '当前标题'); assert.equal(page.renameTitle, '保留的手动标题')
  } finally { result.cleanup() }
})

test('实时显示阈值接收本窗口和其它窗口更新，非法内容及迟到旧读取不覆盖新值', async t => {
  const result = await render('StepView', {}, () => {}, '/projects/fixture/steps/split'), page = result.page.state
  let release: any
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { release = resolve }))
  try {
    const reading = page.loadLlmDisplayConfig()
    page.onLlmDisplayChanged({ detail: { reference_distance_chars: 900 } }); assert.equal(page.llmReferenceDistance, 900)
    release({ ok: true, json: async () => ({ reference_distance_chars: 500 }) }); await reading
    assert.equal(page.llmReferenceDistance, 900)
    page.onLlmDisplayStorage({ key: 'dubforge:llm-display-changed', newValue: JSON.stringify({ reference_distance_chars: 1300, at: 1 }) })
    assert.equal(page.llmReferenceDistance, 1300)
    page.onLlmDisplayStorage({ key: 'different', newValue: JSON.stringify({ reference_distance_chars: 250 }) })
    page.onLlmDisplayStorage({ key: 'dubforge:llm-display-changed', newValue: 'invalid json' })
    page.onLlmDisplayChanged({ detail: { reference_distance_chars: 'broken' } })
    assert.equal(page.llmReferenceDistance, 1300)
    page.applyLlmDisplayChange(9000); assert.equal(page.llmReferenceDistance, 5000)
  } finally { result.cleanup() }
})

test('总览勾选只发一份冻结PUT，预检等待期间开始运行或状态锁定均不修改勾选', async t => {
  const result = await render('PipelineView', {}, page => { page.nav.uid = 'fixture'; page.nav.steps = []; page.nav.autoSteps = null }, '/projects/fixture')
  const page = result.page.state, calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', (url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve }) })
  try {
    const first = page.toggleAuto('tts', false)
    await page.toggleAuto('assemble', false)
    assert.equal(calls.length, 1); assert.equal(page.nav.autoStepsSaving, true)
    assert.equal(calls[0].options.method, 'PUT')
    assert.deepEqual(JSON.parse(calls[0].options.body).steps, navModule.STEP_ORDER.filter((key: string) => key !== 'tts'))
    finish({ ok: true, json: async () => ({ ok: true }) }); await first
    assert.equal(page.nav.autoStepsSaving, false)

    page.nav.autoSteps = null
    page.nav.steps = [{ key: 'split', status: 'running' }]
    await page.toggleAuto('tts', false); assert.equal(calls.length, 1); assert.equal(page.nav.autoSteps, null)
    page.nav.steps = []
    execution.setNavigationExecution('', false, true)
    t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({ locked: false }) } as any))
    await page.editability.refresh(); page.editability.observing.value = true
    t.mock.method(globalThis, 'fetch', (url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve }) })
    const checking = page.toggleAuto('tts', false)
    assert.equal(calls.length, 2); assert.match(String(calls[1].url), /\/editability$/)
    page.nav.steps = [{ key: 'split', status: 'queued' }]
    finish({ ok: true, json: async () => ({ locked: false }) }); await checking
    assert.equal(page.nav.autoSteps, null); assert.equal(page.nav.autoStepsSaving, false)
    assert.equal(calls.filter(call => call.options.method === 'PUT').length, 1)

    page.nav.steps = []
    t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({ locked: true, reason: '项目正在上传' }) } as any))
    await page.editability.refresh()
    await page.toggleAuto('tts', false); assert.equal(page.nav.autoSteps, null)
  } finally { page.editability.observing.value = false; result.cleanup() }
})
