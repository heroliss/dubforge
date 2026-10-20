import assert from 'node:assert/strict'
import test, { after, before } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { setup } from '@css-render/vue3-ssr'
import { assemblyDraftScope, assemblySentenceReason, assemblySentenceRevision,
  assemblyComparisonKnown, assemblyReviewCandidates, assemblySentenceNotices,
  readAssemblySentenceDraft, saveAssemblySentenceDraft, type AssemblySentenceComparison } from '../src/utils/assemblySentenceLink.ts'

const comparison = (): AssemblySentenceComparison => ({
  baselines: [{ version_id: 'dub', name: '配音双语版', artifact_id: 21, artifact_hash: 'a'.repeat(64) }],
  baseline: { version_id: 'dub', name: '配音双语版', artifact_id: 21, artifact_hash: 'a'.repeat(64) }, coverage: 'known', global_changes: [],
  sentences: [{ id: 405, status: 'changed', change_reasons: ['字幕文字已修改'], change_kinds: ['subtitle'],
    current_range: { start: 12, end: 15 }, baseline_range: { start: 10, end: 13 } },
  { id: 406, status: 'changed', change_reasons: ['配音声音已修改'], change_kinds: ['audio'], current_range: { start: 30, end: 32 } },
  { id: 407, status: 'unchanged', dubbing_pending: true }],
  suggestion: { line_ids: [405, 406], ranges: [{ start: 30, end: 32 }, { start: 10, end: 15 }], can_partial: true, replace_audio: true },
})
function storage() {
  const data = new Map<string, string>()
  return { getItem: (key: string) => data.get(key) || null, setItem: (key: string, value: string) => { data.set(key, value) } }
}
test('成片修订携带新旧字幕位置，声音参数待应用不冒充已变更的成片音频', () => {
  assert.deepEqual(assemblyDraftScope(comparison(), [405]), { lineIds: [405], ranges: [{ start: 10, end: 15 }], replaceAudio: false })
  assert.equal(assemblyDraftScope(comparison(), [406]).replaceAudio, true)
  assert.equal(assemblyDraftScope(comparison(), [407]).replaceAudio, false)
  assert.deepEqual(assemblyDraftScope(comparison(), [405, 406], true).ranges, [{ start: 10, end: 15 }, { start: 30, end: 32 }])
  assert.equal(assemblySentenceReason({ id: 1, status: 'unknown' }), '')
})
test('跨页意图限制在本项目、本次请求和有效时间，不把它当作服务器授权', () => {
  const store = storage(), draft = { uid: 'fixture', token: 'one', created: 1000, baseline: 'dub', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405, 405, 406], ranges: [{ start: 10, end: 15 }], replaceAudio: false }
  const target = saveAssemblySentenceDraft(store, draft)
  assert.equal(target.query.partial_request, 'one'); assert.equal(target.hash, '#assembly-partial')
  assert.deepEqual(readAssemblySentenceDraft(store, 'fixture', 'one', 2000)?.lineIds, [405, 406])
  assert.equal(readAssemblySentenceDraft(store, 'another', 'one', 2000), null)
  assert.equal(readAssemblySentenceDraft(store, 'fixture', 'old', 2000), null)
  assert.equal(readAssemblySentenceDraft(store, 'fixture', 'one', 13 * 3600000), null)
  saveAssemblySentenceDraft(store, { ...draft, ranges: [{ start: 20, end: 10 }] })
  assert.equal(readAssemblySentenceDraft(store, 'fixture', 'one', 2000), null)
})
test('独立对照仅随有效内容改变而刷新，不随任务进度反复请求', () => {
  const row: any = { id: 1, text: '字幕', source_text: 'source', start: 1, end: 2, selection: { source: 'dub', take: 1 },
    wav: { url: '/wav?v=1', exists: true }, synthesis_revision: 'one', progress: .1 }
  const initial = assemblySentenceRevision([row])
  row.progress = .8; assert.equal(assemblySentenceRevision([row]), initial)
  row.wav.url = '/wav?v=2'; assert.notEqual(assemblySentenceRevision([row]), initial)
  row.wav.url = '/wav?v=1'; row.playback_start = .8; assert.notEqual(assemblySentenceRevision([row]), initial)
})

let server: ViteDevServer, Link: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()],
    appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Link = (await server.ssrLoadModule('/src/components/DubbingAssemblyLink.vue')).default
})
after(async () => { await server.close() })
async function render(data: AssemblySentenceComparison | null, selected = [405], seed?: (page: any) => void) {
  let page: any
  const received: any[] = [], reading: string[] = [], router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/projects/fixture/steps/tts')
  const app = createSSRApp({ render: () => h(Link, { uid: 'fixture', revision: 'one', selected,
    onComparison: (value: any) => received.push(value), onReadingChange: (value: string) => reading.push(value) }) })
  app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'DubbingAssemblyLink') { page = this.$.setupState; page.value = data; seed?.(page) } } })
  const html = await renderToString(app)
  return { page, html, received, reading, router }
}
test('对照以所选成片为准，待配音和成片待更新分开表达', async () => {
  const { html } = await render(comparison())
  assert.match(html, /2 句成片待更新/); assert.match(html, /1 句配音待更新，重做后再核对成片/)
  assert.match(html, /修订变化片段/); assert.match(html, /修订勾选片段/)
  const unknown = await render({ ...comparison(), coverage: 'unknown', sentences: [], suggestion: undefined })
  assert.match(unknown.html, /暂不能自动识别历史差异/); assert.doesNotMatch(unknown.html, /句子内容与此成片一致/)
})
test('切换成片取消迟到对照，不能把旧基准差异显示到新基准', async t => {
  const { page, received } = await render(null)
  const pending: ((value: any) => void)[] = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => pending.push(resolve)))
  const old = page.refresh(); page.chooseVersion('new')
  pending[1]({ ok: true, json: async () => ({ ...comparison(), baseline: { version_id: 'new', name: '新成片', artifact_id: 22, artifact_hash: 'b'.repeat(64) } }) })
  await new Promise(resolve => setImmediate(resolve))
  pending[0]({ ok: true, json: async () => comparison() }); await old
  assert.equal(page.value.baseline.version_id, 'new')
  assert.equal(received.filter(row => row?.baseline?.version_id === 'dub').length, 0)
})

test('未知历史只列手工修改候选，不把待配音或其他未知句当作成片差异', () => {
  const value: AssemblySentenceComparison = { ...comparison(), coverage: 'unknown',
    sentences: [{ id: 405, status: 'unknown' }, { id: 406, status: 'unknown', dubbing_pending: true }, { id: 407, status: 'unknown' }],
    manual_candidates: [{ id: 405, fields: ['source_text'], reason: '手工修改待核对' }] }
  assert.equal(assemblyComparisonKnown(value), false)
  assert.deepEqual(assemblyReviewCandidates(value).map(row => row.id), [405])
  const notices = assemblySentenceNotices(value)
  assert.equal(notices.get(405)?.label, '成片待核对')
  assert.match(notices.get(405)?.reason || '', /缺少完整逐句快照/)
  assert.equal(notices.has(406), false)
  assert.equal(notices.has(407), false)
  assert.equal(value.sentences?.filter(row => row.status === 'changed').length, 0)
})

test('部分快照可同时显示真实差异和手工待核对，真实差异优先', () => {
  const value: AssemblySentenceComparison = { ...comparison(), coverage: 'partial',
    manual_candidates: [{ id: 405 }, { id: 407 }] }
  const notices = assemblySentenceNotices(value)
  assert.equal(notices.get(405)?.label, '成片待更新')
  assert.equal(notices.get(407)?.label, '成片待核对')
})

test('coverage误称known但仍有unknown行时，不能显示一致或自动修订', async () => {
  const value: AssemblySentenceComparison = { ...comparison(), sentences: [{ id: 405, status: 'unknown' }] }
  assert.equal(assemblyComparisonKnown(value), false)
  const result = await render(value)
  assert.doesNotMatch(result.html, /句子内容与此成片一致/)
  assert.match(result.html, /尚未确认差异/)
  assert.equal(result.page.canSuggest, false)
})

test('旧成片显示未知总数与手工候选，不显示零待更新或暗示其余句一致', async () => {
  const value: AssemblySentenceComparison = { ...comparison(), coverage: 'unknown',
    sentences: [{ id: 405, status: 'unknown' }, { id: 406, status: 'unknown' }],
    manual_candidates: [{ id: 405, fields: ['source_text'] }, { id: 406, fields: ['source_text'] }], suggestion: undefined }
  const result = await render(value)
  assert.match(result.html, /2 句缺少可比较的历史快照/)
  assert.match(result.html, /2 句成片待核对（手工修改候选）/)
  assert.match(result.html, /不代表其余未知句与成片一致/)
  assert.doesNotMatch(result.html, /0 句成片待更新|句子内容与此成片一致/)
})

test('开始核对立刻取消旧结论可用性，保留membership避免筛选暂时清空', async t => {
  const old = comparison(), result = await render(old)
  let finish: (value: unknown) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { finish = resolve }))
  const refresh = result.page.refresh()
  assert.deepEqual(result.reading, ['loading'])
  assert.equal(result.page.value.baseline.artifact_id, old.baseline!.artifact_id)
  assert.equal(result.page.canSuggest, false)
  assert.equal(result.received.length, 0, '加载中不清空父层最后membership')
  result.page.openRevision(true)
  assert.equal(result.router.currentRoute.value.path, '/projects/fixture/steps/tts')
  finish({ ok: true, json: async () => ({ ...comparison(), sentences: [{ id: 405, status: 'unchanged' }] }) })
  await refresh
  assert.deepEqual(result.reading, ['loading', 'ready'])
  assert.equal(result.received.length, 1)
})

test('核对中的旧一致结果不继续作为页面结论展示', async () => {
  const value: AssemblySentenceComparison = { ...comparison(), sentences: [{ id: 405, status: 'unchanged' }] }
  const result = await render(value, [405], page => { page.busy = true })
  assert.match(result.html, /正在重新核对字幕、声音与成片/)
  assert.doesNotMatch(result.html, /句子内容与此成片一致/)
})

test('核对失败保留最后地图但通过error状态禁止使用旧结果导航', async t => {
  const result = await render(comparison())
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('模拟网络中断') })
  await result.page.refresh()
  assert.deepEqual(result.reading, ['loading', 'error'])
  assert.ok(result.page.value.baseline)
  assert.equal(result.page.canSuggest, false)
  result.page.openRevision(false)
  assert.equal(result.router.currentRoute.value.path, '/projects/fixture/steps/tts')
})

test('字幕页将评论错误定位为成片对照问题并可直接前往评论页', async t => {
  const result = await render(comparison())
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status: 400,
    json: async () => ({ code: 'COMMENTS_DISPLAY_TEXT', message: '中文上屏文字为空' }) }) as Response)
  await result.page.refresh()
  assert.equal(result.page.commentProblem, true)
  assert.equal(result.page.canSuggest, false)
  const view = await render(comparison(), [405], p => { p.error = '上屏评论文字尚未就绪'; p.commentProblem = true })
  assert.match(view.html, /成片对照暂不可用/)
  assert.match(view.html, /前往上屏评论检查/)
  assert.match(view.html, /href="\/projects\/fixture\/comments"/)
  assert.doesNotMatch(view.html, /句子内容与此成片一致|字幕与配音失败/)
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => comparison() }) as Response)
  await result.page.refresh(); assert.equal(result.page.commentProblem, false)
})
