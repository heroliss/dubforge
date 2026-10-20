import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, reactive, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { updateStages, updateDuration, updateDifferences, updateIsActive, updateProgress, UPDATE_STATUS } from '../src/utils/publicationUpdate.ts'
import { videoFileInfo } from '../src/utils/publishVideo.ts'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/PublicationUpdatePanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('原稿更新测试禁止未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
const publication = () => ({ id: 'source', project_uid: 'project', status: 'rejected', revision: 3,
  receipt: { bvid: 'BV1234567890' }, snapshot: { account_name: '旅行账号', settings: { title: '旧标题' } } })
const context = () => ({ source: { id: 'source', revision: 3, bvid: 'BV1234567890', account_name: '旅行账号', title: '旧标题' },
  remote: { settings: { title: 'B站现有标题', description: '原简介', tags: ['旅行'] }, videos: [
    { cid: 11, title: '原声版', duration: 100 }, { cid: 22, title: '配音版', duration: 100 }] },
  candidates: [{ id: 44, name: '原声修订成片', duration: 95, exists: true, review_ready: true },
    { id: 55, name: '配音修订成片', duration: 95, exists: false, review_ready: false }],
  suggested_replacements: [{ old_cid: 11, artifact_id: 44 }],
  project_values: { title: '项目新标题', description: '项目新简介', tags: ['历史'], season_id: 1, section_id: 2 },
  chapter_options: { enabled: true, translate: true, permanent: true },
  metadata: { seasons: [{ id: 1, title: '旅行合集', sections: [{ id: 2, title: '第一辑' }] }], declarations: [] }, updates: [] })
const plan = (status = 'prepared') => ({ id: 'update-1', status, fingerprint: 'snapshot-fingerprint', bvid: 'BV1234567890',
  preview: { parts: [{ old_cid: 11, title: '原声版', replaced: true, artifact_name: '原声修订成片', old_duration: 100, new_duration: 95,
    timeline_note: '按新成片裁剪映射时间' }, { old_cid: 22, title: '配音版', replaced: false }],
    before: { title: 'B站现有标题' }, after: { title: '项目新标题' }, chapters: { enabled: true, parts: [
      { old_cid: 11, title: '原声版', timeline_note: '已移除广告段', items: [{ title: '历史开端', start_time: 1 }] }] }, warnings: [] },
  execution: { stages: { upload: { status: 'completed' }, edit: { status: 'completed' }, review: { status: 'completed' },
    chapters: { status: 'failed', error: '章节暂未写入' }, collection: { status: 'pending' }, verify: { status: 'pending' } } } })
async function render(seed: (page: any) => void = () => {}, extra: any = {}) {
  let page: any
  const options = reactive({ publication: publication(), ...extra }), events: any[] = []
  const app = createSSRApp({ render: () => h(Panel, { ...options,
    onBusyChange: (value: boolean) => events.push(['busy', value]), onDirtyChange: (value: boolean) => events.push(['dirty', value]),
    onActiveChange: (value: boolean) => events.push(['active', value]), onChanged: () => events.push(['changed']), onAutomaticUpdate:()=>events.push(['automatic-update']) }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'PublicationUpdatePanel') { page = this.$.setupState; seed(page) } } })
  const html = await renderToString(app)
  return { html, page, events, options }
}
function installFetch(t: any, handler: (path: string, body: any) => any) {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const path = String(url), body = options?.body ? JSON.parse(String(options.body)) : null
    requests.push({ path, method: options?.method || 'GET', body })
    return { ok: true, json: async () => handler(path, body) }
  })
  return requests
}

test('保留BV更新是独立主入口，打开记录不会自动调用远端或新建草稿', async () => {
  const result = await render()
  assert.match(result.html, /更新原稿件/); assert.match(result.html, /保留原BV/)
  assert.doesNotMatch(result.html, /创建新BV|确认更新原稿件/)
  assert.equal(result.page.context, null); assert.equal(result.page.task, null)
})

test('需要重新制作时返回发布步骤设置交付目标，活动或已编辑方案不允许绕过',async()=>{
  const result=await render();assert.match(result.html,/设置项目交付目标/)
  result.page.openAutomaticUpdate();assert.deepEqual(result.events.filter(item=>item[0]==='automatic-update'),[['automatic-update']])
  result.page.edited=true;result.page.openAutomaticUpdate();assert.equal(result.events.filter(item=>item[0]==='automatic-update').length,1)
  result.page.edited=false;result.page.task=plan('uploading');result.page.openAutomaticUpdate();assert.equal(result.events.filter(item=>item[0]==='automatic-update').length,1)
})
test('已有成片是主入口，四个更新范围与唯一检查入口同处一份表单', async () => {
  const closed = await render()
  const buttons = [...closed.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0])
  assert.match(buttons[0], /n-button--primary-type/)
  assert.match(buttons[0], /选择现有成片与资料/)
  assert.match(buttons[1], /设置项目交付目标/)
  assert.doesNotMatch(buttons[1], /n-button--primary-type/)
  assert.doesNotMatch(closed.html, /<small>原稿更新<\/small>/)
  const result = await render(p => { p.seed(context()); p.expanded = true; p.selectField('title', true) })
  for (const scope of ['videos', 'copy', 'chapters', 'collection']) assert.match(result.html, new RegExp(`data-scope="${scope}"`))
  assert.deepEqual(result.page.scopeSummary, ['1 个分P', '1 项文案', '视频章节'])
  assert.match(result.html, /P1[\s\S]*将替换/)
  assert.match(result.html, /P2[\s\S]*保留原视频/)
  assert.match(result.html, /先勾选要替换的分P，再为它选择新成片/)
  assert.match(result.html, /替换此P/)
  assert.match(result.html, /本次更新范围/)
  assert.match(result.html, /其余内容保留；下一步核对方案后再确认提交/)
  const actions = [...result.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0].replace(/<[^>]*>/g, '').trim())
  assert.equal(actions.filter(label => label === '检查更新方案').length, 1)
  assert.doesNotMatch(result.html, /选择现有成片与资料/)
})
test('读取上下文后逐P保留或替换，友好显示合集名，缺失产物禁用', async t => {
  const requests = installFetch(t, () => context()), result = await render()
  await result.page.openEditor()
  assert.equal(requests.length, 1); assert.equal(requests[0].method, 'GET')
  assert.deepEqual(result.page.selectedReplacements, [{ old_cid: 11, artifact_id: 44 }])
  assert.equal(result.page.values.title, 'B站现有标题'); assert.deepEqual(result.page.selectedFields, [])
  assert.equal(result.page.candidateOptions[1].disabled, true)
  assert.equal(result.page.seasonOptions[1].label, '旅行合集'); assert.equal(result.page.sectionOptions[0].label, '第一辑')
  result.page.selectReplacement(11, null); assert.deepEqual(result.page.selectedReplacements, [])
  result.page.toggleReplacement(22, true); assert.equal(result.page.canPrepare, false)
  result.page.selectReplacement(22, 44); assert.deepEqual(result.page.selectedReplacements, [{ old_cid: 22, artifact_id: 44 }])
  result.page.toggleReplacement(22, false); assert.deepEqual(result.page.selectedReplacements, [])
})

test('替换文件以成功时设置名和生成时间区分，搜索保留文件名且选择后完整展示', async () => {
  const value=context()
  Object.assign(value.candidates[0],{name:'final_version_partial_abc.mp4',version_name:'当前重命名',version_generated_name:'原声 · 修订',
    generated_at:'2026-10-17T12:34:56Z',created_at:'2026-10-01T00:00:00Z',version_summary:'原声 · 双语字幕 · 无评论'})
  const {page,html}=await render(p=>{p.expanded=true;p.seed(value)})
  const option=page.candidateOptions[0]
  assert.match(option.label,/原声 · 修订.*生成时间.*原声 · 双语字幕 · 无评论.*final_version_partial_abc\.mp4/)
  assert.equal(option.value,44);assert.equal(option.disabled,false)
  assert.notEqual(page.renderCandidateLabel(option,true).props.compact,true,'下拉中已选项目也要保留完整时间和设置')
  assert.equal(page.renderCandidateTag({option}).props.compact,true,'收起后的输入框用独立紧凑标题')
  assert.equal(page.replacementFile(11).id,44)
  assert.match(html,/原声 · 修订/);assert.match(html,/原声 · 双语字幕 · 无评论/)
  assert.ok(html.includes(videoFileInfo(value.candidates[0]).timeText))
  assert.match(html,/当前版本名：当前重命名/);assert.match(html,/final_version_partial_abc\.mp4/)
  assert.doesNotMatch(option.label,/Invalid Date|0:00|未知时长/)
  const other={...value.candidates[0],id:66,generated_at:'2026-10-17T13:34:56Z'}
  page.context.candidates=[...value.candidates,other]
  assert.notEqual(page.candidateOptions[0].label,page.candidateOptions[2].label)
  page.selectReplacement(11,66);assert.deepEqual(page.selectedReplacements,[{old_cid:11,artifact_id:66}])
})
test('检查方案仅保存本地计划；统一确认一次才启动，双击不重复发送', async t => {
  let release: (value: any) => void = () => {}
  const requests = installFetch(t, (path) => path.endsWith('/plan') ? plan() : new Promise(resolve => { release = resolve }))
  const result = await render(p => { p.expanded = true; p.seed(context()) })
  result.page.adoptProjectCopy()
  await result.page.prepare()
  assert.equal(requests.length, 1); assert.equal(requests[0].path, '/api/publishing/publications/source/updates/plan')
  assert.deepEqual(requests[0].body.replacements, [{ old_cid: 11, artifact_id: 44 }])
  assert.equal(requests[0].body.values.title, '项目新标题')
  assert.equal(result.page.task.status, 'prepared')
  const first = result.page.perform('start'); await result.page.perform('start')
  release(plan('uploading')); await first
  assert.equal(requests.length, 2); assert.deepEqual(requests[1], { path: '/api/publishing/updates/update-1/actions', method: 'POST',
    body: { action: 'start', confirmed: true, fingerprint: 'snapshot-fingerprint' } })
  assert.equal(result.page.task.bvid, 'BV1234567890')
})
test('检查结果展示新旧时长、裁剪时间轴与待整理章节，不误称已经合规或写入', async () => {
  const result = await render(p => { p.seed(context()); p.task = plan(); p.expanded = true })
  assert.match(result.html, /确认本次范围/); assert.match(result.html, /1:40 → 1:35/)
  assert.match(result.html, /按新成片裁剪映射时间/); assert.match(result.html, /按新成片时间轴待整理的章节/)
  assert.match(result.html, /已移除广告段/); assert.match(result.html, /历史开端/); assert.match(result.html, /确认更新原稿件/)
  assert.doesNotMatch(result.html, /本次更新已核验|视频章节已设置/)
})
test('视频已提交但章节失败时只重试后续步骤，六个阶段状态独立', async t => {
  const result = await render(p => { p.task = plan('failed'); p.expanded = true })
  assert.match(result.html, /视频或资料已提交，后续步骤未完成/); assert.match(result.html, /仅重试未完成步骤/)
  assert.match(result.html, /章节暂未写入/); assert.equal(result.page.stages.length, 6)
  for (const [type, text] of [['success', '已完成'], ['error', '未完成'], ['default', '待执行']]) {
    assert.match(result.html, new RegExp(`class="n-text update-stage-state"[^>]*data-status-type="${type}"[^>]*>${text}`))
  }
  const requests = installFetch(t, () => plan('queued'))
  await result.page.perform('retry')
  assert.equal(requests.length, 1); assert.equal(requests[0].body.action, 'retry')
  assert.ok(!requests.some(request => /draft|copy|upload\b/.test(request.path)))
})
test('结果未知时只有核对，不允许开始或重试，网络中断后也不盲目重发', async t => {
  const result = await render(p => { p.task = plan('unknown'); p.expanded = true })
  assert.match(result.html, /核对平台结果/); assert.doesNotMatch(result.html, /仅重试未完成步骤|确认更新原稿件/)
  const requests = installFetch(t, () => plan('waiting'))
  await result.page.perform('start'); await result.page.perform('retry'); assert.equal(requests.length, 0)
  await result.page.perform('reconcile'); assert.equal(requests[0].body.action, 'reconcile')
  const timeout = await render(p => { p.task = plan() })
  t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('网络已中断') })
  await timeout.page.perform('start'); assert.equal(timeout.page.requestUnknown, true)
  await timeout.page.perform('start'); assert.equal(timeout.page.requestUnknown, true)
})
test('明确选择移出合集使用null目标，未选择合集不阻止只替换视频', async t => {
  const result = await render(p => { p.seed(context()); p.expanded = true })
  result.page.changeScope('collection', true); result.page.selectSeason(0)
  assert.equal(result.page.canPrepare, true)
  const requests = installFetch(t, () => plan())
  await result.page.prepare(); assert.deepEqual(requests[0].body.collection, { enabled: true, season_id: null, section_id: null })
})
test('锁定后迟到输入不修改已确认计划，显示保存任务可供状态核对', async () => {
  const result = await render(p => { p.seed(context()); p.task = plan() })
  result.page.selectReplacement(11, 55); result.page.setValue('title', '错误修改'); result.page.chapterOption('instruction', '变化')
  assert.equal(result.page.replaceMap['11'], 44); assert.equal(result.page.values.title, 'B站现有标题'); assert.equal(result.page.instruction, '')
  result.page.revise(); assert.equal(result.page.task, null); assert.equal(result.page.edited, true)
  await nextTick()
})
test('格式化缺失值不伪造时长，状态对象和数组均保留真实完成与跳过', () => {
  assert.equal(updateDuration(null), '时长待读取'); assert.equal(updateDuration(3601), '1:00:01')
  assert.deepEqual(updateDifferences(plan()).map(row => row.label), ['标题'])
  assert.equal(updateStages({ execution: { stages: [{ key: 'upload', status: 'completed', progress: 100 }] } })[0].text, '已完成')
  assert.equal(updateStages({ execution: { stages: { chapters: { status: 'skipped' } } } })[3].text, '不在本次范围')
})
test('原稿更新显示整项进度，并从章节分P状态推导可读的阶段进度', () => {
  const row: any = plan('delivering')
  row.progress = 50
  row.execution.stages.chapters = { status: 'running' }
  row.execution.delivery = { parts: [
    { cid: 33, status: 'completed' }, { cid: 44, status: 'running' }, { cid: 55, status: 'pending' },
  ], collection: { status: 'pending' }, execution: { schema: 1, state: 'running', completed: 2, total: 4, unit: '章' } }
  const stages = updateStages(row)
  assert.equal(stages.find(item => item.key === 'chapters')?.progress, 50)
  assert.equal(stages.find(item => item.key === 'chapters')?.estimated, true)
  const summary = updateProgress(row)
  assert.equal(summary.completed, 3)
  assert.equal(summary.active?.key, 'chapters')
  assert.ok(summary.percentage !== null && summary.percentage > 0 && summary.percentage < 100)
})
test('后续同步阶段持续轮询并锁定重复更新入口，不归为前台正在写请求', async () => {
  assert.equal(UPDATE_STATUS.delivering, '正在同步后续设置')
  assert.equal(updateIsActive({ status: 'delivering', busy: false }), true)
  const result = await render(p => { p.task = plan('delivering'); p.expanded = true })
  assert.match(result.html, /正在同步后续设置/); assert.equal(result.page.active, true)
  assert.equal(result.page.writing, false); assert.equal(result.page.edited, false)
  assert.doesNotMatch(result.html, /确认更新原稿件|准备另一次更新/)
  await result.page.perform('start'); assert.equal(result.page.task.status, 'delivering')
})
test('章节执行结果显示实际整理后的清单，与计划中的候选标题区分', async () => {
  const result = await render(p => { const row: any = plan('completed'); row.execution.delivery = { parts: [
    { cid: 33, title: '原声版', status: 'completed', items: [{ title: '模型整理后的标题', start_time: 1 }] }] }; p.task = row })
  assert.match(result.html, /本次已整理章节/); assert.match(result.html, /模型整理后的标题/)
  assert.doesNotMatch(result.html, /按新成片时间轴待整理的章节|历史开端/)
})
test('已有任务完成后重新读取新CID和当前文案，不复用旧分P映射准备下一次更新', async t => {
  const next: any = context(); next.remote.videos = [{ cid: 99, title: '原声版', duration: 95 }]
  next.suggested_replacements = [{ old_cid: 99, artifact_id: 44 }]; next.updates = [plan('completed')]
  const requests = installFetch(t, () => next), result = await render(p => { p.seed(context()); p.task = plan('completed') })
  await result.page.newAttempt()
  assert.equal(result.page.task, null); assert.deepEqual(result.page.selectedReplacements, [{ old_cid: 99, artifact_id: 44 }])
  assert.equal(requests.length, 1); assert.equal(requests[0].method, 'GET')
})
test('能力说明全部中文，平台禁用替换时仍可选择受支持的资料和章节更新', async () => {
  const source: any = context(); source.remote.edit_supported = false; source.remote.edit_limitation = '特殊稿件请到官网处理'
  source.capabilities = { field_capabilities: Object.fromEntries(['copyright', 'source', 'close_comments', 'close_danmaku', 'topic',
    'watermark', 'subtitles', 'dolby', 'lossless_music', 'commercial_promotion'].map(key => [key, { write: 'read_only', reason: '需官网处理' }])) }
  const result = await render(p => { p.seed(source); p.expanded = true })
  assert.equal(result.page.replaceVideos, false); assert.match(result.html, /特殊稿件请到官网处理/)
  assert.ok(result.page.unsupported.every((item: any) => item.label !== item.key)); assert.match(result.html, /平台字幕/)
  result.page.changeScope('videos', true); assert.equal(result.page.replaceVideos, false)
  result.page.selectField('title', true); assert.equal(result.page.canPrepare, true)
})
test('AI配音更新明确声明作用范围与保留行为，不擅自改变或勾选声明', async () => {
  const source: any = context(); source.candidates[0].has_dubbing = true
  const before = JSON.stringify(source)
  const result = await render(p => { p.seed(source); p.expanded = true })
  assert.match(result.html, /同一稿件全部分P共用声明/); assert.match(result.html, /未勾选“创作声明”时保留B站当前值/)
  assert.equal(result.page.selectedFields.includes('declaration_id'), false); assert.equal(JSON.stringify(source), before)
  const frozen = await render(p => { const row: any = plan(); row.preview.has_dubbing = true; p.task = row })
  assert.match(frozen.html, /本次包含AI配音/); assert.match(frozen.html, /没有声明差异时保留B站现有值/)
})
test('帮助页将保留BV换片引导至原记录更新流程，区分另发新BV与章节新时间轴', async () => {
  const HelpView = (await server.ssrLoadModule('/src/views/HelpView.vue')).default
  const app = createSSRApp({render:()=>h(HelpView)}); setup(app)
  const html = await renderToString(app)
  assert.match(html,/到项目「发布到B站」保存原稿目标、替换分P和同步范围，预检后确认执行/)
  assert.match(html,/章节按新成片时间轴处理/); assert.match(html,/另发新稿会创建新BV/)
  assert.doesNotMatch(html,/需要替换原BV视频请到B站创作中心/)
})
