import assert from 'node:assert/strict'
import test, { before, after, afterEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, component: any, navModule: any, execution: any
const animationProperties = ['window', 'requestAnimationFrame', 'cancelAnimationFrame']
  .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  component = (await server.ssrLoadModule('/src/components/DubbingPanel.vue')).default
  navModule = await server.ssrLoadModule('/src/stores/nav.ts')
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明网络请求') })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', { configurable: true, value: () => {} })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  } })
})
afterEach(() => execution.setNavigationExecution('', false, true))
after(async () => {
  mock.restoreAll()
  for (const [key, descriptor] of animationProperties) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
  await server.close()
})

function fixture(total = 249, synthesized = 52) {
  const lines = Array.from({ length: total }, (_, index) => {
    const id = index + 1, exists = id <= synthesized, take = exists ? 1 : null
    const wav = { id: take, exists, stale: false, confirmed: exists, url: null, seconds: exists ? 1 : null }
    return { id, start: (id - 1) * 3, end: (id - 1) * 3 + 2, duration: 2, est_dur: 1,
      source_text: `Sentence ${id}`, text: `译文 ${id}`, translation_available: true,
      synthesis_revision: `revision-${id}`,
      edit_stale: false, unready_inputs: [], customized: false, custom_params: [], confirmed: exists,
      selection: { source: 'dub', take }, wav, versions: exists ? [{ ...wav }] : [] }
  })
  return { lines, counts: { total, synthesized, confirmed: synthesized },
    synthesis_slots: synthesized ? [1] : [],
    synthesis_reserved_takes: {} as Record<string, number[]>,
    mix: { voice: 1, background: .3 },
    synthesis_task: null, active_line: null, video: { found: false, url: null },
    bg: { found: false }, vocal: { found: false }, original: { found: false },
    edit_blockers: [], orphaned_edits: [], line_settings_supported: false, subtitles: {} }
}

async function render(data = fixture(), seed: (page: any) => void = () => {}, hash = '') {
  data.synthesis_slots = [...new Set(data.lines.flatMap((line: any) => [line.selection.take, ...line.versions.map((version: any) => version.id)]).filter((value: any) => value != null))].sort((a, b) => Number(a) - Number(b))
  const pinia = createPinia(), nav = navModule.useNavStore(pinia), dialogs: any[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/projects/:uid/steps/:key',
    component: defineComponent({ render: () => h(component, { uid: 'fixture' }) }) }] })
  await router.push('/projects/fixture/steps/tts' + hash)
  let page: any
  const Probe = defineComponent({ setup() {
    const dialog = useDialog()
    dialog.warning = dialog.info = ((options: any) => { dialogs.push(options); return Object.assign(options, { destroy() {} }) }) as any
    return () => h(RouterView)
  } })
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Probe))) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'DubbingPanel') { page = this.$.setupState; page.serverState = data; seed(page) }
  } })
  const html = await renderToString(app)
  return { page, html, dialogs, nav, cleanup: async () => {
    page.loader.cancel()
    await page.mixEdits.dispose(); page.selectionEdits.dispose(); page.stopAll()
    nav.reset(); disposePinia(pinia)
  } }
}

test('时间排布状态与单句播放范围清晰披露，不再声称固定源起点', async () => {
  const data: any = fixture(2, 2)
  data.timing = { enabled: true, shifted: 2, unresolved: 1 }
  Object.assign(data.lines[0], { start: 1, end: 2, playback_start: .5, playback_end: 2.5 })
  const result = await render(data, page => { page.sel = 1 })
  try {
    const text = result.html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /自适应排布 · 已移动 2 句/)
    assert.match(text, /1 句暂未容纳/)
    assert.match(text, /位置 提前 0.50 秒 自动/)
    assert.match(text, /原文、译文与配音同步/)
    assert.match(text, /音量已自动保存到项目；用于后续配音成片，版本单独设置优先/)
    assert.doesNotMatch(text, /试听与成片均保持原起点/)
    assert.match(result.page.placementHint(data.lines[0]), /位置来源：自动排布 · 原文、译文与配音同步/)
  } finally { await result.cleanup() }
})

test('策略配音排队尚无逐句范围时，200工作台内容正常显示且不误报读取失败', async t => {
  const result = await render(fixture(2, 0)), data: any = fixture(2, 0)
  data.synthesis_task = { scope: 'policy', job_id: 518, active: true, queued: true, running: false, completed: 0, failed: 0, total: 0 }
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, status: 200, json: async () => data }) as any)
  try {
    await result.page.load()
    assert.equal(result.page.loadError, '')
    assert.equal(result.page.state.lines.length, 2)
    assert.equal(result.page.task.job_id, 518); assert.equal(result.page.task.range_prepared, false)
    assert.deepEqual(result.page.task.lines, []); assert.deepEqual(result.page.checked, [])
    assert.equal(result.page.runDisabled, true, '排队期间不能再生成一轮')
  } finally { await result.cleanup() }
})

test('新配音Job不显示旧工作台的范围、活动句或失败精调，原历史内容保留', async () => {
  const data: any = fixture(2, 0)
  data.synthesis_task = { job_id: 17, scope: 'selected', lines: [{ id: 1, status: 'failed', error: '上一轮句子失败' }], total: 1, failed: 1 }
  data.active_line = 1; data.active_progress = .9
  data.line_config_execution = { job_id: 17, active: true, generation_accepting: true }
  data.lines[0].config_execution = { job_id: 17, state: 'failed', kind: 'settings' }
  const result = await render(data)
  try {
    result.nav.$patch({ uid: 'fixture', stepsLoaded: true, steps: [{ key: 'tts', status: 'queued', job_id: 18, cfg: {} }] })
    assert.equal(result.page.task, null); assert.deepEqual(result.page.errors, {})
    assert.equal(result.page.activeSynthesisLine, null); assert.equal(result.page.isSynthesizing(result.page.state.lines[0]), false)
    assert.equal(result.page.state.line_config_execution, null)
    assert.equal(result.page.configUpdateLabel(result.page.state.lines[0]), '')
    assert.equal(result.page.canAppendSynthesis, false)
    assert.equal(result.page.serverState.synthesis_task.job_id, 17)
    assert.equal(result.page.serverState.active_line, 1)
    assert.equal(result.page.serverState.lines[0].config_execution.state, 'failed')
    result.nav.steps[0].job_id = 17
    assert.equal(result.page.task.failed, 1); assert.equal(result.page.errors[1], '上一轮句子失败')
  } finally { await result.cleanup() }
})

test('工作台真实HTTP错误保留具体原因与已有内容，损坏数据不被当成正常排队', async t => {
  const result = await render(fixture(2, 0))
  let response: any = { ok: false, status: 500, json: async () => ({ code: 'WORKBENCH_READ_FAILED', message: '配音文稿校验失败，请检查译文。' }) }
  t.mock.method(globalThis, 'fetch', async () => response)
  try {
    await result.page.load()
    assert.equal(result.page.loadError, '配音文稿校验失败，请检查译文。')
    assert.equal(result.page.state.lines.length, 2)
    response = { ok: true, status: 200, json: async () => ({ ...fixture(2, 0), synthesis_task: { lines: 'bad' } }) }
    await result.page.load()
    assert.match(result.page.loadError, /配音任务范围返回异常/)
    assert.equal(result.page.state.lines.length, 2)
  } finally { await result.cleanup() }
})

test('句子列表用中性自定义朗读标记，不在参数摘要重复全文或声称声音已更新', async () => {
  const data: any = fixture(1, 1)
  Object.assign(data.lines[0], { text: '还有“没有朝”。', customized: true,
    custom_params: [{ path: 'tts.indextts25.pronunciation_text', label: '朗读文本', value: '还有“没有<朝|CHAO2>”。' }] })
  const result = await render(data)
  try {
    assert.match(result.html, /class="pronunciation-label"/)
    assert.match(result.html, /自定义朗读/)
    assert.match(result.html, /再次合成时使用，字幕不变/)
    assert.match(result.html, /还有“没有朝”。/)
    assert.equal(result.page.paramSummary(data.lines[0]), '')
    assert.equal(result.page.lineSettingsSummary(data.lines[0]), '已保存自定义朗读')
    assert.equal(data.lines[0].text, '还有“没有朝”。')
  } finally { await result.cleanup() }
})

test('句子参数摘要使用可读参考来源和句子编号，避免暴露stable枚举及裸编号', async () => {
  const data: any = fixture(1, 1)
  Object.assign(data.lines[0], { customized: true, custom_params: [
    { path: 'tts.indextts25.timbre_mode', label: '音色参考来源', value: 'stable' },
    { path: 'tts.indextts25.timbre_reference_line_id', label: '音色参考句', value: 2 },
    { path: 'tts.indextts25.emo_mode', label: '情感来源', value: 'stable' },
    { path: 'tts.indextts25.emo_reference_line_id', label: '情感参考句', value: 3 },
  ] })
  const result = await render(data)
  try {
    const summary = '音色参考来源：指定句原声 · 音色参考句：第 2 句 · 情感来源：指定句原声 · 情感参考句：第 3 句'
    assert.equal(result.page.paramSummary(data.lines[0]), summary)
    assert.ok(result.html.includes(summary))
    assert.doesNotMatch(result.page.lineSettingsSummary(data.lines[0]), /stable/)
  } finally { await result.cleanup() }
})

test('暂停时切到跟随预览也立即按当前视频时间选句，而不是等下一句播放', async () => {
  const data: any = fixture(3, 3)
  const result = await render(data, page => {
    page.sel = 3; page.time = 3.5; page.followMode = 'preview'
  })
  try {
    assert.equal(result.page.cursor, null, '播放器暂停时没有活动播放标记')
    assert.equal(result.page.previewLine, 2, '按当前视频位置解析当前句')
    assert.equal(result.page.sel, 2, '模式变化立即选中当前句，不等待播放换句')
  } finally { await result.cleanup() }
})

test('列表逐句显示实际播放范围、自动和人工偏移、采用缩时因子及重试次数', async () => {
  const data: any = fixture(3, 3)
  Object.assign(data.lines[0], { start: 1, end: 2, playback_start: .5, playback_end: 2.5,
    timing: { automatic_start: .75, automatic_end: 2.75, manual_start: .5, manual_active: true,
      effective_mode: 'manual', timing_revision: 'manual-revision' } })
  data.lines[0].wav.duration_retry = { attempts: [{ factor: 1 }, { factor: .85 }, { factor: .7 }], used_factor: .85, retries: 2 }
  Object.assign(data.lines[1], { start: 3, end: 4, playback_start: 3.5, playback_end: 4.5 })
  const result = await render(data)
  try {
    const text = result.html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /位置 提前 0.50 秒 人工/); assert.match(text, /位置 延后 0.50 秒 自动/)
    assert.match(text, /时长因子 1 → 0.85 重试 2 次/)
    assert.match(result.html, /00:00.5 – 00:02.5/)
    assert.match(result.page.lineTimeTitle(data.lines[0]), /原始 00:01.000 — 00:02.000/)
    assert.match(result.page.lineTimeTitle(data.lines[0]), /自动 00:00.750 — 00:02.750/)
    assert.match(result.page.lineTimeTitle(data.lines[0]), /最终 00:00.500 — 00:02.500/)
  } finally { await result.cleanup() }
})

test('关闭自动排布不会误称所有句子回到源时间，人工位置和冲突状态仍披露', async () => {
  const data: any = fixture(2, 2)
  data.timing = { enabled: false, shifted: 0, manual: 1, unresolved: 1, unresolved_ids: [1] }
  const result = await render(data)
  try {
    const text = result.html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /自动排布已关闭 · 保留 1 句人工位置，其他句子按源时间播放/)
    assert.match(text, /仍有 1 句放不下，请检查人工位置或重新合成/)
  } finally { await result.cleanup() }
})

test('排布后的缩时阶段在活动进度和单句状态中统一显示，完成后恢复音频状态', async () => {
  const data: any = fixture(2, 2)
  data.active_line = 1; data.active_progress = .4
  data.synthesis_task = { job_id: 'timing-repair', running: true, active: true, lines: [
    { id: 1, status: 'running', timing_phase: 'shorten', duration_factor: .7 },
  ] }
  const result = await render(data, page => { page.sel = 1 })
  try {
    const text = result.html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /第 1 句 · 排布后缩短重试 · 因子 0.7/)
    assert.match(result.page.label(data.lines[0]), /^排布后缩短重试 · 因子 0.7/)
    assert.equal(result.page.label(data.lines[0]), result.page.synthesisStatus(1))
    const previous = result.page.synthesisProgressKey
    result.page.serverState.synthesis_task.lines[0].duration_factor = .5
    assert.notEqual(result.page.synthesisProgressKey, previous, '下一缩时尝试不沿用上一尝试的平滑进度')
    result.page.serverState.active_line = null
    result.page.serverState.synthesis_task = { active: false, running: false, lines: [
      { id: 1, status: 'done', timing_phase: 'shorten', duration_factor: .5 },
    ] }
    assert.equal(result.page.label(data.lines[0]), '已满意')
  } finally { await result.cleanup() }
})

async function confirmationHtml(options: any) {
  const content = typeof options.content === 'function' ? options.content() : options.content
  if (typeof content === 'string') return content
  return renderToString(createSSRApp({ render: () => content }))
}

function mutations(calls: { url: string; options: any }[]) {
  return calls.filter(call => call.options?.method === 'POST' && call.url.endsWith('/workbench/synthesize'))
}

test('统一项目参数生成的槽保留来源说明，不误称为之前参数的旧版本', async () => {
  const data = fixture(2, 2)
  Object.assign(data.lines[0].wav, { line_settings_mode: 'project', settings_changed: true })
  const result = await render(data, page => { page.sel = 1 })
  try {
    assert.match(result.html, /此槽按统一项目参数生成/)
    assert.match(result.html, /本句精调设置仍保留/)
    assert.match(result.html, /当前修改尚未应用到配音/)
    assert.doesNotMatch(result.html, /此版本使用之前的参数生成/)
    assert.doesNotMatch(result.html, /当前有效参数与该音频不同/)
  } finally { await result.cleanup() }
})

test('当前槽参数未应用在生成按钮前明确提醒，列表保留满意状态并增加提示标签', async () => {
  const data: any = fixture(2, 2)
  data.lines[0].wav.settings_changed = true
  const result = await render(data, page => { page.sel = 1 })
  try {
    const html = result.html, text = html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(html, /role="status"[^>]*aria-label="当前修改尚未应用到配音"|aria-label="当前修改尚未应用到配音"[^>]*role="status"/)
    assert.match(html, /class="voice-settings-notice"/)
    assert.match(text, /当前修改尚未应用到配音/)
    assert.match(text, /当前试听和成片仍使用配音槽 1 中的已有声音/)
    assert.match(text, /重新生成/)
    assert.ok(html.indexOf('当前修改尚未应用到配音') < html.indexOf('生成本句配音'),
      '应在用户操作前看到当前音频与已保存参数的差异')
    assert.match(html, /class="status warning settings-unapplied"[^>]*>配音待更新<\/span>/)
    assert.equal(result.page.label(data.lines[0]), '已满意')
    assert.equal(result.page.statusTone(data.lines[0]), 'confirmed')
    assert.doesNotMatch(text, /此版本使用之前的参数生成/)
  } finally { await result.cleanup() }
})

test('字幕文字修改导致旧声音失效时也进入配音待更新，并说明旧音频不再用于试听或成片',async()=>{
  const data:any=fixture(2,2),line=data.lines[0]
  Object.assign(line.wav,{file_exists:true,exists:false,stale:true,changes_pending:true,
    change_reasons:['朗读文字已修改'],settings_changed:false,confirmed:false})
  line.confirmed=false;line.versions[0]={...line.wav}
  const result=await render(data,page=>{page.sel=1;page.stateFilter='settings'})
  try{
    assert.deepEqual(result.page.shownLines.map((line:any)=>line.id),[1])
    assert.equal(result.page.filters.find((f:any)=>f.value==='settings').count,1)
    assert.match(result.html,/配音待更新/);assert.match(result.html,/朗读文字已修改/)
    assert.match(result.html,/暂不参与试听和成片/)
    assert.doesNotMatch(result.html,/class="silence-detail"/,'待更新原因与旧音频不可用说明集中展示，不重复警告')
    assert.doesNotMatch(result.html,/当前试听和成片仍使用配音槽/)
    assert.equal(result.page.label(line),'配音过期 · 预览无声')
  }finally{await result.cleanup()}
})

test('自定义朗读独立于字幕时给出解释，不误报配音待更新',async()=>{
  const data:any=fixture(1,1)
  Object.assign(data.lines[0].wav,{file_exists:true,changes_pending:false,change_reasons:[],reading_caption_independent:true})
  const result=await render(data,page=>{page.sel=1})
  try{
    assert.match(result.html,/单独修改字幕不会改变朗读内容/)
    assert.doesNotMatch(result.html,/aria-label="当前修改尚未应用到配音"/)
    assert.equal(result.page.filters.find((f:any)=>f.value==='settings').count,0)
  }finally{await result.cleanup()}
})

test('配音待更新筛选只包含当前有效配音槽，保留搜索与隐藏勾选', async () => {
  const data: any = fixture(7, 7)
  for (const line of data.lines.slice(0, 6)) line.wav.settings_changed = true
  data.lines[1].selection.source = 'original'
  data.lines[2].selection.take = null
  data.lines[3].wav.stale = true
  data.lines[3].wav.exists = false
  data.lines[4].wav.exists = false
  data.lines[5].selection.take = 0
  const result = await render(data, page => {
    page.stateFilter = 'settings'; page.checked = [1, 7]
  })
  try {
    assert.deepEqual(result.page.shownLines.map((line: any) => line.id), [1])
    assert.deepEqual(result.page.filters.find((item: any) => item.value === 'settings'),
      { value: 'settings', label: '配音待更新', count: 1 })
    assert.match(result.html, /配音待更新/)
    assert.match(result.html, /含隐藏 1 句/)
    result.page.filter = 'Sentence 1'
    assert.deepEqual(result.page.shownLines.map((line: any) => line.id), [1])
    result.page.filter = '不存在的词'
    assert.deepEqual(result.page.shownLines.map((line: any) => line.id), [])
    assert.deepEqual([...result.page.checked], [1, 7])
  } finally { await result.cleanup() }
})

test('切换配音槽即时更新参数提示与筛选，其他槽差异不影响当前槽', async () => {
  const data: any = fixture(1, 1)
  data.lines[0].versions.push({ ...data.lines[0].wav, id: 2, settings_changed: true })
  const result = await render(data, page => { page.sel = 1; page.stateFilter = 'settings' })
  try {
    assert.deepEqual(result.page.shownLines, [], '另一个未选中的槽参数不同不能提示当前槽')
    assert.doesNotMatch(result.html, /当前修改尚未应用到配音/)
    const line = result.page.serverState.lines[0]
    line.selection.take = 2; line.wav = line.versions[1]
    assert.deepEqual(result.page.shownLines.map((item: any) => item.id), [1])
    line.selection.source = 'original'
    assert.deepEqual(result.page.shownLines, [], '切到原声后不再提示配音参数尚未应用')
  } finally { await result.cleanup() }
})

for (const kind of ['original', 'empty', 'stale', 'missing']) test(`${kind}状态不把可用性或原声选择误称为参数未应用`, async () => {
  const data: any = fixture(1, 1), line = data.lines[0]
  line.wav.settings_changed = true
  if (kind === 'original') line.selection.source = 'original'
  if (kind === 'empty') line.selection.take = null
  if (kind === 'stale') Object.assign(line.wav, { exists: false, stale: true })
  if (kind === 'missing') line.wav.exists = false
  const result = await render(data, page => { page.sel = 1 })
  try {
    assert.doesNotMatch(result.html, /aria-label="当前修改尚未应用到配音"/)
    assert.doesNotMatch(result.html, />配音待更新<\/span>/)
    if (kind === 'stale') assert.equal(result.page.label(line), '配音过期 · 预览无声')
  } finally { await result.cleanup() }
})

for (const phase of ['queued', 'running']) test(`${phase}更新仍展示现有音频参数差异，不覆盖实际排队或合成主状态`, async () => {
  const data: any = fixture(1, 1)
  data.lines[0].wav.settings_changed = true
  data.synthesis_task = { job_id: 42, active: true, running: phase === 'running', queued: phase === 'queued',
    lines: [{ id: 1, take: 1, status: phase }] }
  data.active_line = phase === 'running' ? 1 : null
  const result = await render(data, page => { page.sel = 1 })
  try {
    assert.match(result.html, /当前修改尚未应用到配音/)
    assert.match(result.html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' '), /更新完成前仍使用现有声音/)
    assert.match(result.page.label(data.lines[0]), phase === 'running' ? /合成中/ : /等待合成/)
    assert.equal(result.page.statusTone(data.lines[0]), phase === 'running' ? 'working' : 'queued')
    assert.equal(result.page.queuedSynthesisIds.has(1), true, '已入队的句子不能重复提交')
  } finally { await result.cleanup() }
})

function chooseTarget(options: any, target: { mode: 'new' | 'current' | 'slot'; slot?: number | null }) {
  options.content().props['onUpdate:target'](target)
  return options.content().props.plan
}

function workbenchResponse(data: any, url: unknown) {
  return { ok: true, json: async () => String(url).includes('/workbench?') ? data
    : String(url).endsWith('/workbench/synthesize') ? { status: 'started', job_id: 999 } : {} } as any
}

function expected(data: any, ids: number[]) {
  return { slots: data.synthesis_slots, lines: Object.fromEntries(ids.map(id => [id, `revision-${id}`])) }
}

test('冲突句入口复用生成确认，排除原声和静音，默认覆盖当前槽且确认前无请求', async t => {
  const data: any = fixture(5, 5), calls: any[] = []
  data.timing = { enabled: true, shifted: 1, unresolved: 4, unresolved_ids: [1, 2, 3, 4, 99] }
  data.lines[1].selection.source = 'original'
  data.lines[3].selection.take = null
  data.lines[0].customized = true
  const result = await render(data, page => { page.checked = [5]; page.filter = '5' })
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  try {
    assert.match(result.html, /重新生成冲突句…/)
    assert.match(result.html, /重排不改变声音长度/)
    assert.deepEqual(result.page.conflictIds, [1, 3])
    await result.page.regenerateConflicts()
    assert.equal(result.dialogs.length, 1)
    const options = result.dialogs[0], plan = options.content().props.plan
    assert.equal(options.title, '为冲突句生成配音')
    assert.deepEqual(plan.ids, [1, 3]); assert.equal(plan.target.mode, 'current')
    assert.equal(plan.overwrite, 2); assert.equal(plan.customized, 1)
    assert.deepEqual(plan.takes, { 1: 1, 3: 1 })
    assert.equal(options.positiveText, '确认覆盖并生成 2 句')
    assert.equal(calls.length, 0, '查看方案不读取或提交生成接口')
    assert.deepEqual(data.lines.map((line: any) => line.selection.take), [1, 1, 1, null, 1])
    const text = (await confirmationHtml(options)).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /冲突句 2 句/); assert.match(text, /保留逐句精调参数/)
    assert.match(text, /保留已有配音和满意标记/)
    assert.match(text, /当前服务的语速由服务端管理/)
    assert.doesNotMatch(text, /必要时再按已保存的超长重试因子缩时/)
    options.onNegativeClick()
    assert.equal(calls.length, 0); assert.equal(result.page.submitting, false)
    await result.page.regenerateConflicts()
    await result.dialogs[1].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    assert.deepEqual(JSON.parse(mutations(calls)[0].options.body), {
      line_ids: [1, 3], takes: { 1: 1, 3: 1 }, force: true, expected: expected(data, [1, 3]),
    })
  } finally { await result.cleanup() }
})

test('IndexTTS 冲突生成确认说明先重新排布再按已保存因子缩时', async () => {
  const data: any = fixture(2, 2)
  data.line_settings_supported = true
  data.timing = { enabled: true, shifted: 0, unresolved: 1, unresolved_ids: [1] }
  const result = await render(data)
  try {
    await result.page.regenerateConflicts()
    const text = (await confirmationHtml(result.dialogs[0])).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /生成后先重新排布，必要时再按已保存的超长重试因子缩时/)
    assert.doesNotMatch(text, /当前服务的语速由服务端管理/)
    result.dialogs[0].onNegativeClick()
  } finally { await result.cleanup() }
})

test('项目运行、未保存设置或冲突句内容未就绪时不能启动冲突修复生成', async () => {
  for (const blocked of ['running', 'queued', 'draft', 'translation'] as const) {
    const data: any = fixture(2, 2)
    data.timing = { enabled: true, shifted: 0, unresolved: 1, unresolved_ids: [1] }
    if (blocked === 'running') data.synthesis_task = { running: true, active: true, lines: [{ id: 1 }] }
    if (blocked === 'queued') data.synthesis_task = { running: false, active: false, queued: true, lines: [{ id: 1 }] }
    if (blocked === 'translation') data.lines[0].translation_available = false
    const result = await render(data, page => { if (blocked === 'draft') page.paramsDirty = true })
    try {
      assert.equal(result.page.canRegenerateConflicts, false)
      await result.page.regenerateConflicts()
      assert.equal(result.dialogs.length, 0)
    } finally { await result.cleanup() }
  }
})

test('有参数草稿时不打开重新合成确认，也不启动任务', async t => {
  const result = await render(fixture(3, 1)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return { ok: true, json: async () => ({}) } as any
  })
  try {
    result.page.paramsDirty = true
    await result.page.synthesize([1, 2, 3])
    assert.equal(result.dialogs.length, 0)
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('生成全部配音默认覆盖当前槽，筛选和勾选不改变 249 句范围，确认显示数量摘要', async () => {
  const result = await render(fixture(), page => { page.filter = '2'; page.checked = [2] })
  try {
    await result.page.regenerateAll()
    assert.equal(result.dialogs.length, 1)
    assert.equal(result.dialogs[0].title, '生成全部配音')
    const plan = result.dialogs[0].content().props.plan
    assert.equal(plan.all, true)
    assert.equal(plan.total, 249)
    assert.equal(plan.overwrite, 52)
    assert.equal(plan.create, 197)
    assert.equal(plan.refill, 0)
    assert.equal(plan.newSlot, 2)
    assert.equal(plan.destination, '各句当前配音槽')
    assert.ok(Object.entries(plan.takes).every(([id, take]) => take === (Number(id) <= 52 ? 1 : 2)))
    assert.deepEqual(plan.ids, Array.from({ length: 249 }, (_, index) => index + 1))
    const html = await confirmationHtml(result.dialogs[0])
    const text = html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /全部\s*249\s*句/)
    assert.match(text, /新增\s*197\s*句/)
    assert.match(text, /保留已有配音和满意标记/)
    assert.match(text, /生成到哪个配音槽/)
    assert.match(text, /合成失败的句子保留原来的音频与选择/)
    assert.match(text, /覆盖\s*52\s*句/)
    assert.equal(result.dialogs[0].positiveText, '确认覆盖并生成 249 句')
    assert.doesNotMatch(text, /1、2、3、4、5、6、7、8、9、10/)
    assert.doesNotMatch(text, /仅处理句号/)
    assert.equal(result.page.submitting, true)
    result.dialogs[0].onNegativeClick()
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('首次生成、局部生成和单句使用同一目标选择，取消不保存选择或启动任务', async t => {
  const result = await render(fixture(5, 0)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return { ok: true, json: async () => ({}) } as any
  })
  try {
    await result.page.synthesize([5, 2])
    assert.equal(result.dialogs.length, 1)
    const plan = result.dialogs[0].content().props.plan
    assert.equal(plan.all, false)
    assert.deepEqual(plan.ids, [2, 5])
    assert.equal(plan.create, 2)
    assert.equal(plan.newSlot, 1)
    assert.equal(result.dialogs[0].title, '生成勾选配音')
    const originalSelections = JSON.stringify(result.page.state.lines.map((line: any) => line.selection))
    chooseTarget(result.dialogs[0], { mode: 'current' })
    assert.equal(result.page.submitting, true)
    assert.equal(mutations(calls).length, 0)
    result.dialogs[0].onClose()
    assert.equal(result.page.submitting, false)
    assert.equal(JSON.stringify(result.page.state.lines.map((line: any) => line.selection)), originalSelections)
    assert.equal(calls.length, 0)
    await result.page.synthesize([1])
    assert.equal(result.dialogs.length, 2, result.page.loadError)
    assert.equal(result.dialogs[1].title, '生成本句配音')
    assert.equal(result.dialogs[1].content().props.plan.target.mode, 'current')
    assert.ok(result.dialogs[1], result.page.loadError)
    result.dialogs[1].onNegativeClick()
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('单句日常修正默认当前槽并明确覆盖确认，仍可主动新建对比声音', async () => {
  const result = await render(fixture(2, 2))
  try {
    await result.page.synthesize([1])
    const options = result.dialogs[0], plan = options.content().props.plan
    assert.equal(plan.target.mode, 'current')
    assert.equal(plan.overwrite, 1)
    assert.deepEqual(plan.takes, { 1: 1 })
    assert.equal(options.type, 'warning')
    assert.equal(options.positiveText, '确认覆盖并生成 1 句')
    const text = (await confirmationHtml(options)).replace(/<[^>]*>/g, ' ')
    assert.match(text, /覆盖成功后目标槽的旧音频会被替换/)
    assert.doesNotMatch(text, /新槽保留旧配音，可在列表切回对比/)
    const comparison = chooseTarget(options, { mode: 'new' })
    assert.equal(comparison.overwrite, 0)
    assert.deepEqual(comparison.takes, { 1: 2 })
    assert.equal(options.type, 'info')
    assert.equal(options.positiveText, '开始生成 1 句')
    options.onNegativeClick()
  } finally { await result.cleanup() }
})

test('单句确认直接展示冻结句号和译文，不能只靠数量或折叠范围辨认目标', async () => {
  const data = fixture(5, 5)
  data.lines[3].text = '还有“没有朝”。这次只修正第四句的读音。'
  const result = await render(data, page => { page.sel = 1 })
  try {
    await result.page.synthesize([4])
    const options = result.dialogs[0]
    assert.deepEqual(options.content().props.plan.ids, [4])
    assert.equal(options.content().props.sentenceText, data.lines[3].text)
    const html = await confirmationHtml(options)
    assert.match(html, /<strong[^>]*>第 4 句<\/strong>/)
    assert.match(html, /aria-label="待生成第 4 句"/)
    assert.match(html, /还有“没有朝”。这次只修正第四句的读音。/)
    assert.match(html, /仅合成本句/)
    assert.doesNotMatch(html, /查看句子范围|本次 1 句|译文 1/)
    result.page.sel = 2
    data.lines[3].text = '其他窗口后来的修改'
    assert.equal(options.content().props.sentenceText, '还有“没有朝”。这次只修正第四句的读音。', '预览使用确认时冻结的目标，不能被后续定位和列表变化替换')
    options.onNegativeClick()
  } finally { await result.cleanup() }
})

test('单句新槽只看本句，别句槽2不会把本句的新槽推到3，确认仍保留全局并发保护', async t => {
  const data = fixture(2, 2)
  data.lines[1].versions.push({ ...data.lines[1].wav, id: 2 })
  const result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([1])
    chooseTarget(result.dialogs[0], { mode: 'new' })
    const plan = result.dialogs[0].content().props.plan
    assert.equal(plan.newSlot, 2)
    assert.equal(plan.overwrite, 0)
    assert.deepEqual(plan.takes, { 1: 2 })
    assert.match(await confirmationHtml(result.dialogs[0]), /本句/)
    await result.dialogs[0].onPositiveClick()
    const body = JSON.parse(mutations(calls)[0].options.body)
    assert.deepEqual(body.takes, { 1: 2 })
    assert.deepEqual(body.expected.slots, [1, 2], '编号范围改变不削弱原有确认保护')
    assert.equal(data.lines[0].versions.length, 1, '确认与提交不伪造已合成版本')
  } finally { await result.cleanup() }
})

test('明确选择新槽强制处理冻结范围，去重并按视频顺序写入统一槽号', async t => {
  const data = fixture(4, 2)
  data.lines[2].selection = { source: 'original', take: 7 }
  data.lines[2].versions = [{ ...data.lines[0].wav, id: 7 }]
  const result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([4, 2, 2, 1, 3])
    assert.equal(result.dialogs.length, 1)
    chooseTarget(result.dialogs[0], { mode: 'new' })
    const plan = result.dialogs[0].content().props.plan
    assert.equal(plan.original, 1)
    assert.equal(plan.newSlot, 8)
    assert.match((await confirmationHtml(result.dialogs[0])).replace(/<[^>]*>/g, ' '), /原声/)
    result.page.checked = [4]
    result.page.filter = '4'
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    assert.deepEqual(JSON.parse(mutations(calls)[0].options.body), {
      line_ids: [1, 2, 3, 4], takes: { 1: 8, 2: 8, 3: 8, 4: 8 }, force: true, expected: expected(data, [1, 2, 3, 4]),
    })
    assert.equal(calls[0], mutations(calls)[0], '提交前不再读取完整工作台')
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('选择各句当前槽后披露混合覆盖、补齐和新增，过期物理版本仍算覆盖', async () => {
  const data = fixture(3, 1)
  data.lines[0].wav = { ...data.lines[0].wav, exists: false, stale: true }
  data.lines[0].versions[0] = { ...data.lines[0].versions[0], stale: true }
  data.lines[0].customized = true
  data.lines[0].custom_params = [{ path: 'tts.indextts25.duration_factor', label: '时长因子', value: 1.2 }] as any
  data.lines[1].selection.take = 2
  data.lines[1].versions = []
  const result = await render(data)
  try {
    await result.page.regenerateAll()
    const plan = chooseTarget(result.dialogs[0], { mode: 'current' })
    assert.equal(plan.overwrite, 1)
    assert.equal(plan.refill, 1)
    assert.equal(plan.create, 1)
    assert.equal(plan.customized, 1)
    assert.deepEqual(plan.takes, { 1: 1, 2: 2, 3: 3 })
    assert.equal(result.dialogs[0].type, 'warning')
    assert.equal(result.dialogs[0].positiveText, '确认覆盖并生成 3 句')
    const text = (await confirmationHtml(result.dialogs[0])).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /覆盖\s*1/)
    assert.match(text, /补齐\s*1/)
    assert.match(text, /新增\s*1/)
    assert.match(text, /句子单独设置，单独设置优先/)
    chooseTarget(result.dialogs[0], { mode: 'new' })
    assert.equal(result.dialogs[0].type, 'info')
    assert.equal(result.dialogs[0].positiveText, '开始生成 3 句')
    result.dialogs[0].onNegativeClick()
  } finally { await result.cleanup() }
})

test('指定已有槽覆盖该槽而非当前选中槽，满意数量按照目标槽显示并提交', async t => {
  const data = fixture(3, 2)
  data.lines[0].versions.push({ ...data.lines[0].wav, id: 2, confirmed: true })
  data.lines[1].versions.push({ ...data.lines[1].wav, id: 2, confirmed: false })
  const result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  try {
    await result.page.regenerateAll()
    const props = result.dialogs[0].content().props
    assert.deepEqual(props.slots, [{ value: 1, count: 2 }, { value: 2, count: 2 }])
    const plan = chooseTarget(result.dialogs[0], { mode: 'slot', slot: 2 })
    assert.equal(plan.overwrite, 2)
    assert.equal(plan.create, 1)
    assert.equal(plan.refill, 0)
    assert.equal(plan.confirmed, 1)
    assert.equal(result.dialogs[0].positiveText, '确认覆盖并生成 3 句')
    const text = (await confirmationHtml(result.dialogs[0])).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
    assert.match(text, /覆盖\s*2\s*句/)
    assert.match(text, /含\s*1\s*句已满意/)
    assert.match(text, /其他槽保留/)
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    assert.deepEqual(JSON.parse(mutations(calls)[0].options.body), {
      line_ids: [1, 2, 3], takes: { 1: 2, 2: 2, 3: 2 }, force: true, expected: expected(data, [1, 2, 3]),
    })
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('各句当前槽提交保留不同槽号，未选槽句子统一写入共同新槽', async t => {
  const data = fixture(3, 1)
  data.lines[1].selection.take = 7
  data.lines[1].versions = [{ ...data.lines[0].wav, id: 7 }]
  const result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  try {
    await result.page.regenerateAll()
    chooseTarget(result.dialogs[0], { mode: 'current' })
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    assert.deepEqual(JSON.parse(mutations(calls)[0].options.body), {
      line_ids: [1, 2, 3], takes: { 1: 1, 2: 7, 3: 8 }, force: true, expected: expected(data, [1, 2, 3]),
    })
  } finally { await result.cleanup() }
})

test('无效指定槽禁用提交并保留确认窗口，改回新槽可继续，取消释放忙碌', async t => {
  const result = await render(fixture(2, 1)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse({}, url)
  })
  try {
    await result.page.regenerateAll()
    const options = result.dialogs[0]
    chooseTarget(options, { mode: 'slot', slot: null })
    assert.equal(options.positiveButtonProps.disabled, true)
    assert.match(await confirmationHtml(options), /请选择有效的目标配音槽/)
    assert.equal(await options.onPositiveClick(), false)
    assert.equal(result.page.submitting, true)
    assert.equal(calls.length, 0)
    chooseTarget(options, { mode: 'new' })
    assert.equal(options.positiveButtonProps.disabled, false)
    options.onNegativeClick()
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('项目最大槽达到 10000 时默认新槽不可提交，明确选已有槽仍可生成', async () => {
  const data = fixture(2, 1)
  data.lines[0].selection.take = 10000
  data.lines[0].versions = [{ ...data.lines[0].wav, id: 10000 }]
  const result = await render(data)
  try {
    await result.page.regenerateAll()
    const options = result.dialogs[0]
    assert.equal(options.positiveButtonProps.disabled, true)
    assert.match(await confirmationHtml(options), /配音槽已达到上限/)
    chooseTarget(options, { mode: 'slot', slot: 10000 })
    assert.equal(options.positiveButtonProps.disabled, false)
    assert.equal(options.content().props.plan.overwrite, 1)
    assert.equal(options.content().props.plan.create, 1)
    options.onNegativeClick()
  } finally { await result.cleanup() }
})

test('确认期间选中版本发生变化时，不覆盖刚切换的配音版本', async t => {
  const data = fixture(3, 1), result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return { ok: true, json: async () => ({}) } as any
  })
  try {
    await result.page.regenerateAll()
    data.lines[0].selection.take = 9
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.dialogs[0].positiveText, '刷新并重新确认')
    assert.equal(result.page.submitting, true)
    result.dialogs[0].onNegativeClick()
  } finally { await result.cleanup() }
})

test('跨窗口变化由单次POST携带冻结版本交给后端原子拦截，不先读取完整工作台', async t => {
  const data = fixture(3, 1), result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    if (String(url).endsWith('/synthesize')) return { ok: false, status: 409,
      json: async () => ({ code: 'TTS_CONFIRMATION_CHANGED', message: '配音参数或目标槽已变化，请重新确认' }) } as any
    return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([1, 2])
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(calls[0], mutations(calls)[0])
    assert.deepEqual(JSON.parse(calls[0].options.body).expected, expected(data, [1, 2]))
    assert.equal(mutations(calls).length, 1, '拒绝后不自动重发')
    assert.equal(result.page.acceptedSubmission, null)
    assert.equal(result.page.submitting, true)
    assert.equal(result.dialogs[0].positiveText, '刷新并重新确认')
    assert.match(await confirmationHtml(result.dialogs[0]), /配音参数或目标槽已变化/)
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1, '重新确认只刷新状态，不会重发任务')
    assert.equal(result.dialogs.length, 2, JSON.stringify({ error: result.page.loadError, disabled: result.page.runDisabled,
      busy: result.page.busy, submitting: result.page.submitting, task: result.page.task, dirty: result.page.dirty, loads: result.page.workbenchLoads }))
    assert.equal(result.page.submitting, true)
    result.dialogs[1].onNegativeClick()
  } finally { await result.cleanup() }
})

test('连接中断时保留提交结果未确认状态，只重新读取状态不自动重发', async t => {
  const result = await render(fixture(2, 1)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); throw new Error('测试后端断线')
  })
  try {
    await result.page.regenerateAll()
    chooseTarget(result.dialogs[0], { mode: 'current' })
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    assert.equal(calls.filter(call => !call.options.method || call.options.method === 'GET').length, 1)
    assert.equal(result.page.submissionUncertain, true)
    assert.equal(result.page.runDisabled, true)
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('提交期间立即显示等待、禁用取消关闭和改槽，重复点击不会重复提交', async t => {
  const data = fixture(2, 1), result = await render(data), calls: any[] = []
  let finishPost: ((response: any) => void) | undefined
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    if (String(url).endsWith('/synthesize')) return new Promise(resolve => { finishPost = resolve })
    return workbenchResponse(data, url)
  })
  try {
    await result.page.regenerateAll()
    const options = result.dialogs[0], submission = options.onPositiveClick()
    assert.ok(finishPost)
    assert.equal(options.positiveText, '正在提交…')
    assert.equal(options.positiveButtonProps.loading, true)
    assert.equal(options.positiveButtonProps.disabled, true)
    assert.equal(options.negativeButtonProps.disabled, true)
    assert.equal(options.closable, false)
    assert.equal(options.onClose(), false)
    assert.equal(options.onNegativeClick(), false)
    assert.equal(await options.onPositiveClick(), false)
    chooseTarget(options, { mode: 'new' })
    assert.equal(options.content().props.plan.target.mode, 'current')
    const html = await confirmationHtml(options)
    assert.match(html, /role="status"/)
    assert.match(html, /正在核对配音参数与目标槽/)
    assert.match(html, /fieldset[^>]+disabled/)
    finishPost(workbenchResponse(data, '/workbench/synthesize'))
    await submission
    assert.equal(mutations(calls).length, 1)
    assert.equal(result.page.submitting, false)
    assert.equal(await options.onPositiveClick(), false, '已处理的旧确认窗口不可再次发起任务')
  } finally { await result.cleanup() }
})

test('服务器确认接收后立即结束确认，不等待详细工作台或导航刷新', async t => {
  const data = fixture(2, 1), result = await render(data), calls: any[] = []
  let finishRead: ((response: any) => void) | undefined
  let finishNavigation: (() => void) | undefined
  t.mock.method(result.nav, 'poll', () => new Promise<void>(resolve => { finishNavigation = resolve }))
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    if (String(url).includes('/workbench?')) return new Promise(resolve => { finishRead = resolve })
    return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([1])
    await result.dialogs[0].onPositiveClick()
    assert.ok(finishRead)
    assert.ok(finishNavigation)
    assert.equal(result.page.submitting, false, 'POST回执即可关闭确认，慢刷新仍未完成')
    assert.equal(result.page.task.job_id, 999)
    assert.equal(result.page.task.queued, true)
    assert.equal(result.page.isQueued(data.lines[0]), true)
    assert.equal(result.page.runDisabled, true, '未读到已接收任务前不能再次提交')
    await result.page.synthesize([1])
    assert.equal(result.dialogs.length, 1)
    finishRead(workbenchResponse(data, '/workbench?probe=false'))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    assert.equal(result.page.acceptedSubmission.jobId, 999, '旧任务响应不能撤销已确认入队的状态')
    assert.equal(result.page.task.job_id, 999)
    ;(data as any).synthesis_task = { job_id: 999, active: false, queued: false, running: false, lines: [] }
    const refresh = result.page.load()
    finishRead!(workbenchResponse(data, '/workbench?probe=false'))
    await refresh
    assert.equal(result.page.acceptedSubmission, null, '同一任务的完成状态解除轻量回执锁')
    assert.equal(result.page.runDisabled, false)
    finishNavigation()
  } finally { await result.cleanup() }
})

test('服务器5xx、成功回包无法解析或缺少任务编号时结果待核对，不能直接当失败重发', async t => {
  for (const mode of ['server-error', 'invalid-json', 'missing-job-id']) await t.test(mode, async t => {
    const data = fixture(1, 1), result = await render(data), calls: any[] = []
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      calls.push({ url: String(url), options })
      if (String(url).includes('/workbench?')) throw new Error('状态读取暂不可用')
      return mode === 'server-error' ? { ok: false, status: 503, json: async () => ({ message: '服务暂不可用' }) } as any
        : { ok: true, status: 200, json: async () => {
          if (mode === 'invalid-json') throw new SyntaxError('Invalid JSON')
          return { status: 'started' }
        } } as any
    })
    try {
      await result.page.synthesize([1])
      await result.dialogs[0].onPositiveClick()
      assert.equal(mutations(calls).length, 1)
      assert.equal(result.page.submissionUncertain, true)
      assert.equal(result.page.submitting, false)
      await result.page.synthesize([1])
      assert.equal(result.dialogs.length, 1)
      assert.equal(mutations(calls).length, 1)
    } finally { await result.cleanup() }
  })
})

test('工作台空闲低频轮询、后台标签跳过、回前台及时刷新，慢读取不积压', async t => {
  const data = fixture(2, 1), result = await render(data), calls: any[] = []
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const pageDocument = { hidden: false }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: pageDocument })
  let now = 10000
  t.mock.method(Date, 'now', () => now)
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  const flush = () => new Promise(resolve => setImmediate(resolve))
  try {
    await result.page.load()
    now += 3000
    result.page.pollWorkbench()
    assert.equal(calls.length, 1, '空闲状态不每1.5秒刷新完整工作台')
    now += 1500
    result.page.pollWorkbench()
    await flush()
    assert.equal(calls.length, 2)
    pageDocument.hidden = true; now += 5000
    result.page.pollWorkbench(true)
    assert.equal(calls.length, 2)
    pageDocument.hidden = false
    result.page.resumeWorkbench()
    await flush()
    assert.equal(calls.length, 3)
    result.page.serverState = { ...data, synthesis_task: { active: true, queued: false, job_id: 123, lines: [] } }
    now += 1500
    let finishRead: ((response: any) => void) | undefined
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      calls.push({ url: String(url), options }); return new Promise(resolve => { finishRead = resolve })
    })
    result.page.pollWorkbench()
    assert.equal(calls.length, 4, '活动任务仍及时读取')
    now += 10000
    result.page.pollWorkbench()
    result.page.pollWorkbench(true)
    assert.equal(calls.length, 4, '慢请求期间不追加永无间隔的完整扫描')
    finishRead!(workbenchResponse(data, '/workbench?probe=false'))
    await flush()
    assert.equal(calls.length, 4)
  } finally {
    await result.cleanup()
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument)
    else Reflect.deleteProperty(globalThis, 'document')
  }
})

test('缺少后端版本信息时先刷新，不能绕过并发确认保护', async t => {
  const data = fixture(1, 1), result = await render(data), calls: any[] = []
  delete (data.lines[0] as any).synthesis_revision
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([1])
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(mutations(calls).length, 0)
    assert.equal(calls.length, 0)
    assert.equal(result.dialogs[0].positiveText, '刷新并重新确认')
    await result.dialogs[0].onPositiveClick()
    assert.equal(calls.filter(call => !call.options.method || call.options.method === 'GET').length, 1)
    assert.equal(result.page.submitting, true)
    assert.ok(result.dialogs[1], JSON.stringify({ error: result.page.loadError, disabled: result.page.runDisabled,
      busy: result.page.busy, submitting: result.page.submitting, task: result.page.task, dirty: result.page.dirty }))
    result.dialogs[1].onNegativeClick()
  } finally { await result.cleanup() }
})

test('确认窗口期间开始运行会阻止提交，取消后可在空闲状态重新打开确认', async t => {
  const result = await render(fixture(3, 1)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return { ok: true, json: async () => ({}) } as any
  })
  try {
    await result.page.regenerateAll()
    result.nav.steps = [{ key: 'split', status: 'running' }]
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.page.submitting, true)
    result.dialogs[0].onNegativeClick()
    result.nav.steps = []
    await result.page.regenerateAll()
    assert.equal(result.dialogs.length, 2)
    result.dialogs[1].onNegativeClick()
    assert.equal(mutations(calls).length, 0)
  } finally { await result.cleanup() }
})

test('项目归档锁和活动合成任务阻止打开生成入口', async t => {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse({}, url)
  })
  for (const lock of ['archive', 'task'] as const) {
    const data = fixture(2, 1)
    if (lock === 'task') data.synthesis_task = { running: true, active: true, queued: false, lines: [{ id: 1 }] } as any
    const result = await render(data)
    try {
      if (lock === 'archive') execution.setNavigationExecution('fixture', false, false, true)
      await result.page.regenerateAll()
      assert.equal(result.dialogs.length, 0)
      assert.equal(mutations(calls).length, 0)
      assert.equal(result.page.submitting, false)
    } finally {
      await result.cleanup(); execution.setNavigationExecution('', false, true)
    }
  }
})

test('全部范围中有待核对或缺译文的句子时，不静默缩小范围启动部分合成', async t => {
  const data = fixture(3, 1)
  data.lines[2].translation_available = false
  const result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return { ok: true, json: async () => ({}) } as any
  })
  try {
    await result.page.regenerateAll()
    assert.equal(result.dialogs.length, 0)
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('句子范围包含已移除编号时，整批拒绝而非合成剩下句子', async t => {
  const result = await render(fixture(3, 1)), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options }); return workbenchResponse({}, url)
  })
  try {
    await result.page.synthesize([1, 99])
    assert.equal(result.dialogs.length, 0)
    assert.equal(calls.length, 0)
    assert.equal(result.page.submitting, false)
  } finally { await result.cleanup() }
})

test('从执行方案跳到配音参数时展开普通设置并切回配音工作台', async () => {
  const result = await render(fixture(1, 1), () => {}, '#dubbing-parameters')
  try {
    assert.equal(result.page.activePane, 'editor')
    assert.deepEqual(result.page.parametersOpen, ['parameters'])
    assert.match(result.html, /id="dubbing-parameters"/)
    assert.match(result.html, /配音参数 · 步骤设置/)
  } finally { await result.cleanup() }
})


function mixRetryButton(html:string) {
  const result=[...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(row=>row[0])
    .find(button=>button.replace(/<[^>]*>/g,'').trim()==='重试保存')
  assert.ok(result,'未保存状态应提供重试入口');return result
}

test('TTS运行中的混音按assemble字段保存，其他配音动作继续锁定',async t=>{
  const data:any=fixture(2,1);data.synthesis_task={active:true,running:true,queued:false,lines:[],job_id:411};data.active_line=2
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),options})
    return {ok:true,json:async()=>String(url).endsWith('/editability')
      ?{locked:false,resource_locked:true,locked_paths:['tts'],running_steps:[{key:'tts'}]}
      :String(url).includes('/workbench')?{...data,mix:{voice:.45,background:.3}}:{}} as any
  })
  await execution.checkConfigurationEditability('project:fixture');execution.setNavigationExecution('fixture',true)
  const result=await render(data,page=>{page.observing=true;page.mixEdits.error.value='之前保存失败'})
  try {
    assert.equal(result.page.busy,true);assert.equal(result.page.mixControlsLocked,false)
    assert.equal(result.page.runDisabled,true);assert.doesNotMatch(mixRetryButton(result.html),/\bdisabled(?:[\s=>])/)
    result.page.mixEdits.change('voice',.45);result.page.mixEdits.change('background',.3)
    await result.page.mixEdits.save()
    const writes=calls.filter(call=>call.options?.method==='PUT')
    assert.equal(writes.length,1);assert.match(writes[0].url,/\/step_params$/)
    assert.deepEqual(JSON.parse(writes[0].options.body),{updates:{'assemble.voice_volume':.45,'assemble.bgm_volume':.3}})
    assert.equal(result.page.mixEdits.pending.value,false);assert.equal(result.page.mixEdits.error.value,'')
  } finally {await result.cleanup()}
})

test('合成成片已运行时混音滑块与重试锁定，保存前再核对路径且保留试听值',async t=>{
  const data:any=fixture(2,1),calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),options});return {ok:true,json:async()=>({locked:false,resource_locked:true,locked_paths:['assemble']})} as any
  })
  await execution.checkConfigurationEditability('project:fixture');execution.setNavigationExecution('fixture',true)
  const result=await render(data,page=>{page.observing=true;page.mixEdits.error.value='合成成片正在使用这些音量'})
  try {
    assert.equal(result.page.mixControlsLocked,true);assert.match(mixRetryButton(result.html),/\bdisabled(?:[\s=>])/)
    result.page.mixEdits.change('voice',.4);await result.page.mixEdits.save()
    assert.equal(calls.filter(call=>call.options?.method==='PUT').length,0)
    assert.equal(result.page.volDub,.4);assert.equal(result.page.mixEdits.pending.value,true)
    assert.match(result.page.mixEdits.error.value,/合成成片正在使用/)
    result.page.mixEdits.reconcile({voice:1,background:1},result.page.mixEdits.revision())
    assert.equal(result.page.volDub,.4)
  } finally {await result.cleanup()}
})

test('运行状态读取失败时混音不写入，重试入口可重新核对且不丢试听值',async t=>{
  const data:any=fixture(2,1),calls:any[]=[];let available=false
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url:String(url),options})
    if(String(url).endsWith('/editability'))return {ok:available,json:async()=>available
      ?{locked:false,resource_locked:true,locked_paths:['tts']}:{message:'断线'}} as any
    return {ok:true,json:async()=>String(url).includes('/workbench')?{...data,mix:{voice:.55,background:1}}:{}} as any
  })
  await execution.checkConfigurationEditability('project:fixture')
  const result=await render(data,page=>{page.observing=true;page.mixEdits.error.value='未能核对运行状态，试听值已保留'})
  try {
    assert.equal(result.page.mixControlsLocked,true)
    assert.doesNotMatch(mixRetryButton(result.html),/\bdisabled(?:[\s=>])/)
    result.page.mixEdits.change('voice',.55);await result.page.mixEdits.save()
    assert.equal(calls.filter(call=>call.options?.method==='PUT').length,0)
    assert.equal(result.page.volDub,.55);assert.equal(result.page.mixEdits.pending.value,true)
    result.page.mixEdits.reconcile({voice:1,background:1},result.page.mixEdits.revision());assert.equal(result.page.volDub,.55)
    available=true;await result.page.mixEdits.save()
    assert.equal(calls.filter(call=>call.options?.method==='PUT').length,1)
    assert.equal(result.page.mixEdits.error.value,'');assert.equal(result.page.mixEdits.pending.value,false)
  } finally {await result.cleanup()}
})

test('运行中仅放开逐句声音设置，文字与选槽仍锁定；更新回执在列表可见', async () => {
  const data: any = fixture(2, 1)
  data.line_settings_supported = true
  data.line_config_execution = { editable: true, active: true, accepting: true }
  data.synthesis_task = { active: true, running: true, lines: [], job_id: 5 }
  data.active_line = 2
  data.lines[0].config_execution = { state: 'queued', pending_revision: 2, message: '当前请求完成后更新' }
  const result = await render(data)
  try {
    assert.equal(result.page.lineSettingsDisabled(data.lines[0]), false)
    assert.equal(result.page.actionsDisabled, true)
    assert.equal(result.page.runDisabled, true)
    assert.match(result.html, /参数更新待处理/)
    assert.match(result.html, /快速校对试听/)
    assert.match(result.html, /仅听人声/)
    result.page.gainDirty = true
    assert.equal(result.page.dirty, true)
    assert.equal(result.page.lineSettingsDisabled(data.lines[1]), true)
  } finally { await result.cleanup() }
})

test('不支持模型逐句参数的引擎仍可进入后期音量；已归档项目保持只读', async () => {
  const data: any = fixture(1, 1)
  const result = await render(data)
  try {
    assert.equal(result.page.lineSettingsDisabled(data.lines[0]), false)
    result.nav.archived = true
    execution.setNavigationExecution('fixture', false, false, true)
    assert.equal(result.page.lineSettingsDisabled(data.lines[0]), true)
  } finally { await result.cleanup() }
})

function acceptingFixture() {
  const data: any = fixture(5, 5)
  data.line_settings_supported = true
  data.line_config_execution = { editable: true, active: true, accepting: true, generation_accepting: true }
  data.synthesis_task = { active: true, running: true, queued: false, job_id: 73,
    accepted_request_ids: [], lines: [{ id: 1, take: 1, status: 'running' }, { id: 2, take: 1, status: 'queued' }] }
  data.active_line = 1; data.active_progress = .4
  data.synthesis_reserved_takes = { '1': [1], '2': [1] }
  return data
}

test('合成中单句入口允许加入其他句子，当前和排队句防重复；全部重做等操作保持锁定', async () => {
  const data = acceptingFixture()
  const result = await render(data, page => { page.sel = 3; page.checked = [1, 2, 3, 4] })
  try {
    assert.equal(result.page.synthesisDisabled, false)
    assert.equal(result.page.selectionDisabled, false)
    assert.equal(result.page.runDisabled, true)
    assert.equal(result.page.canRegenerateAll, false)
    assert.equal(result.page.synthesisButtonLabel(1), '正在合成本句')
    assert.equal(result.page.synthesisButtonLabel(2), '本句已在待配音列表')
    assert.equal(result.page.synthesisButtonLabel(3), '加入待配音…')
    assert.deepEqual(result.page.checkedSynthesisIds, [3, 4])
    assert.match(result.html, /加入待配音（2 句）/)
    assert.doesNotMatch(result.html.match(/<input[^>]+aria-label="选择第 3 句"[^>]*>/)?.[0] ?? '', /\bdisabled/)
    await result.page.synthesize([1]); await result.page.synthesize([2]); await result.page.regenerateAll()
    assert.equal(result.dialogs.length, 0)
    await result.page.synthesize([3])
    const options = result.dialogs[0]
    assert.equal(options.title, '加入待配音列表')
    assert.equal(options.positiveText, '确认覆盖并加入 1 句')
    const text = await confirmationHtml(options)
    assert.match(text, /第 3 句/); assert.match(text, /译文 3/)
    assert.match(text, /不打断正在合成的句子/)
    assert.match(text, /当前任务的基础配音参数与已保存的本句设置/)
    chooseTarget(options, { mode: 'new' })
    assert.equal(options.positiveText, '加入待配音 1 句')
    options.onNegativeClick()
  } finally { await result.cleanup() }
})

test('同一任务连续追加保留原队列和进度，旧轮询不能撤回已接收句子或预留槽', async t => {
  const data = acceptingFixture(), calls: any[] = [], result = await render(data)
  let queued = 0, navigationStarts = 0
  t.mock.method(result.nav, 'stepStarted', () => { navigationStarts++ })
  t.mock.method(result.nav, 'poll', async () => {})
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    if (String(url).endsWith('/synthesize')) {
      queued++
      const body = JSON.parse(options?.body as string)
      return { ok: true, json: async () => ({ status: 'queued', job_id: 73, request_id: `request-${queued}`,
        lines: body.line_ids.map((id: number) => ({ id, take: body.takes[id], status: 'queued' })) }) } as any
    }
    return workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([3])
    chooseTarget(result.dialogs[0], { mode: 'new' })
    await result.dialogs[0].onPositiveClick()
    await result.page.load()
    assert.equal(result.page.submitting, false)
    assert.equal(result.page.task.running, true)
    assert.equal(result.page.task.queued, false)
    assert.equal(result.page.serverState.active_line, 1)
    assert.equal(result.page.serverState.active_progress, .4)
    assert.deepEqual(result.page.task.lines.map((line: any) => [line.id, line.status]), [[1, 'running'], [2, 'queued'], [3, 'queued']])
    assert.equal(result.page.acceptedSubmission.requests[0].requestId, 'request-1')
    assert.deepEqual(result.page.serverState.synthesis_slots, [1, 2])
    assert.deepEqual(result.page.serverState.synthesis_reserved_takes['3'], [2])
    assert.equal(result.page.synthesisDisabled, false, '接收后可继续添加另一句，不等待慢轮询')
    await result.page.synthesize([4])
    assert.equal(result.dialogs[1].content().props.plan.newSlot, 2, '别句排队到槽2，不占用本句的槽2')
    chooseTarget(result.dialogs[1], { mode: 'new' })
    await result.dialogs[1].onPositiveClick()
    await result.page.load()
    assert.equal(result.page.acceptedSubmission.requests.length, 2)
    assert.deepEqual(result.page.serverState.synthesis_slots, [1, 2])
    assert.deepEqual(result.page.serverState.synthesis_reserved_takes['4'], [2])
    assert.deepEqual(result.page.task.lines.map((line: any) => line.id), [1, 2, 3, 4])
    assert.equal(navigationStarts, 0, '追加不会重置任务导航和总体进度')
    assert.equal(mutations(calls).length, 2)
    assert.equal(JSON.parse(mutations(calls)[0].options.body).append_to_job_id, 73)
    assert.equal(JSON.parse(mutations(calls)[1].options.body).append_to_job_id, 73)
    assert.deepEqual(JSON.parse(mutations(calls)[1].options.body).expected.slots, [1, 2])
    data.synthesis_task.accepted_request_ids = ['request-1']
    data.synthesis_task.lines[0].status = 'done'
    data.synthesis_task.lines.push({ id: 3, take: 2, status: 'done' })
    data.synthesis_slots = [1, 2]
    data.active_progress = .8
    await result.page.load()
    assert.equal(result.page.acceptedSubmission.requests.length, 1)
    assert.equal(result.page.acceptedSubmission.requests[0].requestId, 'request-2')
    assert.equal(result.page.task.lines.find((line: any) => line.id === 3).status, 'done', '已确认请求按服务器最新进度显示')
    assert.equal(result.page.task.lines.find((line: any) => line.id === 4).status, 'queued')
    assert.equal(result.page.serverState.active_progress, .8)
    data.synthesis_task.accepted_request_ids.push('request-2')
    data.synthesis_task.lines.push({ id: 4, take: 2, status: 'running' })
    data.synthesis_slots = [1, 2]
    await result.page.load()
    assert.equal(result.page.acceptedSubmission, null)
    assert.equal(result.page.task.lines.find((line: any) => line.id === 4).status, 'running')
  } finally { await result.cleanup() }
})

test('停止、排布阶段、其他步骤和未保存草稿禁止追加，确认期间关闭队列也不提交', async t => {
  for (const mode of ['closing', 'preparing', 'other-step', 'dirty', 'saving']) {
    const data = acceptingFixture()
    if (mode === 'closing') data.line_config_execution.generation_accepting = false
    if (mode === 'preparing') { data.line_config_execution.generation_accepting = false; data.line_config_execution.accepting = true }
    if (mode === 'other-step') data.line_config_execution.active = false
    const result = await render(data, page => {
      if (mode === 'dirty') page.lineDirty = true
      if (mode === 'saving') page.mixEdits.change('voice', .6)
    })
    try {
      assert.equal(result.page.synthesisDisabled, true, mode)
      await result.page.synthesize([3])
      assert.equal(result.dialogs.length, 0, mode)
    } finally { await result.cleanup() }
  }
  const result = await render(acceptingFixture()), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url: String(url), options }); return workbenchResponse(acceptingFixture(), url) })
  try {
    await result.page.synthesize([3])
    result.page.serverState.line_config_execution.generation_accepting = false
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(mutations(calls).length, 0)
    assert.equal(result.dialogs[0].positiveText, '刷新并重新确认')
    result.dialogs[0].onNegativeClick()
  } finally { await result.cleanup() }
})

test('追加目标冲突在确认中显示且不自动重发，原任务和音频保持原状', async t => {
  const data = acceptingFixture(), result = await render(data), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    return { ok: false, status: 409, json: async () => ({ code: 'TTS_QUEUE_TARGET_CONFLICT', message: '本句已排队到其他配音槽，请完成后再重做' }) } as any
  })
  try {
    await result.page.synthesize([3])
    assert.equal(await result.dialogs[0].onPositiveClick(), false)
    assert.equal(mutations(calls).length, 1)
    assert.match(await confirmationHtml(result.dialogs[0]), /本句已排队到其他配音槽/)
    assert.equal(result.page.task.job_id, 73)
    assert.equal(result.page.serverState.active_progress, .4)
    assert.equal(data.lines[2].wav.exists, true)
    assert.equal(result.page.acceptedSubmission, null)
    result.dialogs[0].onNegativeClick()
  } finally { await result.cleanup() }
})

test('追加确认期间其他句生成新槽不会阻止未变化句子入队，仍锁定原任务编号', async t => {
  const data = acceptingFixture(), result = await render(data), calls: any[] = []
  t.mock.method(result.nav, 'poll', async () => {})
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    return String(url).endsWith('/synthesize')
      ? { ok: true, json: async () => ({ status: 'queued', job_id: 73, request_id: 'same-task', lines: [{ id: 3, take: 1, status: 'queued' }] }) } as any
      : workbenchResponse(data, url)
  })
  try {
    await result.page.synthesize([3])
    result.page.serverState.lines[0].versions.push({ id: 2, url: '/new-take.wav' })
    result.page.serverState.synthesis_slots.push(2)
    await result.dialogs[0].onPositiveClick()
    assert.equal(mutations(calls).length, 1)
    const body = JSON.parse(mutations(calls)[0].options.body)
    assert.equal(body.append_to_job_id, 73)
    assert.deepEqual(body.line_ids, [3])
    assert.deepEqual(body.takes, { 3: 1 })
    assert.deepEqual(body.expected.slots, [1], '确认所见的槽目录仍交由后端原子核对目标占用')
  } finally { await result.cleanup() }
})

test('显式加入待配音只展示合成主状态，不重复标成参数更新；真正精调仍有更新标签', async () => {
  const data = acceptingFixture()
  data.synthesis_task.lines.push({ id: 3, take: 1, status: 'queued' })
  data.lines[2].config_execution = { kind: 'generation', state: 'queued', pending_revision: 0 }
  data.lines[3].config_execution = { kind: 'config', state: 'queued', pending_revision: 0 }
  const result = await render(data)
  try {
    assert.equal(result.page.label(data.lines[2]), '等待合成')
    assert.equal(result.page.configUpdateLabel(data.lines[2]), '')
    assert.equal(result.page.configUpdateLabel(data.lines[3]), '参数更新待处理')
    assert.equal((result.html.match(/class="config-update-label"/g) ?? []).length, 1)
  } finally { await result.cleanup() }
})

test('加入不同目标槽时等待状态披露目标，完成前仍保留当前试听槽；同槽不重复标注', async () => {
  const data = acceptingFixture()
  data.synthesis_task.lines.push({ id: 3, take: 2, status: 'queued' })
  const result = await render(data, page => { page.sel = 3 })
  try {
    assert.equal(result.page.label(data.lines[2]), '等待合成 · 目标槽 2')
    assert.equal(result.page.label(data.lines[1]), '等待合成')
    assert.equal(data.lines[2].selection.take, 1)
    assert.equal(result.page.serverState.lines[2].selection.take, 1)
    assert.match(result.html, /等待合成 · 目标槽 2/)
  } finally { await result.cleanup() }
})

test('保存参数立即读取工作台并取代旧轮询，慢导航不阻塞配音待更新状态', async t => {
  const data = fixture(2, 2), result = await render(data), reads: { signal: AbortSignal; finish: (response: any) => void }[] = []
  const latest = structuredClone(data)
  Object.assign(latest.lines[0].wav, { settings_changed: true })
  let finishNavigation: (() => void) | undefined
  t.mock.method(result.nav, 'poll', () => new Promise<void>(resolve => { finishNavigation = resolve }))
  t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
    assert.match(String(url), /\/workbench\?probe=false$/)
    return new Promise(resolve => reads.push({ signal: options.signal, finish: resolve }))
  })
  try {
    const oldRead = result.page.load(false)
    assert.equal(reads.length, 1)
    const revision = result.page.serviceRevision
    const saved = result.page.parametersSaved()
    assert.equal(reads.length, 2, '保存事件直接发起读取，不等待轮询定时器')
    assert.equal(reads[0].signal.aborted, true, '保存前开始的旧读取立即取消')
    assert.equal(reads[1].signal.aborted, false)
    assert.equal(result.page.serviceRevision, revision + 1)
    assert.ok(finishNavigation, '导航与配音状态同时刷新')
    reads[1].finish(workbenchResponse(latest, '/workbench?probe=false'))
    await new Promise<void>(resolve => setImmediate(resolve))
    assert.equal(result.page.filters.find((item: any) => item.value === 'settings').count, 1,
      '导航仍未完成时，配音列表和计数已应用新状态')
    reads[0].finish(workbenchResponse(data, '/workbench?probe=false'))
    await oldRead
    assert.equal(result.page.filters.find((item: any) => item.value === 'settings').count, 1,
      '取消的旧读取即使返回，也不能撤销新参数提示')
    finishNavigation()
    await saved
  } finally { await result.cleanup() }
})

test('文字保存立刻使成片对照进入核对中并重读，旧轮询和慢导航不能留下旧一致结论', async t => {
  const data = fixture(2, 2), result = await render(data)
  const comparison = { baseline: { version_id: 'old', name: '旧成片', artifact_id: 1 }, coverage: 'known',
    sentences: [{ id: 1, status: 'unchanged' }, { id: 2, status: 'unchanged' }] }
  result.page.assemblyComparison = comparison
  const latest = structuredClone(data)
  latest.lines[0].source_text = 'Corrected English sentence'
  const reads: { signal: AbortSignal; finish: (value: any) => void }[] = []
  let finishNavigation: (() => void) | undefined
  t.mock.method(result.nav, 'poll', () => new Promise<void>(resolve => { finishNavigation = resolve }))
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => new Promise(resolve => reads.push({ signal: options.signal, finish: resolve })))
  try {
    const oldRead = result.page.load(false), before = result.page.assemblyRevision
    const saved = result.page.textSaved()
    assert.equal(result.page.assemblyReading, 'loading')
    assert.notEqual(result.page.assemblyRevision, before, '不等待工作台或导航返回就强制重新核对')
    assert.equal(result.page.assemblyComparison.baseline.name, '旧成片', '保留membership供筛选阅读位置恢复，不再把它作为当前结论')
    assert.equal(reads.length, 2)
    assert.equal(reads[0].signal.aborted, true)
    reads[1].finish(workbenchResponse(latest, '/workbench?probe=false'))
    await new Promise<void>(resolve => setImmediate(resolve))
    assert.equal(result.page.state.lines[0].source_text, 'Corrected English sentence')
    reads[0].finish(workbenchResponse(data, '/workbench?probe=false')); await oldRead
    assert.equal(result.page.state.lines[0].source_text, 'Corrected English sentence')
    finishNavigation!(); await saved
  } finally { await result.cleanup() }
})

test('仅字幕样式保存也强制核对，工作台句子未变化不能吞掉刷新', async t => {
  const data = fixture(2, 2), result = await render(data)
  t.mock.method(globalThis, 'fetch', async () => workbenchResponse(data, '/workbench?probe=false'))
  try {
    const before = result.page.assemblyRevision
    await result.page.subtitlesRefreshed()
    assert.notEqual(result.page.assemblyRevision, before)
    assert.equal(result.page.assemblyReading, 'loading')
  } finally { await result.cleanup() }
})

test('旧成片英文手工校对进入成片待核对候选，不伪称成片差异或配音待更新', async () => {
  const result = await render(fixture(2, 2), page => {
    page.assemblyComparison = { baseline: { version_id: 'old', name: '旧成片', artifact_id: 1 }, coverage: 'unknown',
      sentences: [{ id: 1, status: 'unknown' }, { id: 2, status: 'unknown' }],
      manual_candidates: [{ id: 1, fields: ['source_text'], reason: '手工修改待核对' }] }
    page.stateFilter = 'assembly'
    page.sel = 1
  })
  try {
    assert.equal(result.page.filters.find((row: any) => row.value === 'assembly').label, '成片待核对')
    assert.equal(result.page.filters.find((row: any) => row.value === 'assembly').count, 1)
    assert.deepEqual(result.page.shownLines.map((line: any) => line.id), [1])
    assert.equal(result.page.filters.find((row: any) => row.value === 'settings').count, 0)
    assert.match(result.html, /assembly-pending-label[^>]*>成片待核对<\/span>/)
    assert.match(result.html, /旧成片缺少完整逐句快照/)
  } finally { await result.cleanup() }
})

test('对照刷新时保留筛选成员和标签入口，不继续展示上次差异的行状态', async () => {
  const result = await render(fixture(2, 2), page => {
    page.assemblyComparison = { baseline: { version_id: 'old', name: '旧成片', artifact_id: 1 }, coverage: 'known',
      sentences: [{ id: 1, status: 'changed' }, { id: 2, status: 'unchanged' }] }
    page.assemblyReading = 'loading'; page.stateFilter = 'assembly'
  })
  try {
    assert.equal(result.page.filters.find((row: any) => row.value === 'assembly').label, '成片核对中')
    assert.equal(result.page.filters.find((row: any) => row.value === 'assembly').count, 1)
    assert.deepEqual(result.page.shownLines.map((line: any) => line.id), [1])
    assert.doesNotMatch(result.html, /<span[^>]+class="assembly-pending-label"/)
  } finally { await result.cleanup() }
})
