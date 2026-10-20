/** 发布时间只决定排列；评分与人工保留分别决定选入范围。 */
type TimedComment = { id: string; source_hash?: string; timestamp?: unknown }

export function commentTimestamp(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

export function chronologicalComments<T extends TimedComment>(items: T[], sources: TimedComment[] = items): T[] {
  const sourceMap = new Map(sources.map((row, index) => [row.id, { row, index }]))
  const key = (item: T, index: number) => {
    const source = sourceMap.get(item.id)
    const row = source && (!item.source_hash || source.row.source_hash === item.source_hash) ? source.row : item
    const timestamp = commentTimestamp(row.timestamp)
    return [timestamp === null ? 1 : 0, timestamp ?? 0, source?.index ?? sources.length + index]
  }
  return items.map((item, index) => ({ item, key: key(item, index) }))
    .sort((a, b) => a.key[0]! - b.key[0]! || a.key[1]! - b.key[1]! || a.key[2]! - b.key[2]!)
    .map(entry => entry.item)
}

const dateFormat = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
export function commentPublishedAt(value: unknown): string {
  const timestamp = commentTimestamp(value)
  const date = timestamp === null ? null : new Date(timestamp * 1000)
  return date && Number.isFinite(date.getTime()) ? dateFormat.format(date) : '发布时间未知'
}
