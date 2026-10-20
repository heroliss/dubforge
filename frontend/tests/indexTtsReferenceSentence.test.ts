import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { currentIndexTtsReferenceLine, indexTtsReferenceModeCarry, isIndexTtsReferenceSentence,
  referenceSentenceOptions, loadReferenceSentences } from '../src/utils/indexTtsReferenceSentence.ts'
import { indexTtsFieldHelp } from '../src/utils/indexTtsSettings.ts'

let server: ViteDevServer, Control: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Control = (await server.ssrLoadModule('/src/components/IndexTtsReferenceSentence.vue')).default
})
after(() => server.close())

const lines = [{ id: 1, start: 1.2, end: 3.8, text: '第一句原文。' },
  { id: 7, start: 3601.1, end: 3612.8, text: '另一个说话人的清晰参考。' }]

test('本句参考只派生有效当前句；仅用户从本句切到指定句时携带句号', () => {
  const prefix = 'tts.indextts25.'
  const values = { [prefix + 'timbre_mode']: 'dynamic', [prefix + 'emo_mode']: 'dynamic',
    [prefix + 'timbre_reference_line_id']: 3, [prefix + 'emo_reference_line_id']: 4 }
  const before = structuredClone(values)
  for (const source of ['timbre', 'emo']) {
    const reference = prefix + source + '_reference_line_id', mode = prefix + source + '_mode'
    assert.equal(currentIndexTtsReferenceLine(reference, values, 7), 7)
    assert.deepEqual(indexTtsReferenceModeCarry(mode, 'stable', values, 7), { path: reference, value: 7 })
    for (const invalid of [undefined, 0, -1, 1.5, NaN]) {
      assert.equal(currentIndexTtsReferenceLine(reference, values, invalid), undefined)
      assert.equal(indexTtsReferenceModeCarry(mode, 'stable', values, invalid), undefined)
    }
    for (const next of ['preset', 'dynamic', 'same', 'text'])
      assert.equal(indexTtsReferenceModeCarry(mode, next, values, 7), undefined)
    for (const previous of ['stable', 'preset', 'same', 'text']) {
      const other = { ...values, [mode]: previous }
      assert.equal(currentIndexTtsReferenceLine(reference, other, 7), undefined)
      assert.equal(indexTtsReferenceModeCarry(mode, 'stable', other, 7), undefined)
    }
  }
  assert.equal(currentIndexTtsReferenceLine(prefix + 'temperature', values, 7), undefined)
  assert.equal(indexTtsReferenceModeCarry(prefix + 'temperature', 'stable', values, 7), undefined)
  assert.deepEqual(values, before, '显示与转换建议均不修改原配置')
})

test('参考句标签显示句号、源时间和原文；未知选择保持原值', () => {
  assert.equal(isIndexTtsReferenceSentence('tts.indextts25.timbre_reference_line_id'), true)
  assert.equal(isIndexTtsReferenceSentence('tts.indextts25.emo_reference_line_id'), true)
  assert.equal(isIndexTtsReferenceSentence('tts.indextts25.emo_audio'), false)
  const options = referenceSentenceOptions(lines, 7)
  assert.deepEqual(options, [{ value: 1, label: '第 1 句 · 00:01.2–00:03.8 · 第一句原文。' },
    { value: 7, label: '第 7 句 · 1:00:01.1–1:00:12.8 · 另一个说话人的清晰参考。' }])
  assert.deepEqual(referenceSentenceOptions(lines, 42)[0], { value: 42, label: '第 42 句' })
  assert.deepEqual(referenceSentenceOptions([], 1), [{ value: 1, label: '第 1 句' }])
  assert.equal(referenceSentenceOptions([{ id: 1, start: 4.5, end: 7, text: '参考' }], 1)[0].label,
    '第 1 句 · 00:04.5–00:07.0 · 参考')
  assert.equal(referenceSentenceOptions([{ id: 1, start: 3599.96, end: 3601, text: '整点' }], 1)[0].label,
    '第 1 句 · 1:00:00.0–1:00:01.0 · 整点')
})

test('参考句帮助优先使用schema完整说明，缺失时才补简短默认说明', () => {
  const prefix = 'tts.indextts25.'
  const values = { [prefix + 'timbre_mode']: 'stable', [prefix + 'emo_mode']: 'stable' }
  for (const name of ['timbre_reference_line_id', 'emo_reference_line_id']) {
    const help = '默认第 1 句，按原文时间截取，最长读取前 15 秒。'
    assert.equal(indexTtsFieldHelp(prefix + name, help, values), help)
    assert.match(indexTtsFieldHelp(prefix + name, undefined, values), /默认第 1 句.*最长读取前 15 秒/)
  }
})

test('本句模式帮助覆盖旧服务的默认第一句说明，明确自动跟随而非使用备用句号', () => {
  const prefix = 'tts.indextts25.'
  const values = { [prefix + 'timbre_mode']: 'dynamic', [prefix + 'emo_mode']: 'dynamic' }
  for (const name of ['timbre_reference_line_id', 'emo_reference_line_id']) {
    const help = indexTtsFieldHelp(prefix + name, '默认第 1 句，按原文时间截取，最长读取前 15 秒。', values)
    assert.match(help, /本句/)
    assert.doesNotMatch(help, /默认第 1 句/)
    assert.match(help, /指定句/)
  }
})

test('两个参考选择器合并并发读取，重新打开可读到新源稿', async t => {
  let release: (value: Response) => void
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
    calls.push([url, options])
    return new Promise<Response>(resolve => { release = resolve })
  })
  const first = loadReferenceSentences('sentence fixture'), second = loadReferenceSentences('sentence fixture')
  assert.equal(first, second)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], '/api/projects/sentence%20fixture/workbench/reference-lines')
  assert.equal(calls[0][1].cache, 'no-store')
  release!(new Response(JSON.stringify({ lines })))
  assert.deepEqual(await first, lines)
  const third = loadReferenceSentences('sentence fixture')
  assert.equal(calls.length, 2)
  release!(new Response(JSON.stringify({ lines: [] })))
  assert.deepEqual(await third, [])
})

test('读取失败保留具体错误且允许重试，不缓存失败状态', async t => {
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    calls++
    return calls === 1 ? new Response(JSON.stringify({ message: '原文句子已变更' }), { status: 409 })
      : new Response(JSON.stringify({ lines }))
  })
  await assert.rejects(loadReferenceSentences('retry fixture'), /原文句子已变更/)
  assert.deepEqual(await loadReferenceSentences('retry fixture'), lines)
})

async function render(t: any, projectUid?: string, state: Record<string, any> = {}, disabled = false,
                      respond?: () => Response | Promise<Response>, followCurrent = false, extra: Record<string, any> = {}) {
  const reads: string[] = [], events: any[] = []
  t.mock.method(globalThis, 'fetch', (url: any) => { reads.push(String(url)); return respond ? respond() : new Promise(() => {}) })
  let page: any, widget: any
  const app = createSSRApp({ render: () => h(Control, { projectUid, label: '音色参考句', modelValue: 7, disabled, followCurrent, ...extra,
    'onUpdate:modelValue': (value: any) => events.push(value) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'IndexTtsReferenceSentence') { page = this.$.setupState; Object.assign(page, state) }
    if (['InputNumber', 'Select'].includes(this.$options.name)) widget = this
  } })
  const html = await renderToString(app)
  return { page, widget, html, reads, events }
}

test('全局及模板使用正整数句号输入，不读取任何项目；项目内使用可搜索原文下拉', async t => {
  const global = await render(t)
  assert.equal(global.widget.$options.name, 'InputNumber')
  assert.equal(global.widget.$props.min, 1)
  assert.equal(global.widget.$props.precision, 0)
  assert.equal(global.widget.$props.value, 7)
  assert.deepEqual(global.reads, [])
  const project = await render(t, 'project fixture', { lines, loaded: true })
  assert.equal(project.widget.$options.name, 'Select')
  assert.equal(project.widget.$props.filterable, true)
  assert.equal(project.widget.$props.value, 7)
  assert.equal(project.widget.$props.options[1].label, '第 7 句 · 1:00:01.1–1:00:12.8 · 另一个说话人的清晰参考。')
  const label = project.widget.$props.renderLabel(project.widget.$props.options[1])
  assert.equal(label.props.title, project.widget.$props.options[1].label)
  assert.equal(label.props.style.textOverflow, 'ellipsis', '长原文在控件和选项中截断，悬浮保留全文')
  assert.deepEqual(project.reads, ['/api/projects/project%20fixture/workbench/reference-lines'])
  project.widget.$emit('update:value', 1)
  assert.deepEqual(project.events, [1])
})

test('不适用模式不会读取句子；句消失时提示并保留已存句号，不自动改为第一句', async t => {
  const disabled = await render(t, 'disabled fixture', {}, true)
  assert.equal(disabled.widget.$props.disabled, true)
  assert.deepEqual(disabled.reads, [])
  const missing = await render(t, 'missing fixture', { lines: [lines[0]], loaded: true })
  assert.match(missing.html, /源稿中没有此句，请重新选择/)
  assert.equal(missing.widget.$props.value, 7)
  assert.deepEqual(missing.widget.$props.options[0], { value: 7, label: '第 7 句' })
  assert.deepEqual(missing.events, [])
})

test('本句原声模式虽禁用选择，仍读取原文显示当前句且不会发出修改事件', async t => {
  const fixture = await render(t, 'current fixture', {}, true,
    () => new Response(JSON.stringify({ lines })), true)
  if (fixture.page.loading) await new Promise(resolve => setImmediate(resolve))
  assert.equal(fixture.widget.$props.disabled, true)
  assert.equal(fixture.widget.$props.value, 7)
  assert.deepEqual(fixture.reads, ['/api/projects/current%20fixture/workbench/reference-lines'])
  assert.equal(fixture.page.options[1].label, '第 7 句 · 1:00:01.1–1:00:12.8 · 另一个说话人的清晰参考。')
  assert.deepEqual(fixture.events, [], '读取原文不自动写入备用参考句')
})

test('没有项目上下文时跟随标记不触发读取，也不自动修改句号', async t => {
  const fixture = await render(t, undefined, {}, true, undefined, true)
  assert.equal(fixture.widget.$options.name, 'InputNumber')
  assert.equal(fixture.widget.$props.disabled, true)
  assert.deepEqual(fixture.reads, [])
  assert.deepEqual(fixture.events, [])
})

test('参考句旁显示有明确名称的试听按钮，无项目和不适用来源均禁用', async t => {
  const global = await render(t)
  assert.match(global.html, /aria-label="试听音色参考句原声"/)
  assert.equal(global.page.previewDisabled, true)
  assert.match(global.page.previewTitle, /项目内试听/)
  const inactive = await render(t, 'inactive preview', { lines, loaded: true }, false, undefined, false,
    { previewEnabled: false })
  assert.equal(inactive.page.previewDisabled, true)
  assert.match(inactive.page.previewTitle, /未使用此参考句/)
  const ready = await render(t, 'ready preview', { lines, loaded: true })
  assert.equal(ready.page.previewDisabled, false)
  assert.match(ready.page.previewTitle, /第 7 句.*15 秒/)
  assert.deepEqual(ready.events, [])
})

test('本句禁用下拉仍可试听；锁定设置不禁止明确启用的只读试听', async t => {
  const current = await render(t, 'current preview', { lines, loaded: true }, true, undefined, true,
    { maxSeconds: 6 })
  assert.equal(current.widget.$props.disabled, true)
  assert.equal(current.page.previewDisabled, false)
  assert.match(current.page.previewTitle, /6 秒/)
  const locked = await render(t, 'locked preview', { lines, loaded: true }, true, undefined, false,
    { previewEnabled: true })
  assert.equal(locked.widget.$props.disabled, true)
  assert.equal(locked.page.previewDisabled, false)
  assert.deepEqual(current.events, [])
  assert.deepEqual(locked.events, [])
})

test('加载期间再次点击可立即停止，不修改参考选择，也不播放迟到响应', async t => {
  const fixture = await render(t, 'cancel preview', { lines, loaded: true })
  let signal: AbortSignal | undefined, release: (value: Response) => void
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => {
    signal = options.signal
    return new Promise<Response>(resolve => { release = resolve })
  })
  fixture.page.togglePreview()
  assert.equal(fixture.page.previewState, 'loading')
  fixture.page.togglePreview()
  assert.equal(fixture.page.previewState, 'idle')
  assert.equal(signal?.aborted, true)
  release!(new Response(JSON.stringify({ url: '/api/projects/cancel%20preview/workbench/audio/vocal?v=1',
    start: 3601.1, end: 3612.8, line_id: 7 })))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(fixture.page.previewState, 'idle')
  assert.equal(fixture.page.previewError, '')
  assert.deepEqual(fixture.events, [])
})

test('展开下拉会重新读取当前源稿，源句变化后显示新选项且不重置已有选择', async t => {
  let current = lines
  const fixture = await render(t, 'refresh fixture', {}, false,
    () => new Response(JSON.stringify({ lines: current })))
  if (fixture.page.loading) await new Promise(resolve => setImmediate(resolve))
  assert.equal(fixture.reads.length, 1, '初始读取合并为一个请求')
  assert.equal(fixture.page.lines[1].text, lines[1].text)
  current = [{ id: 9, start: 14, end: 20, text: '重新整理后的源稿。' }]
  fixture.widget.$emit('update:show', true)
  await fixture.page.load()
  assert.equal(fixture.reads.length, 2, '已完成的读取不缓存，重新展开取得最新源句')
  assert.equal(fixture.page.lines[0].text, '重新整理后的源稿。')
  assert.deepEqual(fixture.page.options.map((option: any) => option.value), [7, 9])
  assert.equal(fixture.page.missing, true)
  assert.deepEqual(fixture.events, [])
})
