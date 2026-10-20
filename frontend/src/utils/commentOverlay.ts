// 字符计数与 Python len 对齐，表情不按 UTF-16 代理对重复计算。
export interface CommentTextLimits { translated: number; original: number }
export interface CommentTextIssue { field: 'display_text' | 'source_text'; message: string }
export const defaultCommentTextLimits: CommentTextLimits = { translated: 240, original: 600 }
export function commentTextLength(value: unknown): number {
  return typeof value === 'string' ? Array.from(value.trim()).length : 0
}
export function commentTextIssues(item: any, mode: string, limits: CommentTextLimits): CommentTextIssue[] {
  const fields: Array<['display_text' | 'source_text', string, number]> = []
  if (mode !== 'original') fields.push(['display_text', '中文', limits.translated])
  if (mode !== 'translated') fields.push(['source_text', '原文', limits.original])
  return fields.flatMap(([field, label, limit]) => {
    const length = commentTextLength(item[field] ?? (field === 'source_text' ? item.text : ''))
    const message = !length ? `${label}上屏文字为空`
      : length > limit ? `${label} ${length}/${limit} 字符，超出 ${length - limit} 字符` : ''
    return message ? [{ field, message }] : []
  })
}

export function commentTextBlockingIssues(item: any, mode: string, limits: CommentTextLimits): CommentTextIssue[] {
  const issues = commentTextIssues(item, mode, limits)
  // 手动保留的长文字仅作提示；缺少当前语言正文仍不能上屏。
  return item.selection_origin === 'manual'
    ? issues.filter(issue => !commentTextLength(item[issue.field] ?? (issue.field === 'source_text' ? item.text : '')))
    : issues
}

// 候选仍按建议长度筛选；用户明确保留后，长度只提示，不阻止预览和使用。
export function commentCandidateReadiness(item: any, mode: string, limits: CommentTextLimits, eligible: boolean | null) {
  if (eligible == null) return { status: 'pending-score' as const, issues: [] as CommentTextIssue[] }
  if (!eligible) return { status: 'not-suggested' as const, issues: [] as CommentTextIssue[] }
  if (mode !== 'original' && !commentTextLength(item.display_text))
    return { status: 'pending-translation' as const, issues: [] as CommentTextIssue[] }
  const issues = commentTextIssues(item, mode, limits)
  return { status: commentTextBlockingIssues(item, mode, limits).length ? 'needs-text' as const : 'ready' as const, issues }
}
