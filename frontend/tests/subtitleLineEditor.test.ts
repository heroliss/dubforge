import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Editor: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Editor = (await server.ssrLoadModule('/src/components/SubtitleLineEditor.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('禁止未声明的网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })

function line() {
  return { id: 3, start: 10, end: 12, source_text: 'Source words', text: '译文',
    base: { start: 9.8, end: 12, source_text: 'Source words', text: '译文' }, translation_available: true,
    edit_revision: 'source-revision', edited: false, edit_stale: false,
    playback_start: 9.5, playback_end: 12.5, selection: { source: 'dub', take: 2 },
    wav: { exists: true, seconds: 3, duration_retry: { attempts: [{ factor: 1 }, { factor: .85 }], used_factor: .85, retries: 1 } },
    timing: { automatic_start: 9.5, automatic_end: 12.5, applied_start: 9.5, applied_end: 12.5,
      manual_start: null, manual_end: null, manual_active: false, manual_stale: false,
      effective_mode: 'automatic', timing_revision: 'timing-revision' } }
}

async function render(value: any = line(), seed: (page: any) => void = () => {}, disabled = false) {
  let page: any
  const events: any[] = [], notices: string[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(Editor, { uid: 'fixture', line: value,
    lineSettingsSupported: true, disabled,
    onSaved: () => events.push(['saved']), onClose: () => events.push(['close']),
    'onDirty-change': (dirty: boolean) => events.push(['dirty-change', dirty]),
    onEditParameters: (id: number) => events.push(['edit-parameters', id]),
  })) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'SubtitleLineEditor') {
      page = this.$.setupState
      page.message.warning = page.message.success = page.message.error = (value: string) => notices.push(value)
      seed(page)
    }
  } })
  const context: any = {}
  const html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  const text = html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
  return { page, html, text, events, notices }
}

test('抽屉默认展示源轴、自动和最终范围，声音历史与时间层分开解释', async () => {
  const result = await render()
  assert.match(result.text, /播放排布/); assert.match(result.text, /源稿校对/)
  assert.match(result.text, /原始时间/); assert.match(result.text, /00:09.800 — 00:12.000/)
  assert.match(result.text, /校对源时间/); assert.match(result.text, /自动排布/)
  assert.match(result.text, /当前最终播放/); assert.match(result.text, /00:09.500 — 00:12.500/)
  assert.match(result.text, /不重做音频、不新建配音槽/)
  assert.match(result.text, /合成起始因子/); assert.match(result.text, /本版本实际因子/)
  assert.match(result.text, /不能仅改时间还原/)
  assert.equal(result.page.activePage, 'playback'); assert.equal(result.page.dirty, false)
})

test('人工播放调整只提交独立时间层，源稿与合成接口不受影响', async t => {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options: any) => {
    calls.push({ url, body: JSON.parse(options.body) }); return new Response('{"ok":true}')
  })
  const result = await render(undefined, page => { page.playbackMode = 'manual'; page.manualStart = 9; page.manualEnd = 13 })
  assert.equal(result.page.canSave, true); assert.equal(result.page.sourceDirty, false)
  assert.match(result.text, /字幕结束 \/ 预留到/); assert.match(result.text, /声音结束 00:12.000/)
  await result.page.savePlayback()
  assert.deepEqual(calls, [{ url: '/api/projects/fixture/workbench/lines/3/timing',
    body: { revision: 'timing-revision', start: 9, end: 13 } }])
  assert.deepEqual(result.events.slice(-3), [['dirty-change', false], ['saved'], ['close']])
})

test('字幕不能假裁完整声音，过早结束阻止保存且保留草稿', async () => {
  const result = await render(undefined, page => { page.playbackMode = 'manual'; page.manualStart = 9; page.manualEnd = 10 })
  assert.match(result.text, /不能早于完整配音结束/); assert.equal(result.page.canSave, false)
  await result.page.savePlayback()
  assert.equal(result.page.manualEnd, 10); assert.equal(result.page.dirty, true)
  assert.equal(result.events.some(([name]: any) => name === 'saved'), false)
})

test('输入精度向上容纳WAV真实尾部，自动排布失败和需求缩时不可误称已解决', async () => {
  const input: any = line()
  input.wav.seconds = 1.00003125
  Object.assign(input.timing, { automatic_ready: false, needs_scale: true })
  const result = await render(input, page => { page.playbackMode = 'manual'; page.manualStart = 10 })
  assert.equal(result.page.minimumEnd, 11.001)
  assert.match(result.text, /尚未容纳 · 源时间回退/)
  assert.match(result.text, /源时间回退不是已解决冲突/)
  result.page.manualEnd = 11
  assert.equal(result.page.canSave, false)
  result.page.manualEnd = result.page.minimumEnd
  assert.equal(result.page.canSave, true)
})

test('恢复自动仅清播放覆盖，生成失败仍保留人工草稿', async t => {
  const input: any = line()
  Object.assign(input.timing, { manual_start: 9, manual_end: 13, manual_active: true, effective_mode: 'manual' })
  const result = await render(input, page => { page.playbackMode = 'automatic' })
  assert.match(result.text, /恢复自动并保存/)
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options: any) => {
    calls.push({ url, body: JSON.parse(options.body) }); return new Response('{"message":"相邻句子正在修改，请重试"}', { status: 409 })
  })
  await result.page.savePlayback()
  assert.deepEqual(calls[0].body, { revision: 'timing-revision', reset: true })
  assert.match(result.page.error, /相邻句子正在修改/); assert.equal(result.page.dirty, true)
  assert.equal(result.events.some(([name]: any) => name === 'close'), false)
})

test('源稿校对使用原接口，未保存播放草稿阻止切页与进入参数', async t => {
  const result = await render()
  result.page.playbackMode = 'manual'; result.page.manualStart = 8.5
  result.page.changePage('source'); result.page.editParameters()
  assert.equal(result.page.activePage, 'playback'); assert.equal(result.notices.length, 2)
  assert.equal(result.events.some(([name]: any) => name === 'edit-parameters'), false)
  result.page.playbackMode = 'automatic'; result.page.changePage('source')
  result.page.draft.text = '修订译文'
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options: any) => {
    calls.push({ url, body: JSON.parse(options.body) }); return new Response('{"ok":true}')
  })
  await result.page.saveSource()
  assert.equal(calls[0].url, '/api/projects/fixture/workbench/lines/3/text')
  assert.equal(calls[0].body.revision, 'source-revision')
  assert.equal(calls[0].body.values.text, '修订译文')
  assert.equal(calls[0].body.start, undefined)
})

test('过期人工位置明确未采用，重新核对保存不强迫新建配音槽', async () => {
  const input: any = line()
  Object.assign(input.timing, { manual_start: 9, manual_end: 13, manual_active: false, manual_stale: true })
  const result = await render(input)
  assert.match(result.text, /旧人工位置当前未采用/)
  assert.equal(result.page.playbackMode, 'manual'); assert.equal(result.page.canSave, true)
})

test('同槽声音历史恢复只调用音频记录接口，明确满意需重确认，保持合成设置不变', async t => {
  const input: any = line()
  input.wav.adjustment_history = { revision: 'audio-revision', current_id: 'shortened', entries: [
    { id: 'natural', label: '首次自然配音', factor: 1, seconds: 4 },
    { id: 'shortened', label: '自动缩短结果', factor: .85, seconds: 3 },
  ] }
  const result = await render(input, page => { page.audioRecord = 'natural' })
  assert.match(result.text, /自动缩时记录/); assert.match(result.text, /不调用模型、不新建槽/)
  assert.match(result.text, /试听后请重新确认满意/)
  assert.equal(result.page.canSave, false, '声音选择不能误由保存时间按钮提交')
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options: any) => {
    calls.push({ url, body: JSON.parse(options.body) }); return new Response('{"ok":true}')
  })
  await result.page.applyAudioRecord()
  assert.deepEqual(calls, [{ url: '/api/projects/fixture/workbench/lines/3/audio-adjustment',
    body: { revision: 'audio-revision', entry_id: 'natural' } }])
  assert.equal(result.events.some(([name]: any) => name === 'saved'), true)
})

test('项目锁住或无有效音频不能写入播放层；只读信息仍可查看', async t => {
  const result = await render(undefined, page => { page.playbackMode = 'manual'; page.manualStart = 9 }, true)
  let called = false
  t.mock.method(globalThis, 'fetch', async () => { called = true; return new Response('{}') })
  await result.page.savePlayback(); assert.equal(called, false)
  const input: any = line(); input.wav.exists = false
  const unavailable = await render(input)
  assert.equal(unavailable.page.timing.editable, false)
  assert.match(unavailable.text, /选择有效配音后可人工调整；原声、静音或缺失音频仍按源稿时间处理/)
})
