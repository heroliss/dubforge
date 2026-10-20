import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'

let server: ViteDevServer, Binding: any, useCatalog: any, useList: any, delivery: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Binding = (await server.ssrLoadModule('/src/components/ProjectTemplateBinding.vue')).default
  useCatalog = (await server.ssrLoadModule('/src/stores/templateCatalog.ts')).useTemplateCatalogStore
  useList = (await server.ssrLoadModule('/src/stores/projectList.ts')).useProjectListStore
  delivery = (await server.ssrLoadModule('/src/utils/projectCard.ts')).projectDeliverySummary
})
after(() => server.close())

const reply = (data: any, status = 200) => ({ ok: status < 400, status, json: async () => data })
const summary = (extra: any = {}) => ({ known: true, template_id: 'history', template_name: '历史系列',
  template_archived: false, override_count: 2, delivery_mode: 'produce', ...extra })
function deferred() {
  let resolve: (value: any) => void = () => {}
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
async function render(t: any, cards = [{ uid: 'one', summary: summary(), disabled: false }]) {
  const pinia = createPinia(), catalog = useCatalog(pinia), list = useList(pinia)
  list.activate('workspace-a')
  const pages: any[] = [], dialogs: any[] = [], changes: string[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => cards.map(card =>
    h(Binding, { uids: [card.uid], compact: true, summary: card.summary, disabled: card.disabled,
      onChanged: () => changes.push(card.uid) })))) })
  app.use(pinia); app.use(createRouter({ history: createMemoryHistory(), routes: [
    { path: '/settings', component: { render: () => null } },
  ] })); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ProjectTemplateBinding') {
    const page = this.$.setupState; pages.push(page)
    page.dialog.warning = (value: any) => { dialogs.push(value); return { destroy() {} } }
  } } })
  const html = await renderToString(app)
  t.after(() => disposePinia(pinia))
  return { pages, catalog, list, dialogs, changes, html }
}

test('项目卡片直接显示冻结来源与覆盖数，首次渲染不为每张卡片读配置或启动轮询', async t => {
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async url => { calls.push(String(url)); return reply({}) })
  const { pages, html } = await render(t, [{ uid: 'a', summary: summary(), disabled: false },
    { uid: 'b', summary: summary({ template_id: 'old', template_name: '旧模板', template_archived: true }), disabled: false }])
  assert.deepEqual(calls, []); assert.equal(pages.length, 2)
  assert.equal(pages[0].selectedTemplate, 'history'); assert.equal(pages[0].templateOptions.at(-1).label, '历史系列')
  assert.equal(pages[1].templateOptions.at(-1).disabled, true)
  assert.match(html, /2 项覆盖/); assert.match(html, /项目模板/)
})

test('同时展开多卡片共享一次目录读取，切换工作目录时旧响应和旧目录不能覆盖当前内容', async t => {
  const pending = deferred(), calls: string[] = []
  t.mock.method(globalThis, 'fetch', async url => { calls.push(String(url)); return pending.promise })
  const { pages, catalog, list } = await render(t, [{ uid: 'a', summary: summary(), disabled: false },
    { uid: 'b', summary: summary(), disabled: false }])
  const first = pages[0].prepareTemplates(true), second = pages[1].prepareTemplates(true)
  assert.equal(calls.length, 1)
  list.activate('workspace-b')
  assert.ok(pages[0].templateOptions.every((item: any) => item.value !== 'private-a'))
  t.mock.method(globalThis, 'fetch', async () => reply([{ id: 'new', name: '新工作目录模板' }]))
  await pages[0].prepareTemplates(true)
  pending.resolve(reply([{ id: 'private-a', name: '旧目录私人模板' }]))
  await Promise.all([first, second])
  assert.equal(catalog.context, 'workspace-b'); assert.deepEqual(catalog.rows.map((row: any) => row.id), ['new'])
  assert.ok(pages[1].templateOptions.every((item: any) => item.value !== 'private-a'))
})

test('目录失败可再次展开重试，未知摘要不会冒充全局默认，禁用卡片不发预检', async t => {
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return calls === 1 ? reply({ message: '目录不可用' }, 500)
    : reply([{ id: 'new', name: '最新模板' }]) })
  const { pages, catalog } = await render(t, [{ uid: 'a', summary: { known: false } as any, disabled: true }])
  assert.equal(pages[0].selectedTemplate, null)
  await pages[0].bind('new'); assert.equal(calls, 0)
  await pages[0].prepareTemplates(true); assert.match(catalog.error, /目录不可用/)
  await pages[0].prepareTemplates(true); assert.equal(catalog.error, '')
  assert.equal(catalog.rows[0].id, 'new'); assert.equal(calls, 2)
})

test('卡片应用模板先核对可编辑性和差异，确认时再次核对，并提交同一指纹', async t => {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options: any) => {
    const path = String(url); calls.push({ path, body: options?.body && JSON.parse(options.body) })
    return reply(path.endsWith('/binding-plan') ? { name: '旅行系列', fingerprint: 'reviewed', projects: [] }
      : path.endsWith('/editability') ? { locked: false } : {})
  })
  const { pages, dialogs, changes } = await render(t)
  await pages[0].bind('travel')
  assert.equal(dialogs.length, 1); assert.ok(calls.every(row => !row.path.endsWith('/bind')))
  assert.deepEqual(calls.find(row => row.path.endsWith('/binding-plan')).body, { uids: ['one'], template_id: 'travel' })
  await dialogs[0].onPositiveClick()
  assert.equal(calls.filter(row => row.path.endsWith('/editability')).length, 2)
  assert.deepEqual(calls.find(row => row.path.endsWith('/bind')).body,
    { uids: ['one'], template_id: 'travel', fingerprint: 'reviewed', confirmed: true })
  assert.deepEqual(changes, ['one'])
})

test('工作目录改变后迟到的应用预检不能再打开确认或写入', async t => {
  const slow = deferred(); let writes = 0
  t.mock.method(globalThis, 'fetch', async (url, options: any) => String(url).endsWith('/binding-plan') ? slow.promise
    : (options?.method === 'POST' ? (writes++, reply({})) : reply({ locked: false })))
  const { pages, list, dialogs } = await render(t)
  const checking = pages[0].bind('travel')
  for (let i = 0; i < 8; i++) await Promise.resolve()
  list.activate('workspace-b')
  for (let i = 0; i < 8; i++) await Promise.resolve()
  slow.resolve(reply({ name: '旧模板', fingerprint: 'old', projects: [] }))
  await checking
  assert.equal(dialogs.length, 0); assert.equal(writes, 0); assert.equal(pages[0].busy, false)
})

test('交付摘要只显示可读实际目标，缺设置和坏摘要明确待核对，不借用其他稿件', () => {
  assert.deepEqual(delivery(null), { delivery: '项目设置待核对', target: '' })
  assert.equal(delivery(summary()).target, '')
  assert.equal(delivery(summary({ delivery_mode: 'draft', target_kind: '' })).target, '发布目标待设置')
  const fresh = delivery(summary({ delivery_mode: 'publish', target_kind: 'new', account_id: 'id-secret', account_name: '发布账号',
    season_id: 42, season_title: '中国历史', section_id: 7, section_title: '正篇' }))
  assert.equal(fresh.target, '发布账号 · 中国历史 / 正篇'); assert.doesNotMatch(fresh.target, /id-secret|42/)
  assert.match(delivery(summary({ delivery_mode: 'upload', target_kind: 'existing', account_name: '账号', bvid: 'BVExact' })).target, /更新原稿 BVExact/)
  assert.match(delivery(summary({ delivery_mode: 'publish', target_kind: 'existing' })).target, /待核对/)
})

test('项目卡片区分首次发布默认新稿、历史待选与主动清空，零制作目标不增加提示',()=>{
  const first={source:'first_publish',has_history:false,explicit:false}
  assert.equal(delivery(summary({delivery_mode:'publish',target_kind:'new',account_name:'账号',target_selection:first})).target,
    '首次发布 · 默认创建新稿 · 账号 · 独立发布')
  assert.equal(delivery(summary({delivery_mode:'publish',target_kind:'',target_selection:{source:'history_requires_choice',has_history:true,explicit:false}})).target,
    '已有交付任务或发布记录 · 运行前请核对稿件目标')
  assert.equal(delivery(summary({delivery_mode:'upload',target_kind:'',target_selection:{source:'explicit_unset',has_history:false,explicit:true}})).target,
    '稿件目标已清空 · 请选择新稿或原稿')
  assert.equal(delivery(summary({delivery_mode:'publish',target_kind:'',target_selection:{source:'invalid',has_history:false,explicit:true}})).target,
    '稿件目标无效 · 请重新选择')
  assert.equal(delivery(summary({target_selection:first})).target,'')
})
