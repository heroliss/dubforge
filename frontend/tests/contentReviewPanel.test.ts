import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { candidateReview, normalizeContentReview, reviewTerms, reviewTime } from '../src/utils/contentReview.ts'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/ContentReviewPanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('不允许未声明请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
function fixture(extra: Record<string, any> = {}) {
  return {
    enabled: true, mute_audio: true, blocked_terms: ['测试词'], blocked_ranges: '',
    revision: 3, configuration_fingerprint: 'fixture-fingerprint', inherited: false, origin: '本项目覆盖',
    findings: [{ ids: [8], start: 59.5, end: 62.125, reason: '命中自定义词', source_text: 'original', text: '译文测试词', rule: 'custom' }],
    comment_findings: [{ id: 42, reason: '命中规则', source_text: 'comment source', text: '评论译文', rule: 'custom' }],
    summary: { lines: 12, blocked_lines: 1, blocked_comments: 1 }, warnings: [], ...extra,
  }
}
async function render(seed: (page: any) => void = () => {}, props: Record<string, any> = {}) {
  let page: any
  const events: any[] = []
  const app = createSSRApp({ render: () => h(Panel, {
    uid: 'fixture', ...props,
    'onDirty-change': (value: boolean) => events.push(['dirty', value]),
    'onBusy-change': (value: boolean) => events.push(['busy', value]),
    onSaved: (value: unknown) => events.push(['saved', value]),
  }) }); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ContentReviewPanel') { page = this.$.setupState; seed(page) } } })
  const html = await renderToString(app)
  return { page, events, html }
}

test('自定义词去掉空行和重复项；时间保留毫秒且不显示 NaN', () => {
  assert.deepEqual(reviewTerms(' a\r\n\n b \na '), ['a', 'b'])
  assert.equal(reviewTime(59.9999), '1:00.000')
  assert.equal(reviewTime(null), '时间未知')
  assert.equal(reviewTime(NaN), '时间未知')
})
test('候选审查标记缺字段不编造结论；空结论与未知接口保持区别',()=>{
  assert.equal(candidateReview(undefined),null);assert.equal(candidateReview({}),null)
  assert.equal(candidateReview({review_ready:null})?.label,'文本审查暂无结论')
  assert.equal(candidateReview({review_ready:false})?.type,'warning')
  assert.match(candidateReview({review_ready:false})!.reason,/上传预检会阻止/)
  assert.equal(candidateReview({review_ready:true,review_status:'忽略服务端状态'})?.label,'已按当前规则处理')
})
test('未知接口结构不误显示审查完成，旧报告无配置版本不能写入', async () => {
  assert.throws(() => normalizeContentReview({}), /完整的内容审查/)
  const { page } = await render(p => p.apply(normalizeContentReview(fixture({ revision: undefined, configuration_fingerprint: undefined }))))
  assert.equal(page.writable, false)
  page.terms = '待保存'
  await page.save()
  assert.equal(page.dirty, true)
})
test('报告兼容缺少可选字段，评论单 id 统一为 ids，非法时间明确为未知', () => {
  const parsed = normalizeContentReview(fixture({ blocked_ranges: undefined, warnings: null,
    findings: [{ ids: [1, '2'], start: null, end: -1 }], comment_findings: [{ id: 'Ugx-comment', text: '评论' }] }))
  assert.deepEqual(parsed.findings[0].ids, [1]); assert.equal(parsed.findings[0].start, null)
  assert.equal(parsed.findings[0].end, null); assert.equal(parsed.blocked_ranges, '')
  assert.deepEqual(parsed.comment_findings[0].ids, ['Ugx-comment']); assert.deepEqual(parsed.warnings, [])
})
test('真实组件展示原文译文、时段理由、评论及人工时段说明，内容按文本转义', async () => {
  const { html } = await render(p => p.apply(normalizeContentReview(fixture({
    findings: [{ ids: [8], start: 59.5, end: 62.125, reason: '<script>原因</script>', source_text: '<img src=x>', text: '译文测试词', rule: 'custom' }],
    original_findings: [{ ids: [3], start: 12.5, end: 15, source_text: '原声命中内容', reason: '原声规则', rule: 'builtin' }],
    blocked_ranges: '0-12.5:需核对画面', warnings: ['尚未提供全部译文'],
  }))))
  for (const value of ['发布前内容审查', '0:59.500', '1:02.125', '句 8', '译文测试词', '评论 42', 'comment source', '评论译文', '原声识别稿命中明细', '原声命中内容', '原声规则', '尚未提供全部译文', '人工屏蔽时段', '原视频时间轴', '完整混音', '恢复继承', '不能保证审核通过', '已有成片、稿件已选文件和已上传文件不会自动替换']) assert.ok(html.includes(value), value)
  assert.match(html, /&lt;script&gt;原因/); assert.match(html, /&lt;img src=x&gt;/)
  assert.doesNotMatch(html, /<script>原因|<img src=x>/)
})
test('关闭审查与关闭静音如实说明，零命中不声称审核通过', async () => {
  const { html } = await render(p => p.apply(normalizeContentReview(fixture({ enabled: false, mute_audio: false, findings: [], comment_findings: [], summary: { lines: 12, blocked_lines: 0, blocked_comments: 0 } }))))
  assert.match(html, /自动屏蔽已关闭/)
  assert.match(html, /关闭静音仍可能保留原片或配音中的相关声音/)
  assert.match(html, /未命中已配置的文本规则/)
  assert.doesNotMatch(html, /已通过审核|安全可发布/)
})
test('加载只读，刷新为明确 POST，未触发模型或设置写入', async t => {
  const { page } = await render(), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([String(url), options]); return { ok: true, json: async () => fixture() } })
  await page.load(); await page.refresh()
  assert.equal(calls.length, 2)
  assert.equal(calls[0][0], '/api/projects/fixture/content-review')
  assert.equal(calls[0][1].method, 'GET'); assert.equal(calls[0][1].body, undefined)
  assert.equal(calls[1][1].method, 'POST'); assert.equal(calls[1][1].body, undefined)
  assert.equal(page.report.summary.blocked_lines, 1)
})
test('保存包含人工时段及并发凭证，成功报告替换草稿且仅发送一次', async t => {
  const { page, events } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.enabled = false; page.muteAudio = false; page.terms = ' 新词 \n新词\n'; page.ranges = '60.5-72:人工核对'
  const calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([String(url), options]); return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => fixture({ enabled: false, mute_audio: false, blocked_terms: ['新词'], blocked_ranges: '60.5-72:人工核对', revision: 4 }) }) }) })
  const first = page.save(); await page.save()
  assert.equal(calls.length, 1); assert.equal(page.operation, 'save')
  assert.deepEqual(JSON.parse(calls[0][1].body), { enabled: false, mute_audio: false, blocked_terms: ['新词'], blocked_ranges: '60.5-72:人工核对', revision: 3, configuration_fingerprint: 'fixture-fingerprint' })
  finish(); await first
  assert.equal(page.report.revision, 4); assert.equal(page.dirty, false)
  assert.match(page.notice, /重新生成字幕和成片/)
  assert.equal(events.filter(e => e[0] === 'saved').length, 1)
})
test('恢复继承也携带版本，成功后以继承报告展示，不覆盖未保存输入', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture())))
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => { calls.push(options); return { ok: true, json: async () => fixture({ inherited: true, origin: '全局设置', revision: 4 }) } })
  page.terms = '尚未保存'; await page.reset(); await page.refresh(); assert.equal(calls.length, 0)
  page.discard(); await page.reset()
  assert.deepEqual(JSON.parse(calls[0].body), { reset: true, revision: 3, configuration_fingerprint: 'fixture-fingerprint' })
  assert.equal(page.report.inherited, true); assert.equal(page.dirty, false)
  await page.reset(); assert.equal(calls.length, 1)
})
test('保存冲突保留编辑内容与原报告，不自动重试或误报成功', async t => {
  const { page, events } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.terms = '待保存词'; let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return { ok: false, status: 409, json: async () => ({ message: '设置已变化，请刷新后重试' }) } })
  await page.save()
  assert.equal(calls, 1); assert.equal(page.terms, '待保存词'); assert.equal(page.dirty, true)
  assert.equal(page.report.revision, 3); assert.match(page.error, /设置已变化/)
  assert.equal(events.filter(e => e[0] === 'saved').length, 0)
})
test('读取失败与缺字段保留已有报告，并且读取不丢弃草稿', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture())))
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({}) }))
  await page.load(); assert.equal(page.report.summary.lines, 12); assert.match(page.error, /完整的内容审查/)
  page.terms = '正在编辑'
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => fixture({ revision: 9, blocked_terms: ['新版'] }) }))
  await page.load(); assert.equal(page.terms, '正在编辑'); assert.equal(page.report.revision, 3)
})
test('并发冲突可以显式读取最新版本并保留修改，之后保存使用新凭证', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.terms = '正在编辑\n自定义词'; page.ranges = '60-70:人工核对'
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(options)
    if (calls.length === 1) return { ok: false, status: 409, json: async () => ({ code: 'CONTENT_REVIEW_CONFLICT', message: '请读取最新设置' }) }
    return { ok: true, json: async () => fixture({ revision: 4, configuration_fingerprint: 'latest', blocked_terms: calls.length === 2 ? ['外部修改'] : ['正在编辑', '自定义词'], blocked_ranges: calls.length === 2 ? '' : '60-70:人工核对' }) }
  })
  await page.save(); assert.equal(page.conflict, true)
  await page.rebase(); assert.equal(calls[1].method, 'GET')
  assert.equal(page.report.revision, 4); assert.equal(page.terms, '正在编辑\n自定义词'); assert.equal(page.ranges, '60-70:人工核对'); assert.equal(page.dirty, true)
  await page.save(); assert.equal(JSON.parse(calls[2].body).configuration_fingerprint, 'latest'); assert.equal(page.dirty, false)
})
test('项目上下文重置取消旧请求，迟到报告不能复活旧内容', async t => {
  const { page } = await render(), calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', async (_url, options) => { calls.push(options); return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => fixture() }) }) })
  const pending = page.load(); page.resetProject()
  assert.equal(calls[0].signal.aborted, true); assert.equal(page.report, null)
  finish(); await pending; assert.equal(page.report, null); assert.equal(page.operation, null)
})
test('父级禁用时不能保存、恢复继承或刷新，并保留已展示报告', async () => {
  const { page, html } = await render(p => p.apply(normalizeContentReview(fixture())), { disabled: true })
  page.terms = '修改'; await page.save(); await page.reset(); await page.refresh()
  assert.equal(page.report.revision, 3); assert.equal(page.dirty, true)
  assert.match(html, /n-input--disabled/)
})
test('配音、成片和稿件页接入同一审查面板，未保存设置门禁覆盖正式生成与上传', () => {
  const step = readFileSync(new URL('../src/views/StepView.vue', import.meta.url), 'utf8')
  const publish = readFileSync(new URL('../src/views/PublishView.vue', import.meta.url), 'utf8')
  assert.match(step, /ContentReviewPanel v-if="key === 'tts' \|\| key === 'assemble'"/)
  assert.match(step, /AssemblyVersionWorkspace[^>]*reviewDirty \|\| reviewBusy/)
  assert.match(step, /@dirty-change="reviewDirty = \$event"/)
  assert.match(publish, /ContentReviewPanel v-if="current.project_uid"[^>]*:uid="current.project_uid"/)
  assert.match(publish, /reviewDirty\.value \|\| reviewBusy\.value\) \{ message.warning\('请先保存或放弃审查设置/)
  assert.match(publish, /reviewDirty \|\| reviewBusy \|\| !draft.parts.length/)
})

test('实际投稿页审查草稿阻止上传、投稿和切换，不向发布接口发请求', async () => {
  const View = (await server.ssrLoadModule('/src/views/PublishView.vue')).default
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/publish', component: View }, { path: '/projects/:uid/steps/assemble', component: { render: () => null } },
  ] })
  await router.push('/publish')
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(RouterView) }) }) })
  app.use(router); setup(app)
  app.mixin({ created() {
    if (this.$options.__name !== 'PublishView') return
    page = this.$.setupState
    page.catalog = { accounts: [], templates: [], defaults: {}, worker_ready: true }
    page.apply({ id: 'p', project_uid: 'fixture', revision: 1, status: 'draft', receipt: {}, snapshot: {},
      base_settings: { title: '测试', description: '', tags: [], tid: 21, copyright: 3, source: 'https://example.com', declaration_id: 1, visibility: 'public' },
      draft: { account_id: '', parts: [{ artifact_id: 1, title: 'P1' }], overrides: {}, content: {}, cover_id: '' } })
    page.source = { files: [] }; page.reviewDirty = true
  } })
  const html = await renderToString(app)
  assert.match(html, /发布前内容审查/)
  const buttons = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0]).filter(button=>/class="[^"]*\bn-button\b/.test(button))
  for (const title of ['仅上传文件', '上传并投稿']) {
    const button=buttons.find(button=>button.replace(/<[^>]*>/g,'').trim()===title)
    assert.ok(button,'必须检查实际操作按钮');assert.match(button,/disabled/)
  }
  await page.upload(); await page.upload(true); await page.submit(); assert.equal(page.canSwitch(), false)
  page.reviewDirty = false; page.reviewBusy = true
  await page.upload(); await page.submit(); assert.equal(page.canSwitch(), false)
  let prevented = false
  page.beforeUnload({ preventDefault() { prevented = true }, returnValue: '' })
  assert.equal(prevented, true)
})

test('实际配音页审查草稿阻止依赖运行与直接运行；成片页展示同一入口', async t => {
  const globals = ['requestAnimationFrame', 'cancelAnimationFrame', 'window'] as const
  const original = new Map(globals.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', { configurable: true, value: () => {} })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { requestAnimationFrame: () => 0, cancelAnimationFrame: () => {}, setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {} } })
  t.after(() => { for (const key of globals) { const descriptor = original.get(key); if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key) } })
  const View = (await server.ssrLoadModule('/src/views/StepView.vue')).default
  const { useNavStore } = await server.ssrLoadModule('/src/stores/nav.ts')
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', (_url, options) => { if (options?.method === 'POST' || options?.method === 'PUT') calls.push(options); return new Promise(() => {}) })
  for (const key of ['tts', 'assemble']) {
    const pinia = createPinia(), nav = useNavStore(pinia)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/projects/:uid/steps/:key', component: View }] })
    await router.push(`/projects/fixture/steps/${key}`)
    nav.$patch({ uid: 'fixture', stepsLoaded: true, steps: ['download', 'preprocess', 'transcribe', 'split', 'translate', 'subtitles', 'tts', 'assemble'].map(name => ({ key: name, label: name, status: 'pending', fresh: false, selected: true, cfg: {}, artifacts: [], unready_inputs: [] })) })
    let page: any
    const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(RouterView) }) }) })
    app.use(pinia); app.use(router); setup(app)
    app.mixin({ created() { if (this.$options.__name === 'StepView') { page = this.$.setupState; page.reviewDirty = true } } })
    try {
      const html = await renderToString(app)
      assert.match(html, /发布前内容审查/); assert.equal(page.canRun, false)
      await page.run(); await page.runWithDependencies(); assert.equal(calls.length, 0)
      page.reviewDirty = false; page.reviewBusy = true; assert.equal(page.canRun, false)
      await page.run(); await page.runWithDependencies(); assert.equal(calls.length, 0)
    } finally { nav.reset(); disposePinia(pinia) }
  }
})
