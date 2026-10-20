import { fmtSize } from './format.ts'
import { executionTimeQuantity } from './executionTimeQuantity.ts'
// 执行过程只展示生产者提供的事实；工作量与阶段百分比分开，不推算整项耗时。
export type Execution = Record<string, any>
export type ExecutionStatusType = 'success' | 'info' | 'warning' | 'error' | 'default'
// 状态颜色独立于业务归一化；未知状态保持中性，避免被误示为正在执行。
export function executionStatusType(value: unknown): ExecutionStatusType {
  const key = String(value || '')
  if (['completed', 'done', 'success'].includes(key)) return 'success'
  if (['running', 'active', 'uploading', 'submitting'].includes(key)) return 'info'
  if (['attention', 'partial', 'rejected', 'unknown', 'paused', 'retrying', 'pausing', 'stopping'].includes(key)) return 'warning'
  if (['failed', 'error', 'interrupted'].includes(key)) return 'error'
  return 'default'
}
const states: Record<string, string> = {
  queued: '等待执行', running: '进行中', waiting: '等待中', retrying: '正在重试', paused: '已暂停',
  completed: '已完成', failed: '执行失败', interrupted: '已中断', cancelled: '已停止', skipped: '已跳过', attention: '需要处理',
  pausing: '正在暂停', stopping: '正在停止', unknown: '结果待核对',
}
const aliases: Record<string, string> = { pending: 'queued', done: 'completed', stopped: 'cancelled',
  monitoring: 'waiting', partial: 'attention', rejected: 'attention' }
export function executionState(value: unknown) {
  const key = String(value || 'running')
  return states[key] ? key : aliases[key] || 'running'
}
export function executionDuration(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return ''
  const seconds = Math.floor(value)
  return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
    : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分 ${seconds % 60} 秒`
}
function number(value: unknown) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null }
function text(value: unknown) { return typeof value === 'string' ? value.trim().slice(0, 1200) : '' }
function timestamp(value: unknown) {
  // 只接受带时区的服务端时间，不以浏览器本地时区或当前时间补齐缺失事实。
  if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null
  const milliseconds = Date.parse(value)
  return Number.isFinite(milliseconds) ? milliseconds / 1000 : null
}
function executionTiming(value: Execution, history: any[], state: string, age: number) {
  const updated = timestamp(value.updated_at), inactive = number(value.inactive_seconds)
  const snapshotClock = updated !== null && inactive !== null ? updated + inactive : null
  const phase = history.at(-1), phaseStart = timestamp(phase?.started_at)
  const runStart = timestamp(value.started_at)
  // 当前阶段采用服务端快照时钟；elapsed_seconds 在旧接口中可能是执行时长，也可能包含排队。
  const currentPhase = state !== 'queued' && ['running', 'waiting', 'retrying', 'pausing', 'stopping'].includes(state)
    && phase?.phase === value.phase && phase?.state === state && !phase.finished_at
    && phaseStart !== null && snapshotClock !== null && phaseStart <= snapshotClock
    && (runStart === null || phaseStart >= runStart)
  const phaseElapsed = currentPhase ? executionDuration(snapshotClock! - phaseStart! + age) : ''
  const queuePhase = (phase: unknown) => typeof phase === 'string' && /(?:^|\.)(?:queue|queued)$/.test(phase)
  const queue = runStart === null || phaseStart === null ? null : [...history].reverse().find(row => {
    const start = timestamp(row.started_at), end = timestamp(row.finished_at)
    return queuePhase(row.phase) && row.state === 'completed' && start !== null && end !== null
      && start >= runStart && end >= start && end <= phaseStart
  })
  const queueSeconds = queue ? timestamp(queue.finished_at)! - timestamp(queue.started_at)! : null
  return { phaseElapsed, priorQueueElapsed: state !== 'queued' && queueSeconds !== null && queueSeconds >= 1
    ? executionDuration(queueSeconds) : '' }
}
function encodingWork(value: Execution, history: any[], completed: number | null, total: number | null, unit: string) {
  // 合成步骤按版本映射总体进度；编码快照的成片秒和 phase ETA 则只属于当前编码。
  if (value.phase !== 'encoding' || unit !== '成片秒') return { workPercentage: null, workProgressLabel: '', versionLabel: '' }
  const workPercentage = completed !== null && total !== null && total > 0 && completed <= total
    ? completed / total * 100 : null
  const runStart = timestamp(value.started_at), phase = history.at(-1), phaseStart = timestamp(phase?.started_at)
  const version = runStart === null || phaseStart === null || phase?.phase !== 'encoding' ? null
    : [...history].reverse().find(row => {
      const start = timestamp(row.started_at), end = timestamp(row.finished_at)
      return row.phase === 'checking_version' && row.state === 'completed' && start !== null && end !== null
        && start >= runStart && end >= start && end <= phaseStart
    })
  // 旧服务只在本轮阶段标签中保存版本身份；格式或时间依据不完整时不猜测当前版本。
  const named = version?.label.match(/^核对成片版本\s+(\d+)\/(\d+)[：:]\s*(.+)$/)
  const index = Number(named?.[1]), count = Number(named?.[2]), name = named?.[3].trim()
  const versionLabel = Number.isSafeInteger(index) && Number.isSafeInteger(count) && index >= 1 && index <= count && name
    ? `当前版本 ${index}/${count}：${name}` : ''
  return { workPercentage, workProgressLabel: workPercentage === null ? '合成步骤' : '当前编码阶段', versionLabel }
}
function nextCheck(value: unknown) {
  if (typeof value !== 'number') return text(value)
  const date = new Date(value * 1000)
  return value > 0 && Number.isFinite(date.getTime()) ? date.toISOString() : ''
}
function metricValue(key:string, value:unknown) {
  if (key === 'resource') {
    const resources: Record<string, string> = { gpu: 'GPU 计算资源', cpu: 'CPU 处理资源', io: '文件读写资源', network: '网络执行资源' }
    return resources[String(value)] || String(value).slice(0, 160)
  }
  if (key === 'current_stream' || key === 'current_format') return String(value).trim().slice(0, 160)
  if (typeof value === 'number') {
    if (key.endsWith('_seconds') || key === 'duration') return executionDuration(value)
    if (key === 'speed_bytes_per_second') return `${fmtSize(value)}/秒`
    if (key.endsWith('_bytes')) return fmtSize(value)
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (key === 'device') {
    if (value === 'cpu') return 'CPU'
    if (value === 'cuda') return 'GPU'
    if (typeof value === 'string' && /^cuda:\d+$/.test(value)) return `GPU ${value.split(':')[1]}`
    if (value === 'auto') return '自动选择'
  }
  return String(value).slice(0,160)
}
export function normalizeExecution(value: any, observedSeconds = 0) {
  if (!value || typeof value !== 'object' || value.schema !== 1) return null
  const state = executionState(value.state)
  // 耗时可在两次服务端快照之间走秒；工作量和模型进度始终只接受服务端事实。
  const clockActive = ['queued', 'running', 'waiting', 'retrying', 'pausing', 'stopping'].includes(state)
  const age = clockActive ? number(observedSeconds) || 0 : 0
  const duration = (raw: unknown) => {
    const seconds = number(raw)
    return seconds === null ? '' : executionDuration(seconds + age)
  }
  const history = Array.isArray(value.history) ? value.history.slice(-24).filter((row: any) => row && typeof row === 'object').map((row: any) => ({
    phase: text(row.phase), label: text(row.label) || '处理阶段', state: executionState(row.state), stateLabel: states[executionState(row.state)], statusType: executionStatusType(row.state),
    detail: text(row.detail), started_at: text(row.started_at), finished_at: text(row.finished_at),
  })) : []
  const metricLabels: Record<string, string> = { batch: '当前批次', batches: '批次数', batch_size: '本批数量',
    chunk: '当前分块', chunks: '分块数', current_chunk: '当前分块', total_chunks: '分块数',
    model: '当前模型', profile: '模型配置', request: '请求次数', request_count: '请求次数',
    reasoning_chars: '思考字符', output_chars: '接收字符', request_elapsed_seconds: '请求用时',
    current_part: '当前分P', total_parts: '分P数', current_title: '当前文件', completed_parts: '已完成分P',
    line: '当前句', active_line: '当前句', failed: '失败数', cached: '复用数', processed: '已处理数',
    batch_index: '当前批次', batch_total: '批次数', current_id: '当前条目', reused: '复用数', skipped: '跳过数',
    generated: '生成数', repaired: '修复数', input_chars: '输入字符', loaded_bytes: '已传大小', total_bytes: '文件大小',
    speed_bytes_per_second: '传输速度', downloaded_bytes: '已下载大小', audio_seconds: '音频时长', duration: '媒体时长',
    versions: '版本数', resource: state === 'queued' ? '等待资源' : '执行资源', waiting_for: '等待原因', validation: '校验',
    cached_lines: '复用句数', generated_lines: '生成句数', repair_lines: '修复句数', duration_factor: '时长因子',
    device: '处理设备', source_lines: '源稿句数', file: '当前文件', current_stream: '当前媒体流', current_format: '流编号' }
  // 流描述来自本次下载元数据；旧记录只有编号时保留编号，不根据平台格式号猜测画质或媒体类型。
  const hasCurrentStream = !!text(value.metrics?.current_stream)
  const metrics = Object.entries(value.metrics || {}).filter(([key, entry]) => metricLabels[key]
    && (key !== 'current_stream' || typeof entry === 'string')
    && (key !== 'current_format' || !hasCurrentStream && (typeof entry === 'string' || typeof entry === 'number'))
    && (typeof entry === 'string' && entry.trim() || typeof entry === 'number' && Number.isFinite(entry) || typeof entry === 'boolean'))
    .slice(0, 12).map(([key, entry]) => ({ key, label: metricLabels[key], value: metricValue(key, entry) }))
  const defaultLabels: Record<string, string> = { queued: '等待执行当前任务', waiting: '等待当前处理结果', retrying: '正在重试当前处理',
    paused: '任务已暂停，等待继续', pausing: '正在等待安全暂停', stopping: '正在等待安全收尾', completed: '当前任务已完成',
    failed: '当前任务执行失败', interrupted: '当前任务已中断', cancelled: '当前任务已停止', skipped: '当前任务已跳过',
    attention: '当前任务需要处理', unknown: '处理结果尚未确认，请先核对' }
  const completed = number(value.completed), total = number(value.total), unit = text(value.unit)
  const bytes = ['字节', 'bytes', 'byte', 'B'].includes(unit)
  const timeQuantity = executionTimeQuantity(completed, total, unit)
  const active = ['queued', 'running', 'waiting', 'retrying'].includes(state)
  const sentenceSeconds = number(value.metrics?.sentence_elapsed_seconds)
  const sentenceActive = clockActive && sentenceSeconds !== null
  const sentenceProgress = number(value.metrics?.sentence_progress)
  let label=text(value.label) || defaultLabels[state] || '正在处理当前任务'
  if(state==='failed' && value.phase==='cleanup') {
    // 父任务失败会再次标记收尾阶段；只使用紧邻收尾前已明确失败的阶段，不能复活更早的重试错误。
    const previous=[...history].reverse().find((row:any)=>row.phase!=='cleanup')
    label=previous?.phase && previous.state==='failed' && previous.label!=='处理阶段'?previous.label:defaultLabels.failed
  }
  const resource = value.metrics?.resource
  const detail = text(value.detail) || (state === 'queued' && ['gpu', 'cpu'].includes(resource)
    ? `正在等待 ${metricValue('resource', resource)}；轮到本任务后会自动开始。` : '')
  const timing = executionTiming(value, history, state, age)
  const work = encodingWork(value, history, completed, total, unit)
  return { ...value, state, stateLabel: states[state], statusType: executionStatusType(value.state), label, detail,
    completed, total, unit, completedText: timeQuantity?.completedText ?? (completed === null ? '' : bytes ? fmtSize(completed) : String(completed)),
    totalText: timeQuantity?.totalText ?? (total === null ? '' : bytes ? fmtSize(total) : String(total)), displayUnit: timeQuantity?.displayUnit ?? (bytes ? '' : unit), attempt: number(value.attempt),
    max_attempts: number(value.max_attempts), elapsed: duration(value.elapsed_seconds), ...timing, ...work,
    inactive: state !== 'queued' && number(value.inactive_seconds) !== null && value.inactive_seconds + age >= 20 ? duration(value.inactive_seconds) : '',
    sentenceActive, sentenceElapsed: sentenceActive ? duration(sentenceSeconds) : '',
    sentenceProgress: sentenceActive && sentenceProgress !== null && sentenceProgress <= 1 ? Number((sentenceProgress * 100).toFixed(1)) : null,
    sentenceAttempt: sentenceActive ? number(value.metrics?.sentence_attempt) : null,
    sentenceAttempts: sentenceActive ? number(value.metrics?.sentence_attempts) : null,
    nextCheck: active ? nextCheck(value.next_check_at) : '', history, metrics, active, clockActive }
}
function currentExecution(source: any, runId?: unknown) {
  const value = source?.execution
  if (!normalizeExecution(value)) return null
  if (source?.job_id != null && value.job_id != null && String(source.job_id) !== String(value.job_id)) return null
  if (runId != null && value.run_id != null && String(runId) !== String(value.run_id)) return null
  return value
}
export function executionForStep(step: any) {
  if (!step) return null
  const value = currentExecution(step)
  const state = step.cancelling ? 'stopping' : step.pause_state === 'paused' ? 'paused'
    : step.pause_state === 'requested' || step.pausing ? 'pausing' : executionState(step.status)
  const stopping = step.cancelling ? `${step.stop_reason === 'timeout' ? '超时停止中' : '正在停止'}，等待当前处理安全收尾`
    : step.pause_state === 'requested' || step.pausing && step.pause_state !== 'paused' ? '正在暂停，等待当前处理完成' : ''
  if (value) return { ...value, ...(stopping || state === 'paused' ? { state, detail: stopping || '等待继续执行' } : {}) }
  if (!['running', 'queued'].includes(step.status)) return null
  const storedTask = step.synthesis_task
  const task = storedTask?.job_id != null && step.job_id != null && String(storedTask.job_id) !== String(step.job_id) ? null : storedTask
  return { schema: 1, state, label: stopping || (state === 'paused' ? '已暂停，等待继续' : step.progress_phase)
    || (step.status === 'queued' ? `等待执行${step.label || '当前步骤'}` : `正在执行${step.label || '当前步骤'}`),
    completed: task?.completed, total: task?.total, unit: task ? '句' : '', elapsed_seconds: step.elapsed_seconds }
}
/** 只跟随本次冻结计划当前项，不把同项目的手动任务拼进自动流程。 */
export function workflowCurrentStep(run: any, plan: any, steps: any[]) {
  if (!run || !['queued', 'running', 'stopping'].includes(run.status) || !plan || String(plan.id) !== String(run.id)) return null
  const key = plan.steps?.[Number(run.cursor)]
  return key ? steps.find(step => step.key === key && ['running', 'queued'].includes(step.status)) || null : null
}
/** 当前步骤的执行诊断必须属于本轮；旧Job或控制状态不借用子步骤心跳。 */
export function workflowExecutionStep(run: any, plan: any, steps: any[]) {
  if (!run?.id || run.status !== 'running' || !Array.isArray(plan?.steps)
    || !Number.isInteger(run.cursor) || run.cursor < 0 || run.cursor >= plan.steps.length) return null
  const parent = currentExecution(run, run.id), step = workflowCurrentStep(run, plan, steps)
  const child = currentExecution(step)
  const job = (value: unknown) => {
    if (typeof value !== 'number' && (typeof value !== 'string' || !/^\d+$/.test(value))) return null
    const id = Number(value)
    return Number.isSafeInteger(id) && id > 0 ? id : null
  }
  if (!parent || parent.owner !== 'workflow' || String(parent.run_id) !== String(run.id)
    || parent.state !== 'running' || parent.phase !== `production.${step?.key}` || parent.finished_at
    || !step || step.cancelling || step.pausing || ['paused', 'requested'].includes(step.pause_state)
    || !child || child.owner !== 'step' || child.finished_at
    || !['queued', 'running', 'waiting', 'retrying'].includes(child.state)
    || job(step.job_id) === null || job(step.job_id) !== job(child.job_id)
    || child.run_id != null && String(child.run_id) !== String(run.id)
    || step.project_uid && run.project_uid && step.project_uid !== run.project_uid) return null
  const parentStart = timestamp(parent.started_at), childStart = timestamp(child.started_at), updated = timestamp(child.updated_at)
  return parentStart !== null && childStart !== null && updated !== null && childStart >= parentStart && updated >= childStart ? step : null
}
export function executionForWorkflow(run: any, plan: any, steps: any[]) {
  if (!run) return null
  const step = workflowExecutionStep(run, plan, steps)
  if (step) return executionForStep(step)
  const value = currentExecution(run, run.id)
  if (value) return value
  return ['queued', 'running', 'stopping', 'monitoring'].includes(run.status)
    ? { schema: 1, state: run.status === 'monitoring' ? 'waiting' : executionState(run.status), label: run.phase || '等待当前流程进度' } : null
}
export function executionForPublication(row: any, progress?: any) {
  const value = currentExecution(row)
  if (value) return value
  if (!progress) return null
  return { schema: 1, state: row.status === 'uploaded' ? 'waiting' : row.status === 'failed' ? 'failed'
    : row.status === 'paused' ? 'paused' : row.status === 'queued' ? 'queued' : 'running', label: progress.phase,
    detail: progress.part || '', completed: progress.completedParts, total: progress.totalParts, unit: '个分P' }
}
export function executionForChapter(row: any, chapter: any) {
  const value = currentExecution(row?.chapter_sync)
  if (value) return {...value, detail:value.detail || chapter?.message || ''}
  if (!chapter?.enabled && !chapter?.status) return null
  return { schema: 1, state: chapter.status === 'pending' ? 'waiting' : chapter.status === 'unknown' ? 'unknown'
    : executionState(chapter.status || 'waiting'), label: chapter.label, detail: chapter.message }
}
export function executionForComment(job: any, detail: any) {
  if (!job) return null
  // 终态优先于停止请求/重试标记，避免已停止后仍显示“等待安全收尾”。
  const ended = ['done','failed','interrupted','cancelled','partial'].includes(job.status)
  const cancelling = !ended && job.cancelling
  const fallback = { schema: 1, state: ended ? executionState(job.status) : cancelling ? 'stopping' : job.phase === 'retrying' ? 'retrying'
    : ['waiting', 'thinking', 'finalizing'].includes(job.phase) ? job.phase === 'waiting' ? 'waiting' : 'running' : executionState(job.status),
    label: detail.phase || (job.action === 'translate' ? '处理评论译文' : '逐条评价评论'),
    detail: cancelling ? '正在停止，等待当前处理安全收尾' : '', completed: detail.completed, total: detail.total, unit: '条评论',
    attempt: job.attempt, max_attempts: job.attempts, elapsed_seconds: job.elapsed_seconds, inactive_seconds: job.inactive_seconds,
    metrics: Object.fromEntries(['profile', 'model', 'batch', 'batches', 'batch_size', 'request', 'request_elapsed_seconds', 'reasoning_chars', 'output_chars']
      .map(key => [key, job[key]])) }
  const value = currentExecution(job)
  return value ? {...fallback, ...value, completed:value.completed ?? fallback.completed, total:value.total ?? fallback.total,
    unit:value.unit || fallback.unit, metrics:{...fallback.metrics,...value.metrics}} : fallback
}
