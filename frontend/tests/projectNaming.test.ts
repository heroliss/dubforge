import assert from 'node:assert/strict'
import test, { before, after, beforeEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

// 只加载本地组件和离线接口桩，不连接后端或运行项目。
let server: ViteDevServer, Batch: any, Projects: any, execution: any
const root = fileURLToPath(new URL('..', import.meta.url))
const ok = (value: any) => ({ ok: true, json: async () => value })
function deferred() {
  let resolve: any
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const row = (index: number, fields: any = {}) => ({ index, title: '名称' + index,
  source: 'https://example.com/' + index, template: 'series:travel', template_name: '旅行',
  settings: { 'comments.count': 5 }, selectable: true, errors: [], warnings: [], duplicates: [], ...fields })
const project = (fields: any = {}) => ({ uid: 'fixture', title: '我的管理名称', title_mode: 'manual',
  status: 'created', running_step: null, steps: [], params: {}, dir_exists: true, ...fields })

before(async () => {
  server = await createServer({ root, cacheDir: root + '/.test-vite-cache', configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Batch = (await server.ssrLoadModule('/src/components/BatchProjectImport.vue')).default
  Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明的网络请求') })
})
beforeEach(() => execution.setNavigationExecution('', false, true))
after(async () => { mock.restoreAll(); await server.close() })

async function render(component: any, seed: (page: any) => void) {
  let page: any
  const pinia = createPinia()
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/:pathMatch(.*)*', component: { render: () => null } },
  ] })
  await router.push('/projects')
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () =>
    h(NDialogProvider, null, { default: () => h(component) }) }) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === (component === Batch ? 'BatchProjectImport' : 'ProjectsView')) {
    page = this.$.setupState; seed(page)
  } } })
  const context: any = {}
  const html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  return { page, html, cleanup: () => disposePinia(pinia) }
}
function button(html: string, label: string) {
  const match = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .find(row => row[0].replace(/<[^>]*>/g, '').trim() === label)
  assert.ok(match, label)
  return match[0]
}

test('批量预览显示自动与自定义来源，解析时不抓取标题或创建项目', async () => {
  const items = [row(0, { title: 'YouTube 视频 · abc', title_mode: 'auto', requested_title: '' }),
    row(1, { title: '北京旅行第1集', title_mode: 'manual', requested_title: '北京旅行第1集' })]
  const rendered = await render(Batch, page => {
    page.show = true; page.plan = { items, selected: [0, 1], total: 2 }; page.selected = [0, 1]
  })
  try {
    assert.match(rendered.html, /未填名称时自动命名，获取视频资料后优先使用中文标题/)
    assert.match(rendered.html, /自动名称/); assert.match(rendered.html, /自定义名称/)
    assert.match(rendered.html, /YouTube 视频 · abc/); assert.match(rendered.html, /北京旅行第1集/)
  } finally { rendered.cleanup() }
})

test('失败条目重导保留原命名意图，旧回执不猜自动模式', async t => {
  const items = [
    row(0, { title: 'YouTube 视频 · auto-id', title_mode: 'auto', requested_title: '', status: 'error' }),
    row(1, { title: '显示名称已变化', title_mode: 'manual', requested_title: '我的原始名称', status: 'error' }),
    row(2, { title: '旧回执的名称', status: 'error' }),
    row(3, { title: '仅记录自动模式', title_mode: 'auto', status: 'error' }),
  ]
  const rendered = await render(Batch, page => {
    page.result = { complete: false, running: false, items }; page.request = { request_id: 'old', selected: [0, 1, 2, 3] }
  })
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), body: JSON.parse(String(options?.body)) })
    return ok({ items, selected: [0, 1, 2, 3], total: 4 })
  })
  try {
    rendered.page.retryFailedAsNew()
    assert.equal(calls.length, 1); assert.equal(calls[0].url, '/api/project-import/preview')
    const projects = JSON.parse(calls[0].body.text).projects
    assert.deepEqual(projects.map((entry: any) => entry.title), ['', '我的原始名称', '旧回执的名称', ''])
    assert.deepEqual(projects[0].settings, { 'comments.count': 5 })
    assert.equal(projects[0].template, 'series:travel')
    assert.equal(rendered.page.request, null)
  } finally { rendered.cleanup() }
})

test('使用视频标题提交自动模式，空手工输入不阻止恢复自动命名', async t => {
  const entry = project()
  const rendered = await render(Projects, page => {
    page.projects = [entry]; page.loading = false; page.renaming = entry; page.renameTitle = ''
  })
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), method: options?.method, body: JSON.parse(String(options?.body)) })
    return ok({ uid: 'fixture', title: '缓存的中文视频标题', title_mode: 'auto' })
  })
  try {
    assert.match(rendered.html, /手动保存的名称会保留/)
    assert.doesNotMatch(button(rendered.html, '使用视频标题'), /\sdisabled(?:\s|=|>)/)
    await rendered.page.useVideoTitle()
    assert.deepEqual(calls, [{ url: '/api/projects/fixture', method: 'PATCH', body: { title_mode: 'auto' } }])
    assert.equal(entry.title, '缓存的中文视频标题'); assert.equal(entry.title_mode, 'auto')
    assert.equal(rendered.page.renaming, null)
  } finally { rendered.cleanup() }
})

test('手工保存继续只传标题，并采纳后端的手工模式', async t => {
  const entry = project({ title_mode: 'auto' })
  const rendered = await render(Projects, page => {
    page.projects = [entry]; page.loading = false; page.renaming = entry; page.renameTitle = '自己起的名字'
  })
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(JSON.parse(String(options?.body)))
    return ok({ uid: 'fixture', title: '自己起的名字', title_mode: 'manual' })
  })
  try {
    await rendered.page.applyRename()
    assert.deepEqual(calls, [{ title: '自己起的名字' }])
    assert.equal(entry.title_mode, 'manual'); assert.equal(entry.title, '自己起的名字')
  } finally { rendered.cleanup() }
})

test('自动命名请求进行中防止重击和手工保存，失败保留正在编辑的名称', async t => {
  const entry = project(), pending = deferred()
  const rendered = await render(Projects, page => {
    page.projects = [entry]; page.loading = false; page.renaming = entry; page.renameTitle = '保留这个草稿'
  })
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', (_url, options) => {
    calls.push(JSON.parse(String(options?.body))); return pending.promise as any
  })
  try {
    const first = rendered.page.useVideoTitle()
    assert.equal(rendered.page.renameSaving, true)
    await rendered.page.useVideoTitle(); await rendered.page.applyRename()
    assert.deepEqual(calls, [{ title_mode: 'auto' }])
    pending.resolve({ ok: false, status: 400, json: async () => ({ message: '项目正在运行' }) })
    await first
    assert.equal(entry.title, '我的管理名称'); assert.equal(entry.title_mode, 'manual')
    assert.equal(rendered.page.renameTitle, '保留这个草稿')
    assert.equal(rendered.page.renaming.uid, entry.uid); assert.equal(rendered.page.renameSaving, false)
  } finally { rendered.cleanup() }
})

test('运行中使用视频标题和手工改名均被同一锁拦截', async () => {
  const entry = project({ running_step: 'split', status: 'processing' })
  const rendered = await render(Projects, page => {
    page.projects = [entry]; page.loading = false; page.renaming = entry; page.renameTitle = '保留草稿'
  })
  try {
    assert.match(button(rendered.html, '使用视频标题'), /\sdisabled(?:\s|=|>)/)
    await rendered.page.useVideoTitle(); await rendered.page.applyRename()
    assert.equal(entry.title, '我的管理名称'); assert.equal(entry.title_mode, 'manual')
    assert.equal(rendered.page.renameTitle, '保留草稿')
  } finally { rendered.cleanup() }
})

test('发送自动命名前重新核对权限，等待期间刚启动任务也不提交', async t => {
  const entry = project({ uid: 'preflight' }), pending = deferred()
  let fresh = false
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', (url) => {
    calls.push(String(url))
    assert.match(String(url), /\/editability$/)
    return (fresh ? pending.promise : Promise.resolve(ok({ locked: false }))) as any
  })
  await execution.checkConfigurationEditability('project:preflight')
  const rendered = await render(Projects, page => {
    page.projects = [entry]; page.loading = false; page.renaming = entry; page.renameTitle = '保留草稿'
  })
  try {
    rendered.page.renameObserving = true; fresh = true
    const saving = rendered.page.useVideoTitle()
    assert.equal(calls.length, 2)
    rendered.page.projects[0].running_step = 'split'; rendered.page.projects[0].status = 'processing'
    pending.resolve(ok({ locked: false })); await saving
    assert.equal(calls.length, 2); assert.equal(entry.title_mode, 'manual')
    assert.equal(rendered.page.renaming.uid, entry.uid); assert.equal(rendered.page.renameTitle, '保留草稿')
  } finally { rendered.page.renameObserving = false; rendered.cleanup() }
})
