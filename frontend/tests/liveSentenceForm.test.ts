import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Form: any
const path = 'tts.indextts25.temperature'
const fields = [{ path, label: '采样温度', type: 'float', min: .1, max: 2 }]
const defaults = { [path]: .8, 'tts.backend': 'indextts25', 'tts.indextts25.do_sample': true }
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(() => server.close())

function document(active = true, accepting = true) {
  return { defaults, overridden: {}, steps: { tts: { fields, values: defaults } }, context: defaults,
    revision: 4, execution: { editable: true, active, accepting, state: 'saved', revision: 4 } }
}
async function render(t: any, active = true, accepting = true, execution: Record<string, any> = {}, options: { lineId?: number | null } = {}) {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  let page: any
  const savedEvents: true[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null,
    { default: () => h(Form, { uid: 'live-fixture', stepKey: 'tts', lineId: options.lineId === null ? undefined : options.lineId ?? 2,
      onSaved: () => savedEvents.push(true) }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ProjectParameterForm') {
      page = this.$.setupState
      page.fields = fields; page.draft = { ...defaults }; page.defaults = defaults
      page.saved = JSON.stringify(defaults); page.loading = false; page.revision = 4
      page.lineExecution = { ...document(active, accepting).execution, ...execution }
    }
  } })
  const html = await renderToString(app)
  return { page, html, savedEvents }
}

test('单句与项目参数保存确认后立即通知父页，不等待参数回读或下一次轮询', async t => {
  for (const scope of ['line', 'project'] as const) await t.test(scope, async t => {
    const { page, savedEvents } = await render(t, false, true, {}, { lineId: scope === 'line' ? 2 : null })
    page.editField(path, .7)
    let finishWrite: (response: any) => void = () => {}
    let finishRead: ((response: any) => void) | undefined
    const calls: any[] = []
    t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
      calls.push({ url: String(url), method: options?.method ?? 'GET' })
      return new Promise(resolve => {
        if (options?.method === 'PUT') finishWrite = resolve
        else finishRead = resolve
      })
    })
    const saving = page.save()
    assert.equal(savedEvents.length, 0, '请求未获确认，不能提前宣称参数已保存')
    assert.equal(calls.length, 1)
    finishWrite({ ok: true, json: async () => ({ ok: true, revision: 5 }) })
    await new Promise<void>(resolve => setImmediate(resolve))
    assert.equal(savedEvents.length, 1, '成功回执立即触发父页刷新，不被慢参数接口阻塞')
    assert.ok(finishRead, '表单仍回读后端真实参数')
    assert.equal(page.saving, true, '保存收尾仍保护表单，避免重复提交')
    assert.match(calls[0].url, scope === 'line' ? /workbench\/lines\/2\/params$/ : /step_params$/)
    const latest = document(false)
    latest.steps.tts.values = { ...defaults, [path]: .7 }
    finishRead({ ok: true, json: async () => latest })
    await saving
    assert.equal(savedEvents.length, 1, '回读完成不会重复触发保存通知')
    assert.equal(page.draft[path], .7)
    assert.equal(page.dirty, false)
  })
})

test('保存失败不通知父页，保留待保存的参数草稿', async t => {
  const { page, savedEvents } = await render(t, false)
  page.editField(path, .7)
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    calls.push(options.method)
    return { ok: false, json: async () => ({ message: '参数已由其他窗口修改' }) }
  })
  await page.save()
  assert.deepEqual(calls, ['PUT'], '保存失败不会回读覆盖草稿')
  assert.equal(savedEvents.length, 0)
  assert.equal(page.draft[path], .7)
  assert.equal(page.dirty, true)
  assert.equal(page.saving, false)
})

test('保存成功但参数回读失败时，已提交变更仍立即通知父页刷新', async t => {
  const { page, savedEvents } = await render(t, false)
  page.editField(path, .6)
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => options?.method === 'PUT'
    ? { ok: true, json: async () => ({ ok: true, revision: 5 }) }
    : { ok: false, json: async () => ({ message: '参数读取暂不可用' }) })
  await page.save()
  assert.equal(savedEvents.length, 1, '已保存的参数不能因回读失败而等待轮询发现')
  assert.equal(page.error, '参数读取暂不可用')
  assert.equal(page.draft[path], .6)
  assert.equal(page.saving, false)
})

test('运行中的逐句表单区分保存并更新和仅保存，未开始请求不热切换', async t => {
  const { page, html } = await render(t)
  assert.match(html, /保存并更新本句配音/)
  assert.match(html, /仅保存设置/)
  assert.match(html, /当前请求保持原配置/)
  assert.match(html, /成功后覆盖当前槽/)
  assert.match(html, /需要保留旧声音对比/)
  assert.doesNotMatch(html, /当前句结束后用新槽更新/)
  page.editField(path, .7)
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    if (options?.body) writes.push(JSON.parse(options.body))
    return { ok: true, json: async () => document() }
  })
  await page.save()
  assert.equal(writes.length, 1)
  assert.equal(writes[0].execution, 'auto')
  assert.equal(writes[0].revision, 4)
  assert.deepEqual(writes[0].updates, { [path]: .7 })
  assert.equal(page.dirty, false)
})

test('明确仅保存设置不排入当前任务，空闲保存也不启动新配音', async t => {
  for (const active of [true, false]) {
    const { page, html } = await render(t, active)
    if (!active) assert.doesNotMatch(html, /保存并更新本句配音/)
    page.editField(path, .6)
    const writes: any[] = []
    t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
      if (options?.body) writes.push(JSON.parse(options.body))
      return { ok: true, json: async () => document(active) }
    })
    await page.save('save')
    assert.equal(writes[0].execution, 'save')
  }
})

test('排布收尾只保存下次设置；保存冲突保留草稿供用户处理', async t => {
  const { page, html } = await render(t, true, false)
  assert.match(html, /仅保存本句设置/)
  assert.doesNotMatch(html, /保存并更新本句配音/)
  page.editField(path, .5)
  let body: any
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    body = JSON.parse(options.body)
    return { ok: false, json: async () => ({ code: 'LINE_SETTINGS_CONFLICT', message: '本句设置已变化，请重新读取' }) }
  })
  await page.save()
  assert.equal(body.execution, 'save')
  assert.equal(page.draft[path], .5)
  assert.equal(page.dirty, true)
  assert.equal(page.saving, false)
})

test('本轮更新失败可显式重试同一版本，不伪造配置变更或另起任务', async t => {
  const { page } = await render(t)
  page.lineExecution = { ...document().execution, state: 'failed', message: '旧配音保留' }
  assert.equal(page.canRetryVoice, true)
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    if (options?.body) writes.push(JSON.parse(options.body))
    return { ok: true, json: async () => document() }
  })
  await page.save('retry')
  assert.equal(writes.length, 1)
  assert.equal(writes[0].execution, 'retry')
  assert.equal(writes[0].revision, 4)
  assert.deepEqual(writes[0].updates, {})
  assert.deepEqual(writes[0].reset, [])
})

test('追加生成的参数说明沿用已确认目标槽，不误称覆盖当前选槽', async t => {
  const { page, html } = await render(t, true, true, { kind: 'generation', state: 'failed', target_take: 3 })
  assert.match(html, /仍生成到已确认的配音槽 3/)
  assert.match(html, /重试本句配音/)
  assert.doesNotMatch(html, /成功后覆盖当前槽/)
  assert.equal(page.canRetryVoice, true)
})
