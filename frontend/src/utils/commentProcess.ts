import { commentAction, commentJobSummary, type CommentAction } from './commentWorkflow.ts'

export type CommentCollection = { status?: string; busy?: boolean; count?: number; limit?: number; collected_at?: string; started_at?: string; finished_at?: string; elapsed_seconds?: number | null; error?: string; code?: string; reused?: boolean }
export type CommentAutomation = { state: string; message: string }
export type CommentStage = { id: string; label: string; state: string; detail: string }
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null
export function commentPreparationHint(automation?: CommentAutomation | null, pending = '') {
  if (automation?.state === 'running') return '评分和译文逐步保存；准备完成后由当前自动流程确认保存。'
  if (pending === 'prepare') return '评分和译文逐步保存；上屏草稿完成后仍需确认。'
  return '评分和译文逐步保存；上屏方案以最终保存结果为准。'
}
export function commentProgressJob(progress: any, state: any, pending = '') {
  const active = Object.values(progress?.jobs || {}).find((job: any) => job?.status === 'running')
  const current = active || progress?.job || state?.job
  // 整体准备包含真实评分/翻译子任务；诊断仍指向它们，不制造 prepare 模型动作。
  if (pending === 'prepare') return current?.status === 'running' ? current : null
  if (pending && (!current || current.action !== pending || current.status !== 'running')) return { action: pending, status: 'running', phase: 'preparing', phase_label: '准备提交' }
  return current || null
}
export function commentProcessStages(progress: any, state: any, collection: CommentCollection | null = null, pending = ''): CommentStage[] {
  const source = collection?.busy ? collection : progress?.collection || collection || {}, rows = Array.isArray(state?.rows) ? state.rows : null
  const sourceCount = count(source.count) ?? rows?.length ?? null
  const current: any = commentProgressJob(progress, state, pending)
  const jobs = { ...progress?.jobs }
  const preparation = progress?.prepare_job || state?.prepare_job
  if (current?.action) jobs[current.action] = current
  const ai = (action: CommentAction, label: string) => {
    const job = jobs[action]
    const valid = action === 'filter' ? count(state?.rating_progress?.saved)
      : rows ? rows.filter((row: any) => typeof row?.text_zh === 'string' && row.text_zh.trim()).length : null
    const total = count(job?.total), done = count(job?.processed)
    // 后续准备已成功复用缓存时，旧的停止批次仍可查日志，但不再表示本阶段未就绪。
    const recovered = preparation?.status === 'done' && !!valid
      && Date.parse(preparation.finished_at) >= Date.parse(job?.finished_at)
    const status = job?.status === 'running' ? 'running' : recovered ? 'available' : ['failed', 'interrupted', 'cancelled', 'partial'].includes(job?.status) ? 'attention'
      : job?.status === 'done' ? 'done' : valid ? 'available' : 'idle'
    return { id: action, label, state: status, detail: status === 'running' ? total === null ? '准备处理' : `已保存 ${done || 0} / ${total} 条`
      : valid !== null ? `有效${action === 'filter' ? '评分' : '译文'} ${valid} 条` : job ? commentJobSummary(job, action) || '已有处理记录' : '按需运行' }
  }
  const selected = Array.isArray(state?.overlay?.items) ? state.overlay.items.length : null
  return [
    { id: 'collection', label: '获取原文', state: source.busy || source.status === 'running' ? 'running' : source.status === 'failed' ? 'attention'
      : ['completed', 'done'].includes(source.status || '') ? 'done' : sourceCount ? 'available' : 'idle',
      detail: source.busy || source.status === 'running' ? '正在向平台获取' : sourceCount !== null && (sourceCount > 0 || ['completed', 'done'].includes(source.status || '')) ? `已获取 ${sourceCount} 条${source.reused ? ' · 已复用' : ''}` : '尚未获取' },
    ai('filter', '评论评分'), ai('translate', '按需翻译'),
    { id: 'overlay', label: '上屏方案', state: selected !== null ? 'available' : 'idle', detail: selected !== null ? selected ? `已确认 ${selected} 条` : '已保存空方案' : state ? '尚未确认' : '查看已保存方案' },
  ]
}
export const commentPhaseLabels: Record<string, string> = { preparing: '准备请求', waiting: '等待模型响应', thinking: '模型思考中', output: '正在接收结果', validating: '校验结果', retrying: '正在定向重试', finalizing: '保存并整理结果', done: '处理完成', partial: '部分完成', failed: '处理失败', interrupted: '处理已中断', cancelled: '处理已停止' }
export function commentJobProgress(job: any) {
  const total = count(job?.total), processed = count(job?.processed) || 0, skipped = count(job?.skipped) || 0
  const completed = total === null ? null : Math.min(total, processed + skipped)
  return { total, processed, skipped, completed, percent: total ? Math.min(100, Math.round((completed || 0) / total * 100)) : null,
    action: commentAction(job?.action), phase: ['failed','interrupted','cancelled'].includes(job?.status)
      ? commentPhaseLabels[job.status] : job?.phase_label || commentPhaseLabels[job?.phase] || (job?.status === 'running' ? '等待本批处理结果' : '') }
}
export function commentDuration(value: unknown): string {
  const seconds = count(value)
  if (seconds === null) return ''
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
