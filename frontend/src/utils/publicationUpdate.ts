/** 原稿更新的显示状态独立于首次投稿，不把上传完成解释成审核通过。 */
export const UPDATE_STATUS: Record<string, string> = {
  prepared: '方案已检查 · 待确认', uploaded: '文件已上传 · 待确认更新', queued: '等待执行', uploading: '正在上传替换视频',
  submitting: '正在更新原稿件', waiting: '等待平台转码或审核', delivering: '正在同步后续设置', failed: '部分步骤未完成',
  unknown: '操作结果待核对', paused: '更新已暂停', completed: '本次更新已核验',
}
export const UPDATE_STAGE_LABELS: Record<string, string> = {
  upload: '上传新视频', edit: '提交原稿更新', review: '平台转码与审核',
  chapters: '同步视频章节', collection: '同步合集', verify: '回读核验',
}
const stageStatuses: Record<string, string> = {
  pending: '待执行', queued: '待执行', running: '进行中', active: '进行中', uploading: '进行中',
  submitting: '进行中', waiting: '等待平台处理', completed: '已完成', success: '已完成',
  skipped: '不在本次范围', failed: '未完成', unknown: '待核对', paused: '已暂停', prepared: '待确认',
}

const TERMINAL_STAGE_STATES = new Set(['completed', 'success', 'skipped'])

function clampProgress(value: unknown): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : null
}

/**
 * 章节和合集交付没有一个与上传相同的字节百分比；它们仍然会保存每个
 * 分P/设置节点的状态。这里把这些事实汇总为“已完成节点 / 总节点”，并
 * 标记为 estimated，避免把推导值伪装成服务端精确百分比。
 */
function deliveryProgress(task: any, key: string): { progress: number | null; estimated: boolean } {
  const delivery = task?.execution?.delivery || {}
  const nodes = key === 'chapters' ? (Array.isArray(delivery.parts) ? delivery.parts : [])
    : key === 'collection' ? (delivery.collection ? [delivery.collection] : []) : []
  if (!nodes.length) return { progress: null, estimated: false }
  const done = nodes.filter((node: any) => TERMINAL_STAGE_STATES.has(String(node?.status || ''))).length
  const current = delivery.execution
  const currentCompleted = clampProgress(current?.completed)
  const currentTotal = Number(current?.total)
  // 当前章节整理可能在一个分P内部提供 Recorder 进度；只把它作为当前节点的
  // 小数部分，不能覆盖已经完成的其他分P。
  const fraction = currentCompleted !== null && Number.isFinite(currentTotal) && currentTotal > 0
    ? currentCompleted / currentTotal : 0
  const active = nodes.some((node: any) => ['running', 'active', 'uploading', 'submitting'].includes(String(node?.status || '')))
  const value = (done + (active ? Math.max(0, Math.min(1, fraction)) : 0)) / nodes.length * 100
  return { progress: Math.max(0, Math.min(100, value)), estimated: true }
}

export function updateIsActive(task: any): boolean {
  return !!task && (!!task.busy || ['queued', 'uploading', 'submitting', 'waiting', 'delivering'].includes(task.status))
}

/** Statuses that should keep an update visible as an in-flight delivery. */
export const UPDATE_ACTIVE = ['queued', 'uploading', 'submitting', 'waiting', 'delivering'] as const

/**
 * Small projection used by overview/project cards.  The publication page has
 * the complete update task; list endpoints intentionally return only this
 * bounded object, so progress remains useful without loading frozen plans.
 */
export function publicationUpdateProgress(publication: any) {
  const task = publication?.latest_update
  if (!task || typeof task !== 'object') return null
  const progressDetail = task.execution?.progress_detail || task.progress_detail || {}
  const raw = Number(task.progress ?? task.execution?.progress ?? progressDetail.progress)
  const status = String(task.status || 'unknown')
  const progress = status === 'completed' ? 100
    : Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : null
  const text = UPDATE_STATUS[status] || '原稿更新状态待核对'
  const active = (UPDATE_ACTIVE as readonly string[]).includes(status)
  const tone = status === 'completed' ? 'success' : ['failed', 'unknown'].includes(status) ? 'warning'
    : status === 'paused' ? 'warning' : active ? 'info' : 'default'
  const stageSource = task.stages || task.execution?.stages || progressDetail.stages || {}
  const stages = updateStages({ execution: { stages: stageSource, delivery: task.execution?.delivery } })
  const current = stages.find(stage => ['running', 'active', 'uploading', 'submitting', 'waiting'].includes(stage.status))
    || stages.find(stage => stage.status === 'failed')
  return { task, status, text, progress, active, tone,
    phase: task.phase || progressDetail.current_stage_label || current?.label || '', current, stages }
}
export function updateNeedsReconcile(task: any): boolean {
  return task?.status === 'unknown' || task?.error_code === 'PUBLISH_UPDATE_UNKNOWN'
}
export function updateStages(task: any) {
  const raw = task?.execution?.stages || {}
  return Object.entries(UPDATE_STAGE_LABELS).map(([key, label]) => {
    const value = Array.isArray(raw) ? raw.find((row: any) => row.key === key || row.id === key) || {} : raw[key] || {}
    const status = String(value.status || 'pending')
    const reported = clampProgress(value.progress)
    const derived = reported === null ? deliveryProgress(task, key) : { progress: reported, estimated: false }
    // 后端会为待执行阶段写入 0 作为稳定快照，但用户不需要看到一排
    // “0%”进度条；只有正在执行、等待平台或已有实际进度时才显示细条。
    // 等待远端转码/审核时没有可量化的完成比例；后端会保留 0 作为
    // 稳定快照，但界面应显示“等待平台处理”，不要显示误导性的 0%细条。
    const hasActualProgress = ['running', 'active', 'uploading', 'submitting'].includes(status)
      || (status === 'waiting' && ((reported !== null && reported > 0) || (derived.progress !== null && derived.progress > 0)))
      || (reported !== null && reported > 0) || derived.progress !== null && derived.progress > 0
    const progress = TERMINAL_STAGE_STATES.has(status) ? 100 : hasActualProgress ? derived.progress : null
    return { key, label: value.label || label, status, text: stageStatuses[status] || '等待状态更新',
      error: value.error || '', progress, estimated: progress !== null && reported === null && !TERMINAL_STAGE_STATES.has(status) }
  })
}

/**
 * 原稿更新的整项进度只作为导航摘要使用。服务端的 row.progress 是按阶段
 * 完成数保存的；有更细的阶段进度时优先使用它，阶段数只是没有细粒度
 * 数据时的兜底，因此不会把“等待审核”误显示成正在上传。
 */
export function updateProgress(task: any) {
  const stages = updateStages(task)
  if (!task || task.status === 'prepared') return { percentage: null, completed: 0, total: stages.length, active: null, indeterminate: false }
  const reported = clampProgress(task.progress)
  const considered = stages.filter(stage => stage.status !== 'skipped')
  const source = considered.length ? considered : stages
  const sum = source.reduce((total, stage) => total + (stage.progress ?? (TERMINAL_STAGE_STATES.has(stage.status) ? 100 : 0)), 0)
  const percentage = source.length ? Math.max(0, Math.min(100, sum / source.length)) : reported
  const completed = stages.filter(stage => TERMINAL_STAGE_STATES.has(stage.status)).length
  const active = stages.find(stage => ['running', 'active', 'uploading', 'submitting', 'waiting'].includes(stage.status)) || null
  return { percentage: percentage === null ? reported : percentage, completed, total: stages.length,
    active, indeterminate: !!active && active.progress === null }
}
export function updateDuration(value: unknown): string {
  const number = Number(value)
  if (value === null || value === undefined || !Number.isFinite(number) || number < 0) return '时长待读取'
  const seconds = Math.round(number), hours = Math.floor(seconds / 3600)
  const rest = `${Math.floor(seconds % 3600 / 60)}:${String(seconds % 60).padStart(2, '0')}`
  return hours ? `${hours}:${rest.padStart(5, '0')}` : rest
}
export const UPDATE_COPY_FIELDS: Record<string, string> = {
  title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态', declaration_id: '创作声明',
}
export const UPDATE_FIELD_LABELS: Record<string, string> = {
  ...UPDATE_COPY_FIELDS,tid:'分区',human_type2:'分区',copyright:'稿件类型',source:'转载来源',cover:'封面',
}
export function updateValue(value: any) {
  if (Array.isArray(value)) return value.join('、') || '（空）'
  return value === '' || value === null || value === undefined ? '（空）' : String(value)
}
export function updateDifferences(task: any) {
  const before = task?.preview?.before?.settings || task?.preview?.before || {}
  const after = task?.preview?.after?.settings || task?.preview?.after || {}
  return Object.entries(UPDATE_COPY_FIELDS).filter(([key]) => Object.hasOwn(after, key)
    && JSON.stringify(before[key]) !== JSON.stringify(after[key]))
    .map(([key, label]) => ({ key, label, before: updateValue(before[key]), after: updateValue(after[key]) }))
}
