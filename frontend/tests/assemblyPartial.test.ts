import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { partialRanges, partialSummary, partialTime } from '../src/utils/assemblyPartial.ts'
import { versionSpec, versionSummary } from '../src/utils/assemblyVersions.ts'
import { saveAssemblySentenceDraft } from '../src/utils/assemblySentenceLink.ts'

let server: ViteDevServer, Editor: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  ;({ default: Editor } = await server.ssrLoadModule('/src/components/AssemblyPartialEditor.vue'))
})
after(async () => { await server.close() })
async function render(seed: (page: any, props: any) => void = () => {}, extraProps: Record<string, any> = {}) {
  let page: any, props: any
  const created: any[] = [], baselines: string[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/projects/fixture/steps/assemble')
  const app = createSSRApp({ render: () => h(Editor, { uid: 'fixture', ...extraProps,
    onCreated: (v: any) => created.push(v), onBaselineChange: (id: string) => baselines.push(id) }) })
  app.use(router)
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssemblyPartialEditor') {
    page = this.$.setupState; props = this.$.props; seed(page, props)
  } } })
  return { html: await renderToString(app), page, props, created, baselines }
}
function response(value: any) { return { ok: true, json: async () => value } as Response }
const context = { baselines: [{ version_id: 'base', name: '原声双语' }], duration: 2000,
  lines: [{ id: 405, start: 1898.52, end: 1903.88, text: '修订字幕' }, { id: 406, start: 1904.18, end: 1910.1, text: '另一句' }] }

test('任意多段时间支持秒和时分秒，保留毫秒且拒绝非法范围', () => {
  assert.deepEqual(partialRanges([{ key: 'a', start: '05:25.125', end: '360' }], 500), [{ start: 325.125, end: 360 }])
  for (const [start, end] of [['', '5'], ['5', '5'], ['-1', '2'], ['0', 'Infinity'], ['1', '501']])
    assert.throws(() => partialRanges([{ key: 'a', start, end }], 500))
  assert.equal(partialTime(1898.52), '00:31:38.520')
  assert.equal(partialTime(59.9996), '00:01:00.000')
  assert.equal(partialTime(3798.52), '01:03:18.520')
  assert.match(partialSummary({ ranges: [{ start: 5, end: 6 }], encode_seconds: 4 }), /1 段.*00:00:01.000.*00:00:04.000/)
})
test('补充范围以时分秒确认并就地拒绝无效输入，不发送检查请求', async t => {
  const { page } = await render(p => { p.context = context; p.baseline = 'base'; p.lineIds = [405] })
  page.ranges = [{ key: 'custom', start: '05:25.125', end: '360' }]
  page.commitRangeTime(page.ranges[0], 'start'); page.commitRangeTime(page.ranges[0], 'end')
  assert.deepEqual(page.ranges, [{ key: 'custom', start: '00:05:25.125', end: '00:06:00.000' }])
  assert.equal(page.canCheck, true)
  page.ranges[0].end = '00:99:00'
  page.commitRangeTime(page.ranges[0], 'end')
  assert.equal(page.ranges[0].end, '00:99:00')
  assert.equal(page.canCheck, false)
  assert.match(page.rangeIssues[0], /第 1 段/)
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response({}) })
  await page.check()
  assert.equal(requests, 0)
  const invalid = await render(p => { p.expanded = true; p.context = context; p.baseline = 'base'; p.ranges = [{ key: 'bad', start: '30', end: '20' }] })
  assert.match(invalid.html, /时间待完善/)
  assert.match(invalid.html, /aria-invalid="true"/)
  assert.match(invalid.html, /第 1 段的结束时间需晚于开始时间/)
})

test('版本草稿保留局部方案引用，不复制服务器派生清单', () => {
  const version = { id: 'child', name: '局部', dubbing: false, burn_subtitles: true, include_comments: false,
    overrides: {}, partial: { plan_id: 'immutable-plan', injected: 'ignored' }, partial_plan: { private_path: 'never-send' } }
  const spec = versionSpec(version)
  assert.deepEqual(spec.partial, { plan_id: 'immutable-plan' })
  assert.equal('partial_plan' in spec, false)
  assert.match(versionSummary(spec), /局部修订/)
})
test('范围修订只调用方案接口，由父工作台统一保存并启动任务', async t => {
  const { page, created } = await render(p => { p.expanded = true; p.context = context; p.baseline = 'base' })
  page.lineIds = [405, 406]; page.replaceAudio = false; await nextTick()
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url: String(url), body: JSON.parse(String(options?.body)) })
    return response(String(url).endsWith('partial-plan')
      ? { fingerprint: 'confirmed', ranges: [{ start: 1898.52, end: 1910.1 }], encode_seconds: 16 }
      : { version: { id: 'child', partial: { plan_id: 'abc' } } })
  })
  await page.check(); assert.equal(page.plan.fingerprint, 'confirmed')
  await page.create()
  assert.equal(requests.length, 2); assert.match(requests[0].url, /partial-plan$/)
  assert.deepEqual(requests[0].body.line_ids, [405, 406]); assert.equal(requests[0].body.replace_audio, false)
  assert.equal(requests[1].body.fingerprint, 'confirmed'); assert.equal(created.length, 1)
  assert.equal(page.expanded, false)
  const { html } = await render(p => { p.expanded = true; p.context = context; p.baseline = 'base' })
  assert.match(html, /其余沿用基准成片/); assert.match(html, /同时更新所选范围的声音/)
  assert.match(html, /检查修订范围/); assert.match(html, /不立即生成或投稿/)
  assert.match(html, /当前全部画面内容/)
  const checked = await render(p => { p.expanded = true; p.context = context; p.baseline = 'base'; p.plan = { ranges: [] } })
  assert.match(checked.html, /生成片段修订版/); assert.match(checked.html, /保存为独立版本并开始生成/)
})
test('切换项目中止旧检查，迟到响应不能向新项目添加版本', async t => {
  const { page, props, created } = await render(p => { p.context = context; p.baseline = 'base'; p.lineIds = [405] })
  let resolve: (r: any) => void = () => {}, signal: any
  t.mock.method(globalThis, 'fetch', (_url, options) => {
    signal = options?.signal; return new Promise<Response>(r => { resolve = r })
  })
  const pending = page.check()
  assert.equal(page.busy, true)
  props.uid = 'another'; page.reset()
  assert.equal(signal.aborted, true); assert.equal(page.busy, false)
  resolve(response({ fingerprint: 'old-project' })); await pending
  assert.equal(page.plan, null); assert.equal(page.context, null); assert.equal(created.length, 0)
})
test('修改任一范围或声音选择会使先前检查失效', async () => {
  // SSR 完成后 Vue 会销毁 watcher；在组件存活期间验证失效行为。
  await render(page => {
    page.context = context; page.baseline = 'base'
    page.plan = { fingerprint: 'old' }; page.replaceAudio = true; assert.equal(page.plan, null)
    page.plan = { fingerprint: 'old' }; page.addRange(); assert.equal(page.plan, null)
  })
})

test('旧后端缺少接口时给出明确重启说明，不保留可提交的过期方案',async t=>{
  const {page}=await render(p=>{p.context=context;p.baseline='base';p.lineIds=[405]})
  page.plan={fingerprint:'stale'}
  t.mock.method(globalThis,'fetch',async()=>({status:404,ok:false,json:async()=>({detail:'Not Found'})}) as Response)
  await page.check()
  assert.equal(page.plan,null);assert.match(page.error,/重启后端/);assert.equal(page.busy,false)
})

test('从句子列表带入历史和当前范围，只预填不创建、不自动生成', async t => {
  const { page, props, created } = await render()
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  saveAssemblySentenceDraft(storage, { uid: 'fixture', token: 'incoming', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405, 406], ranges: [{ start: 1890, end: 1910.1 }], replaceAudio: true })
  props.requestToken = 'incoming'
  const requests: string[] = []
  t.mock.method(globalThis, 'fetch', async url => { requests.push(String(url)); return response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] }) })
  await page.openRequested(storage)
  assert.equal(page.expanded, true); assert.deepEqual(page.lineIds, [405, 406]); assert.equal(page.replaceAudio, true)
  assert.deepEqual(page.ranges, []); assert.match(page.requestNotice, /已从句子列表带入 2 句/)
  assert.match(page.requestNotice, /新旧位置由预检随最终勾选计算/)
  assert.equal(created.length, 0); assert.deepEqual(requests, ['/api/projects/fixture/assembly-versions/partial-context'])
  page.lineIds = [406]; await page.openRequested(storage); assert.deepEqual(page.lineIds, [406])
})

test('跨页后同编号基准内容若已更新，拒绝继续套用旧范围', async t => {
  const { page, props } = await render()
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  saveAssemblySentenceDraft(storage, { uid: 'fixture', token: 'old-base', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405], ranges: [], replaceAudio: false })
  props.requestToken = 'old-base'
  t.mock.method(globalThis, 'fetch', async () => response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'b'.repeat(64) }] }))
  await page.openRequested(storage)
  assert.match(page.error, /基准成片已改变/); assert.deepEqual(page.lineIds, []); assert.equal(page.plan, null)
})

test('新跨页请求保留正在编辑的范围，明确载入后才替换草稿', async t => {
  const { page, props } = await render(p => { p.lineIds = [405]; p.name = '尚未完成的手动方案' })
  props.requestToken = 'new'
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  saveAssemblySentenceDraft(storage, { uid: 'fixture', token: 'new', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [406], ranges: [], replaceAudio: false })
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async url => { calls.push(String(url)); return response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] }) })
  await page.openRequested(storage)
  assert.equal(page.incomingRequest, true); assert.deepEqual(page.lineIds, [405]); assert.equal(calls.length, 0)
  await page.openRequested(storage, true)
  assert.equal(page.incomingRequest, false); assert.deepEqual(page.lineIds, [406]); assert.equal(calls.length, 1)
})

test('导入后换基准会清空原基准带入的范围，要求重新选择', async () => {
  await render(page => {
    page.baseline = 'base'; page.requestBaseline = 'base'; page.lineIds = [405]
    page.ranges = [{ key: 'old', start: '10', end: '15' }]
    page.baseline = 'another'
    assert.deepEqual(page.lineIds, []); assert.deepEqual(page.ranges, [])
    assert.match(page.requestNotice, /重新选择/)
  })
})

test('读取中新的请求取代旧请求，迟到旧请求不会丢掉最新预填', async t => {
  const { page, props } = await render()
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  const draft = { uid: 'fixture', token: 'one', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405], ranges: [], replaceAudio: false }
  saveAssemblySentenceDraft(storage, draft); props.requestToken = 'one'
  const pending: ((value: Response) => void)[] = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => pending.push(resolve)))
  const first = page.openRequested(storage)
  saveAssemblySentenceDraft(storage, { ...draft, token: 'two', lineIds: [406] }); props.requestToken = 'two'
  await page.openRequested(storage)
  const current = response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] })
  pending[0](current); await first; await new Promise(resolve => setImmediate(resolve))
  assert.equal(pending.length, 2)
  pending[1](current); await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(page.lineIds, [406]); assert.equal(page.appliedRequest, 'two')
})

const candidates = [{ id: 405, fields: ['source_text'], current: { source_text: 'Updated original China', text: '修订字幕' }, reason: '手工修改待核对' }]
test('嵌入模式只显示一份复选清单，不重复基准、展开按钮或句子下拉', async () => {
  const { html, page } = await render(p => { p.context = context; p.baseline = 'base' }, { embedded: true, preferredVersion: 'base', candidates })
  assert.equal(page.expanded, true); assert.equal(page.candidateOnly, true)
  assert.match(html, /修订第 405 句/); assert.doesNotMatch(html, /修订第 406 句/)
  assert.match(html, /有编辑记录/); assert.match(html, /搜索句号或字幕文字/)
  assert.doesNotMatch(html, /aria-label="基准成片"|选择局部重制句子|收起范围修订/)
  assert.match(html, /编辑记录仅供定位，不代表这些修改尚未应用/)
})
test('筛选和搜索只改变显示，同一份选句跨筛选保留并可清空', async () => {
  const { page } = await render(p => { p.context = context; p.baseline = 'base' }, { embedded: true, candidates })
  page.toggleLine(405, true); assert.deepEqual(page.lineIds, [405])
  page.query = '406'; assert.deepEqual(page.filteredRows.map((row: any) => row.id), [406])
  page.toggleLine(406, true); assert.deepEqual(page.lineIds, [405, 406]); assert.equal(page.selectedHidden, 1)
  page.query = 'china'; assert.deepEqual(page.filteredRows.map((row: any) => row.id), [405])
  page.query = ''; page.candidateOnly = false
  assert.equal(page.filteredRows.length, 2); assert.deepEqual(page.lineIds, [405, 406])
  page.clearSelection(); assert.deepEqual(page.lineIds, []); assert.equal(page.canCheck, false)
})
test('长视频只逐批呈现清单，但搜索可直接找到列表后方句子', async () => {
  const { page } = await render(p => { p.context = { ...context, lines: Array.from({ length: 1600 }, (_, i) => ({ id: i + 1, start: i, end: i + .8, text: `字幕 ${i + 1}` })) }; p.baseline = 'base' }, { embedded: true })
  assert.equal(page.filteredRows.length, 1600); assert.equal(page.displayedRows.length, 80)
  page.query = '1599'; assert.deepEqual(page.filteredRows.map((row: any) => row.id), [1599])
  page.toggleLine(1599, true); assert.deepEqual(page.lineIds, [1599])
})
test('父级唯一基准切换会清空旧范围和计划，不留下独立基准', async () => {
  await render((page, props) => {
    page.context = { ...context, baselines: [...context.baselines, { version_id: 'another', name: '另一成片' }] }
    page.baseline = 'base'; page.lineIds = [405]; page.ranges = [{ key: 'old', start: '10', end: '15' }]
    page.plan = { fingerprint: 'old' }; page.requestNotice = '旧提示'; page.name = '旧名称'
    props.preferredVersion = 'another'; page.syncPreferred('another')
    assert.equal(page.baseline, 'another'); assert.deepEqual(page.lineIds, []); assert.deepEqual(page.ranges, [])
    assert.equal(page.plan, null); assert.equal(page.requestNotice, ''); assert.equal(page.name, '')
  }, { embedded: true, preferredVersion: 'base' })
})
test('新生成的父级基准尚不在旧上下文中时仍同步，并清空旧范围', async () => {
  await render((page, props) => {
    page.context = context; page.baseline = 'base'; page.lineIds = [405]; page.ranges = [{ key: 'old', start: '10', end: '15' }]
    page.plan = { fingerprint: 'old' }; page.requestNotice = '旧提示'
    props.preferredVersion = 'newly-created'; page.syncPreferred('newly-created')
    assert.equal(page.baseline, 'newly-created'); assert.deepEqual(page.lineIds, []); assert.deepEqual(page.ranges, [])
    assert.equal(page.plan, null); assert.equal(page.requestNotice, '')
    assert.equal(page.options.some((row: any) => row.value === 'newly-created'), false)
  }, { embedded: true, preferredVersion: 'base' })
})
test('重新读取旧上下文时仍以父级新基准为准，不回退到列表中的旧基准', async t => {
  const { page } = await render(p => { p.context = context; p.baseline = 'newly-created' }, { embedded: true, preferredVersion: 'newly-created' })
  t.mock.method(globalThis, 'fetch', async () => response(context))
  await page.load()
  assert.equal(page.baseline, 'newly-created')
})
test('32版本上限只阻止创建，仍可修改选句并检查范围', async t => {
  const { page, created, html } = await render(p => { p.context = context; p.baseline = 'base' }, { embedded: true, versionLimit: true })
  page.toggleLine(405, true); assert.equal(page.canCheck, true); assert.equal(page.locked, false)
  const requests: string[] = []
  t.mock.method(globalThis, 'fetch', async url => { requests.push(String(url)); return response({ fingerprint: 'within-limit-plan', ranges: [] }) })
  await page.check(); assert.equal(page.plan.fingerprint, 'within-limit-plan')
  await page.create(); assert.equal(requests.length, 1); assert.match(requests[0], /partial-plan$/)
  assert.equal(created.length, 0); assert.equal(page.error, ''); assert.match(html, /可继续选择和检查范围/)
})
test('嵌入式跨页请求成功带入后通知唯一基准，不丢新旧范围', async t => {
  const { page, props, baselines } = await render(() => {}, { embedded: true, preferredVersion: 'different' })
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  saveAssemblySentenceDraft(storage, { uid: 'fixture', token: 'shared-base', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405, 406], ranges: [{ start: 1890, end: 1910.1 }], replaceAudio: true })
  props.requestToken = 'shared-base'
  t.mock.method(globalThis, 'fetch', async () => response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] }))
  await page.openRequested(storage)
  assert.deepEqual(baselines, ['base']); assert.equal(page.baseline, 'base'); assert.deepEqual(page.lineIds, [405, 406])
  assert.deepEqual(page.ranges, []); assert.equal(page.replaceAudio, true)
})
test('清单空搜索有恢复提示，检查后返回调整不产生额外提交', async () => {
  const { html } = await render(p => { p.context = context; p.baseline = 'base'; p.query = 'not-found' }, { embedded: true })
  assert.match(html, /没有找到匹配的句子/); assert.match(html, /清空选择/)
  const checked = await render(p => { p.context = context; p.baseline = 'base'; p.plan = { ranges: [] } }, { embedded: true })
  assert.match(checked.html, /返回调整/); assert.doesNotMatch(checked.html, /重新检查范围/)
})

test('带入405与406后取消406，只按405检查，不把旧合并范围当作补充时间段', async t => {
  const { page, props } = await render(() => {}, { embedded: true, preferredVersion: 'base' })
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  saveAssemblySentenceDraft(storage, { uid: 'fixture', token: 'cancel-406', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [405, 406], ranges: [{ start: 1890, end: 1910.1 }], replaceAudio: false })
  props.requestToken = 'cancel-406'
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url: String(url), body: options?.body ? JSON.parse(String(options.body)) : null })
    return response(String(url).endsWith('partial-context')
      ? { ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] }
      : { fingerprint: 'only-405', ranges: [{ start: 1898.52, end: 1903.88 }] })
  })
  await page.openRequested(storage); page.toggleLine(406, false); await page.check()
  assert.deepEqual(page.lineIds, [405]); assert.deepEqual(page.ranges, [])
  assert.deepEqual(requests[1].body.line_ids, [405]); assert.deepEqual(requests[1].body.ranges, [])
  assert.equal(page.plan.fingerprint, 'only-405')
})
test('纯自定义时间范围仍完整带入，逐句选择丢失时拒绝回退到旧合并范围', async t => {
  const values = new Map<string, string>(), storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
  const draft = { uid: 'fixture', token: 'ranges-only', created: Date.now(), baseline: 'base', artifactId: 21, artifactHash: 'a'.repeat(64),
    lineIds: [] as number[], ranges: [{ start: 1890, end: 1910.1 }], replaceAudio: false }
  t.mock.method(globalThis, 'fetch', async () => response({ ...context, baselines: [{ ...context.baselines[0], artifact_id: 21, artifact_hash: 'a'.repeat(64) }] }))
  const { page, props } = await render()
  saveAssemblySentenceDraft(storage, draft); props.requestToken = draft.token
  await page.openRequested(storage)
  assert.deepEqual(page.lineIds, []); assert.equal(page.ranges[0].start, '00:31:30.000'); assert.equal(page.ranges[0].end, '00:31:50.100')
  assert.match(page.requestNotice, /1 段自定义时间范围/); assert.equal(page.canCheck, true)
  const missing = await render()
  saveAssemblySentenceDraft(storage, { ...draft, token: 'missing', lineIds: [999] }); missing.props.requestToken = 'missing'
  await missing.page.openRequested(storage)
  assert.match(missing.page.error, /句子已不存在/); assert.deepEqual(missing.page.ranges, []); assert.deepEqual(missing.page.lineIds, [])
  assert.equal(missing.page.canCheck, false)
})
