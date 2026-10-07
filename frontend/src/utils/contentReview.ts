export interface ContentFinding {
  ids: (number | string)[]
  start: number | null
  end: number | null
  reason: string
  source_text: string
  text: string
  rule: string
}

export interface ContentReviewReport {
  enabled: boolean
  mute_audio: boolean
  blocked_terms: string[]
  blocked_ranges: string
  revision: number | null
  configuration_fingerprint: string
  inherited: boolean | null
  origin: string
  findings: ContentFinding[]
  original_findings: ContentFinding[]
  comment_findings: ContentFinding[]
  summary: { lines: number; blocked_lines: number; blocked_comments: number }
  warnings: string[]
}

export interface CandidateReviewSource {
  review_ready?: boolean | null
  review_status?: string
  review_reason?: string
}

export function candidateReview(value?: CandidateReviewSource | null): { label: string; reason: string; type: 'warning' | 'info' | 'default' } | null {
  if (!value || (value.review_ready !== true && value.review_ready !== false && value.review_ready !== null)) return null
  const reason = typeof value.review_reason === 'string' ? value.review_reason : ''
  if (value.review_ready === false) return { label: '需审查修订', reason: reason || '请按当前审查设置修订并重新生成；上传预检会阻止使用此文件。', type: 'warning' }
  if (value.review_ready === true) return { label: '已按当前规则处理', reason, type: 'info' }
  return { label: typeof value.review_status === 'string' && value.review_status.trim() ? value.review_status : '文本审查暂无结论', reason, type: 'default' }
}

export function reviewTerms(value: string): string[] {
  return [...new Set(value.split(/\r?\n/).map(line => line.trim()).filter(Boolean))]
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}
function seconds(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}
function findings(value: unknown, commentIds = false): ContentFinding[] {
  if (!Array.isArray(value)) return []
  return value.filter(row => row && typeof row === 'object').map(row => ({
    ids: (Array.isArray(row.ids) ? row.ids : row.id !== undefined ? [row.id] : [])
      .filter((id: unknown) => (typeof id === 'number' && Number.isInteger(id)) || (commentIds && typeof id === 'string' && !!id.trim())),
    start: seconds(row.start), end: seconds(row.end),
    reason: typeof row.reason === 'string' ? row.reason : '命中内容规则',
    source_text: typeof row.source_text === 'string' ? row.source_text : '',
    text: typeof row.text === 'string' ? row.text : '',
    rule: typeof row.rule === 'string' ? row.rule : '',
  }))
}

export function normalizeContentReview(value: unknown): ContentReviewReport {
  const row = value as Record<string, any> | null
  // 未知结构不能被展示成“未命中”，旧接口也不能产生无并发保护的设置写入。
  if (!row || typeof row.enabled !== 'boolean' || typeof row.mute_audio !== 'boolean'
      || !Array.isArray(row.findings) || !row.summary || typeof row.summary !== 'object') {
    throw new Error('服务暂未返回完整的内容审查结果，请刷新重试')
  }
  return {
    enabled: row.enabled, mute_audio: row.mute_audio,
    blocked_terms: Array.isArray(row.blocked_terms) ? row.blocked_terms.filter((term: unknown) => typeof term === 'string') : [],
    blocked_ranges: typeof row.blocked_ranges === 'string' ? row.blocked_ranges : '',
    revision: typeof row.revision === 'number' && Number.isInteger(row.revision) && row.revision >= 0 ? row.revision : null,
    configuration_fingerprint: typeof row.configuration_fingerprint === 'string' ? row.configuration_fingerprint : '',
    inherited: typeof row.inherited === 'boolean' ? row.inherited : null,
    origin: typeof row.origin === 'string' ? row.origin : '',
    findings: findings(row.findings), original_findings: findings(row.original_findings), comment_findings: findings(row.comment_findings, true),
    summary: { lines: count(row.summary.lines), blocked_lines: count(row.summary.blocked_lines), blocked_comments: count(row.summary.blocked_comments) },
    warnings: Array.isArray(row.warnings) ? row.warnings.filter((warning: unknown) => typeof warning === 'string') : [],
  }
}

export function reviewTime(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return '时间未知'
  const millis = Math.round(value * 1000), seconds = Math.floor(millis / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}.${String(millis % 1000).padStart(3, '0')}`
}
