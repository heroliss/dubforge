import { WORKFLOW_OPEN, supersedingPublicationUpdate, workflowModeLabel, workflowPublication } from './workflow.ts'
import { isPublicationComplete } from './publishing.ts'

export function projectDeliverySummary(summary: any, workflowStatus?: string | null) {
  // 已确认的投稿任务也被安全历史计数覆盖；它不等于已经有了平台稿件。
  // 正在执行时继续冻结的目标，不能用后来默认设置引导用户另选或重投。
  if (WORKFLOW_OPEN.includes(workflowStatus || '')) return {
    delivery: '沿用本次已确认方案', target: '当前流程继续已确认的交付目标，无需重新选择稿件。',
  }
  if (!summary?.known) return { delivery: '项目设置待核对', target: '' }
  const delivery = workflowModeLabel(summary.delivery_mode, '交付目标待设置')
  if (summary.delivery_mode === 'produce') return { delivery, target: '' }
  const account = summary.account_name || (summary.account_id ? '账号名称未载入' : '账号待设置')
  if (summary.target_kind === 'existing') {
    return { delivery, target: `${account} · 更新原稿${summary.bvid ? ` ${summary.bvid}` : '（待核对）'}` }
  }
  if (summary.target_kind !== 'new') return { delivery, target: summary.target_selection?.source === 'invalid'
    ? '稿件目标无效 · 请重新选择'
    : summary.target_selection?.source === 'explicit_unset'
    ? '稿件目标已清空 · 请选择新稿或原稿'
    : summary.target_selection?.has_history === true ? '已有交付任务或发布记录 · 运行前请核对稿件目标' : '发布目标待设置' }
  const collection = summary.season_id
    ? [summary.season_title || '合集名称未载入', summary.section_title || (summary.section_id ? '小节名称未载入' : '')].filter(Boolean).join(' / ')
    : '独立发布'
  const defaultLabel = summary.target_selection?.source === 'first_publish' ? '首次发布 · 默认创建新稿 · ' : ''
  return { delivery, target: `${defaultLabel}${account} · ${collection}` }
}

export function projectCurrentDelivery(project: any, summary: any) {
  if (!summary || summary.run?.status !== project.workflow_status) return null
  if (!supersedingPublicationUpdate(summary, project.publication)) return null
  return workflowPublication(summary, project.publication)
}

// 卡片保留当前更新及所有待处理状态；已完成的原投稿过程在发布详情中查看。
export function projectCurrentPublication(project: any) {
  const publication = project?.publication, update = publication?.latest_update
  if (!publication?.id || !update?.id || update.source_publication_id && update.source_publication_id !== publication.id
    || !isPublicationComplete(publication.status) || !['done', 'completed'].includes(publication.execution?.state)
    || !['queued', 'uploading', 'submitting', 'waiting', 'delivering', 'uploaded', 'failed', 'unknown', 'paused', 'completed'].includes(update.status)) return publication
  return { ...publication, execution: null }
}
