import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Editor: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Editor = (await server.ssrLoadModule('/src/components/LlmParametersEditor.vue')).default
})
after(() => server.close())

async function render(props: any = {}, fixture: any = {}) {
  let page: any
  const updates: string[] = [], controls: Record<string, any> = {}
  const app = createSSRApp({ render: () => h(Editor, { modelValue: '', model: 'glm-5.3-flash', baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    ...props, 'onUpdate:modelValue': (value: string) => updates.push(value) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'LlmParametersEditor') { page = this.$.setupState; Object.assign(page, fixture) }
    if (['InputNumber', 'Select', 'Input'].includes(this.$options.name ?? '') && this.$attrs['aria-label']) controls[this.$attrs['aria-label']] = this
  } })
  const html = await renderToString(app)
  return { page, updates, controls, html }
}

test('空配置仅显示通用说明，不代填数值，折叠请求预览不调用接口', async t => {
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; throw new Error('不得请求') })
  const { page, updates, html } = await render()
  assert.equal(requests, 0); assert.deepEqual(updates, [])
  assert.equal(page.previewOpen, false); assert.equal(page.configuredCount, 0)
  assert.match(html, /参数用途与通用建议/); assert.match(html, /具体默认值、范围和支持项请查服务商文档/)
  assert.doesNotMatch(html, /GLM|Flash 推荐|官方默认|131072/)
  assert.match(html, /预览实际请求 JSON/)
  assert.equal(page.fieldValue('temperature'), null); assert.equal(page.fieldValue('reasoning_effort'), '')
})

test('真实控件事件编辑唯一 JSON 草稿，清空仅删对应项，false/0保持显式值', async () => {
  const raw = '{"custom":{"keep":1},"thinking":{"type":"enabled","budget":99},"temperature":1}'
  const { page, updates, controls } = await render({ modelValue: raw })
  controls.temperature.$props['onUpdate:value'](0)
  assert.equal(JSON.parse(updates.at(-1)!).temperature, 0)
  assert.deepEqual(JSON.parse(updates.at(-1)!).custom, { keep: 1 })
  controls['thinking.clear_thinking'].$props['onUpdate:value']('false')
  assert.deepEqual(JSON.parse(updates.at(-1)!).thinking, { type: 'enabled', budget: 99, clear_thinking: false })
  controls.temperature.$props['onUpdate:value'](null)
  assert.equal(Object.hasOwn(JSON.parse(updates.at(-1)!), 'temperature'), false)
  page.change('stream', 'false'); assert.equal(JSON.parse(updates.at(-1)!).stream, false)
})

test('高级 JSON 非法或类型异常时控件禁用并保留文本；锁定时所有修改入口受保护', async () => {
  for (const raw of ['{"temperature":', '[]', '{"temperature":"hot","thinking":false}']) {
    const { page, controls, updates, html } = await render({ modelValue: raw })
    assert.equal(controls.temperature.$props.disabled, true)
    page.change('temperature', 1); assert.deepEqual(updates, [])
    assert.match(html, /JSON|类型/)
    if (!page.parsed.valid) assert.equal(page.advancedOpen, true)
  }
  const locked = await render({ modelValue: '{"temperature":1}', disabled: true })
  locked.page.change('temperature', 0); locked.page.updateJson('{}'); locked.page.formatJson()
  assert.deepEqual(locked.updates, [])
  assert.equal(locked.controls.temperature.$props.disabled, true)
})

test('不同模型共用通用控件，不推断型号支持，已有自定义档位和开关保持可见', async () => {
  const raw = '{"reasoning_effort":"custom-effort","thinking":{"type":"disabled"}}'
  for (const model of ['glm-5.3-flash', 'glm-4.7', 'gpt-5.4', 'ark-code-latest', 'future-model']) {
    const { page, html, updates } = await render({ model, modelValue: raw })
    assert.equal(page.fieldValue('reasoning_effort'), 'custom-effort'); assert.equal(page.fieldValue('thinking.type'), 'disabled')
    assert.ok(page.options('reasoning_effort').some((option: any) => option.value === 'custom-effort'))
    assert.ok(page.options('thinking.type').some((option: any) => option.value === 'disabled'))
    assert.doesNotMatch(html, /GLM|GPT|Flash 推荐|官方默认|官方不支持/)
    assert.deepEqual(updates, [])
  }
})

test('保留用户数值，不按模型名套用范围上限或自动夹紧；采样关联说明有支持条件', async () => {
  const raw = '{"temperature":1.5,"top_p":0,"max_tokens":131073}'
  const { page, controls, html, updates } = await render({ modelValue: raw })
  assert.doesNotMatch(html, /超出.*官方|官方默认/)
  assert.equal(page.fieldValue('temperature'), 1.5); assert.equal(page.fieldValue('top_p'), 0)
  assert.equal(page.fieldValue('max_tokens'), 131073); assert.deepEqual(updates, [])
  assert.equal(controls.temperature.$props.disabled, false); assert.equal(controls.temperature.$props.max, undefined)
  const configured = await render({ model: 'future-model', modelValue: '{"do_sample":false,"temperature":1}' })
  assert.match(configured.html, /若接口支持关闭采样/)
  assert.deepEqual(configured.updates, [])
})

test('请求预览只发模型、地址和参数，任务模式明确，失败不修改草稿', async t => {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    calls.push({ url: _url, ...options })
    return { ok: true, json: async () => ({ ok: true, body: { model: 'glm-5.3-flash', stream: true },
      notes: ['使用全局步骤设置'], changes: [{ path: 'stream', kind: 'add', after: true }], redacted_paths: [] }) } as any
  })
  const { page, updates } = await render({ modelValue: '{"temperature":1}' })
  page.previewOpen = true; page.previewMode = 'translate'; await page.loadPreview()
  assert.equal(calls[0].url, '/api/settings/preview_llm')
  assert.deepEqual(JSON.parse(calls[0].body), { profile: { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.3-flash', params: { temperature: 1 } }, mode: 'translate' })
  assert.match(page.previewJson, /"stream": true/); assert.deepEqual(updates, [])
  page.cancelPreview()
})

test('不合法 JSON 不发预览请求；接口失败不会显示上次请求作为当前结果', async t => {
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return { ok: false, json: async () => ({ message: '模型参数有误' }) } as any })
  const bad = await render({ modelValue: 'null' })
  bad.page.previewOpen = true; await bad.page.loadPreview()
  assert.equal(requests, 0); assert.ok(bad.page.previewError); assert.equal(bad.page.preview, null)
  const valid = await render()
  valid.page.previewOpen = true; valid.page.preview = { body: { previous: true } }; await valid.page.loadPreview()
  assert.equal(requests, 1); assert.equal(valid.page.preview, null); assert.equal(valid.page.previewError, '模型参数有误')
  assert.deepEqual(valid.updates, [])
  bad.page.cancelPreview(); valid.page.cancelPreview()
})

test('迟到的旧预览不能覆盖新场景，关闭预览会取消请求并隐藏加载状态', async t => {
  const pending: { resolve: (value: any) => void; signal: AbortSignal }[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => new Promise(resolve => pending.push({ resolve, signal: options.signal })))
  const { page } = await render()
  page.previewOpen = true
  const first = page.loadPreview()
  page.previewMode = 'translate'; const second = page.loadPreview()
  assert.equal(pending[0].signal.aborted, true)
  pending[0].resolve({ ok: true, json: async () => ({ body: { old: true } }) }); await first
  assert.equal(page.preview, null)
  pending[1].resolve({ ok: true, json: async () => ({ body: { current: true } }) }); await second
  assert.deepEqual(page.preview.body, { current: true })
  const third = page.loadPreview(); page.togglePreview()
  assert.equal(pending[2].signal.aborted, true); assert.equal(page.previewLoading, false); assert.equal(page.previewOpen, false)
  pending[2].resolve({ ok: true, json: async () => ({ body: { late: true } }) }); await third
  assert.equal(page.preview, null)
})

test('本机预览等待正文超过10秒会取消并恢复重试，迟到正文不能覆盖成功重试', async t => {
  let signal: AbortSignal | undefined, lateJson: ((value: any) => void) | undefined
  let calls = 0
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    calls++; signal = options.signal
    if (calls === 1) return { ok: true, json: () => new Promise(resolve => { lateJson = resolve }) } as any
    return { ok: true, json: async () => ({ body: { retried: true } }) } as any
  })
  const { page, updates } = await render()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  page.previewOpen = true
  const first = page.loadPreview()
  await Promise.resolve()
  assert.equal(page.previewLoading, true)
  t.mock.timers.tick(9999); await Promise.resolve()
  assert.equal(page.previewLoading, true); assert.equal(signal?.aborted, false)
  t.mock.timers.tick(1); await first
  assert.equal(signal?.aborted, true); assert.equal(page.previewLoading, false)
  assert.match(page.previewError, /超过 10 秒.*刷新预览.*重试/); assert.equal(page.preview, null)
  await page.loadPreview()
  assert.equal(calls, 2); assert.deepEqual(page.preview.body, { retried: true }); assert.equal(page.previewError, '')
  lateJson?.({ body: { late: true } }); await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(page.preview.body, { retried: true }); assert.deepEqual(updates, [])
  page.cancelPreview()
})

test('预览展示最终服务端 JSON 与变化说明，包含 false 前值并说明全局/项目差异', async () => {
  const { html } = await render({}, { previewOpen: true, preview: {
    body: { model: 'glm-5.3-flash', stream: false, reasoning_effort: 'low' },
    notes: ['低思考强度覆盖配置值'], changes: [{ path: 'tool_stream', kind: 'remove', before: false }], redacted_paths: ['secret'],
  } })
  assert.match(html, /实际请求 JSON/); assert.match(html, /项目可另有覆盖/)
  assert.match(html, /tool_stream：false → 不发送/); assert.match(html, /&quot;stream&quot;: false/)
  assert.match(html, /隐藏可能含敏感信息的字段：secret/)
})
