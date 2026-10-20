import type { AssemblySentenceComparison } from './assemblySentenceLink.ts'
import { partialTime } from './assemblyPartial.ts'

export type AssemblyChangeKind = 'subtitle' | 'audio' | 'comment' | 'cuts' | 'settings'
export type AssemblyChange = { id: string; line_id?: number; comment_id?: string; reason: string; before?: any; current?: any;
  selectable?: boolean; blocked_reason?: string; linked_ids?: string[];
  baseline_range?: { start: number; end: number } | null; current_range?: { start: number; end: number } | null }
export type AssemblyChangeGroup = { kind: AssemblyChangeKind; label: string; status: 'changed' | 'unchanged' | 'unknown'; count: number; changes: AssemblyChange[] }
export type AssemblyChanges = AssemblySentenceComparison & { groups?: AssemblyChangeGroup[]; full_rebuild_reasons?: string[];
  needs_update?: boolean; whole_render_changed?: boolean; partial_unavailable_reasons?: string[];
  summary?: { total_changes?: number; changed?: number; unknown?: number; dubbing_pending?: number };
  manual_candidates?: { id: number; fields: string[]; current: { source_text: string; text: string; start: number; end: number }; reason: string; range: { start: number; end: number } }[] }

export function changeGroupCount(group: AssemblyChangeGroup) { return group.status === 'unknown' ? '待核对' : String(group.count) }

// 只有真实差异参与选择；旧记录缺失另行核对，不能被当作无差异或自动选中。
export function visibleChangeGroups(data: AssemblyChanges | null) {
  return (data?.groups || []).filter(group => group.status === 'changed' && group.changes.length > 0)
}
export function actualChanges(data: AssemblyChanges | null) { return visibleChangeGroups(data).flatMap(group => group.changes) }
const changeLinks = new WeakMap<AssemblyChange[], Map<string, { ids: string[]; blocked: string }>>()
function selectionLinks(rows: AssemblyChange[]) {
  const cached = changeLinks.get(rows)
  if (cached) return cached
  const byId = new Map(rows.map(row => [row.id, row])), neighbors = new Map(rows.map(row => [row.id, new Set(row.linked_ids || [])]))
  for (const row of rows) for (const id of row.linked_ids || []) neighbors.get(id)?.add(row.id)
  const result = new Map<string, { ids: string[]; blocked: string }>()
  for (const row of rows) {
    if (result.has(row.id)) continue
    const ids: string[] = [], pending = [row.id], seen = new Set<string>()
    let blocked = ''
    while (pending.length) {
      const id = pending.pop()!
      if (seen.has(id)) continue
      seen.add(id)
      const item = byId.get(id)
      if (!item) { blocked ||= '关联改动尚未完整读取，请重新检查。'; continue }
      ids.push(id)
      if (item.selectable === false || item.blocked_reason) blocked = item.blocked_reason || blocked || '此改动暂时无法独立应用，请先处理关联内容。'
      pending.push(...(neighbors.get(id) || []))
    }
    const linked = { ids, blocked }
    for (const id of ids) result.set(id, linked)
  }
  changeLinks.set(rows, result)
  return result
}
export function linkedChangeIds(rows: AssemblyChange[], ids: string[]) {
  const links = selectionLinks(rows)
  return [...new Set(ids.flatMap(id => links.get(id)?.ids || []))]
}
export function changeBlockedReason(row: AssemblyChange, rows: AssemblyChange[]) {
  return selectionLinks(rows).get(row.id)?.blocked || ''
}
export type ChangeSelection = { baseline: string; seen: string[]; selected: string[]; added: string[] }
export function reconcileChangeSelection(data: AssemblyChanges | null, previous?: ChangeSelection | null): ChangeSelection {
  const rows = actualChanges(data), baseline = data?.baseline?.version_id || ''
  const available = rows.filter(row => !changeBlockedReason(row, rows)).map(row => row.id)
  if (!previous || previous.baseline !== baseline) return { baseline, seen: rows.map(row => row.id), selected: available, added: [] }
  return { baseline, seen: [...new Set([...previous.seen, ...rows.map(row => row.id)])],
    selected: previous.selected.filter(id => available.includes(id) && linkedChangeIds(rows, [id]).every(link => previous.selected.includes(link))),
    added: rows.filter(row => !previous.seen.includes(row.id)).map(row => row.id) }
}
export function toggleChangeSelection(rows: AssemblyChange[], selected: string[], ids: string[], checked: boolean) {
  const linked = linkedChangeIds(rows, ids)
  if (!checked) return selected.filter(id => !linked.includes(id))
  return [...new Set([...selected, ...linked.filter(id => { const row = rows.find(item => item.id === id); return row && !changeBlockedReason(row, rows) })])]
}
export function changeRangeLabel(item: AssemblyChange) {
  const format = (row: { start: number; end: number }) => `${partialTime(row.start)}–${partialTime(row.end)}`
  const before = item.baseline_range, current = item.current_range
  if (before && current && (before.start !== current.start || before.end !== current.end)) return `原来 ${format(before)} → 当前 ${format(current)}`
  return current ? format(current) : before ? `原来 ${format(before)}` : ''
}
export function changeCount(data: AssemblyChanges) {
  return data.summary?.total_changes ?? data.groups?.reduce((total, group) => total + group.count, 0) ?? 0
}
export function changeText(kind: AssemblyChangeKind, value: any): string {
  if (kind === 'cuts' && value === null) return '保留完整视频'
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.map(row => changeText(kind, row)).filter(Boolean).join('\n')
  if (kind === 'subtitle') return [value.source_text && `原文：${value.source_text}`, value.text && `译文：${value.text}`].filter(Boolean).join('\n')
  if (kind === 'comment') {
    const parts = Array.isArray(value.parts) ? value.parts : [value.parts || value.display_text || value.text]
    return parts.map((part: any) => typeof part === 'string' ? part : part?.text || part?.display_text || '').filter(Boolean).join('\n')
  }
  if (kind === 'audio' && value.source) return `${value.source === 'original' ? '原声' : '配音'}${typeof value.gain_db === 'number' ? ` · 增益 ${value.gain_db > 0 ? '+' : ''}${value.gain_db} dB` : ''}`
  if (kind === 'settings' && typeof value !== 'object') {
    const labels: Record<string, string> = { true: '开启', false: '关闭', off: '关闭', uniform: '统一响度', normalize: '保留动态', source: '原文', trans: '译文', bilingual: '双语' }
    return labels[String(value)] || String(value)
  }
  if (kind === 'cuts') {
    if (typeof value === 'string') return value
    if (typeof value !== 'object') return ''
    const ranges = value.ranges || value.effective || []
    return ranges.length ? `删除 ${ranges.map((row: any) => `${partialTime(Number(row.start))}–${partialTime(Number(row.end))}`).join('、')}` : '保留完整视频'
  }
  return ''
}
export function changeComparisonText(kind: AssemblyChangeKind, item: AssemblyChange, side: 'before' | 'current'): string {
  if (kind !== 'subtitle') return changeText(kind, item[side])
  const text = (value: any, field: string) => typeof value?.[field] === 'string' ? value[field] : ''
  // 文档包含两种语言；对照只展示真实改动字段，时间改动由统一时间范围表达。
  return (['source_text', 'text'] as const)
    .filter(field => text(item.before, field) !== text(item.current, field))
    .map(field => `${field === 'source_text' ? '原文' : '译文'}：${text(item[side], field) || '（空）'}`)
    .join('\n')
}
export function updateAction(data: AssemblyChanges | null) {
  if (!data?.baseline) return 'none'
  if (data.suggestion?.can_partial && (data.suggestion.line_ids.length || data.suggestion.ranges.length)) return 'partial'
  // 局部更新的能力限制不表示成片已经过期，例如未修改的剪辑成片。
  if (data.coverage !== 'known' || data.needs_update || data.whole_render_changed || data.global_changes?.length || changeCount(data)) return 'full'
  return 'unchanged'
}
