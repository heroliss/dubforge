export type ReviewDecision = 'pending' | 'block' | 'allow'
export type ReviewDecisionAction = 'block' | 'allow' | 'reset'
export type ReviewAudioMode = 'voice_only' | 'all_audio'
export interface ReviewAudioProcessing {
  requested_mode: ReviewAudioMode | null
  effective_mode: ReviewAudioMode | 'unchanged' | null
  fallback_reason: string
}
export interface ContentContextRow {
  id: number | string
  start: number | null
  end: number | null
  source_text: string
  text: string
  source_audio_text: string
}
export interface ContentFinding {
  key: string
  evidence_fingerprint: string
  decision: ReviewDecision
  active: boolean
  decision_stale: boolean
  decision_reason: string
  decided_at: string
  ids: (number | string)[]
  context_ids: (number | string)[]
  context_rows: ContentContextRow[]
  fields: string[]
  start: number | null
  end: number | null
  reason: string
  source_text: string
  source_audio_text: string
  evidence_texts: string[]
  text: string
  rule: string
}
export interface ContentReviewRange { start: number; end: number }
export interface ContentReviewReport {
  enabled: boolean
  mute_audio: boolean
  audio_mode: ReviewAudioMode
  audio_processing: ReviewAudioProcessing | null
  blocked_terms: string[]
  blocked_ranges: string
  revision: number | null
  configuration_fingerprint: string
  decision_revision: string
  inherited: boolean | null
  origin: string
  checked_at: string
  rule_version: string
  blocker: { code: string; message: string } | null
  coverage: { original: number | null; source: number | null; translated: number | null; comments: number | null; complete_text: boolean }
  effective_ranges: ContentReviewRange[]
  effective_audio_ranges: ContentReviewRange[]
  blocked_ids: (number | string)[]
  stale_decision_count: number
  findings: ContentFinding[]
  original_findings: ContentFinding[]
  comment_findings: ContentFinding[]
  summary: { lines: number; blocked_lines: number; blocked_comments: number; matched_findings: number | null; pending_findings: number | null; allowed_findings: number | null; confirmed_findings: number | null }
  warnings: string[]
}
export interface CandidateReviewSource {
  review_ready?: boolean | null
  review_status?: string
  review_reason?: string
  review_revision_reason?: string
  review_revision_reasons?: { code: string; message: string; changes?: { label: string; before: unknown; after: unknown }[] }[]
}
export function candidateReview(value?: CandidateReviewSource | null): { label: string; reason: string; type: 'warning' | 'info' | 'default' } | null {
  if (!value || (value.review_ready !== true && value.review_ready !== false && value.review_ready !== null)) return null
  const reason = typeof value.review_revision_reason === 'string' && value.review_revision_reason.trim()
    ? value.review_revision_reason : reviewRevisionDetails(value).slice(0, 3).map(row => row.message).join('；')
      || (typeof value.review_reason === 'string' ? value.review_reason : '')
  const status = typeof value.review_status === 'string' ? value.review_status.trim() : ''
  if (value.review_ready === false) return { label: status || '需审查修订', reason: reason || '请按当前审查设置修订并重新生成；上传预检会阻止使用此文件。', type: 'warning' }
  if (value.review_ready === true) return { label: status || '已按当前规则处理', reason, type: 'info' }
  return { label: status || '文本审查暂无结论', reason, type: 'default' }
}
export function reviewRevisionDetails(value?: CandidateReviewSource | null) {
  return (Array.isArray(value?.review_revision_reasons) ? value.review_revision_reasons : [])
    .filter(reason => reason && typeof reason.message === 'string' && reason.message.trim())
    .map(reason => ({ code: text(reason.code), message: reason.message,
      changes: (Array.isArray(reason.changes) ? reason.changes : []).filter(change => change && typeof change.label === 'string')
        .map(change => ({ label: change.label, before: reviewChangeValue(change.before), after: reviewChangeValue(change.after) })) }))
}
function reviewChangeValue(value: unknown): string {
  if (typeof value === 'string') return value || '（空）'
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (Array.isArray(value)) return value.length ? value.map(reviewChangeValue).join('、') : '无'
  if (value && typeof value === 'object' && 'start' in value && 'end' in value) {
    const range = value as { start: unknown; end: unknown }
    return `${reviewTime(seconds(range.start))}–${reviewTime(seconds(range.end))}`
  }
  return '未提供'
}
export function reviewTerms(value: string): string[] {
  return [...new Set(value.split(/\r?\n/).map(line => line.trim()).filter(Boolean))]
}
function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}
function optionalCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null
}
function seconds(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}
function text(value: unknown): string { return typeof value === 'string' ? value : '' }
export function normalizeReviewAudioProcessing(value: unknown): ReviewAudioProcessing | null {
  const row = value as Record<string, unknown> | null
  if (!row || typeof row !== 'object') return null
  return {
    requested_mode: row.requested_mode === 'voice_only' || row.requested_mode === 'all_audio' ? row.requested_mode : null,
    effective_mode: row.effective_mode === 'voice_only' || row.effective_mode === 'all_audio' || row.effective_mode === 'unchanged' ? row.effective_mode : null,
    fallback_reason: text(row.fallback_reason),
  }
}
function ids(value: unknown, commentIds = false): (number | string)[] {
  return (Array.isArray(value) ? value : []).filter((id: unknown) => (typeof id === 'number' && Number.isInteger(id)) || (commentIds && typeof id === 'string' && !!id.trim()))
}
function ranges(value: unknown): ContentReviewRange[] {
  if (!Array.isArray(value)) return []
  return value.filter(row => row && seconds(row.start) !== null && seconds(row.end) !== null && row.end > row.start)
    .map(row => ({ start: row.start, end: row.end }))
}
function findings(value: unknown, commentIds = false): ContentFinding[] {
  if (!Array.isArray(value)) return []
  return value.filter(row => row && typeof row === 'object').map(row => ({
    key: text(row.key), evidence_fingerprint: text(row.evidence_fingerprint),
    decision: row.decision === 'block' || row.decision === 'allow' ? row.decision : 'pending',
    active: typeof row.active === 'boolean' ? row.active : row.decision !== 'allow',
    decision_stale: row.decision_stale === true, decision_reason: text(row.decision_reason), decided_at: text(row.decided_at),
    ids: ids(Array.isArray(row.ids) ? row.ids : row.id !== undefined ? [row.id] : [], commentIds),
    context_ids: ids(row.context_ids, commentIds),
    context_rows: (Array.isArray(row.context_rows) ? row.context_rows : []).filter((item: any) => item && typeof item === 'object')
      .map((item: any) => ({ id: item.id, start: seconds(item.start), end: seconds(item.end), source_text: text(item.source_text), text: text(item.text), source_audio_text: text(item.source_audio_text) })),
    fields: Array.isArray(row.fields) ? row.fields.filter((field: unknown) => typeof field === 'string') : [],
    start: seconds(row.start), end: seconds(row.end),
    reason: typeof row.reason === 'string' ? row.reason : '命中内容规则',
    source_text: text(row.source_text), source_audio_text: text(row.source_audio_text),
    evidence_texts: Array.isArray(row.evidence_texts) ? row.evidence_texts.filter((value: unknown) => typeof value === 'string') : [],
    text: text(row.text), rule: text(row.rule),
  }))
}
export function normalizeContentReview(value: unknown): ContentReviewReport {
  const row = value as Record<string, any> | null
  // 未知结构不能被展示成“未命中”，旧接口也不能产生无并发保护的写入。
  if (!row || typeof row.enabled !== 'boolean' || typeof row.mute_audio !== 'boolean'
      || !Array.isArray(row.findings) || !row.summary || typeof row.summary !== 'object') {
    throw new Error('服务暂未返回完整的内容审查结果，请刷新重试')
  }
  const coverage = row.coverage || {}
  return {
    enabled: row.enabled, mute_audio: row.mute_audio,
    audio_mode: row.audio_mode === 'all_audio' ? 'all_audio' : 'voice_only',
    audio_processing: normalizeReviewAudioProcessing(row.audio_processing),
    blocked_terms: Array.isArray(row.blocked_terms) ? row.blocked_terms.filter((term: unknown) => typeof term === 'string') : [],
    blocked_ranges: text(row.blocked_ranges),
    revision: typeof row.revision === 'number' && Number.isInteger(row.revision) && row.revision >= 0 ? row.revision : null,
    configuration_fingerprint: text(row.configuration_fingerprint), decision_revision: text(row.decision_revision),
    inherited: typeof row.inherited === 'boolean' ? row.inherited : null, origin: text(row.origin),
    checked_at: row.enabled ? text(row.checked_at) : '', rule_version: text(row.rule_version ?? row.version),
    blocker: row.blocker && typeof row.blocker.message === 'string' ? { code: text(row.blocker.code), message: row.blocker.message } : null,
    coverage: { original: optionalCount(coverage.original ?? coverage.original_lines), source: optionalCount(coverage.source ?? coverage.source_lines), translated: optionalCount(coverage.translated ?? coverage.translated_lines), comments: optionalCount(coverage.comments), complete_text: row.enabled && coverage.complete_text === true },
    effective_ranges: row.enabled ? ranges(row.effective_ranges ?? row.ranges) : [],
    effective_audio_ranges: row.enabled && row.mute_audio ? ranges(row.effective_audio_ranges) : [],
    blocked_ids: row.enabled ? ids(row.blocked_ids) : [], stale_decision_count: count(row.stale_decision_count),
    findings: findings(row.findings), original_findings: findings(row.original_findings), comment_findings: findings(row.comment_findings, true),
    summary: { lines: count(row.summary.lines), blocked_lines: count(row.summary.blocked_lines), blocked_comments: count(row.summary.blocked_comments), matched_findings: optionalCount(row.summary.matched_findings), pending_findings: optionalCount(row.summary.pending_findings), allowed_findings: optionalCount(row.summary.allowed_findings), confirmed_findings: optionalCount(row.summary.confirmed_findings) },
    warnings: Array.isArray(row.warnings) ? row.warnings.filter((warning: unknown) => typeof warning === 'string') : [],
  }
}
export function decisionLabel(finding: ContentFinding): string {
  return finding.decision === 'block' ? '已确认屏蔽' : finding.decision === 'allow' ? '已允许此项' : '待复核 · 暂按规则屏蔽'
}
export function findingEffect(finding: ContentFinding, report: ContentReviewReport, comment = false): string {
  if (!report.enabled) return '审查已关闭，未执行屏蔽'
  if (finding.active) return comment ? '此评论会被屏蔽' : `隐藏对应字幕；${reviewAudioEffect(report)}`
  if (!comment && ((!finding.key.startsWith('original:') && finding.ids.some(id => report.blocked_ids.includes(id)))
      || (finding.start !== null && finding.end !== null && report.effective_ranges.some(range => finding.start! < range.end && finding.end! > range.start)))) {
    return '已允许此项；其他命中或人工时段仍覆盖此段'
  }
  return comment ? '此项误报已允许，可重新核对评论方案' : '此项误报已允许，不再单独触发屏蔽'
}
export function reviewAudioEffect(report: Pick<ContentReviewReport, 'mute_audio' | 'audio_mode'> & Partial<Pick<ContentReviewReport, 'audio_processing'>>): string {
  if (!report.mute_audio) return '声音未静音'
  if (report.audio_processing?.effective_mode === 'unchanged') return '当前没有生效声音屏蔽范围'
  if (report.audio_processing?.effective_mode === 'voice_only') return '屏蔽命中语音，保留分离背景'
  if (report.audio_processing?.effective_mode === 'all_audio') return '命中时段整段静音（含背景）'
  return report.audio_mode === 'all_audio' ? '命中时段整段静音（含背景）' : '屏蔽命中语音；分离背景不可用时整段静音'
}
export function reviewDate(value: string): string {
  const stamp = Date.parse(value)
  return Number.isFinite(stamp) ? new Date(stamp).toLocaleString('zh-CN', { hourCycle: 'h23' }) : '尚未提供'
}
export function reviewTime(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return '时间未知'
  const millis = Math.round(value * 1000), seconds = Math.floor(millis / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}.${String(millis % 1000).padStart(3, '0')}`
}
