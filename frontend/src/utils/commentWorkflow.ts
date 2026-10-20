export type CommentAction = 'filter' | 'translate'

export function commentAction(value: unknown): CommentAction | undefined {
  return value === 'filter' || value === 'translate' ? value : undefined
}

// 运行或失败任务优先；最近任务已成功时，仍能直接定位尚未解决的问题。
export function preferredCommentAction(job: any, rows: any[] = []): CommentAction | undefined {
  const current = commentAction(job?.action)
  if (current && ['running','failed','partial','interrupted','cancelled'].includes(job?.status)) return current
  const issues: { action: CommentAction; updated: number }[] = []
  for (const row of rows) for (const action of ['translate','filter'] as const) {
    const issue = row.ai_issues?.[action]
    if (issue) {
      const updated = Date.parse(issue.updated_at)
      issues.push({ action, updated: Number.isFinite(updated) ? updated : 0 })
    }
  }
  return issues.sort((a,b) => b.updated - a.updated)[0]?.action || current
}

export function commentIssueCounts(rows: any[], action: CommentAction) {
  const counts = { blocked: 0, deferred: 0, failed: 0 }
  for (const row of rows) {
    const issue = row.ai_issues?.[action]
    if (!issue) continue
    if (issue.status === 'blocked') counts.blocked++
    else if (issue.status === 'deferred') counts.deferred++
    else counts.failed++
  }
  return counts
}

export function hasCommentTranslation(row: any): boolean {
  return typeof row?.text_zh === 'string' && !!row.text_zh.trim()
}

export function commentIssueLabel(issue: any): string {
  if (issue?.status === 'blocked') {
    return issue.request_scope === 'batch' ? '所在请求被服务商审核拦截'
      : issue.request_scope === 'item' ? '服务商审核拦截（单条请求）' : '服务商审核拦截（请求范围未记录）'
  }
  return issue?.status === 'deferred' ? '暂缓处理' : '处理失败'
}

export function commentIssueContext(issue: any): string {
  if (issue?.status === 'blocked') {
    return issue.request_scope === 'batch' ? '拒绝来自包含多条评论的请求，不能据此确定哪条评论触发了审核。'
      : issue.request_scope === 'item' ? '服务商拒绝了这条评论的独立请求，请结合原文与返回原因核对。'
        : '旧记录未保存请求范围，不能确定这条评论是否被服务商单独拒绝。'
  }
  return issue?.status === 'deferred' ? '本条尚未完成独立处理，可使用“重试未完成”继续。'
    : '本次处理未完成，可核对错误原因后使用“重试未完成”。'
}

export function commentIssueCauses(issue: any): string[] {
  return (Array.isArray(issue?.cause) ? issue.cause : []).flatMap((cause: any) => {
    if (!cause || typeof cause !== 'object') return []
    const profile = typeof cause.profile === 'string' && cause.profile.trim() ? cause.profile.trim() : '服务商'
    const providerCode = typeof cause.provider_code === 'string' ? cause.provider_code.trim() : ''
    const finish = typeof cause.finish_reason === 'string' ? cause.finish_reason.trim() : ''
    const code = typeof cause.code === 'string' ? cause.code.trim() : ''
    const reason = providerCode || (finish ? `结束原因 ${finish}` : code)
    return reason ? [`${profile}：${reason}`] : []
  })
}

function jobCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null
}

export function commentJobSummary(job: any, action: CommentAction = commentAction(job?.action) || 'filter'): string {
  if (!job) return ''
  const processed = jobCount(job.processed), cached = jobCount(job.cached)
  const blocked = jobCount(job.blocked), failed = jobCount(job.failed), deferred = jobCount(job.deferred)
  const precise = blocked !== null && failed !== null
  const output = action === 'translate' ? '译文' : '评分', parts: string[] = []
  if (processed !== null) parts.push(`本次新增${output} ${processed} 条`)
  if (cached !== null) parts.push(`复用有效${output} ${cached} 条`)
  if (blocked) {
    const protectedCount = jobCount(job.blocked_cached)
    parts.push(`审核拦截记录 ${blocked} 条${protectedCount ? `（其中旧记录 ${protectedCount} 条未重发）` : ''}`)
  }
  if (failed) parts.push(`处理失败 ${failed} 条`)
  if (deferred) parts.push(`暂缓 ${deferred} 条`)
  if (!precise && jobCount(job.skipped)) parts.push(`跳过问题项 ${job.skipped} 条`)
  return parts.join('；')
}

export function commentJobFeedback(job: any, action: CommentAction): { type: 'info' | 'success' | 'warning'; text: string } {
  const summary = commentJobSummary(job, action)
  if (job?.status === 'partial' || ['failed','interrupted','cancelled'].includes(job?.status)) {
    return { type: 'warning', text: summary || job.warning || job.error || '处理尚未全部完成，请查看逐条状态与诊断。' }
  }
  if (job?.status !== 'done') return { type: 'info', text: summary || '处理请求已接收，请查看任务进度。' }
  if (jobCount(job.processed) === 0) return { type: 'info', text: `${action === 'translate' ? '本次没有新增译文' : '本次没有新增评分'}${summary ? `；${summary}` : '，请查看有效缓存与未完成项。'}` }
  if (jobCount(job.processed) === null) return { type: 'info', text: summary || '处理已结束，请核对逐条结果。' }
  return { type: 'success', text: summary || '处理已结束，请核对逐条结果。' }
}

export function commentWorkspaceLink(uid: string, diagnostics = false, action?: CommentAction) {
  return `/projects/${encodeURIComponent(uid)}/comments${diagnostics && action ? `?comment_action=${action}` : ''}${diagnostics ? '#llm-conversation' : ''}`
}

export function commentFailure(error: any): boolean {
  return /^COMMENTS?_/.test(String(error?.code || '')) || /评论/.test(String(typeof error === 'string' ? error : error?.message || ''))
}
