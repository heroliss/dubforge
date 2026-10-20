import { publicationStage } from './publishing.ts'
import { updateIsActive, updateNeedsReconcile } from './publicationUpdate.ts'

export const WORKFLOW_ENDS = [
  { label: '仅制作成片', value: 'produce' },
  { label: '准备发布草稿', value: 'draft' },
  { label: '上传后等待确认投稿', value: 'upload' },
  { label: '自动投稿', value: 'publish' },
]
// 原稿目标仅保存在项目中；交付终点仍由四种模式决定。
export const WORKFLOW_RUN_ENDS = [...WORKFLOW_ENDS, { label: '更新原稿件（保留原BV）', value: 'update' }]
export const WORKFLOW_BUSY = ['queued', 'running', 'stopping']
export const WORKFLOW_OPEN = [...WORKFLOW_BUSY, 'monitoring']
export function workflowModeLabel(mode?: string, fallback = '正在读取') {
  return WORKFLOW_RUN_ENDS.find(item => item.value === mode)?.label || fallback
}
export function workflowRunStage(run: any) {
  if (run?.status === 'ready' && (run?.mode === 'upload' || run?.recipe?.delivery_mode === 'upload' || run?.delivery_mode === 'upload')) return { text: '已上传，等待确认投稿', tone: 'success' as const }
  const states: Record<string, [string, 'info' | 'warning' | 'success' | 'error']> = {
    queued: ['等待执行', 'info'], running: ['执行中', 'info'], stopping: ['正在停止', 'warning'],
    monitoring: ['等待平台审核', 'info'], paused: ['已暂停', 'warning'], failed: ['需处理', 'error'],
    attention: ['需处理', 'warning'], ready: ['草稿已就绪', 'success'], completed: ['流程已完成', 'success'],
  }
  const [text, tone] = states[run?.status] || ['尚未运行', 'info']
  return { text, tone }
}

function updateTime(value: unknown): number | null {
  if (typeof value !== 'string' || !value.trim()) return null
  // 数据库时间使用 UTC；旧接口未附时区，不能随浏览器所在地改变先后关系。
  const result = Date.parse(/(?:z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`)
  return Number.isFinite(result) ? result : null
}

function hasUnverifiedUpdate(task: any): boolean {
  const unknownCode = (value: unknown) => typeof value === 'string' && /^PUBLISH_.*_UNKNOWN$/.test(value)
  if (updateNeedsReconcile(task) || unknownCode(task?.error_code) || unknownCode(task?.execution?.error_code)
    || task?.execution?.edit_unverified || task?.execution?.outcome === 'unknown') return true
  const execution = task?.execution || {}, delivery = execution.delivery || {}
  const stages = Array.isArray(execution.stages) ? execution.stages : Object.values(execution.stages || {})
  const nodes = [...stages, ...(Array.isArray(delivery.parts) ? delivery.parts : []), delivery.collection]
  return nodes.some((node: any) => ['unknown', 'committing'].includes(node?.status) || unknownCode(node?.error_code))
}

// 自动流程保留当时的中断快照；同一稿件后来单独修复成功，只改变当前交付显示。
// 身份或时间缺少证据、仍在执行、远端写入待核实时，继续保留原流程的提示。
function newerPublicationUpdate(run: any, task: any, publication: any) {
  const latest = publication?.latest_update
  if (!latest?.id || !run.publication_id || publication.id !== run.publication_id
    || task?.source_publication_id && task.source_publication_id !== publication.id
    || latest.source_publication_id && latest.source_publication_id !== publication.id
    || !['paused', 'failed', 'attention', 'completed', 'ready', 'cancelled'].includes(run.status)
    || updateIsActive(task) || hasUnverifiedUpdate(task)
    || !['queued', 'uploading', 'submitting', 'waiting', 'delivering', 'uploaded', 'failed', 'unknown', 'paused', 'completed'].includes(latest.status)) return null
  const previousTime = updateTime(task?.updated_at), latestTime = updateTime(latest.updated_at)
  if (previousTime === null || latestTime === null || latestTime <= previousTime) return null
  return latest
}

export function supersedingPublicationUpdate(summary: any, publication: any) {
  const run = summary?.run
  return run?.mode === 'update'
    ? newerPublicationUpdate(run, run.publication_update || run.publication || run.update, publication) : null
}

// 总览和侧栏共用：执行策略不冒充稿件状态，确认任务不被后来改动的默认终点改写。
export function workflowPublication(summary: any, publication: any, error = '') {
  if (error) return { text: '状态读取失败', tone: 'warning' as const, dot: 'stale', note: error }
  const run = summary?.run
  const mode = run && WORKFLOW_OPEN.includes(run.status) ? run.mode : summary?.mode
  if (run?.mode === 'update') {
    const previous = run.publication_update || run.publication || run.update
    const latest = supersedingPublicationUpdate(summary, publication)
    const task = latest || previous
    const stages: Record<string, [string, 'info' | 'warning' | 'success' | 'error']> = {
      queued:['等待更新原稿','info'], prepared:['原稿更新草稿已就绪','success'], uploaded:['已上传，等待确认更新原稿','success'], uploading:['上传替换视频','info'], submitting:['提交原稿更新','info'],
      waiting:['等待平台处理','info'], delivering:['同步章节与后续设置','info'],
      completed:['原稿更新已核验','success'], failed:['原稿更新需处理','error'], unknown:['原稿更新结果待核对','warning'], paused:['原稿更新已暂停','warning'],
    }
    const state = task?.status || (run.status === 'completed' ? 'completed' : run.status === 'monitoring' ? 'waiting' : run.status)
    const [text,tone] = stages[state] || [WORKFLOW_BUSY.includes(run.status) ? '正在执行原稿更新流程' : '原稿更新待继续','info']
    return {text,tone,dot:tone==='success'?'done':tone==='error'||tone==='warning'?'stale':'running',
      note:latest ? latest.error || latest.phase || '请进入发布页查看最近一次原稿更新。'
        :run.phase || '保留原BV，按本次确认范围同步视频、资料与平台设置。'}
  }
  const note = mode === 'publish' ? '已启用自动投稿 · 确认完整流程后自动上传并投稿'
    : mode === 'upload' ? '上传完成后等待手动确认投稿'
    : mode === 'draft' ? '自动流程准备草稿，投稿前由你确认'
    : mode === 'produce' ? '自动流程仅制作成片，发布可单独操作' : '正在读取执行终点'
  const current = run?.publication
    ? publication?.id === run.publication.id && !WORKFLOW_BUSY.includes(run.status)
      ? { ...run.publication, latest_update: publication.latest_update } : run.publication
    : (run && run.mode !== 'produce' && WORKFLOW_BUSY.includes(run.status) ? null : publication)
  // A manually started “更新原稿件” task is persisted on the publication
  // record as latest_update.  It can outlive the page that started it, so the
  // shared sidebar/overview must expose that progress even without an active
  // automatic workflow run.
  const update = current?.latest_update
  if (update && ['queued', 'uploading', 'submitting', 'waiting', 'delivering'].includes(update.status)) {
    const labels: Record<string, string> = { queued: '等待更新原稿', uploading: '上传替换视频',
      submitting: '提交原稿更新', waiting: '等待平台处理', delivering: '同步章节与后续设置' }
    return { text: labels[update.status] || '更新原稿件中', tone: 'info' as const, dot: 'running',
      note: update.phase || '保留原BV，按已确认范围更新视频和平台资料。' }
  }
  if (update && ['failed', 'unknown', 'paused'].includes(update.status)) {
    return { text: update.status === 'failed' ? '原稿更新需处理' : update.status === 'unknown' ? '原稿更新结果待核对' : '原稿更新已暂停',
      tone: 'warning' as const, dot: 'stale', note: update.error || update.phase || '请进入发布页查看更新任务。' }
  }
  if (update?.status === 'completed') {
    return { text: '原稿更新已核验', tone: 'success' as const, dot: 'done',
      note: update.phase || '最近一次保留原BV更新已完成并回读核验。' }
  }
  if (current?.status === 'uploaded' && run?.mode === 'publish' && WORKFLOW_BUSY.includes(run.status)) return {
    text: '上传完成 · 准备投稿', tone: 'info' as const, dot: 'running', note: '按本次确认继续投稿，无需再次点击',
  }
  if (current?.id && current.status !== 'pending') return { ...publicationStage(current.status), note }
  if (run && run.mode !== 'produce') {
    if (WORKFLOW_BUSY.includes(run.status)) return {
      text: run.status === 'stopping' ? '正在停止' : run.cursor >= run.total ? '准备发布资料' : '等待制作完成',
      tone: 'info' as const, dot: 'running', note: `本次执行到：${workflowModeLabel(run.mode)}`,
    }
    if (['failed', 'paused', 'attention'].includes(run.status)) return {
      text: '流程待继续', tone: 'warning' as const, dot: 'stale', note: '请在上方处理当前流程；尚未进入投稿',
    }
  }
  return { text: '尚未开始', tone: 'default' as const, dot: 'pending', note }
}
