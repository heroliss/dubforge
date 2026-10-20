import { parseCutTime } from './outputCuts.ts'
import { formatTimecode } from './timecode.ts'

export type PartialRangeDraft = { key: string; start: string; end: string }
export type PartialRange = { start: number; end: number }

export function partialRanges(rows: PartialRangeDraft[], duration?: number | null): PartialRange[] {
  return rows.map((row, index) => {
    const start = parseCutTime(row.start), end = parseCutTime(row.end)
    if (start === null || end === null || start < 0 || end <= start)
      throw new Error(`第 ${index + 1} 段的结束时间需晚于开始时间，可填写秒数或时:分:秒。`)
    if (duration && end > duration) throw new Error(`第 ${index + 1} 段超出视频时长。`)
    return { start, end }
  })
}

export function partialTime(value: number) {
  return formatTimecode(value, 3)
}

export function partialSummary(plan: any) {
  const ranges = plan.ranges || plan.requested_ranges || []
  const seconds = ranges.reduce((sum: number, row: PartialRange) => sum + row.end - row.start, 0)
  const encoded = typeof plan.encode_seconds === 'number' ? `，含边界需编码约 ${formatTimecode(plan.encode_seconds, 3)}` : ''
  return `${ranges.length} 段 · 更新约 ${formatTimecode(seconds, 3)}${encoded}`
}
