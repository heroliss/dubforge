import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { normalizeContentReview } from '../src/utils/contentReview.ts'

const root = fileURLToPath(new URL('..', import.meta.url))
let server: ViteDevServer, Preview: any
before(async () => {
  server = await createServer({ root, configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Preview = (await server.ssrLoadModule('/src/components/ContentReviewPreview.vue')).default
})
after(() => server.close())
const bundled: Record<string, string> = {}
for (const component of ['ContentReviewPreview', 'ContentReviewPanel']) {
  const { descriptor } = parse(await readFile(new URL(`../src/components/${component}.vue`, import.meta.url), 'utf8'))
  const exposed = component === 'ContentReviewPreview' ? 'clip,mode,source,loading,error,conflict,generate,cancel' : 'report,selected,previewBusy,terms,operation,conflict,error,apply,decide,load,resetProject,dirty'
  const result = await build({ stdin: { contents: `import { reactive, mountHooks, unmountHooks } from 'vue';
    const fixtureProps=reactive(__PROPS__), events=[], defineProps=()=>fixtureProps, defineEmits=()=> (...values)=>events.push(values);
    ${descriptor.scriptSetup!.content}
    export { fixtureProps, events, mountHooks, unmountHooks, ${exposed} };`, loader: 'ts', resolveDir: `${root}/src/components` },
    bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'boundaries', setup(b) {
      b.onResolve({ filter: /^(vue|naive-ui)$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
      b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue'
        ? `export {ref,computed,watch,reactive} from '@vue/reactivity';export const mountHooks=[],unmountHooks=[],onMounted=f=>mountHooks.push(f),onBeforeUnmount=f=>unmountHooks.push(f);`
        : a.path === 'naive-ui' ? `export const NAlert={},NButton={},NCard={},NCheckbox={},NInput={},NRadio={},NRadioGroup={},NTag={};` : 'export default {};', resolveDir: root }))
    } }] })
  bundled[component] = result.outputFiles[0].text
}
let serial = 0
function report(extra: Record<string, any> = {}) {
  return { enabled: true, mute_audio: true, audio_mode: 'voice_only', blocked_terms: [], blocked_ranges: '', revision: 3, configuration_fingerprint: 'cfg3', decision_revision: '0',
    checked_at: '2026-10-08T10:00:00Z', rule_version: 'rules', findings: [{ key: 'current:8', evidence_fingerprint: 'e8', ids: [8], start: 59, end: 62, decision: 'pending', active: true }],
    summary: { lines: 10, blocked_lines: 1, blocked_comments: 0 }, ...extra }
}
function clip(extra: Record<string, any> = {}) {
  return { key: 'current:8', start: 57, end: 64, focus_start: 59, focus_end: 62, truncated: false,
    original_url: '/api/projects/fixture/content-review/previews/clip/original.mp4', processed_url: '/api/projects/fixture/content-review/previews/clip/processed.mp4',
    muted_ranges: [{ start: 1.85, end: 5.15 }], hidden_ids: [8], subtitles_available: true, warnings: [], scope: 'source_audio', fingerprint: 'clip1', ...extra }
}
const response = (value: any, status = 200) => ({ ok: status < 400, status, json: async () => value })
async function fixture(t: any, component = 'ContentReviewPreview', extraProps: Record<string, any> = {}) {
  const current = normalizeContentReview(report())
  const props = component === 'ContentReviewPreview' ? { uid: 'fixture', finding: current.findings[0], report: current, disabled: false } : { uid: 'fixture', disabled: false }
  const page = await import(`data:text/javascript;base64,${Buffer.from(bundled[component].replace('__PROPS__', JSON.stringify({ ...props, ...extraProps })) + `\n//${serial++}`).toString('base64')}`)
  t.after(() => page.unmountHooks.forEach((hook: () => void) => hook()))
  return page
}
async function renderPreview(seed: (page: any) => void = () => {}, reportExtra: Record<string, any> = {}) {
  const current = normalizeContentReview(report(reportExtra))
  const app = createSSRApp({ render: () => h(Preview, { uid: 'fixture', finding: current.findings[0], report: current }) }); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ContentReviewPreview') seed(this.$.setupState) } })
  return renderToString(app)
}

test('打开前没有播放器或片段网络请求；生成后只用一个播放器比较原声规则且不自动播放', async t => {
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return response(clip()) })
  const html = await renderPreview()
  assert.equal(calls, 0); assert.doesNotMatch(html, /<video\b/)
  const loaded = await renderPreview(page => { page.clip = clip(); page.mode = 'processed' })
  assert.equal([...loaded.matchAll(/<video\b/g)].length, 1)
  for (const value of ['原声片段核对', '处理前', '当前屏蔽效果', '成片字幕开关以版本方案为准', '最终配音、评论和画面仍需在成片中核对', '隐藏 1 句字幕', '片段内时间']) assert.ok(loaded.includes(value), value)
  assert.match(loaded, /processed\.mp4/); assert.doesNotMatch(loaded, /autoplay|original\.mp4/)
})

test('片段按声音模式解释背景保留与整段静音，并展示实际兜底警告', async () => {
  const voice = await renderPreview(page => { page.clip = clip({ warnings: ['分离背景不可用，此片段已整段静音'],
    audio_processing: { requested_mode: 'voice_only', effective_mode: 'all_audio', background_identity: { path: '内部背景路径' }, fallback_reason: '分离背景不可用，此片段已整段静音', bgm_volume: .2 } }) })
  assert.match(voice, /屏蔽命中语音；分离背景不可用时整段静音/)
  assert.match(voice, /实际效果请听验处理后片段/)
  assert.match(voice, /分离背景不可用，此片段已整段静音/)
  assert.equal((voice.match(/分离背景不可用，此片段已整段静音/g) || []).length, 1)
  assert.match(voice, /本次声音处理：命中时段整段静音（含背景）/)
  assert.doesNotMatch(voice, /内部背景路径|background_identity|bgm_volume/)
  assert.match(voice, /声音处理范围/)
  const all = await renderPreview(page => { page.clip = clip() }, { audio_mode: 'all_audio' })
  assert.match(all, /命中时段整段静音（含背景）/)
  assert.doesNotMatch(all, /使用分离后的背景音保留/)
  const off = await renderPreview(() => {}, { mute_audio: false })
  assert.match(off, /声音设置：声音未静音/)
})

test('片段生成携带当前证据和版本，防双击，切换只改变一个播放器来源', async t => {
  const calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', (url, options) => { calls.push([url, options]); return new Promise(resolve => { finish = () => resolve(response(clip())) }) })
  const page = await fixture(t)
  const first = page.generate(); await page.generate(); assert.equal(calls.length, 1)
  assert.equal(calls[0][0], '/api/projects/fixture/content-review/preview')
  assert.deepEqual(JSON.parse(calls[0][1].body), { key: 'current:8', evidence_fingerprint: 'e8', decision_revision: '0', revision: 3, configuration_fingerprint: 'cfg3' })
  finish(); await first; assert.equal(page.loading.value, false); assert.match(page.source.value, /original\.mp4/)
  page.mode.value = 'processed'; assert.match(page.source.value, /processed\.mp4/); assert.equal(calls.length, 1)
  assert.deepEqual(page.events, [['busy-change', true], ['busy-change', false]])
})

test('设置、人工版本变化与禁用撤销旧预览，迟到响应不能恢复播放器', async t => {
  const calls: any[] = [], finish: any[] = []
  t.mock.method(globalThis, 'fetch', (_url, options) => { calls.push(options); return new Promise(resolve => finish.push(() => resolve(response(clip())))) })
  const page = await fixture(t)
  const first = page.generate(); page.fixtureProps.report.decision_revision = '1'
  assert.equal(calls[0].signal.aborted, true); assert.equal(page.clip.value, null)
  finish[0](); await first; assert.equal(page.clip.value, null)
  const second = page.generate(); page.fixtureProps.disabled = true
  assert.equal(calls[1].signal.aborted, true); finish[1](); await second
  assert.equal(page.loading.value, false); assert.equal(page.clip.value, null)
  page.fixtureProps.disabled = false; const third = page.generate(); page.unmountHooks.forEach((hook: () => void) => hook())
  assert.equal(calls[2].signal.aborted, true); finish[2](); await third; assert.equal(page.clip.value, null)
})

test('声音模式变化撤下旧预览，并中止在途片段生成', async t => {
  let finish: any, signal: AbortSignal | undefined
  t.mock.method(globalThis, 'fetch', (_url, options) => { signal = options!.signal as AbortSignal; return new Promise(resolve => { finish = () => resolve(response(clip())) }) })
  const page = await fixture(t), pending = page.generate()
  page.fixtureProps.report.audio_mode = 'all_audio'
  assert.equal(signal!.aborted, true)
  finish(); await pending; assert.equal(page.clip.value, null)
})

test('片段冲突要求重新读取，429可手动重试，异常来源URL不会加载', async t => {
  let status = 409, calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return response({ message: '证据已变化' }, status) })
  const page = await fixture(t)
  await page.generate(); await page.generate(); assert.equal(calls, 1); assert.equal(page.conflict.value, true)
  assert.ok(page.events.some((event: any[]) => event[0] === 'conflict'))
  page.fixtureProps.report.decision_revision = '2'; status = 429
  await page.generate(); assert.equal(page.conflict.value, false); assert.equal(page.clip.value, null)
  t.mock.method(globalThis, 'fetch', async () => response(clip({ original_url: 'https://example.com/foreign.mp4' })))
  await page.generate(); assert.equal(page.clip.value, null); assert.match(page.error.value, /结果不完整/)
})

test('真实监听的配置草稿变化撤下播放器，逐条保存busy事件完整，禁用与项目切换中止旧请求', async t => {
  const calls: any[] = [], finish: any[] = []
  t.mock.method(globalThis, 'fetch', (_url, options) => { calls.push(options); return new Promise(resolve => finish.push(() => resolve(response(report({ decision_revision: '1' }))))) })
  const page = await fixture(t, 'ContentReviewPanel')
  page.apply(normalizeContentReview(report())); page.selected.value = page.report.value.findings[0]
  page.terms.value = '用户配置草稿'; assert.equal(page.selected.value, null); assert.equal(page.dirty.value, true)
  const pending = page.decide(page.report.value.findings[0], 'allow')
  assert.ok(page.events.some((event: any[]) => event[0] === 'busy-change' && event[1] === true))
  page.fixtureProps.disabled = true; assert.equal(calls[0].signal.aborted, true); assert.equal(page.conflict.value, true)
  finish[0](); await pending; assert.equal(page.report.value.decision_revision, '0'); assert.equal(page.terms.value, '用户配置草稿')
  assert.equal(page.events.at(-1)[0], 'busy-change'); assert.equal(page.events.at(-1)[1], false)
  page.fixtureProps.disabled = false; const read = page.load(); page.fixtureProps.uid = 'another'
  assert.equal(calls[1].signal.aborted, true); assert.equal(page.report.value, null)
  finish[1](); await read; assert.equal(page.report.value, null)
})
