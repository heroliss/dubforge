import assert from 'node:assert/strict'
import test, { after, before } from 'node:test'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { setup } from '@css-render/vue3-ssr'
import { actualChanges, changeBlockedReason, changeComparisonText, changeGroupCount, changeText, linkedChangeIds, reconcileChangeSelection, toggleChangeSelection, updateAction, visibleChangeGroups, type AssemblyChanges } from '../src/utils/assemblyChanges.ts'
import { versionSpec, versionSummary } from '../src/utils/assemblyVersions.ts'

function fixture(): AssemblyChanges {
  return { baseline: { version_id: 'base', name: '原声双语成片', artifact_id: 21, artifact_hash: 'a'.repeat(64), file_exists: true },
    baselines: [{ version_id: 'base', name: '原声双语成片', artifact_id: 21, artifact_hash: 'a'.repeat(64) }],
    coverage: 'known', global_changes: [], full_rebuild_reasons: [], manual_candidates: [],
    summary: { changed: 1, total_changes: 2 }, sentences: [{ id: 405, status: 'changed', change_kinds: ['subtitle'] }],
    groups: [{ kind: 'subtitle', label: '字幕', status: 'changed', count: 1, changes: [{ id: 'subtitle:405', line_id: 405,
      reason: '原文已修改', before: { source_text: 'Old English sentence.', text: '译文保留' }, current: { source_text: 'Corrected English sentence.', text: '译文保留' },
      baseline_range: { start: 10, end: 12 }, current_range: { start: 10, end: 12 } }] },
    { kind: 'comment', label: '上屏评论', status: 'changed', count: 1, changes: [{ id: 'comment:one', reason: '评论文字已修改',
      before: [{ parts: ['旧评论'], start: 20, end: 25 }], current: [{ parts: ['新评论'], start: 20, end: 25 }] }] }],
    suggestion: { line_ids: [405], ranges: [{ start: 10, end: 12 }, { start: 20, end: 25 }], can_partial: true, replace_audio: false } }
}
test('所选更新版本只保存固定计划引用，不发送派生目标或内部文件', () => {
  const version = { id: 'update-child', name: '两句更新', dubbing: false, burn_subtitles: true, include_comments: false,
    overrides: {}, update: { plan_id: 'fixed-selected-plan', target: { internal_path: 'never-send' } },
    update_summary: { method: 'full', private_path: 'never-send' }, values: { 'assemble.subtitle_track': 'bilingual' } }
  const spec = versionSpec(version)
  assert.deepEqual(spec.update, { plan_id: 'fixed-selected-plan' })
  assert.equal('update_summary' in spec, false)
  assert.equal('values' in spec, false)
  assert.match(versionSummary(version), /所选更新/)
  assert.doesNotMatch(versionSummary(version), /局部修订/)
})
test('未知历史不显示零改动一致；时间轴和整体设置有变化选择完整更新', () => {
  const data = fixture()
  assert.equal(updateAction(data), 'partial')
  assert.equal(updateAction({ ...data, coverage: 'unknown', suggestion: { ...data.suggestion!, can_partial: false } }), 'full')
  assert.equal(updateAction({ ...data, global_changes: ['剪辑时间轴变化'], full_rebuild_reasons: ['剪辑时间轴变化'], suggestion: { ...data.suggestion!, can_partial: false } }), 'full')
  assert.equal(changeGroupCount({ kind: 'subtitle', label: '字幕', status: 'unknown', count: 0, changes: [] }), '待核对')
})
test('前后对照显示真实英文与评论文字，不暴露事件哈希或内部对象', () => {
  assert.equal(changeText('subtitle', { source_text: 'English', text: '中文' }), '原文：English\n译文：中文')
  assert.equal(changeText('comment', [{ id: 'private-id', parts: ['中文评论', 'English comment'], style_signature: 'private-hash' }]), '中文评论\nEnglish comment')
  assert.equal(changeText('settings', false), '关闭')
  assert.equal(changeText('cuts', { ranges: [{ start: 82, end: 88 }] }), '删除 00:01:22.000–00:01:28.000')
  assert.equal(changeText('cuts', null), '保留完整视频')
  assert.equal(changeText('cuts', '尾帧修正后的剪辑渲染规则'), '尾帧修正后的剪辑渲染规则')
})
test('字幕前后对照仅显示实际变化的语言字段，纯时间变化不重复全文', () => {
  const row = fixture().groups![0].changes[0]
  assert.equal(changeComparisonText('subtitle', row, 'before'), '原文：Old English sentence.')
  assert.equal(changeComparisonText('subtitle', row, 'current'), '原文：Corrected English sentence.')
  const both = { ...row, before: { source_text: 'Old English', text: '旧译文' }, current: { source_text: 'New English', text: '新译文' } }
  assert.equal(changeComparisonText('subtitle', both, 'before'), '原文：Old English\n译文：旧译文')
  assert.equal(changeComparisonText('subtitle', both, 'current'), '原文：New English\n译文：新译文')
  const timing = { ...row, current: { ...row.before, start: 11, end: 13 } }
  assert.equal(changeComparisonText('subtitle', timing, 'before'), '')
  assert.equal(changeComparisonText('subtitle', timing, 'current'), '')
  assert.equal(changeComparisonText('subtitle', { ...both, current: { ...both.current, source_text: '' } }, 'current'), '原文：（空）\n译文：新译文')
  const comment = fixture().groups![1].changes[0]
  assert.equal(changeComparisonText('comment', comment, 'before'), '旧评论')
  assert.equal(changeComparisonText('settings', { id: 'enabled', reason: '启用', before: false, current: true }, 'current'), '开启')
})
test('未修改的剪辑成片不把局部能力限制误报为需要完整更新', () => {
  const data: AssemblyChanges = { ...fixture(), needs_update: false, whole_render_changed: false,
    summary: { changed: 0, total_changes: 0 }, groups: [],
    full_rebuild_reasons: ['局部替换暂不支持此时间轴'], partial_unavailable_reasons: ['局部替换暂不支持此时间轴'],
    suggestion: { line_ids: [], ranges: [], can_partial: false } }
  assert.equal(updateAction(data), 'unchanged')
  assert.equal(updateAction({ ...data, needs_update: true }), 'full')
})
test('只列出真实差异；未知历史与一致分组不混入勾选清单', () => {
  const data = fixture()
  data.groups!.push({ kind: 'audio', label: '声音', status: 'unchanged', count: 0, changes: [] },
    { kind: 'settings', label: '呈现设置', status: 'unknown', count: 0, changes: [] })
  assert.deepEqual(visibleChangeGroups(data).map(row => row.kind), ['subtitle', 'comment'])
  assert.deepEqual(actualChanges(data).map(row => row.id), ['subtitle:405', 'comment:one'])
})
test('首次默认选中可用差异，刷新保留取消选择且不勾选新增改动', () => {
  const data = fixture(), first = reconcileChangeSelection(data)
  assert.deepEqual(first.selected, ['subtitle:405', 'comment:one'])
  const declined = { ...first, selected: ['subtitle:405'] }
  const updated = fixture()
  updated.groups![0].changes.push({ id: 'subtitle:406', line_id: 406, reason: '原文已修改' })
  const refreshed = reconcileChangeSelection(updated, declined)
  assert.deepEqual(refreshed.selected, ['subtitle:405'])
  assert.deepEqual(refreshed.added, ['subtitle:406'])
  assert.deepEqual(reconcileChangeSelection(updated, refreshed).selected, ['subtitle:405'])
  assert.deepEqual(reconcileChangeSelection({ ...updated, baseline: { ...updated.baseline!, version_id: 'other' } }, refreshed).selected,
    ['subtitle:405', 'subtitle:406', 'comment:one'])
})
test('依赖按关联组选择和取消，阻塞原因覆盖整个关联组', () => {
  const rows = [
    { id: 'subtitle:405', reason: '时间修改', linked_ids: ['audio:405'] },
    { id: 'audio:405', reason: '配音时间修改' },
    { id: 'comment:one', reason: '评论修改' },
  ]
  assert.deepEqual(new Set(linkedChangeIds(rows, ['audio:405'])), new Set(['audio:405', 'subtitle:405']))
  assert.deepEqual(new Set(toggleChangeSelection(rows, [], ['subtitle:405'], true)), new Set(['subtitle:405', 'audio:405']))
  assert.deepEqual(toggleChangeSelection(rows, rows.map(row => row.id), ['audio:405'], false), ['comment:one'])
  const blocked = rows.map(row => row.id === 'audio:405' ? { ...row, selectable: false, blocked_reason: '请先生成本句最新配音' } : row)
  assert.equal(changeBlockedReason(blocked[0], blocked), '请先生成本句最新配音')
  assert.deepEqual(toggleChangeSelection(blocked, [], ['subtitle:405'], true), [])
  assert.equal(changeBlockedReason({ id: 'missing', reason: '关联缺失', linked_ids: ['not-read'] },
    [{ id: 'missing', reason: '关联缺失', linked_ids: ['not-read'] }]), '关联改动尚未完整读取，请重新检查。')
})
test('刷新新增关联依赖时取消旧勾选，避免静默选入新增内容或发送不完整关联组', () => {
  const data = fixture(), first = reconcileChangeSelection(data), next = fixture()
  next.groups![0].changes[0].linked_ids = ['audio:405']
  next.groups!.push({ kind: 'audio', label: '声音', status: 'changed', count: 1,
    changes: [{ id: 'audio:405', reason: '播放时间联动' }] })
  const updated = reconcileChangeSelection(next, first)
  assert.deepEqual(updated.selected, ['comment:one'])
  assert.deepEqual(updated.added, ['audio:405'])
})
let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/AssemblyChangesPanel.vue')).default
})
after(async () => { await server.close() })
async function render(value = fixture(), seed: (page: any) => void = () => {}, overrides: Record<string, any> = {}) {
  let page: any, props: any, editor: any, editorProps: any
  const created: any[] = [], full: string[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/projects/fixture/steps/assemble')
  const app = createSSRApp({ render: () => h(Panel, { uid: 'fixture', ...overrides, onCreated: (row: any) => created.push(row), onFull: (id: string) => full.push(id) }) })
  app.use(router); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'AssemblyChangesPanel') { page = this.$.setupState; props = this.$.props; page.acceptComparison(value); seed(page) }
    if (this.$options.__name === 'AssemblyPartialEditor') {
      editor = this.$.setupState; editorProps = this.$.props
      editor.context = { baselines: value.baselines, duration: 2000, lines: [405, 406].map(id => ({id, start: id, end: id + 1, text: `Updated English ${id}`})) }
      editor.baseline = value.baseline?.version_id
    }
  } })
  const html = await renderToString(app)
  return { page, props, html, created, full, editor, editorProps }
}
// 保留真实 setup、挂载与响应式 watch；不挂载无关模板，也不借 SSR 结束后的 computed 缓存测试切换。
async function mountState(t: any, initial: Record<string, any> = {}, value = fixture(), response?: (url: string, options: any) => any) {
  type HostNode = { children: HostNode[]; parent?: HostNode; text?: string }
  const renderer = createRenderer<HostNode, HostNode>({
    createElement: () => ({ children: [] }), createText: text => ({ children: [], text }),
    createComment: text => ({ children: [], text }), patchProp() {},
    insert(node, parent) { node.parent = parent; parent.children.push(node) },
    remove(node) { if (node.parent) node.parent.children = node.parent.children.filter(row => row !== node) },
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text },
    parentNode: node => node.parent || null, nextSibling: () => null,
  })
  const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { addEventListener() {}, removeEventListener() {} } })
  const calls: { url: string; options: any }[] = [], baselines: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    calls.push({ url: String(url), options })
    return response ? response(String(url), options) : { ok: true, json: async () => value } as Response
  })
  const props = reactive({ uid: 'fixture', preferredVersion: 'base', ...initial })
  let page: any
  const HeadlessPanel = { ...Panel, render: () => null }
  const app = renderer.createApp({ render: () => h(HeadlessPanel, { ...props, onBaselineChange: (id: string) => {
    baselines.push(id); props.preferredVersion = id
  } }) })
  // Vite 的 SFC 模块登记需要此注入；这里仍为普通 mount，不启用 SSR 的一次性 watch 行为。
  app.provide(ssrContextKey, { modules: new Set() })
  app.mixin({ created() { if (this.$options.__name === 'AssemblyChangesPanel') page = this.$.setupState } })
  t.after(() => {
    app.unmount()
    if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  app.mount({ children: [] })
  await nextTick(); await new Promise(resolve => setImmediate(resolve))
  return { page, props, calls, baselines }
}

test('挂载后切到未生成版本立即废弃旧计划，切回原基准重新读取并恢复可勾选范围', async t => {
  const { page, props, calls } = await mountState(t)
  page.plan = { fingerprint: 'old-confirmation' }; page.plannedBody = { base_version_id: 'base', change_ids: ['subtitle:405'] }
  props.preferredVersion = 'unbuilt-version'; await nextTick()
  assert.equal(page.plan, null); assert.equal(page.plannedBody, null); assert.equal(page.selection, null)
  assert.equal(page.chosenBaseline, 'unbuilt-version'); assert.equal(page.comparisonReady, false)
  props.preferredVersion = 'base'; await nextTick(); await new Promise(resolve => setImmediate(resolve))
  assert.equal(page.comparisonReady, true); assert.ok(page.selection)
  page.toggleChanges(['comment:one'], false); assert.deepEqual(page.selectedChanges, ['subtitle:405'])
  await page.create()
  assert.equal(calls.filter(row => row.options?.method === 'POST').length, 0)
  assert.ok(calls.some(row => /version_id=base/.test(row.url)))
})

test('父级已选第二基准而旧比较尚在时必须读取第二基准，异步期间不能使用旧计划', async t => {
  const value = fixture(), second = { ...value.baseline!, version_id: 'second', name: '第二基准' }
  value.baselines.push(second)
  let finish: ((value: Response) => void) | undefined
  const { page, props, calls } = await mountState(t, {}, value, url => /version_id=second/.test(url)
    ? new Promise<Response>(resolve => { finish = resolve })
    : { ok: true, json: async () => value } as Response)
  page.plan = { fingerprint: 'old-plan' }; page.plannedBody = { base_version_id: 'base', change_ids: ['subtitle:405'] }
  props.preferredVersion = 'second'; await nextTick()
  assert.ok(finish); assert.equal(page.chosenBaseline, 'second'); assert.equal(page.data.baseline.version_id, 'base')
  assert.equal(page.reading, true); assert.equal(page.comparisonReady, false); assert.equal(page.plan, null)
  await page.create(); assert.equal(calls.filter(row => row.options?.method === 'POST').length, 0)
  finish!({ ok: true, json: async () => ({ ...value, baseline: second }) } as Response)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(page.data.baseline.version_id, 'second'); assert.equal(page.comparisonReady, true)
})

test('显式修订核验后的旧基准回报仍同步父级，编辑器忙碌结束后再刷新比较', async t => {
  const { page, props, calls, baselines } = await mountState(t, { preferredVersion: 'unbuilt-version', requestToken: 'verified-request' })
  assert.equal(page.data.baseline.version_id, 'base'); assert.equal(page.chosenBaseline, 'unbuilt-version')
  page.manualBusy = true
  const reads = calls.length
  page.requestedBaseline('base'); await nextTick()
  assert.deepEqual(baselines, ['base']); assert.equal(props.preferredVersion, 'base')
  assert.equal(calls.length, reads, '不能打断编辑器正在完成的基准核验')
  page.manualBusy = false; await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls.length, reads + 1); assert.match(calls.at(-1)!.url, /version_id=base/)
  assert.equal(page.comparisonReady, true); assert.ok(page.selection)
  assert.equal(page.plan, null)
})

test('成片页直接呈现英文修改前后与评论对照', async () => {
  const result = await render()
  assert.match(result.html, /Old English sentence/); assert.match(result.html, /Corrected English sentence/)
  assert.match(result.html, /检查所选改动（2 项）/)
  assert.match(result.html, /本次应用 2 项/)
  assert.match(result.html, /成片中 · 取消勾选时保留/)
  assert.doesNotMatch(result.html, /译文保留/)
  const comment = await render(fixture(), page => { page.group = 'comment' })
  assert.match(comment.html, /旧评论/); assert.match(comment.html, /新评论/)
})
test('版本工作台由版本清单唯一控制基准，不再显示第二个成片选择器', async () => {
  const result = await render(fixture(), () => {}, { preferredVersion: 'base' })
  assert.match(result.html, /当前基准/)
  assert.match(result.html, /由下方版本清单控制/)
  assert.match(result.html, /原声双语成片/)
  assert.doesNotMatch(result.html, /aria-label="检查改动的成片版本"/)
})

test('首次制作没有正式基准时显示空状态，不把草稿说成仍在读取或打开片段重制', async t => {
  const value: AssemblyChanges = { ...fixture(), baseline: null, baselines: [], coverage: 'unknown', groups: [] }
  const { html, page, editorProps } = await render(value, () => {}, { preferredVersion: 'new-draft' })
  assert.match(html, /尚未生成正式成片/)
  assert.doesNotMatch(html, /正在读取成片版本|当前基准成片|成片更新方式|按片段重制|id="assembly-partial"/)
  assert.equal(editorProps, undefined)
  assert.equal(page.manualVisited, false)
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return { ok: true, json: async () => ({}) } as Response })
  page.chooseMode('ranges'); page.full(); await page.check(); await page.create()
  assert.equal(requests, 0)
})

test('当前草稿未生成时不使用其他版本的成片和差异执行更新', async t => {
  const { html, page, created, full } = await render(fixture(), p => {
    p.plan = { fingerprint: 'other-baseline-plan' }; p.plannedBody = { base_version_id: 'base', change_ids: ['subtitle:405'] }
    p.manualVisited = true
  }, { preferredVersion: 'unbuilt-version' })
  assert.match(html, /当前版本尚未生成正式成片/)
  assert.doesNotMatch(html, /Old English sentence|应用所选改动并生成|当前基准成片|id="assembly-partial"/)
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return { ok: true, json: async () => ({}) } as Response })
  await page.create(); await page.check(); page.full()
  assert.equal(requests, 0); assert.deepEqual(created, []); assert.deepEqual(full, [])
})

test('明确带入的句子修订仍交由编辑器验证基准，不被当前未生成版本的空状态吞掉', async () => {
  const { html, editorProps } = await render(fixture(), () => {}, {
    preferredVersion: 'unbuilt-version', requestToken: 'explicit-sentence-draft',
  })
  assert.equal(editorProps.requestToken, 'explicit-sentence-draft')
  assert.match(html, /id="assembly-partial"/)
  assert.doesNotMatch(html, /Old English sentence|应用所选改动并生成|当前基准成片/)
})

test('父级刚切换版本时旧计划立即失效，读取失败不恢复旧基准的执行入口', async t => {
  const { page, props, full } = await render(fixture(), p => {
    p.plan = { fingerprint: 'old-plan' }; p.plannedBody = { base_version_id: 'base', change_ids: ['subtitle:405'] }
  }, { preferredVersion: 'base' })
  props.preferredVersion = 'new-version'
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; throw new Error('临时断开') })
  await page.create(); await page.check(); page.full()
  assert.equal(requests, 0); assert.deepEqual(full, [])
  await page.refresh()
  assert.equal(requests, 1); assert.match(page.error, /临时断开/)
  await page.create(); assert.equal(requests, 1)
})
test('两种语言都修改时保留完整差异，仅改时间时只显示时间与句号', async () => {
  const both = fixture()
  both.groups![0].changes[0].before.text = '旧译文'
  both.groups![0].changes[0].current.text = '新译文'
  const changed = await render(both)
  assert.match(changed.html, /原文：Old English sentence/)
  assert.match(changed.html, /译文：旧译文/); assert.match(changed.html, /译文：新译文/)
  const timing = fixture(), row = timing.groups![0].changes[0]
  row.current = { ...row.before }; row.current_range = { start: 11, end: 13 }
  row.reason = '字幕显示时间已变化'
  const timed = await render(timing)
  assert.match(timed.html, /第 405 句/)
  assert.match(timed.html, /原来 00:00:10.000–00:00:12.000 → 当前 00:00:11.000–00:00:13.000/)
  assert.doesNotMatch(timed.html, /Old English sentence|译文保留|成片中 · 取消勾选时保留/)
})
test('旧成片默认进入唯一片段编辑器，候选与基准由父级共享', async () => {
  const unknown: AssemblyChanges = { ...fixture(), coverage: 'unknown', summary: { changed: 0, total_changes: 0 },
    groups: [{ kind: 'subtitle', label: '字幕', status: 'unknown', count: 0, changes: [] }],
    suggestion: { line_ids: [], ranges: [], can_partial: false }, manual_candidates: [405, 406].map(id => ({ id, fields: ['source_text'], reason: '手工修改待核对', current: { source_text: `Updated English ${id}`, text: '译文', start: id, end: id + 1 }, range: { start: id, end: id + 1 } })) }
  const { html, page, editorProps } = await render(unknown)
  assert.equal(page.mode, 'ranges')
  assert.equal(editorProps.embedded, true)
  assert.equal(editorProps.preferredVersion, 'base')
  assert.deepEqual(editorProps.candidates.map((row: any) => row.id), [405, 406])
  assert.match(html, /第 405 句/); assert.match(html, /第 406 句/); assert.match(html, /Updated English 405/)
  assert.match(html, /完整更新并建立对照/); assert.doesNotMatch(html, /这一部分与所选成片一致/)
  assert.doesNotMatch(html, /检查勾选片段|添加手工修订版本|manual-candidates|选择局部重制句子/)
})
test('字幕与评论按明确ID检查和创建，未选内容不混入请求', async t => {
  const { page, created } = await render()
  page.toggleChanges(['comment:one'], false)
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), body: JSON.parse(String(options?.body)) })
    return { ok: true, json: async () => String(url).endsWith('update-plan') ? { fingerprint: 'selected-current', method: 'partial', selection_count: 1, remaining_count: 1, ranges: [{ start: 10, end: 12 }], encode_seconds: 4 } : { version: { id: 'child' }, method: 'partial', remaining_count: 1, ranges: [{ start: 10, end: 12 }] } } as Response
  })
  await page.check(false); assert.equal(page.plan.fingerprint, 'selected-current'); assert.equal(created.length, 0)
  await page.create()
  assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, { base_version_id: 'base', change_ids: ['subtitle:405'] })
  assert.match(calls[0].url, /update-plan$/); assert.match(calls[1].url, /update-create$/)
  assert.equal(calls[1].body.fingerprint, 'selected-current')
  assert.equal(created.length, 1); assert.equal(page.plan, null)
  assert.equal(created[0].update_summary.remaining_count, 1)
  assert.equal(created[0].partial_summary.method, 'partial')
})
test('切换片段模式清除差异计划，不让隐藏的操作链继续执行', async t => {
  const { page, created } = await render()
  page.plan = { fingerprint: 'previous' }; page.plannedBody = { change_ids: ['subtitle:405'] }
  page.chooseMode('ranges')
  assert.equal(page.plan, null); assert.equal(page.plannedBody, null); assert.equal(page.manualVisited, true)
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return { ok: true, json: async () => ({}) } as Response })
  await page.check(); await page.create()
  assert.equal(calls, 0); assert.deepEqual(created, [])
})

test('跨页句子请求只打开一个片段编辑器，使用同一基准并保留请求身份', async () => {
  const { html, page, editorProps } = await render(fixture(), () => {}, {requestToken:'incoming-lines'})
  assert.equal(page.mode, 'ranges'); assert.equal(page.manualVisited, true)
  assert.equal(editorProps.requestToken, 'incoming-lines'); assert.equal(editorProps.preferredVersion, 'base')
  assert.equal((html.match(/id="assembly-partial"/g) || []).length, 1)
  assert.doesNotMatch(html, /手工修改待核对 ·|局部修订已有成片<|选择局部重制句子/)
})

test('父级切换基准后预检只发送最新基准，不沿用另一个隐藏表单的基准', async t => {
  const { page } = await render(), calls:any[]=[]
  const value=fixture(), second={...value.baseline!,version_id:'second',name:'第二基准'}
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),body:options?.body?JSON.parse(String(options.body)):null})
    return {ok:true,json:async()=>options?.method==='POST'?{fingerprint:'second-plan',method:'partial'}:{...value,baselines:[...value.baselines,second],baseline:second}} as Response
  })
  page.chooseBaseline('second'); await new Promise(resolve=>setImmediate(resolve))
  assert.equal(page.chosenBaseline,'second'); assert.equal(page.data.baseline.version_id,'second')
  await page.check()
  assert.equal(calls.at(-1).body.base_version_id,'second')
  assert.match(calls[0].url,/version_id=second/)
})

test('差异检查错误不封锁片段入口；版本上限只交给新增修订版本检查', async () => {
  const { page, editorProps }=await render(fixture(), page=>{page.error='评论差异暂时无法读取';page.chooseMode('ranges')},{versionLimit:true})
  assert.equal(page.mode,'ranges'); assert.equal(editorProps.disabled,false); assert.equal(editorProps.versionLimit,true)
  page.plan={fingerprint:'hidden'};page.manualBusy=true;page.chooseMode('changes')
  assert.equal(page.mode,'ranges');assert.equal(page.plan.fingerprint,'hidden')
})
test('零选择不发预检；勾选变化会使旧计划失效', async t => {
  const { page } = await render()
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(JSON.parse(String(options?.body)))
    return { ok: true, json: async () => ({ fingerprint: 'planned', method: 'partial' }) } as Response
  })
  page.toggleChanges(['subtitle:405', 'comment:one'], false)
  await page.check(false); assert.equal(calls.length, 0)
  page.toggleChanges(['subtitle:405'], true)
  await page.check(false); assert.equal(page.plan.fingerprint, 'planned')
  page.toggleChanges(['comment:one'], true)
  assert.equal(page.plan, null); assert.equal(page.plannedBody, null)
  await page.create(); assert.equal(calls.length, 1)
})
test('完整生成仍发送明确选择并保留full方法，不转交旧版全量入口', async t => {
  const { page, created, full } = await render()
  page.toggleChanges(['comment:one'], false)
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.deepEqual(JSON.parse(String(options?.body)).change_ids, ['subtitle:405'])
    return { ok: true, json: async () => String(url).endsWith('update-plan')
      ? { fingerprint: 'full-selection', method: 'full', selection_count: 1, remaining_count: 1, ranges: [] }
      : { version: { id: 'full-child' }, method: 'full', remaining_count: 1 } } as Response
  })
  await page.check(false); await page.create()
  assert.equal(created.length, 1); assert.equal(created[0].update_summary.method, 'full')
  assert.equal(created[0].partial_summary, undefined)
  assert.deepEqual(full, [])
})
test('界面单独显示阻塞与依赖，且不把未勾选差异说成已应用', async () => {
  const value = fixture()
  value.groups![0].changes[0].selectable = false
  value.groups![0].changes[0].blocked_reason = '请先生成最新配音'
  value.groups!.push({ kind: 'audio', label: '声音', status: 'unchanged', count: 0, changes: [] },
    { kind: 'settings', label: '呈现设置', status: 'unknown', count: 0, changes: [] })
  value.coverage = 'partial'
  const { html } = await render(value)
  assert.match(html, /请先生成最新配音/); assert.match(html, /需先处理/)
  assert.match(html, /本次应用 1 项/); assert.match(html, /保留 1 项待更新/)
  assert.match(html, /待核对：呈现设置/)
  assert.doesNotMatch(html, /选择本组声音改动|当前内容已应用|选择本组呈现设置改动/)
})
test('组件刷新后保持取消勾选，新差异需要用户选择', async t => {
  const { page } = await render()
  page.toggleChanges(['comment:one'], false)
  const next = fixture()
  next.groups![0].changes.push({ id: 'subtitle:406', reason: '字幕修改', line_id: 406 })
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => next }) as Response)
  await page.refresh()
  assert.deepEqual(page.selectedChanges, ['subtitle:405'])
  assert.deepEqual(page.selection.added, ['subtitle:406'])
})
test('刷新期间保留已打开的片段编辑器，但隐藏旧差异清单', async () => {
  const { html, editorProps } = await render(fixture(), page => { page.chooseMode('ranges'); page.reading = true })
  assert.ok(editorProps); assert.equal(editorProps.disabled, true)
  assert.doesNotMatch(html, /Old English sentence/)
})
test('旧记录完整更新仍转交预检，已知差异不能绕过选择直接全量更新', async () => {
  const { page, props, full } = await render({ ...fixture(), coverage: 'unknown' })
  page.full(); assert.deepEqual(full, ['base'])
  props.disabled = true; page.full(); assert.equal(full.length, 1)
  const known = await render(); known.page.full(); assert.equal(known.full.length, 0)
})

test('首次读取期间的目标版本切换使用最新选择，不拿默认成片代替', async t => {
  const { page, props } = await render()
  const pending: ((value: Response) => void)[] = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => pending.push(resolve)))
  const first = page.refresh(); props.preferredVersion = 'second'
  const base = fixture(), second = { ...base.baseline!, version_id: 'second', name: '第二个成片' }
  const value = { ...base, baselines: [...base.baselines, second] }
  pending[0]({ ok: true, json: async () => value } as Response)
  await first; await new Promise(resolve => setImmediate(resolve))
  assert.equal(pending.length, 2)
  pending[1]({ ok: true, json: async () => ({ ...value, baseline: second }) } as Response)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(page.data.baseline.version_id, 'second')
})

test('重新核对失败后不再允许把旧比较作为可更新结论', async t => {
  const { page, full } = await render()
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('临时断开') })
  await page.refresh(); assert.match(page.error, /临时断开/)
  page.full(); assert.equal(full.length, 0)
  const view = await render(fixture(), p => { p.error = '读取失败，请重试。' })
  assert.doesNotMatch(view.html, /检查所选改动|当前内容已应用/)
})

test('评论错误标明成片改动检查的范围并提供评论页恢复入口', async t => {
  const { page } = await render()
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status: 400,
    json: async () => ({ code: 'COMMENTS_DISPLAY_TEXT', message: '中文上屏文字为空' }) }) as Response)
  await page.refresh()
  assert.equal(page.commentProblem, true, '用稳定错误码识别旧服务端没有评论前缀的提示')
  assert.equal(page.plan, null)
  const view = await render(fixture(), p => { p.error = '上屏评论文字尚未就绪'; p.commentProblem = true })
  assert.match(view.html, /暂时无法检查成片改动/)
  assert.match(view.html, /前往上屏评论检查/)
  assert.match(view.html, /href="\/projects\/fixture\/comments"/)
  assert.doesNotMatch(view.html, /当前内容已应用|检查所选改动/)
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => fixture() }) as Response)
  await page.refresh(); assert.equal(page.error, ''); assert.equal(page.commentProblem, false)
})
