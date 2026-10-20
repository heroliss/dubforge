import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { darkTheme, NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { executionDuration, executionStatusType, normalizeExecution, executionForStep, executionForWorkflow, workflowCurrentStep,
  workflowExecutionStep, executionForPublication, executionForChapter, executionForComment } from '../src/utils/executionProgress.ts'
import { runningWorkflowStep } from './fixtures/workflowExecution.ts'

let server: ViteDevServer, Progress: any, Subtitle: any, useNav: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Progress = (await server.ssrLoadModule('/src/components/ExecutionProgress.vue')).default
  Subtitle = (await server.ssrLoadModule('/src/components/SubtitleExport.vue')).default
  useNav = (await server.ssrLoadModule('/src/stores/nav.ts')).useNavStore
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止真实业务请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })
async function render(props: any, dark = false) {
  const app = createSSRApp({ render: () => h(NConfigProvider, { theme: dark ? darkTheme : null }, { default: () => h(Progress, props) }) }); setup(app)
  return renderToString(app)
}
const base = { schema: 1, state: 'running', phase: 'model_wait', label: '等待模型首字', detail: '当前分块 2 / 10' }

test('执行状态使用统一语义色，等待与未知值保持中性，不改变业务状态', () => {
  const groups = {
    success: ['completed', 'done', 'success'], info: ['running', 'active', 'uploading', 'submitting'],
    warning: ['attention', 'partial', 'rejected', 'unknown', 'paused', 'retrying', 'pausing', 'stopping'],
    error: ['failed', 'error', 'interrupted'],
    default: ['queued', 'pending', 'waiting', 'monitoring', 'cancelled', 'stopped', 'skipped', 'future-state', '', null, undefined],
  }
  for (const [type, values] of Object.entries(groups)) {
    for (const value of values) assert.equal(executionStatusType(value), type, String(value))
  }
  const unknown = normalizeExecution({ ...base, state: 'future-state', history: [{ state: 'future-state' }] })!
  assert.equal(unknown.state, 'running'); assert.equal(unknown.stateLabel, '进行中')
  assert.equal(unknown.statusType, 'default'); assert.equal(unknown.history[0].statusType, 'default')
})

test('当前状态和最近过程都按各自状态着色，完成与失败不沿用进行中的颜色', async () => {
  const html = await render({ execution: { ...base, state: 'completed', history: [
    { label: '阶段一', state: 'completed' }, { label: '阶段二', state: 'running' },
    { label: '阶段三', state: 'attention' }, { label: '阶段四', state: 'failed' },
    { label: '阶段五', state: 'waiting' },
  ] } })
  assert.match(html, /class="n-text execution-state"[^>]*data-status-type="success"[^>]*>已完成</)
  for (const [type, label] of [['success', '已完成'], ['info', '进行中'], ['warning', '需要处理'], ['error', '执行失败'], ['default', '等待中']]) {
    assert.match(html, new RegExp(`class="n-text execution-history-state"[^>]*data-status-type="${type}"[^>]*>${label}<`))
  }
  assert.match(html, /--execution-accent:#18a058/)
  const failed = await render({ execution: { ...base, state: 'failed' } })
  assert.match(failed, /--execution-accent:#d03050/)
  const waiting = await render({ execution: { ...base, state: 'waiting' } })
  assert.match(waiting, /--execution-accent:rgb\(118, 124, 130\)/)
})

test('状态文字和卡片强调色共同跟随浅色与深色主题，不互相覆盖', async () => {
  for (const dark of [false, true]) for (const state of ['completed', 'running', 'attention', 'failed', 'waiting']) {
    const html = await render({ execution: { ...base, state, history: [{ state }] } }, dark)
    const accent = html.match(/--execution-accent:([^;]+)/)![1]
    const statuses = [...html.matchAll(/<span class="n-text execution-(?:state|history-state)" style="([^"]+)"/g)]
    assert.equal(statuses.length, 2)
    for (const item of statuses) assert.ok(item[1].includes(`--n-text-color:${accent};`), `${dark ? '深' : '浅'}色 ${state}`)
  }
})

test('执行事实有界且保留零值，未知诊断字段不会进入可见指标', () => {
  const view = normalizeExecution({ ...base, completed: 0, total: 10, elapsed_seconds: 65, inactive_seconds: 25,
    history: Array.from({ length: 40 }, (_, index) => ({ label: `阶段 ${index}`, state: 'completed' })),
    metrics: { model: '测试模型', output_chars: 0, api_key: '不要展示', response: { private: true }, request: Infinity } })!
  assert.equal(view.completed, 0); assert.equal(view.elapsed, '1 分 5 秒'); assert.equal(view.inactive, '25 秒')
  assert.equal(view.history.length, 24); assert.equal(view.history[0].label, '阶段 16')
  assert.deepEqual(view.metrics.map(row => row.key), ['model', 'output_chars'])
  assert.equal(normalizeExecution({ schema: 2, label: '未来协议' }), null)
  assert.equal(executionDuration(NaN), ''); assert.equal(executionDuration(-1), '')
  assert.equal(executionDuration(3725), '1 小时 2 分 5 秒')
  assert.equal(normalizeExecution({...base,next_check_at:1e100})?.nextCheck,'')
  assert.match(normalizeExecution({...base,next_check_at:1800000000})?.nextCheck || '',/^2027-/)
  assert.equal(normalizeExecution({schema:1,state:'completed'})?.label,'当前任务已完成')
  const transfer=normalizeExecution({...base,metrics:{loaded_bytes:2*1024*1024,speed_bytes_per_second:2048,device:'cuda:0',duration_factor:.85,source_lines:12}})!
  assert.equal(transfer.metrics.find(row=>row.key==='loaded_bytes')?.value,'2.0 MB')
  assert.equal(transfer.metrics.find(row=>row.key==='speed_bytes_per_second')?.value,'2.0 KB/秒')
  assert.equal(transfer.metrics.find(row=>row.key==='device')?.value,'GPU 0')
})

test('等待GPU资源解释排队，不把正常等待渲染成长期无活动或模型执行', async () => {
  const execution = { schema: 1, state: 'queued', phase: 'queue', label: '等待执行资源', elapsed_seconds: 180, inactive_seconds: 180, metrics: { resource: 'gpu' } }
  const current = normalizeExecution(execution)!
  assert.equal(current.inactive, '')
  assert.match(current.detail, /等待 GPU 计算资源.*自动开始/)
  assert.deepEqual(current.metrics, [{ key: 'resource', label: '等待资源', value: 'GPU 计算资源' }])
  const html = await render({ execution, percentage: 0 })
  assert.match(html, /已等待 3 分 0 秒/)
  assert.doesNotMatch(html, /距进度更新|所需资源|>gpu<|0%|aria-valuenow/)
  const actual = normalizeExecution({ ...execution, state: 'running', detail: '模型正在生成' })!
  assert.equal(actual.detail, '模型正在生成'); assert.equal(actual.inactive, '3 分 0 秒')
})

const afterQueue = { schema: 1, state: 'running', phase: 'synthesizing', label: '处理第 9 句配音',
  started_at: '2026-10-20T00:00:00Z', updated_at: '2026-10-20T00:35:00Z', inactive_seconds: 2,
  elapsed_seconds: 2102, completed: 8, total: 646, unit: '句', history: [
    { phase: 'queue', label: '等待执行资源', state: 'completed', started_at: '2026-10-20T00:00:00Z', finished_at: '2026-10-20T00:34:00Z' },
    { phase: 'prepare', state: 'completed', started_at: '2026-10-20T00:34:00Z', finished_at: '2026-10-20T00:34:02Z' },
    { phase: 'synthesizing', label: '处理第 9 句配音', state: 'running', started_at: '2026-10-20T00:34:02Z', finished_at: null },
  ] }

test('排队后的阶段时间使用服务端历史和快照时钟，不从两种elapsed口径扣除排队或使用浏览器时钟', () => {
  for (const elapsed_seconds of [62, 2102, undefined]) {
    const execution = { ...afterQueue, elapsed_seconds }, original = structuredClone(execution)
    const view = normalizeExecution(execution, 5.9)!
    assert.equal(view.phaseElapsed, '1 分 5 秒')
    assert.equal(view.priorQueueElapsed, '34 分 0 秒')
    assert.equal(view.completed, 8); assert.equal(view.total, 646)
    assert.deepEqual(execution, original)
  }
  assert.equal(normalizeExecution({ ...afterQueue, updated_at: '2026-10-20T08:35:00+08:00' })?.phaseElapsed, '1 分 0 秒')
})

test('排队交接在普通与紧凑卡片中区分本阶段和此前排队，不把累计时间冒充配音执行时间', async () => {
  for (const compact of [false, true]) {
    const html = await render({ execution: afterQueue, compact, eta: '当前阶段预计剩余约 2 小时' })
    assert.match(html, /本阶段已用 1 分 0 秒/)
    assert.match(html, /此前排队 34 分 0 秒/)
    assert.match(html, /已完成 8 \/ 646 句/)
    assert.match(html, /当前阶段预计剩余约 2 小时/)
    assert.doesNotMatch(html, /(?:累计)?已用 35 分 2 秒/)
    assert.equal((html.match(/class="execution-facts"/g) || []).length, 1)
  }
  const fallback = await render({ execution: { ...base, elapsed_seconds: 2102 } })
  assert.match(fallback.replace(/<!--[\s\S]*?-->/g, ''), /累计已用 35 分 2 秒（含等待）/)
  assert.doesNotMatch(fallback, /本阶段已用|此前排队/)
})

test('阶段时钟缺失、历史已结束或不一致时不补造活动计时，旧轮次排队不混入本次', () => {
  for (const changed of [
    { updated_at: undefined }, { inactive_seconds: undefined }, { inactive_seconds: Infinity },
    { updated_at: '2026-10-20T00:35:00' }, { updated_at: '无效时间' },
    { updated_at: '2026-10-20T00:30:00Z' }, { phase: 'other' },
    { started_at: '2026-10-20T00:36:00Z' },
    { history: afterQueue.history.map(row => row.phase === 'synthesizing' ? { ...row, finished_at: '2026-10-20T00:35:00Z' } : row) },
    { history: afterQueue.history.map(row => row.phase === 'synthesizing' ? { ...row, state: 'failed' } : row) },
  ]) assert.equal(normalizeExecution({ ...afterQueue, ...changed })?.phaseElapsed, '')
  const resumed = normalizeExecution({ ...afterQueue, started_at: '2026-10-20T00:34:01Z' })!
  assert.equal(resumed.phaseElapsed, '1 分 0 秒'); assert.equal(resumed.priorQueueElapsed, '')
  for (const state of ['paused', 'completed', 'failed', 'cancelled', 'unknown']) {
    assert.equal(normalizeExecution({ ...afterQueue, state }, 100)?.phaseElapsed, '')
  }
  const queued = normalizeExecution({ ...afterQueue, state: 'queued' }, 100)!
  assert.equal(queued.phaseElapsed, ''); assert.equal(queued.priorQueueElapsed, '')
})

const encodingVersion = { ...afterQueue, phase: 'encoding', label: '编码成片画面、音频与字幕',
  completed: 1444.01, total: 1726.92, unit: '成片秒', history: [
    { phase: 'checking_version', label: '核对成片版本 1/2：配音', state: 'completed',
      started_at: '2026-10-20T00:34:00Z', finished_at: '2026-10-20T00:34:01Z' },
    { phase: 'encoding', state: 'running', started_at: '2026-10-20T00:34:02Z', finished_at: null },
  ] }

test('合成编码的实际阶段比例与phase ETA同范围，不把跨版本整体进度标为本阶段', async () => {
  const original = structuredClone(encodingVersion), current = normalizeExecution(encodingVersion)!
  assert.equal(Number(current.workPercentage!.toFixed(1)), 83.6)
  assert.equal(current.workProgressLabel, '当前编码阶段'); assert.equal(current.versionLabel, '当前版本 1/2：配音')
  const html = await render({ execution: encodingVersion, percentage: 41.4, estimated: true,
    eta: '当前阶段预计剩余约 2 分钟', compact: true })
  assert.match(html, /当前编码阶段 83\.6%/); assert.match(html, /当前版本 1\/2：配音/)
  assert.match(html, /当前阶段预计剩余约 2 分钟/)
  assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /41\.4%|本阶段 41|估算 83|不代表整项/)
  assert.deepEqual(encodingVersion, original)
  assert.doesNotMatch(await render({ execution: encodingVersion, percentage: null }), /class="execution-bar"/)
})

test('进入下一版本编码允许阶段进度重新开始，不用上个版本比例或名称维持单调增长', async () => {
  const next = { ...encodingVersion, completed: 17.27, updated_at: '2026-10-20T00:41:00Z', history: [...encodingVersion.history,
    { phase: 'checking_version', label: '核对成片版本 2/2：原声', state: 'completed',
      started_at: '2026-10-20T00:40:00Z', finished_at: '2026-10-20T00:40:01Z' },
    { phase: 'encoding', state: 'running', started_at: '2026-10-20T00:40:02Z', finished_at: null },
  ] }
  const html = await render({ execution: next, percentage: 50, eta: '当前阶段预计剩余约 2 分钟' })
  assert.match(html, /当前编码阶段 1%/); assert.match(html, /当前版本 2\/2：原声/)
  assert.doesNotMatch(html, /83\.6%|当前版本 1\/2|当前编码阶段 50%/)
})

test('未知编码总量不推算阶段百分比，历史缺失或旧轮次不猜版本，其他计量沿用调用方范围', async () => {
  for (const total of [null, undefined, 0, -1, NaN, Infinity, '1726.92']) {
    const execution = { ...encodingVersion, total }, view = normalizeExecution(execution)!
    assert.equal(view.workPercentage, null); assert.equal(view.workProgressLabel, '合成步骤')
    const html = await render({ execution, percentage: 41.4, indeterminate: true })
    assert.doesNotMatch(html, /class="execution-bar"|83\.6%|当前编码阶段 0%/)
  }
  for (const changed of [{ history: [] }, { started_at: undefined }, { started_at: '2026-10-20T00:34:01Z' },
    { history: encodingVersion.history.map(row => row.phase === 'checking_version' ? { ...row, label: '未知版本标签' } : row) }]) {
    const view = normalizeExecution({ ...encodingVersion, ...changed })!
    assert.equal(view.versionLabel, ''); assert.equal(Number(view.workPercentage!.toFixed(1)), 83.6)
  }
  for (const changed of [{ phase: 'verifying_audio_master' }, { phase: 'synthesizing', unit: '句' }, { unit: '字节' }]) {
    const execution = { ...encodingVersion, ...changed }, view = normalizeExecution(execution)!
    assert.equal(view.workPercentage, null); assert.equal(view.workProgressLabel, ''); assert.equal(view.versionLabel, '')
    assert.match(await render({ execution, percentage: 41.4 }), /本阶段 41\.4%/)
  }
})

test('不足一秒的已完成排队省略，仍保留真实一秒以上等待', async () => {
  for (const duration of [0, .428, .999, 1, 2]) {
    const execution = { ...afterQueue, history: afterQueue.history.map(row => row.phase === 'queue'
      ? { ...row, finished_at: new Date(Date.parse(row.started_at) + duration * 1000).toISOString() } : row) }
    const view = normalizeExecution(execution)!
    if (duration < 1) {
      assert.equal(view.priorQueueElapsed, '')
      assert.doesNotMatch(await render({ execution }), /此前排队/)
    } else assert.equal(view.priorQueueElapsed, `${duration} 秒`)
  }
})

test('本地走秒只推进耗时，完成句数、百分比和模型回报保持服务端事实', () => {
  const execution = { ...base, phase: 'tts.synthesize', label: '处理第 70 句配音', completed: 69, total: 305, unit: '句',
    elapsed_seconds: 2246, inactive_seconds: 31, metrics: { current_id: 70, sentence_elapsed_seconds: 31,
      sentence_progress: .421, sentence_attempt: 2, sentence_attempts: 3, sentence_started_at: '2026-10-20T00:00:00Z' } }
  const before = structuredClone(execution)
  const view = normalizeExecution(execution, 5.9)!
  assert.equal(view.elapsed, '37 分 31 秒'); assert.equal(view.sentenceElapsed, '36 秒')
  assert.equal(view.inactive, '36 秒'); assert.equal(view.completed, 69); assert.equal(view.total, 305)
  assert.equal(view.sentenceProgress, 42.1); assert.equal(view.sentenceAttempt, 2); assert.equal(view.sentenceAttempts, 3)
  assert.ok(view.metrics.every(row => !row.key.startsWith('sentence_')))
  assert.deepEqual(execution, before)
})

test('暂停和终态冻结计时，不继续显示当前句生成；未知或损坏的耗时不会推算', () => {
  for (const state of ['paused', 'completed', 'failed', 'interrupted', 'cancelled', 'skipped', 'attention', 'unknown']) {
    const view = normalizeExecution({ ...base, state, elapsed_seconds: 65, inactive_seconds: 25,
      metrics: { sentence_elapsed_seconds: 40, sentence_progress: .5 } }, 100)!
    assert.equal(view.elapsed, '1 分 5 秒'); assert.equal(view.inactive, '25 秒')
    assert.equal(view.clockActive, false); assert.equal(view.sentenceActive, false); assert.equal(view.sentenceProgress, null)
  }
  for (const age of [NaN, Infinity, -5]) {
    assert.equal(normalizeExecution({ ...base, elapsed_seconds: 65 }, age)?.elapsed, '1 分 5 秒')
  }
  for (const seconds of [null, undefined, -1, NaN, Infinity, '40']) {
    const view = normalizeExecution({ ...base, elapsed_seconds: seconds, metrics: { sentence_elapsed_seconds: seconds, sentence_progress: .5 } }, 40)!
    assert.equal(view.elapsed, ''); assert.equal(view.sentenceActive, false); assert.equal(view.sentenceProgress, null)
  }
})

test('单句长耗时说明与总工作量分开，模型没有回报时不补造句内百分比', async () => {
  const execution = { ...base, phase: 'tts.synthesize', label: '处理第 70 句配音', detail: '', completed: 69, total: 305,
    unit: '句', elapsed_seconds: 2246, inactive_seconds: 31, metrics: { current_id: 70, sentence_elapsed_seconds: 31 } }
  const html = await render({ execution, percentage: 22.6, compact: true })
  assert.match(html, /已完成 69 \/ 305 句/); assert.match(html, /已用 37 分 26 秒/); assert.match(html, /本句已用 31 秒/)
  assert.match(html.replace(/<!--[\s\S]*?-->/g, ''), /句数在音频生成并保存后更新；当前模型暂未回报句内进度。/)
  assert.match(html, /本阶段 22.6%/); assert.doesNotMatch(html, /本句模型进度|距最近活动|距进度更新|aria-valuenow="0"/)
  for (const sentence_progress of [NaN, Infinity, -1, 1.5, '0.5', null]) {
    const view = normalizeExecution({ ...execution, metrics: { ...execution.metrics, sentence_progress } })!
    assert.equal(view.sentenceProgress, null)
  }
})

test('真实句内回报独立显示，逐秒计时不放在屏幕阅读器的动态播报区域', async () => {
  const html = await render({ execution: { ...base, label: '处理第 70 句配音', detail: '', completed: 69, total: 305, unit: '句',
    elapsed_seconds: 2246, metrics: { sentence_elapsed_seconds: 31, sentence_progress: .421, sentence_attempt: 2, sentence_attempts: 3 } } })
  assert.match(html, /本句模型进度 42.1%/); assert.match(html, /本句第 2 \/ 3 次生成/)
  assert.doesNotMatch(html, /暂未回报句内进度/)
  const announcement = html.match(/<div class="execution-heading"[^>]*role="status"[^>]*>(.*?)<\/div>/s)?.[1] || ''
  assert.match(announcement, /处理第 70 句配音/)
  assert.doesNotMatch(announcement, /本句已用|37 分 26 秒|31 秒/)
})

test('下载当前媒体流显示本次元数据描述，不通过格式编号猜测画质或媒体类型', async () => {
  for (const stream of ['4K · 60 fps · AV1 视频流', '1080p · VP9 视频流', 'Opus 音频流 · 128 kbps']) {
    const execution = { ...base, metrics: { current_format: '平台自定义-401', current_stream: `  ${stream}  `, downloaded_bytes: 2048 } }
    const view = normalizeExecution(execution)!
    assert.deepEqual(view.metrics.find(row => row.key === 'current_stream'), { key: 'current_stream', label: '当前媒体流', value: stream })
    assert.ok(!view.metrics.some(row => row.key === 'current_format'))
    assert.equal(execution.metrics.current_format, '平台自定义-401')
    assert.equal(view.metrics.find(row => row.key === 'downloaded_bytes')?.value, '2.0 KB')
    const html = await render({ execution })
    assert.match(html, /当前媒体流/); assert.ok(html.includes(stream))
    assert.doesNotMatch(html, /当前格式|流编号|平台自定义-401/)
    if (stream.startsWith('Opus')) assert.doesNotMatch(html, /视频流/)
  }
})

test('旧下载记录和异常流描述安全回退到原始流编号，不伪造格式说明', async () => {
  for (const current_stream of [undefined, null, '', '  ', false, 401, { height: 2160 }, ['AV1']]) {
    const execution = { ...base, metrics: { current_format: ' 616 ', current_stream } }
    assert.deepEqual(normalizeExecution(execution)?.metrics, [{ key: 'current_format', label: '流编号', value: '616' }])
    const html = await render({ execution })
    assert.match(html, /流编号.*616/)
    assert.doesNotMatch(html, /当前格式|当前媒体流|1080p|4K|VP9|AV1|\[object Object\]/)
  }
  assert.deepEqual(normalizeExecution({ ...base, metrics: { current_format: 401 } })?.metrics,
    [{ key: 'current_format', label: '流编号', value: '401' }])
  for (const current_format of [null, '', ' ', false, {}, [], Infinity, NaN]) {
    assert.deepEqual(normalizeExecution({ ...base, metrics: { current_format, current_stream: {} } })?.metrics, [])
  }
  assert.equal(normalizeExecution({ ...base, metrics: { current_stream: 'AV1 视频流' } })?.metrics[0]?.value, 'AV1 视频流')
})

test('步骤只接受当前任务执行过程，暂停和安全收尾优先于旧阶段状态', () => {
  const step = { status: 'running', job_id: 8, label: '语义整理', progress_phase: '新任务正在处理', execution: { ...base, job_id: 7 } }
  assert.equal(executionForStep(step)?.label, '新任务正在处理')
  const current = { ...step, execution: { ...base, job_id: 8 } }
  assert.equal(executionForStep(current)?.label, '等待模型首字')
  assert.equal(executionForStep({ ...current, pause_state: 'paused', pausing: true })?.state, 'paused')
  const stopped = executionForStep({ ...current, cancelling: true, stop_reason: 'timeout' })!
  assert.equal(stopped.state, 'stopping'); assert.match(stopped.detail, /超时停止中.*安全收尾/)
  assert.equal(executionForStep({ ...step, status: 'done', execution: null }), null)
  assert.equal(executionForStep({...step,execution:null,synthesis_task:{job_id:7,completed:100,total:100}})?.completed,undefined)
})

test('自动流程只关联冻结计划当前步，监测审核时不会借用并行手动任务', () => {
  const run = { id: 'run', cursor: 1, status: 'running', phase: '等待翻译开始' }
  const plan = { id: 'run', steps: ['split', 'translate'] }
  const manual = { key: 'tts', status: 'running', job_id: 99, progress_phase: '手动生成配音' }
  assert.equal(workflowCurrentStep(run, plan, [manual]), null)
  assert.equal(executionForWorkflow(run, plan, [manual])?.label, '等待翻译开始')
  const translation = { key: 'translate', status: 'queued', label: '翻译', job_id: 10 }
  assert.equal(workflowCurrentStep(run, plan, [manual, translation]), translation)
  assert.equal(workflowCurrentStep(run, { ...plan, id: 'old' }, [translation]), null)
  assert.equal(workflowCurrentStep({...run,status:'monitoring'},plan,[translation]),null)
  assert.equal(executionForWorkflow({ ...run, execution: { ...base, run_id: 'old' } }, plan, [])?.label, '等待翻译开始')
  assert.equal(executionForWorkflow({ ...run, status: 'monitoring', phase: '等待审核', cursor: 2 }, plan, [manual])?.label, '等待审核')
})

test('工作流当前制作采用匹配本轮的子Job心跳，不把54分钟阶段间隔当停滞',async()=>{
  const {run,plan,step}=runningWorkflowStep(),original=structuredClone({run,plan,step})
  assert.equal(workflowExecutionStep(run,plan,[step]),step)
  const execution=executionForWorkflow(run,plan,[step])!,view=normalizeExecution(execution)!
  assert.equal(execution.owner,'step');assert.equal(execution.job_id,520)
  assert.equal(view.completed,453);assert.equal(view.total,646);assert.equal(view.unit,'句');assert.equal(view.inactive,'')
  const html=await render({execution,percentage:step.progress})
  assert.match(html,/已完成 453 \/ 646 句/);assert.match(html,/处理第 454 句配音/);assert.match(html,/本阶段 70.5%/)
  assert.doesNotMatch(html,/距进度更新 54|5 \/ 8 制作步骤/)
  assert.deepEqual({run,plan,step},original)
  const queued={...step,status:'queued',execution:{...step.execution,state:'queued',phase:'queue',label:'等待执行资源'}}
  assert.equal(executionForWorkflow(run,plan,[queued])?.state,'queued')
  assert.equal(workflowExecutionStep(run,plan,[{...step,job_id:'520',execution:{...step.execution,job_id:'520'}}])?.key,'tts')
})

test('错计划游标、旧Job、未知身份及控制状态保留父流程，不拼接子步骤诊断',()=>{
  const cases:Array<(value:any)=>void>=[
    value=>value.plan.id='old',value=>value.run.cursor=4,value=>value.run.cursor='5',value=>value.run.cursor=-1,
    value=>value.run.execution.run_id='old',value=>value.run.execution.phase='publish.prepare',value=>value.run.execution.state='unknown',
    value=>value.run.execution.started_at=null,value=>value.step.execution.job_id=519,value=>value.step.job_id=null,
    value=>value.step.execution.job_id=null,value=>value.step.execution.run_id='other',value=>value.step.project_uid='other',
    value=>value.step.execution.started_at='2026-10-19T19:46:29Z',value=>value.step.execution.started_at='2026-10-19T20:54:12',
    value=>value.step.execution.updated_at='2026-10-19T19:00:00Z',value=>value.step.execution.state='completed',
    value=>value.step.execution.state='unknown',value=>value.step.pause_state='paused',value=>value.step.pause_state='requested',
    value=>value.step.pausing=true,value=>value.step.cancelling=true,
    ...['queued','stopping','paused','monitoring','failed','unknown'].map(status=>(value:any)=>{
      value.run.status=status;value.run.execution.state=status==='monitoring'?'waiting':status
    }),
  ]
  for(const change of cases) {
    const value=runningWorkflowStep();change(value)
    assert.equal(workflowExecutionStep(value.run,value.plan,[value.step]),null)
    assert.notEqual(executionForWorkflow(value.run,value.plan,[value.step])?.owner,'step')
  }
})

test('等待、重试、人工处理不同；未知进度没有0%或整项时长假象', async () => {
  for (const [state, label] of [['waiting', '等待中'], ['retrying', '正在重试'], ['attention', '需要处理'],['unknown','结果待核对'],['stopping','正在停止']]) {
    const html = await render({ execution: { ...base, state, attempt: state === 'retrying' ? 2 : undefined, max_attempts: 3 }, indeterminate: true, percentage: 90 })
    assert.match(html, new RegExp(label)); assert.match(html, /等待模型首字/)
    assert.doesNotMatch(html, /90%|0%|aria-valuenow/)
    if (state === 'retrying') assert.match(html, /第 2.*3.*次尝试/)
  }
  const measured = await render({ execution: { ...base, completed: 2, total: 10, unit: '块' }, percentage: 31.5 })
  assert.match(measured, /已完成 2 \/ 10 块/); assert.match(measured, /本阶段 31.5%/)
  const estimated = await render({ execution: base, percentage: 31.5, estimated: true, eta: '当前阶段预计剩余约 2 分钟' })
  assert.match(estimated, /本阶段估算 31.5%/); assert.match(estimated, /不代表整项或整批剩余时间/)
  assert.doesNotMatch(await render({execution:{...base,state:'queued'},percentage:0}),/0%|aria-valuenow/)
})

test('生产者阶段为主，日志和最近过程默认折叠且保留收尾记录', async () => {
  const html = await render({ execution: { ...base, label: '正在释放模型资源', history: [{ label: '校验结果', state: 'completed', detail: '第2块校验通过' }] },
    activity: { label: '最新动态', message: '收尾清理: 已删除临时文件', time: '12:34:56' } })
  assert.match(html, /正在释放模型资源/); assert.match(html, /最近过程 · 1 项/)
  assert.match(html, /收尾清理: 已删除临时文件/); assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
  assert.equal((html.match(/aria-label="执行过程"/g) || []).length, 1)
})
test('失败后收尾不会覆盖明确的实际失败阶段，也不会猜测或复活更早的重试错误',async()=>{
  const cleanup={schema:1,state:'failed',phase:'cleanup',label:'登记结果与清理临时文件',detail:'视频编码器无法申请内存',history:[
    {phase:'encoding_preview',label:'编码试片视频',state:'failed'},
    {phase:'cleanup',label:'登记结果与清理临时文件',state:'failed'},
  ]}
  const view=normalizeExecution(cleanup)!
  assert.equal(view.label,'编码试片视频');assert.equal(view.detail,'视频编码器无法申请内存')
  assert.equal(cleanup.label,'登记结果与清理临时文件')
  const html=await render({execution:cleanup})
  assert.match(html,/<div class="execution-heading"[^>]*><strong[^>]*>编码试片视频<\/strong>/)
  assert.match(html,/登记结果与清理临时文件/)
  for(const history of [[],[{phase:'cleanup',label:'登记结果与清理临时文件',state:'failed'}],
    [{phase:'llm',label:'先前模型请求失败',state:'failed'},{phase:'encoding_preview',label:'编码视频',state:'completed'},{phase:'cleanup',state:'failed'}],
    [{label:'没有阶段标识的旧记录',state:'failed'}]]){
    assert.equal(normalizeExecution({...cleanup,history})?.label,'当前任务执行失败')
  }
  assert.equal(normalizeExecution({...cleanup,state:'running'})?.label,'登记结果与清理临时文件')
  assert.equal(normalizeExecution({...cleanup,state:'completed'})?.label,'登记结果与清理临时文件')
  assert.equal(normalizeExecution({...cleanup,phase:'finalizing',label:'校验输出'})?.label,'校验输出')
})

test('核心字节工作量使用文件大小格式，计数保留原计量、时长显示时分秒',async()=>{
  const html=await render({execution:{...base,completed:256000000,total:2000000000,unit:'字节'}})
  assert.match(html,/已完成 244.1 MB \/ 1.86 GB/)
  assert.doesNotMatch(html,/256000000|2000000000|244.1 MB \/ 1.86 GB 字节/)
  assert.match(await render({execution:{...base,completed:2411724,total:10485760,unit:'字节'}}),/已完成 2.3 MB \/ 10.0 MB/)
  assert.match(await render({execution:{...base,completed:0,unit:'字节'}}),/已完成 0 B/)
  for(const unit of ['句','块'])assert.match(await render({execution:{...base,completed:2,total:10,unit}}),new RegExp(`已完成 2 / 10 ${unit}`))
  assert.match(await render({execution:{...base,completed:2,total:10,unit:'秒'}}),/已完成 00:00:02 \/ 00:00:10/)
})

test('暂停或终态隐藏旧的下次核对时间，实际等待与重试仍保留计划时间',async()=>{
  const next_check_at=1800000000
  for(const state of ['paused','completed','failed','interrupted','cancelled','skipped','attention','unknown','pausing','stopping']){
    assert.equal(normalizeExecution({...base,state,next_check_at})?.nextCheck,'')
    assert.doesNotMatch(await render({execution:{...base,state,next_check_at}}),/下次核对/)
  }
  for(const state of ['waiting','retrying'])assert.match(await render({execution:{...base,state,next_check_at}}),/下次核对/)
})

test('发布与章节是独立叶任务；评论保留工作量、分批、思考和重试事实', () => {
  const pub = { status: 'published', execution: { ...base, state: 'completed', label: '投稿已完成' },
    chapter_sync: { execution: { ...base, state: 'retrying', label: '章节翻译重试', attempt: 2 } } }
  assert.equal(executionForPublication(pub)?.state, 'completed')
  assert.equal(executionForChapter(pub, {})?.state, 'retrying')
  const job = { status: 'running', phase: 'retrying', attempt: 2, attempts: 3, batch: 2, batches: 10, reasoning_chars: 600 }
  const view = normalizeExecution(executionForComment(job, { phase: '重试本批评论', completed: 20, total: 100 }))!
  assert.equal(view.state, 'retrying'); assert.equal(view.completed, 20); assert.equal(view.attempt, 2)
  assert.ok(view.metrics.some(metric => metric.key === 'reasoning_chars' && metric.value === '600'))
})

test('无需执行的已跳过阶段保留原因，不重复显示完成零项与累计零秒',async()=>{
  const execution={...base,state:'skipped',label:'源视频没有章节，无需同步',completed:0,total:0,unit:'分P',elapsed_seconds:0}
  const skipped=await render({execution})
  assert.match(skipped,/源视频没有章节，无需同步/);assert.match(skipped,/已跳过/)
  assert.doesNotMatch(skipped,/已完成 0|累计已用|execution-facts/)
  const failed=await render({execution:{...execution,state:'failed',label:'章节设置失败'}})
  assert.match(failed,/章节设置失败/);assert.match(failed,/execution-facts/)
})

test('评论已结束时不会被遗留停止或重试标记恢复成进行中', () => {
  for (const status of ['done','failed','cancelled','interrupted']) {
    const view = executionForComment({ status, cancelling: true, phase: 'retrying' }, { phase: '处理已结束', total: 10 })!
    assert.notEqual(view.state, 'stopping'); assert.notEqual(view.state, 'retrying')
    assert.equal(view.detail, '')
  }
})

test('字幕导出在字幕工作区显示过程，停止绑定当前job且不启动新任务', async t => {
  const pinia = createPinia(), nav = useNav(pinia)
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/projects/subtitle-fixture/steps/tts?panel=subtitles')
  nav.$patch({ uid: 'subtitle-fixture', stepsLoaded: true, steps: [{ key: 'subtitles', status: 'running', job_id: 42,
    progress: 25, execution: { ...base, job_id: 42, label: '正在写入双语字幕' } }] })
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, {
    default: () => h(Subtitle, { uid: 'subtitle-fixture', state: { status: 'running', files: [] }, disabled: true }),
  }) }) }); app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'SubtitleExport') page = this.$.setupState } })
  try {
    const html = await renderToString(app)
    assert.match(html, /正在写入双语字幕/); assert.match(html, /停止字幕导出/)
    const requests: any[] = []
    t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
      requests.push({ url, body: JSON.parse(options.body) }); return { ok: true, json: async () => ({ status: 'stale_action' }) } as any
    })
    t.mock.method(nav, 'poll', async () => {})
    await page.stopExport()
    assert.deepEqual(requests, [{ url: '/api/projects/subtitle-fixture/steps/subtitles/cancel', body: { job_id: 42 } }])
  } finally { nav.reset(); disposePinia(pinia) }
})
