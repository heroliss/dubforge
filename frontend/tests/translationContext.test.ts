import assert from 'node:assert/strict'
import test, { before, after, beforeEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { NMessageProvider } from 'naive-ui'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Component: any, execution: any
const root = fileURLToPath(new URL('..', import.meta.url))
const fields = (updates: any = {}) => ({ enabled: false, title: '', author: '', description: '', summary: '', terms: '', ...updates })
const initial = (updates: any = {}) => ({ context: fields(), source_fields: fields({ title: 'Original video', summary: '已有缓存摘要' }),
  revision: 'revision-one', has_saved: false, has_source: true, summary_stale: false, ...updates })
const ok = (value: any) => ({ ok: true, json: async () => value })
before(async () => {
  server = await createServer({ root, cacheDir: root + '/.test-vite-cache', configFile: false,
    plugins: [vue()], appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Component = (await server.ssrLoadModule('/src/components/TranslationContextPanel.vue')).default
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
})
beforeEach(() => { execution.setNavigationExecution('', false, true); mock.restoreAll() })
after(async () => { mock.restoreAll(); await server.close() })
async function render(value: any, disabled = false) {
  let page: any
  const calls: any[] = []
  mock.method(globalThis, 'fetch', async (url: any, options: any = {}) => {
    calls.push([url, options]); return ok(value)
  })
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Component, { uid: 'fixture', disabled }) }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'TranslationContextPanel') { page = this.$.setupState; page.apply(value); page.loading = false } } })
  const html = await renderToString(app)
  await Promise.resolve()
  return { page, html, calls }
}

test('翻译页展示自动资料且保存前保持未启用，明确全文原文优先', async () => {
  const { page, html } = await render(initial())
  assert.match(html, /翻译背景与术语/); assert.match(html, /未启用/)
  assert.match(html, /Original video/); assert.match(html, /已有缓存摘要/)
  assert.match(html, /原文编号与事实仍以语义整理结果为准/)
  assert.match(html, /不额外请求 LLM/)
  assert.equal(page.draft.enabled, false); assert.equal(page.dirty, false)
  page.draft.enabled = true
  assert.equal(page.dirty, true)
})

test('填入最新资料只更新草稿、保留项目术语，放弃后恢复已保存内容', async () => {
  const value = initial({ has_saved: true, context: fields({ enabled: true, title: '旧标题', terms: 'yen → 日元' }) })
  const { page, calls } = await render(value)
  await page.load(true)
  assert.equal(page.draft.title, 'Original video')
  assert.equal(page.draft.terms, 'yen → 日元')
  assert.equal(page.dirty, true)
  assert.equal(calls.filter(([, options]) => options.method === 'PUT').length, 0)
  page.discard()
  assert.equal(page.draft.title, '旧标题'); assert.equal(page.dirty, false)
})

test('运行中不能保存背景修改', async () => {
  const { page, calls, html } = await render(initial(), true)
  page.draft.enabled = true
  await page.save()
  assert.equal(calls.filter(([, options]) => options.method === 'PUT').length, 0)
  const saveButton = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .find(match => match[0].includes('保存背景与术语'))?.[0]
  assert.ok(saveButton?.includes('disabled'))
})

test('缓存资料暂缺时仍可检查最新资料，并保留手工草稿', async () => {
  const { page, html } = await render(initial({ has_source: false, source_fields: fields() }))
  page.draft.title = '手工背景'; page.draft.terms = 'term → 术语'
  await page.load(true)
  assert.match(html, /填入最新资料/)
  assert.equal(page.draft.title, '手工背景'); assert.equal(page.draft.terms, 'term → 术语')
  assert.equal(page.dirty, true)
})

test('保存发送启用字段和读取版本，等待中重复点击不会重复提交', async () => {
  const { page } = await render(initial())
  page.draft.enabled = true; page.draft.terms = 'yen → 日元'
  let resolve: any, sends = 0
  const pending = new Promise(done => { resolve = done })
  mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    sends++; assert.equal(options.method, 'PUT')
    const body = JSON.parse(options.body)
    assert.equal(body.revision, 'revision-one'); assert.equal(body.context.terms, 'yen → 日元')
    assert.equal(body.context.enabled, true)
    await pending
    return ok(initial({ has_saved: true, revision: 'revision-two', context: body.context }))
  })
  const saving = page.save()
  await Promise.resolve(); await page.save()
  assert.equal(sends, 1)
  resolve(); await saving
  assert.equal(page.dirty, false); assert.equal(page.state.revision, 'revision-two')
})

test('版本冲突保留用户草稿，不谎称保存成功', async () => {
  const { page } = await render(initial())
  page.draft.enabled = true; page.draft.terms = 'my draft'
  mock.method(globalThis, 'fetch', async () => ({ ok: false, json: async () => ({ message: '背景资料已在其他页面修改，请重新读取后再保存' }) }))
  await page.save()
  assert.match(page.error, /其他页面修改/)
  assert.equal(page.draft.terms, 'my draft'); assert.equal(page.dirty, true)
  assert.equal(page.state.revision, 'revision-one')
})
