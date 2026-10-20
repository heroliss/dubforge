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
import { candidateReview, findingEffect, normalizeContentReview, reviewRevisionDetails, reviewTerms, reviewTime } from '../src/utils/contentReview.ts'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/ContentReviewPanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('不允许未声明请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
function fixture(extra: Record<string, any> = {}) {
  return {
    enabled: true, mute_audio: true, audio_mode: 'voice_only', blocked_terms: ['测试词'], blocked_ranges: '',
    revision: 3, configuration_fingerprint: 'fixture-fingerprint', decision_revision: '0', inherited: false, origin: '本项目覆盖',
    rule_version: 'fixture-rules-1', checked_at: '2026-10-08T08:00:00+00:00',
    coverage: { original: 15, source: 12, translated: 10, comments: 4, complete_text: false },
    effective_ranges: [{ start: 59.5, end: 62.125 }], effective_audio_ranges: [{ start: 59.35, end: 63.5 }], blocked_ids: [8], stale_decision_count: 0,
    findings: [{ key: 'current:8', evidence_fingerprint: 'evidence-8', decision: 'pending', active: true,
      ids: [8], start: 59.5, end: 62.125, reason: '命中自定义词', source_text: 'original', text: '译文测试词', rule: 'custom', fields: ['text'], context_ids: [8],
      context_rows: [{ id: 7, start: 57, end: 59.3, source_text: 'preceding context', text: '前文' }, { id: 8, start: 59.5, end: 62.125, source_text: 'original', text: '译文测试词' }, { id: 9, start: 63, end: 65, source_text: 'following context', text: '后文' }] }],
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
  assert.equal(candidateReview({review_ready:true,review_status:'字幕已处理·声音未静音'})?.label,'字幕已处理·声音未静音')
  assert.equal(candidateReview({review_ready:true,review_status:' '})?.label,'已按当前规则处理')
})

test('修订失效摘要优先具体原因，变化详情格式化原有与当前时段', () => {
  const value = { review_ready: false, review_reason: '旧通用原因', review_revision_reason: '人工屏蔽时段已变化',
    review_revision_reasons: [{ code: 'review_config_changed', message: '人工屏蔽时段已变化',
      changes: [{ label: '人工屏蔽时段', before: [{ start: 96.48, end: 133 }], after: [] }] }] }
  assert.equal(candidateReview(value)?.reason, '人工屏蔽时段已变化')
  assert.deepEqual(reviewRevisionDetails(value)[0].changes, [{ label: '人工屏蔽时段', before: '1:36.480–2:13.000', after: '无' }])
  assert.equal(candidateReview({ ...value, review_revision_reason: undefined })?.reason, '人工屏蔽时段已变化')
  assert.deepEqual(reviewRevisionDetails({ review_revision_reasons: [null, { message: '', code: 'empty' }] } as any), [])
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
  for (const value of ['内容审查与屏蔽', '字幕导出时隐藏命中句', '合成成片时处理命中时段的声音', '0:59.500', '1:02.125', '句 8', '译文测试词', '评论 42', 'comment source', '评论译文', '原声识别稿命中明细', '原声命中内容', '原声规则', '尚未提供全部译文', '人工屏蔽时段', '原视频时间轴', '仅屏蔽语音，保留背景', '整段静音（含背景）', '背景文件缺失或不可用', '重新检查文字不调用模型，也不生成字幕文件或成片', '恢复继承', '不能保证审核通过', '已有成片、稿件已选文件和已上传文件不会自动替换']) assert.ok(html.includes(value), value)
  assert.match(html, /&lt;script&gt;原因/); assert.match(html, /&lt;img src=x&gt;/)
  assert.doesNotMatch(html, /<script>原因|<img src=x>/)
})
test('关闭审查与关闭静音如实说明，零命中不声称审核通过', async () => {
  const { html } = await render(p => p.apply(normalizeContentReview(fixture({ enabled: false, mute_audio: false, findings: [], comment_findings: [], stale_decision_count: 3, summary: { lines: 12, blocked_lines: 0, blocked_comments: 0 } }))))
  assert.match(html, /自动屏蔽已关闭/)
  assert.match(html, /关闭静音仍可能保留原片或配音中的相关声音/)
  assert.match(html, /本次未执行文本筛查/)
  assert.match(html, /检查时间：未检查/)
  assert.doesNotMatch(html, /已检查|未命中已配置的文本规则|本次检查|原判断不再应用|已通过审核|安全可发布/)
})

test('声音模式默认屏蔽语音，整段静音报告与关闭开关分别说明', async () => {
  const voice = normalizeContentReview(fixture({ audio_mode: undefined }))
  assert.equal(voice.audio_mode, 'voice_only')
  assert.match(findingEffect(voice.findings[0], voice), /屏蔽命中语音；分离背景不可用时整段静音/)
  const all = normalizeContentReview(fixture({ audio_mode: 'all_audio' }))
  assert.match(findingEffect(all.findings[0], all), /整段静音（含背景）/)
  assert.doesNotMatch(findingEffect(all.findings[0], all), /屏蔽命中语音/)
  const { html, page } = await render(p => p.apply(all))
  assert.match(html, /字幕隐藏 · 命中时段整段静音（含背景）/)
  page.muteAudio = false
  assert.equal(page.audioMode, 'all_audio')
  assert.equal(page.draft.audio_mode, 'all_audio')
  page.discard(); assert.equal(page.muteAudio, true); assert.equal(page.audioMode, 'all_audio')
  const off = normalizeContentReview(fixture({ mute_audio: false, audio_mode: 'all_audio' }))
  assert.match(findingEffect(off.findings[0], off), /声音未静音/)
  assert.deepEqual(off.effective_audio_ranges, [])
})

test('仅修改声音模式也可保存，并保留版本凭据', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.audioMode = 'all_audio'; assert.equal(page.dirty, true)
  let body: any
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    body = JSON.parse(String(options!.body))
    return { ok: true, json: async () => fixture({ audio_mode: 'all_audio', revision: 4 }) }
  })
  await page.save()
  assert.equal(body.audio_mode, 'all_audio'); assert.equal(body.mute_audio, true)
  assert.equal(body.revision, 3); assert.equal(body.configuration_fingerprint, 'fixture-fingerprint')
  assert.equal(page.report.audio_mode, 'all_audio'); assert.equal(page.dirty, false)
})

test('报告按实际处理模式显示兜底，内部背景指纹不展示或保存为设置', async () => {
  const { page, html } = await render(p => p.apply(normalizeContentReview(fixture({
    audio_processing: { requested_mode: 'voice_only', effective_mode: 'all_audio',
      fallback_reason: '分离背景不可用，本次命中时段整段静音', background_identity: { path: '内部背景路径' }, bgm_volume: .2 },
  }))))
  assert.match(html, /字幕隐藏 · 命中时段整段静音（含背景）/)
  assert.match(html, /声音处理兜底：分离背景不可用，本次命中时段整段静音/)
  assert.doesNotMatch(html, /内部背景路径|background_identity|bgm_volume/)
  assert.equal(page.draft.audio_mode, 'voice_only')
  assert.equal('audio_processing' in page.draft, false)
  assert.match(findingEffect(page.report.findings[0], page.report), /整段静音/)
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
  assert.match(page.notice, /已重新检查当前文字；字幕文件和成片尚未重新生成/)
})
test('保存包含人工时段及并发凭证，成功报告替换草稿且仅发送一次', async t => {
  const { page, events } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.enabled = false; page.muteAudio = false; page.terms = ' 新词 \n新词\n'; page.ranges = '60.5-72:人工核对'
  const calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([String(url), options]); return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => fixture({ enabled: false, mute_audio: false, blocked_terms: ['新词'], blocked_ranges: '60.5-72:人工核对', revision: 4 }) }) }) })
  const first = page.save(); await page.save()
  assert.equal(calls.length, 1); assert.equal(page.operation, 'save')
  assert.deepEqual(JSON.parse(calls[0][1].body), { enabled: false, mute_audio: false, audio_mode: 'voice_only', blocked_terms: ['新词'], blocked_ranges: '60.5-72:人工核对', revision: 3, configuration_fingerprint: 'fixture-fingerprint' })
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
  page.terms = '正在编辑\n自定义词'; page.ranges = '60-70:人工核对'; page.audioMode = 'all_audio'
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(options)
    if (calls.length === 1) return { ok: false, status: 409, json: async () => ({ code: 'CONTENT_REVIEW_CONFLICT', message: '请读取最新设置' }) }
    return { ok: true, json: async () => fixture({ revision: 4, configuration_fingerprint: 'latest', blocked_terms: calls.length === 2 ? ['外部修改'] : ['正在编辑', '自定义词'], blocked_ranges: calls.length === 2 ? '' : '60-70:人工核对' }) }
  })
  await page.save(); assert.equal(page.conflict, true)
  await page.rebase(); assert.equal(calls[1].method, 'GET')
  assert.equal(page.report.revision, 4); assert.equal(page.terms, '正在编辑\n自定义词'); assert.equal(page.ranges, '60-70:人工核对'); assert.equal(page.dirty, true)
  assert.equal(page.audioMode, 'all_audio')
  await page.save(); assert.equal(JSON.parse(calls[2].body).configuration_fingerprint, 'latest'); assert.equal(page.dirty, false)
  assert.equal(JSON.parse(calls[2].body).audio_mode, 'all_audio')
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

test('检查覆盖分开显示原声、当前原文、译文、评论与规则版本；零覆盖不冒充检查完成', async () => {
  const covered = await render(p => p.apply(normalizeContentReview(fixture())))
  for (const value of ['原声识别稿', '15 句', '当前整理原文', '12 句', '当前译文', '10 句', '4 条', 'fixture-rules-1', '本次检查', '0:59.350', '1:03.500']) assert.ok(covered.html.includes(value), value)
  const empty = await render(p => p.apply(normalizeContentReview(fixture({ findings: [], original_findings: [], comment_findings: [],
    coverage: { original: 0, source: 0, translated: 0, comments: 0 }, summary: { lines: 0, blocked_lines: 0, blocked_comments: 0 }, effective_ranges: [] }))))
  assert.match(empty.html, /无可检查文字/); assert.match(empty.html, /尚无可检查的整理稿/)
  assert.doesNotMatch(empty.html, /已检查 0|未命中已配置的文本规则|已通过审核/)
})

test('完整附近上下文、人工标记与真实命中文字证据可见；允许后仍被其他范围覆盖如实提示', async () => {
  const data = fixture({ stale_decision_count: 2, findings: [{ ...fixture().findings[0], decision: 'allow', active: false, decision_reason: '<b>正常介绍</b>' }],
    comment_findings: [{ key: 'comment:c1', ids: ['c1'], evidence_fingerprint: 'comment-1', decision: 'block', active: true,
      source_text: 'neutral source', text: '普通介绍', reason: '真实原文命中', evidence_texts: ['<script>真实命中文字</script>'] }] })
  const { html } = await render(p => p.apply(normalizeContentReview(data)))
  for (const value of ['待复核', '已确认屏蔽', '已允许此项', '确认屏蔽', '误报，允许此项', '恢复自动判断', '前文', '后文', '参与命中', '其他命中或人工时段仍覆盖此段', '原判断不再应用', '命中文字证据', '&lt;script&gt;真实命中文字', '&lt;b&gt;正常介绍']) assert.ok(html.includes(value), value)
  assert.doesNotMatch(html, /<script>真实命中文字|<b>正常介绍/)
  const parsed = normalizeContentReview(data)
  assert.match(findingEffect(parsed.findings[0], parsed), /其他命中/)
  const original = { ...parsed.findings[0], key: 'original:8', start: 100, end: 102 }
  assert.doesNotMatch(findingEffect(original, parsed), /仍覆盖/)
})

test('逐条判断携带证据与两个并发版本，防止双击，并保留未保存配置草稿', async t => {
  const { page, events } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.terms = '正在编辑词'; page.ranges = '100-102:配置草稿'; page.reasons = { 'current:8': ' 正常旅行语境 ' }
  const old = page.report.findings[0], calls: any[] = []; let finish: any
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push([String(url), options]); return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => fixture({ decision_revision: '1', findings: [{ ...fixture().findings[0], decision: 'allow', active: false }] }) }) }) })
  const first = page.decide(old, 'allow'); await page.decide(old, 'allow')
  assert.equal(calls.length, 1); assert.equal(page.operation, 'decision')
  assert.equal(page.mutating, true)
  assert.equal(calls[0][0], '/api/projects/fixture/content-review/decisions')
  assert.deepEqual(JSON.parse(calls[0][1].body), { key: 'current:8', decision: 'allow', evidence_fingerprint: 'evidence-8', decision_revision: '0', revision: 3, configuration_fingerprint: 'fixture-fingerprint', reason: '正常旅行语境' })
  finish(); await first
  assert.equal(page.report.findings[0].decision, 'allow'); assert.equal(page.report.decision_revision, '1')
  assert.equal(page.terms, '正在编辑词'); assert.equal(page.ranges, '100-102:配置草稿'); assert.equal(page.dirty, true)
  assert.equal(events.filter(event => event[0] === 'saved').length, 1)
  assert.equal(page.mutating, false)
})

test('复核冲突不应用旧证据、不自动重试，显式重读保留草稿后才使用新证据判断', async t => {
  const { page, events } = await render(p => p.apply(normalizeContentReview(fixture())))
  page.terms = '配置草稿'
  const old = page.report.findings[0], calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(options)
    if (calls.length === 1) return { ok: false, status: 409, json: async () => ({ code: 'CONTENT_REVIEW_DECISION_CONFLICT', message: '文字已变化，请刷新重新核对' }) }
    return { ok: true, json: async () => fixture({ decision_revision: '7', findings: [{ ...fixture().findings[0], evidence_fingerprint: 'new-evidence' }] }) }
  })
  await page.decide(old, 'allow'); await page.decide(old, 'block')
  assert.equal(calls.length, 1); assert.equal(page.conflict, true); assert.equal(page.report.findings[0].evidence_fingerprint, 'evidence-8')
  assert.equal(page.terms, '配置草稿'); assert.equal(events.filter(event => event[0] === 'saved').length, 0)
  await page.rebase(); assert.equal(calls[1].method, 'GET'); assert.equal(page.conflict, false); assert.equal(page.terms, '配置草稿')
  await page.decide(old, 'allow'); assert.equal(calls.length, 2); assert.equal(page.conflict, true)
  await page.rebase(); await page.decide(page.report.findings[0], 'reset')
  assert.equal(JSON.parse(calls[3].body).decision, 'reset'); assert.equal(JSON.parse(calls[3].body).decision_revision, '7')
  assert.equal(JSON.parse(calls[3].body).evidence_fingerprint, 'new-evidence')
})

test('旧接口无人工版本不可判断，配置未保存不打开片段；重读报告会收起旧预览', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture({ decision_revision: undefined }))))
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return { ok: true, json: async () => fixture() } })
  await page.decide(page.report.findings[0], 'allow'); assert.equal(calls, 0)
  page.apply(normalizeContentReview(fixture())); page.selectPreview(page.report.findings[0]); assert.ok(page.selected)
  page.terms = '修改'; page.selected = null
  page.selectPreview(page.report.findings[0]); assert.equal(page.selected, null)
  page.discard(); page.selectPreview(page.report.findings[0]); page.apply(normalizeContentReview(fixture({ decision_revision: '2' })))
  assert.equal(page.selected, null)
})

test('纯确认屏蔽不要求重新生成；允许后实际范围变化才提示重生成，仍重叠则提示仍屏蔽', async t => {
  const { page } = await render(p => p.apply(normalizeContentReview(fixture())))
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => fixture({ decision_revision: '1', findings: [{ ...fixture().findings[0], decision: 'block', active: true }] }) }))
  await page.decide(page.report.findings[0], 'block')
  assert.match(page.notice, /当前处理效果未变化/); assert.doesNotMatch(page.notice, /重新生成/)
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => fixture({ decision_revision: '2', findings: [{ ...fixture().findings[0], decision: 'allow', active: false }] }) }))
  await page.decide(page.report.findings[0], 'allow')
  assert.match(page.notice, /当前处理效果未变化/); assert.match(page.notice, /仍覆盖/); assert.doesNotMatch(page.notice, /重新生成/)
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => fixture({ decision_revision: '3', findings: [{ ...fixture().findings[0], decision: 'allow', active: false }], effective_ranges: [], effective_audio_ranges: [], blocked_ids: [] }) }))
  await page.decide(page.report.findings[0], 'reset')
  assert.match(page.notice, /处理结果已变化/); assert.match(page.notice, /重新生成受影响/)
})

test('报告阻断错误显式可见，不能以空命中假报未命中，也不能执行人工判断', async t => {
  const { page, html } = await render(p => p.apply(normalizeContentReview(fixture({ findings: [], blocker: { code: 'CONTENT_REVIEW_TIMELINE', message: '字幕时间顺序无效' } }))))
  assert.match(html, /当前文字无法完整核对：字幕时间顺序无效/)
  assert.doesNotMatch(html, /当前整理稿未命中/)
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return { ok: true, json: async () => fixture() } })
  await page.decide(fixture().findings[0], 'allow'); assert.equal(calls, 0)
})

test('命中字段按整理稿、原始识别稿和评论解释；识别稿空译文不标成缺翻译', async () => {
  const { html } = await render(p => p.apply(normalizeContentReview(fixture({
    findings: [{ ...fixture().findings[0], fields: ['source_text', 'text', 'source_audio'] }],
    original_findings: [{ key: 'original:8', evidence_fingerprint: 'raw-8', ids: [8], start: 59, end: 62, source_text: '原始识别句', text: '', fields: ['source_text'], reason: '识别稿命中' }],
    comment_findings: [{ key: 'comment:c1', evidence_fingerprint: 'c1', ids: ['c1'], source_text: '评论内容', text: '上屏译文', fields: ['text', 'source_text', 'text_zh', 'display_text', 'original', 'review_evidence'], reason: '评论命中' }],
  }))))
  assert.match(html, /命中位置：整理原文、译文、原声文字/)
  assert.match(html, /命中位置：原始识别稿/)
  assert.match(html, /命中位置：评论原文、上屏原文、评论译文、上屏文字、采集原文、保存的命中文字/)
  const original = html.slice(html.indexOf('原声识别稿命中明细'), html.indexOf('评论命中明细'))
  assert.match(original, /原始识别句/)
  assert.doesNotMatch(original, /尚无译文|<b[^>]*>译文<\/b>/)
})
test('配音、成片和稿件页接入同一审查面板，未保存设置门禁覆盖正式生成与上传', () => {
  const step = readFileSync(new URL('../src/views/StepView.vue', import.meta.url), 'utf8')
  const publish = readFileSync(new URL('../src/views/PublishView.vue', import.meta.url), 'utf8')
  assert.equal((step.match(/<ContentReviewPanel\b/g)||[]).length,2,'配音与成片各有一个实际审查消费者')
  assert.match(step, /<details[^>]*v-if="key\s*===\s*'assemble'"[^>]*class="assembly-review-context"[\s\S]*?<ContentReviewPanel[^>]*@dirty-change="reviewDirty=\$event"[^>]*@busy-change="reviewBusy=\$event"/)
  assert.match(step, /<ContentReviewPanel v-else-if="key === 'tts'"[^>]*@dirty-change="reviewDirty = \$event"[^>]*@busy-change="reviewBusy = \$event"/)
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
  assert.match(html, /内容审查与屏蔽/)
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
      assert.match(html, /内容审查与屏蔽/); assert.equal(page.canRun, false)
      await page.run(); await page.runWithDependencies(); assert.equal(calls.length, 0)
      page.reviewDirty = false; page.reviewBusy = true; assert.equal(page.canRun, false)
      await page.run(); await page.runWithDependencies(); assert.equal(calls.length, 0)
    } finally { nav.reset(); disposePinia(pinia) }
  }
})
