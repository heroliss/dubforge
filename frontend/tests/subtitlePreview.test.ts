import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { srtToVtt, subtitlesToVtt } from '../src/utils/subtitlePreview.ts'

const lines = [{ id: 1, start: 1.125, end: 2.5, source_text: 'Hello <friend> & all', text: '大家好' },
  { id: 2, start: 4, end: 5, source_text: 'Next', text: '下一句' }]

test('校对稿预览保留准确起止时间、句间空隙与双语顺序，特殊文字不被解析为标签', () => {
  const bilingual = subtitlesToVtt(lines, 'bilingual')
  assert.match(bilingual, /00:00:01.125 --> 00:00:02.500\n大家好\nHello &lt;friend&gt; &amp; all/)
  assert.match(bilingual, /00:00:04.000 --> 00:00:05.000/)
  assert.match(subtitlesToVtt(lines, 'bilingual', true), /Hello &lt;friend&gt; &amp; all\n大家好/)
  assert.doesNotMatch(subtitlesToVtt(lines, 'source'), /大家好/)
  assert.doesNotMatch(subtitlesToVtt(lines, 'target'), /Hello/)
  assert.match(subtitlesToVtt([{ ...lines[0], text: '' }], 'bilingual'), /Hello/)
})

test('导出预览保留文件的双语换行与时长，支持 BOM 和 CRLF', () => {
  const vtt = srtToVtt('\uFEFF1\r\n00:00:01,000 --> 00:00:03,500\r\n译文\r\n<Source> & text\r\n\r\n')
  assert.equal(vtt, 'WEBVTT\n\n00:00:01.000 --> 00:00:03.500\n译文\n&lt;Source&gt; &amp; text\n')
})

const bundle = await build({ stdin: {
  contents: `export { useSubtitlePreview } from './src/composables/useSubtitlePreview'; export { ref, nextTick, effectScope } from 'vue'`,
  resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'ts',
}, bundle: true, write: false, format: 'esm', platform: 'browser', define: { 'process.env.NODE_ENV': '"production"' } })
const { useSubtitlePreview, ref, nextTick, effectScope } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

function fixture(context: any) {
  const state = ref({ lines: structuredClone(lines), files: [{ name: 'source.srt', url: '/source?v=1' }], ready: true, sourceFirst: false })
  const blobs = new Map<string, Blob>(), revoked: string[] = [], requests: any[] = []
  let version = 0
  context.mock.method(URL, 'createObjectURL', (blob: Blob) => { const url = `blob:test-${++version}`; blobs.set(url, blob); return url })
  context.mock.method(URL, 'revokeObjectURL', (url: string) => revoked.push(url))
  context.mock.method(globalThis, 'fetch', (url: string, { signal }: any) => new Promise(resolve => requests.push({url, signal, resolve})))
  const scope = effectScope()
  context.after(() => scope.stop())
  const preview = scope.run(() => useSubtitlePreview({ lines: () => state.value.lines,
    files: () => state.value.files, exportsReady: () => state.value.ready, sourceFirst: () => state.value.sourceFirst }))!
  return { state, scope, preview, blobs, revoked, requests }
}

test('字幕默认双语；重复轮询不重载，保存文字和时间后更新，关闭和离开时释放资源', async context => {
  const f = fixture(context), first = f.preview.url.value
  assert.equal(f.preview.mode.value, 'bilingual')
  f.state.value = JSON.parse(JSON.stringify(f.state.value))
  await nextTick()
  assert.equal(f.preview.url.value, first)
  f.state.value.lines[0] = { ...lines[0], text: '修改后的译文', end: 3 }
  await nextTick()
  assert.match(await f.blobs.get(f.preview.url.value)!.text(), /00:00:03.000\n修改后的译文/)
  assert.ok(f.revoked.includes(first))
  f.preview.mode.value = 'off'
  await nextTick()
  assert.equal(f.preview.url.value, null)
  f.preview.mode.value = 'source'
  await nextTick()
  const last = f.preview.url.value
  f.scope.stop()
  assert.ok(f.revoked.includes(last))
})

test('没有译文时回退原文；导出过期后自动使用当前文字', async context => {
  const f = fixture(context)
  f.state.value.lines = lines.map(line => ({ ...line, text: '' }))
  await nextTick()
  assert.equal(f.preview.mode.value, 'source')
  assert.match(await f.blobs.get(f.preview.url.value)!.text(), /Hello/)
  f.preview.mode.value = 'export:source.srt'
  await nextTick()
  assert.equal(f.requests.length, 1)
  f.state.value.ready = false
  await nextTick()
  assert.equal(f.preview.mode.value, 'source')
  assert.equal(f.requests[0].signal.aborted, true)
})

test('快速从文件切回当前字幕，晚到的文件响应不能覆盖新字幕', async context => {
  const f = fixture(context)
  f.preview.mode.value = 'export:source.srt'
  await nextTick()
  f.preview.mode.value = 'target'
  await nextTick()
  const current = f.preview.url.value
  f.requests[0].resolve({ ok: true, text: async () => '1\n00:00:01,000 --> 00:00:02,000\n旧字幕' })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(f.preview.url.value, current)
  assert.match(await f.blobs.get(current)!.text(), /大家好/)
})
