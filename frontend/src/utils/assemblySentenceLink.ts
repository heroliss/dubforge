import type { PartialRange } from './assemblyPartial.ts'

export type AssemblySentenceChange = {
  id: number; status: 'unchanged' | 'changed' | 'unknown'; change_reasons?: string[]
  change_kinds?: string[]; current_range?: PartialRange | null; baseline_range?: PartialRange | null
  dubbing_pending?: boolean
}
export type AssemblySentenceComparison = {
  baselines: { version_id: string; name: string; artifact_id: number; artifact_hash: string }[]
  baseline?: { version_id: string; name: string; artifact_id: number; artifact_hash: string; file_exists?: boolean } | null
  coverage?: 'known' | 'partial' | 'unknown'; sentences?: AssemblySentenceChange[]
  manual_candidates?: { id: number; fields?: string[]; reason?: string }[]
  global_changes?: string[]
  suggestion?: { line_ids: number[]; ranges: PartialRange[]; can_partial: boolean; reason?: string; replace_audio?: boolean }
}
export type AssemblySentenceDraft = {
  uid: string; token: string; created: number; baseline: string; artifactId: number; artifactHash: string
  lineIds: number[]; ranges: PartialRange[]; replaceAudio: boolean
}
type DraftStorage = Pick<Storage, 'getItem' | 'setItem'>
const key = (uid: string) => `dubforge.assembly-sentence-draft.${uid}`

// 仅传递待核对的操作意图；成片身份、时间轴和输入仍由后端检查。
export function saveAssemblySentenceDraft(storage: DraftStorage, draft: AssemblySentenceDraft) {
  storage.setItem(key(draft.uid), JSON.stringify(draft))
  return { path: `/projects/${encodeURIComponent(draft.uid)}/steps/assemble`,
    query: { partial_request: draft.token }, hash: '#assembly-partial' }
}
export function readAssemblySentenceDraft(storage: DraftStorage, uid: string, token: unknown, now = Date.now()): AssemblySentenceDraft | null {
  if (typeof token !== 'string' || !token || token.length > 80) return null
  try {
    const value = JSON.parse(storage.getItem(key(uid)) || 'null')
    if (!value || value.uid !== uid || value.token !== token || !Number.isFinite(value.created)
      || value.created > now + 60000 || now - value.created > 12 * 3600000
      || typeof value.baseline !== 'string' || !value.baseline || value.baseline.length > 80
      || !Number.isInteger(value.artifactId) || value.artifactId < 1 || typeof value.replaceAudio !== 'boolean'
      || typeof value.artifactHash !== 'string' || !/^[a-f0-9]{64}$/.test(value.artifactHash)
      || !Array.isArray(value.lineIds) || value.lineIds.length > 10000
      || value.lineIds.some((id: unknown) => !Number.isSafeInteger(id) || Number(id) < 1)
      || !Array.isArray(value.ranges) || value.ranges.length > 200
      || value.ranges.some((row: any) => !row || !Number.isFinite(row.start) || !Number.isFinite(row.end) || row.start < 0 || row.end <= row.start)
      || !value.lineIds.length && !value.ranges.length) return null
    return { uid, token, created: value.created, baseline: value.baseline, artifactId: value.artifactId, artifactHash: value.artifactHash,
      lineIds: [...new Set<number>(value.lineIds)], ranges: value.ranges.map((row: PartialRange) => ({ start: row.start, end: row.end })), replaceAudio: value.replaceAudio }
  } catch { return null }
}

export function assemblySentenceRevision(lines: any[]) {
  return JSON.stringify(lines.map(line => [line.id, line.source_text, line.text, line.start, line.end,
    line.playback_start, line.playback_end, line.audio_gain_db, line.selection,
    line.wav?.url, line.wav?.exists, line.wav?.changes_pending, line.synthesis_revision]))
}

export function assemblySentenceReason(row?: AssemblySentenceChange) {
  return row?.status === 'changed' ? (row.change_reasons || []).join('；') || '当前内容与基准成片不同' : ''
}

export function assemblyComparisonKnown(value: AssemblySentenceComparison | null | undefined) {
  return value?.coverage === 'known' && !(value.sentences || []).some(row => row.status === 'unknown')
}

export function assemblyReviewCandidates(value: AssemblySentenceComparison | null | undefined) {
  if (!value || assemblyComparisonKnown(value)) return []
  return (value.manual_candidates || []).filter(row => Number.isSafeInteger(row.id) && row.id > 0)
}

export function assemblySentenceNotices(value: AssemblySentenceComparison | null | undefined) {
  const result = new Map<number, { label: string; reason: string }>()
  for (const row of assemblyReviewCandidates(value)) result.set(row.id, {
    label: '成片待核对', reason: '本句有手工修改；旧成片缺少完整逐句快照，尚不能确认是否已应用，请核对原成片。',
  })
  for (const row of value?.sentences || []) if (row.status === 'changed')
    result.set(row.id, { label: '成片待更新', reason: assemblySentenceReason(row) })
  return result
}

export function assemblyDraftScope(comparison: AssemblySentenceComparison, ids: number[], automatic = false) {
  const selected = new Set(ids), rows = (comparison.sentences || []).filter(row => selected.has(row.id))
  // 时间改动需要同时抹去旧位置；不可仅根据当前起止时间修订。
  const spans = rows.flatMap(row => [row.baseline_range, row.current_range]).filter((row): row is PartialRange => !!row)
    .sort((a, b) => a.start - b.start)
  const merged: PartialRange[] = []
  for (const row of [...(automatic ? comparison.suggestion?.ranges || spans : spans)].sort((a, b) => a.start - b.start)) {
    const previous = merged[merged.length - 1]
    if (previous && row.start <= previous.end) previous.end = Math.max(previous.end, row.end)
    else merged.push({ start: row.start, end: row.end })
  }
  if (merged.length > 200 || selected.size > 10000) throw new Error('修订范围过多，请缩小勾选范围；单次最多 200 段、10000 句。')
  return { lineIds: [...selected], ranges: merged,
    replaceAudio: automatic ? comparison.suggestion?.replace_audio === true
      : rows.some(row => row.change_kinds?.includes('audio')) }
}
