import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'
import { INDEX_TTS_PRONUNCIATION_PATH as path } from '../src/utils/dubbingPreset.ts'

let server: ViteDevServer, Form: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(() => server.close())

const lineText = '清朝、明朝，还有“没有朝”。'
const annotated = '清朝、明朝，还有“没有<朝|CHAO2>”。'
const temperature = 'tts.indextts25.temperature'
const fields = [
  { path: temperature, label: '采样温度', type: 'float' },
  { path, label: '朗读文本', type: 'text', group: '朗读内容', help: '自由编辑实际朗读文本，字幕保持原样。' },
]
function document(value = '', active = false) {
  const defaults = { [temperature]: .8, [path]: '' }
  return { line_text: lineText, defaults, context: { 'tts.backend': 'indextts25' },
    steps: { tts: { fields, values: { ...defaults, [path]: value } } },
    overridden: { [path]: !!value }, revision: 8,
    sources: { [path]: value ? { kind: 'line' } : { kind: 'line_default', label: '跟随本句译文' } },
    parent_sources: { [path]: { kind: 'line_default', label: '跟随本句译文' } },
    execution: { editable: true, active, accepting: active, state: 'saved', revision: 8 } }
}
async function render(t: any, value = '', active = false) {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  let page: any
  const doc = document(value, active)
  const app = createSSRApp({ render: () => h(NMessageProvider, null,
    { default: () => h(Form, { uid: 'pronunciation-fixture', stepKey: 'tts', lineId: 4 }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name !== 'ProjectParameterForm') return
    page = this.$.setupState
    page.fields = fields; page.defaults = doc.defaults; page.lineText = doc.line_text
    page.draft = { ...doc.context, ...doc.steps.tts.values }; page.overrides = doc.overridden
    page.sources = doc.sources; page.parentSources = doc.parent_sources
    page.saved = JSON.stringify(page.draft); page.loading = false; page.revision = doc.revision
    page.lineExecution = doc.execution
  } })
  const html = await renderToString(app)
  return { page, html }
}

test('逐句朗读内容置于音色参数前，显示字幕参考，完整用法只在问号中展开', async t => {
  const { page, html } = await render(t, annotated)
  assert.deepEqual(page.groups.map((group: any) => group.name), ['朗读内容', '预设参数'])
  assert.ok(html.indexOf('aria-label="朗读内容"') < html.indexOf('aria-label="预设参数"'))
  assert.doesNotMatch(html, /这是实际交给 IndexTTS|没有“第 N 个字”/)
  const help = page.fieldHelp(fields.find(field => field.path === path))
  assert.match(help, /可自由修改标点、数字、措辞或整句/)
  assert.match(help, /只影响配音，字幕和时间不变/)
  assert.match(help, /可选标音：没有<朝\|CHAO2>/)
  assert.match(help, /只指定该处“朝”，2 表示第二声/)
  assert.match(help, /保存后重新生成本句/)
  assert.match(help, /恢复跟随译文/)
  assert.equal(help.split('\n\n').length, 3)
  assert.equal((help.match(/<[^>]*>/g) ?? []).length, 1)
  assert.ok(help.length <= 230, `帮助需保持简短，当前 ${help.length} 字符`)
  assert.doesNotMatch(html, /载入当前译文|默认显示并跟随当前译文，可自由修改/)
  assert.match(html, /恢复跟随译文/)
  assert.match(html, /class="[^"]*wide-field[^"]*"/)
  assert.match(html, /&lt;朝\|CHAO2&gt;/)
  assert.equal(page.draft[path], annotated)
  assert.equal(page.lineText, lineText)
  assert.equal(page.disabledField(fields[1]), false)
  assert.equal(page.dirty, false)
})

test('默认框内直接显示译文且保持继承；打开表单和修改其他参数不保存朗读覆盖', async t => {
  const { page, html } = await render(t)
  assert.equal(page.draft[path], '')
  assert.equal(page.fieldValue(path), lineText)
  assert.equal(page.changed(path), false)
  assert.equal(page.dirty, false)
  assert.equal(page.fieldSource(path).kind, 'line_default')
  assert.match(html, /<textarea[^>]*aria-label="朗读文本"[^>]*>清朝、明朝，还有“没有朝”。<\/textarea>/)
  assert.doesNotMatch(html, /字幕译文|载入当前译文/)
  const restore = html.match(/<button\b[^>]*aria-label="朗读文本恢复跟随译文"[^>]*>/)?.[0]
  assert.ok(restore)
  assert.match(restore, /disabled/)
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    if (options?.body) writes.push(JSON.parse(options.body))
    return new Response(JSON.stringify(document()))
  })
  page.editField(temperature, .7)
  await page.save()
  assert.deepEqual(writes[0].updates, { [temperature]: .7 })
  assert.deepEqual(writes[0].reset, [])
  assert.equal(page.draft[path], '')
  assert.equal(page.fieldValue(path), lineText)
})

test('继承文字随当前译文刷新，明确自定义即使相同也保持独立；恢复立刻还原显示且等待保存', async t => {
  const { page } = await render(t)
  const updated = '新的字幕译文。'
  page.lineText = updated
  assert.equal(page.fieldValue(path), updated)
  assert.equal(page.dirty, false)
  page.editField(path, updated)
  assert.equal(page.changed(path), true)
  assert.equal(page.fieldSource(path).kind, 'line')
  page.lineText = '再次修改的字幕。'
  assert.equal(page.fieldValue(path), updated)
  page.inheritField(path)
  assert.equal(page.draft[path], '')
  assert.equal(page.fieldValue(path), '再次修改的字幕。')
  assert.equal(page.changed(path), false)
  assert.equal(page.fieldSource(path).kind, 'line_default')
  assert.equal(page.dirty, true)
  assert.deepEqual([...page.resetKeys], [path])
  assert.equal(Object.hasOwn(page.edited, path), false)
})

test('删除编辑可临时留空而不强制回填；保存空白后回到跟随译文', async t => {
  const { page } = await render(t, annotated)
  page.editField(path, '')
  assert.equal(page.fieldValue(path), '')
  assert.equal(page.changed(path), true)
  assert.equal(page.dirty, true)
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    if (options?.body) writes.push(JSON.parse(options.body))
    return new Response(JSON.stringify(document()))
  })
  await page.save()
  assert.deepEqual(writes[0].updates, { [path]: '' })
  assert.equal(page.fieldValue(path), lineText)
  assert.equal(page.changed(path), false)
  assert.equal(page.dirty, false)
})

test('读取服务端当前译文；保存仅写句级朗读字段，空闲保存不启动模型', async t => {
  const { page } = await render(t)
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    calls.push({ url: String(url), body: options?.body ? JSON.parse(options.body) : undefined })
    return new Response(JSON.stringify(document(annotated)))
  })
  await page.load()
  assert.equal(page.lineText, lineText)
  assert.equal(page.draft[path], annotated)
  page.editField(path, annotated.replace('清朝', '清<朝|CHAO2>'))
  await page.save()
  const writes = calls.filter(call => call.body)
  assert.equal(writes.length, 1)
  assert.equal(writes[0].url, '/api/projects/pronunciation-fixture/workbench/lines/4/params')
  assert.deepEqual(writes[0].body, { updates: { [path]: annotated.replace('清朝', '清<朝|CHAO2>') }, reset: [], revision: 8, execution: 'save' })
  assert.equal(page.lineText, lineText)
})

test('运行中自定义朗读复用自动更新与仅保存协议，清空恢复跟随当前译文', async t => {
  for (const execution of ['auto', 'save'] as const) {
    const { page } = await render(t, '', true)
    page.editField(path, annotated)
    const writes: any[] = []
    t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
      if (options?.body) writes.push(JSON.parse(options.body))
      return new Response(JSON.stringify(document(writes.at(-1)?.reset.includes(path) ? '' : annotated, true)))
    })
    await page.save(execution)
    assert.equal(writes[0].execution, execution)
    assert.deepEqual(writes[0].updates, { [path]: annotated })
    page.inheritField(path)
    assert.equal(page.draft[path], '')
    assert.equal(page.lineText, lineText)
    assert.equal(page.fieldResetLabel(path), '恢复跟随译文')
    assert.equal(page.fieldValue(path), lineText)
    assert.equal(page.fieldSource(path).kind, 'line_default')
    await page.save(execution)
    assert.deepEqual(writes[1].updates, {})
    assert.deepEqual(writes[1].reset, [path])
    assert.equal(page.fieldValue(path), lineText)
    assert.equal(page.changed(path), false)
    assert.equal(page.dirty, false)
  }
})

test('保存失败保留朗读草稿与字幕参考，便于连接恢复后重试', async t => {
  const { page } = await render(t)
  page.editField(path, '19条，公元前2070年之前，还没有中国')
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ message: '暂时无法保存，请重试' }), { status: 503 }))
  await page.save()
  assert.equal(page.draft[path], '19条，公元前2070年之前，还没有中国')
  assert.equal(page.lineText, lineText)
  assert.equal(page.dirty, true)
  assert.equal(page.saving, false)
})


test('朗读字段允许普通标点、整句替换和与译文相同的明确自定义，不作前端内容校验', async t => {
  const values = ['19条，公元前2070年之前，还没有中国', '另起一句，十三年之后再见。', '<literal> [pause] 123', lineText]
  for (const value of values) await t.test(value, async t => {
    const { page } = await render(t)
    const calls: any[] = []
    t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
      if (options?.body) calls.push(JSON.parse(options.body))
      return new Response(JSON.stringify(document(value)))
    })
    page.editField(path, value)
    assert.equal(page.dirty, true)
    await page.save()
    assert.equal(calls.length, 1)
    assert.deepEqual(calls[0], { updates: { [path]: value }, reset: [], revision: 8, execution: 'save' })
    assert.equal(page.draft[path], value)
    assert.equal(page.lineText, lineText)
    assert.equal(page.changed(path), true)
    assert.equal(page.dirty, false)
  })
})
