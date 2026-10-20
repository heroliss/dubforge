import { formatTimecode, parseTimecode } from './timecode.ts'

export type OutputCutRange = { id: string; start: number | null; end: number | null; reason: string; enabled: boolean }
export type OutputCutInterval = { start: number; end: number }
export type OutputCutsSnapshot = { source_duration: number; ranges: OutputCutInterval[]; output_duration?: number }
export type OutputCutSegment = { start: number; end: number; output_start: number; output_end: number }
export type VirtualPreviewSegment = {
  source_start: number; source_end: number
  media_kind: 'target' | 'source' | 'version' | 'unavailable'
  media_version_id?: string | null
  media_start: number | null; media_end: number | null
  virtual_start: number; virtual_end: number
  available: boolean
}
export type VirtualPreviewDonor = { kind: 'source' | 'version'; version_id?: string | null; cuts?: OutputCutsSnapshot | null }
export type OutputCutTimeBias = 'next' | 'previous'
export type OutputCutChapter = { title?: string; start_time?: number; end_time?: number | null; start?: number; end?: number | null }
export type OutputCutsState = {
  revision: string; source_fingerprint: string; duration: number | null; frame_rate?: number | null; enabled: boolean
  ranges: OutputCutRange[]; effective_ranges: OutputCutInterval[]; removed_duration: number; output_duration: number | null
  warnings: string[]; source_url: string | null; source_changed?: boolean; bound_source_fingerprint?: string | null
  version_id?: string | null; scope?: 'project' | 'version';
  baseline?: { version_id?: string | null; artifact_id?: number | string | null; generated_at?: string | null; ranges?: OutputCutRange[] } | null;
  cut_baseline?: OutputCutsState['baseline']; base_version_id?: string | null; baseline_version_id?: string | null;
}

/** 秒和时:分:秒输入共用解析器；不接受负数、无限值或越界的分秒字段。 */
export function parseCutTime(value: string): number | null {
  return parseTimecode(value)
}

export function formatCutTime(value: number | null | undefined): string {
  return formatTimecode(value, 3)
}

export function cloneCutRanges(ranges: OutputCutRange[]): OutputCutRange[] {
  return ranges.map(row => ({ id: row.id, start: row.start, end: row.end, reason: row.reason || '', enabled: row.enabled !== false }))
}

export function mergeCutRanges(ranges: OutputCutRange[], duration: number | null, enabled = true): OutputCutInterval[] {
  if (!enabled || typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0) return []
  const intervals = ranges.filter(row => row.enabled !== false && typeof row.start === 'number' && typeof row.end === 'number'
    && Number.isFinite(row.start) && Number.isFinite(row.end) && row.end > row.start && row.start >= 0 && row.end <= duration)
    .map(row => ({ start: row.start as number, end: row.end as number })).sort((a, b) => a.start - b.start || a.end - b.end)
  const merged: OutputCutInterval[] = []
  for (const row of intervals) {
    const previous = merged[merged.length - 1]
    if (previous && row.start <= previous.end) previous.end = Math.max(previous.end, row.end)
    else merged.push({ ...row })
  }
  return merged
}

export function validateCutRanges(ranges: OutputCutRange[], duration: number | null, enabled = true): string[] {
  const errors: string[] = []
  if (typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0) return ['原视频与时长就绪后才能保存剪辑方案。']
  if (ranges.length > 200) errors.push('删除清单最多保留 200 个片段。')
  ranges.forEach((row, index) => {
    const prefix = `第 ${index + 1} 段：`
    if (typeof row.start !== 'number' || typeof row.end !== 'number' || !Number.isFinite(row.start) || !Number.isFinite(row.end)) errors.push(prefix + '请填写有效的起点和终点。')
    else if (row.start < 0 || row.end > duration) errors.push(prefix + '时间必须位于原视频范围内。')
    else if (row.end <= row.start) errors.push(prefix + '终点必须晚于起点。')
  })
  const removed = mergeCutRanges(ranges, duration, enabled).reduce((sum, row) => sum + row.end - row.start, 0)
  if (enabled && duration - removed < .001) errors.push('至少保留一段视频，不能删除全部内容。')
  return errors
}

/** 跳过试听不触发播放；调用方仅在正在播放时推进到保留区间。 */
export function nextRetainedTime(time: number, ranges: OutputCutInterval[]): number {
  const interval = ranges.find(row => time >= row.start && time < row.end)
  return interval ? interval.end : time
}

/**
 * 求“草稿删除范围”相对于“已生成成片删除范围”的新增部分。
 *
 * 成片预览文件已经包含 base 中的删除，因而不能再次把 base 范围映射到
 * 成片时间轴。这个函数只返回本次编辑新增的删除段，供浏览器试听时跳过；
 * 恢复旧删除段无法从当前成片文件恢复，调用方应提示用户回到源视频或选择
 * 其它成片作为基准。
 */
export function subtractCutIntervals(draft: readonly OutputCutInterval[], base: readonly OutputCutInterval[]): OutputCutInterval[] {
  const baseRanges = [...base]
    .filter(row => Number.isFinite(row.start) && Number.isFinite(row.end) && row.end > row.start)
    .map(row => ({ start: row.start, end: row.end }))
    .sort((a, b) => a.start - b.start || a.end - b.end)
  const result: OutputCutInterval[] = []
  for (const row of [...draft].filter(item => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start)) {
    let cursor = row.start
    for (const cut of baseRanges) {
      if (cut.end <= cursor) continue
      if (cut.start >= row.end) break
      if (cut.start > cursor) result.push({ start: cursor, end: Math.min(cut.start, row.end) })
      cursor = Math.max(cursor, cut.end)
      if (cursor >= row.end) break
    }
    if (cursor < row.end) result.push({ start: cursor, end: row.end })
  }
  return mergeIntervals(result)
}

/** 将源时间轴上的新增删除段映射到已经生成的成片时间轴。 */
export function mapSourceIntervalsToOutput(ranges: readonly OutputCutInterval[], cuts: OutputCutsSnapshot | null | undefined): OutputCutInterval[] {
  if (!cuts) return mergeIntervals(ranges.map(row => ({ ...row })))
  const result: OutputCutInterval[] = []
  for (const row of ranges) {
    const left = sourceToOutputTime(cuts, row.start)
    const right = sourceToOutputTime(cuts, row.end)
    if (left === null || right === null || right <= left + OUTPUT_CUT_EPSILON) continue
    result.push({ start: left, end: right })
  }
  return mergeIntervals(result)
}

/**
 * 按原片时间轴建立“当前成片 + 恢复来源”的试听计划。
 *
 * `baseCuts` 必须来自当前播放器实际对应的正式成片快照，不能直接拿当前
 * 编辑草稿代替；`draftCuts` 是用户当前清单。草稿仍保留的源区间如果已经
 * 存在于基片，就从基片播放；如果基片已删除，则从 donor 播放。新增删除
 * 区间不会生成试听片段。该计划只用于试听，不参与正式成片指纹。
 */
export function buildVirtualPreviewSegments(
  sourceDuration: number,
  baseCuts: OutputCutsSnapshot | null | undefined,
  draftCuts: OutputCutsSnapshot | null | undefined,
  donor: VirtualPreviewDonor = { kind: 'source' },
): VirtualPreviewSegment[] {
  if (!Number.isFinite(sourceDuration) || sourceDuration <= 0) return []
  const base = normalizedCutIntervals(baseCuts, sourceDuration)
  const draft = normalizedCutIntervals(draftCuts, sourceDuration)
  const boundaries = new Set<number>([0, sourceDuration])
  for (const row of [...base, ...draft]) { boundaries.add(row.start); boundaries.add(row.end) }
  const ordered = [...boundaries].sort((a, b) => a - b)
  const result: VirtualPreviewSegment[] = []
  let virtual = 0
  for (let index = 0; index + 1 < ordered.length; index++) {
    const start = ordered[index]!, end = ordered[index + 1]!
    if (end <= start + OUTPUT_CUT_EPSILON || containsInterval(draft, start, end)) continue
    const fromTarget = !containsInterval(base, start, end)
    const sourceSegments = fromTarget
      ? mapPreviewMediaInterval(start, end, baseCuts, 'target')
      : mapPreviewMediaInterval(start, end, donor.cuts, donor.kind === 'version' ? 'version' : 'source', donor.version_id)
    for (const media of sourceSegments) {
      const length = media.source_end - media.source_start
      if (length <= OUTPUT_CUT_EPSILON) continue
      result.push({ ...media, virtual_start: virtual, virtual_end: virtual + length })
      virtual += length
    }
  }
  return result
}

function normalizedCutIntervals(cuts: OutputCutsSnapshot | null | undefined, duration: number): OutputCutInterval[] {
  if (!cuts) return []
  return mergeIntervals((cuts.ranges || []).map(row => ({
    start: Math.max(0, Math.min(duration, row.start)),
    end: Math.max(0, Math.min(duration, row.end)),
  })))
}

function containsInterval(ranges: readonly OutputCutInterval[], start: number, end: number) {
  return ranges.some(row => start >= row.start - OUTPUT_CUT_EPSILON && end <= row.end + OUTPUT_CUT_EPSILON)
}

function mapPreviewMediaInterval(
  start: number,
  end: number,
  cuts: OutputCutsSnapshot | null | undefined,
  kind: VirtualPreviewSegment['media_kind'],
  versionId?: string | null,
): Omit<VirtualPreviewSegment, 'virtual_start' | 'virtual_end'>[] {
  const segments = cuts ? retainedOutputSegments(cuts) : [{ start: 0, end: Number.POSITIVE_INFINITY, output_start: 0, output_end: Number.POSITIVE_INFINITY }]
  const result: Omit<VirtualPreviewSegment, 'virtual_start' | 'virtual_end'>[] = []
  let cursor = start
  for (const segment of segments) {
    const left = Math.max(start, segment.start), right = Math.min(end, segment.end)
    if (right <= left + OUTPUT_CUT_EPSILON) continue
    if (left > cursor + OUTPUT_CUT_EPSILON) {
      result.push({ source_start: cursor, source_end: left, media_kind: 'unavailable', media_version_id: versionId || null, media_start: null, media_end: null, available: false })
    }
    const mediaStart = segment.output_start + (left - segment.start)
    const mediaEnd = mediaStart + (right - left)
    result.push({ source_start: left, source_end: right, media_kind: kind, media_version_id: versionId || null, media_start: mediaStart, media_end: mediaEnd, available: true })
    cursor = right
  }
  if (cursor < end - OUTPUT_CUT_EPSILON) result.push({ source_start: cursor, source_end: end, media_kind: 'unavailable', media_version_id: versionId || null, media_start: null, media_end: null, available: false })
  return result
}

function mergeIntervals(ranges: readonly OutputCutInterval[]): OutputCutInterval[] {
  const ordered = [...ranges]
    .filter(row => Number.isFinite(row.start) && Number.isFinite(row.end) && row.end > row.start)
    .map(row => ({ start: row.start, end: row.end }))
    .sort((a, b) => a.start - b.start || a.end - b.end)
  const result: OutputCutInterval[] = []
  for (const row of ordered) {
    const previous = result[result.length - 1]
    if (previous && row.start <= previous.end + OUTPUT_CUT_EPSILON) previous.end = Math.max(previous.end, row.end)
    else result.push(row)
  }
  return result
}

const OUTPUT_CUT_EPSILON = 0.000001

/**
 * 将剪辑方案展开为“源视频保留区间→成片区间”的单调映射表。
 *
 * 这个表是预览同步和章节/字幕时间换算的共同底层语义：删除区间不会出现在
 * 成片时间轴中，相邻保留区间在输出侧首尾相接。输入即使乱序或有重叠，也会
 * 在这里归一化；不会修改调用方传入的 ranges。
 */
export function retainedOutputSegments(cuts: OutputCutsSnapshot | null | undefined): OutputCutSegment[] {
  if (!cuts || !Number.isFinite(cuts.source_duration) || cuts.source_duration <= 0) return []
  const sourceDuration = cuts.source_duration
  const intervals = (Array.isArray(cuts.ranges) ? cuts.ranges : [])
    .filter(row => row && Number.isFinite(row.start) && Number.isFinite(row.end) && row.end > row.start)
    .map(row => ({ start: Math.max(0, row.start), end: Math.min(sourceDuration, row.end) }))
    .filter(row => row.end > row.start + OUTPUT_CUT_EPSILON)
    .sort((a, b) => a.start - b.start || a.end - b.end)
  const merged: OutputCutInterval[] = []
  for (const row of intervals) {
    const previous = merged[merged.length - 1]
    if (previous && row.start <= previous.end + OUTPUT_CUT_EPSILON) previous.end = Math.max(previous.end, row.end)
    else merged.push({ ...row })
  }
  const result: OutputCutSegment[] = []
  let sourceCursor = 0, outputCursor = 0
  for (const cut of merged) {
    if (cut.start > sourceCursor + OUTPUT_CUT_EPSILON) {
      const length = cut.start - sourceCursor
      result.push({ start: sourceCursor, end: cut.start, output_start: outputCursor, output_end: outputCursor + length })
      outputCursor += length
    }
    sourceCursor = Math.max(sourceCursor, cut.end)
  }
  if (sourceCursor < sourceDuration - OUTPUT_CUT_EPSILON) {
    result.push({ start: sourceCursor, end: sourceDuration, output_start: outputCursor, output_end: outputCursor + sourceDuration - sourceCursor })
  }
  return result
}

/** 将原视频时间定位到成片时间；删除段内部的时间收敛到剪辑接缝。 */
export function sourceToOutputTime(cuts: OutputCutsSnapshot | null | undefined, seconds: number): number | null {
  if (!Number.isFinite(seconds)) return null
  const segments = retainedOutputSegments(cuts)
  if (!cuts) return seconds
  if (!segments.length) return 0
  const sourceDuration = Number.isFinite(cuts.source_duration) ? cuts.source_duration : segments[segments.length - 1]!.end
  const sourceTime = Math.max(0, Math.min(sourceDuration, seconds))
  for (const segment of segments) {
    if (sourceTime < segment.start) return segment.output_start
    if (sourceTime <= segment.end + OUTPUT_CUT_EPSILON) {
      return segment.output_start + Math.max(0, Math.min(segment.end - segment.start, sourceTime - segment.start))
    }
  }
  return segments[segments.length - 1]!.output_end
}

/**
 * 将成片时间反向定位到原视频时间。删除接缝处没有唯一的源位置：
 * `next` 进入接缝后的下一段，`previous` 停在接缝前的上一段末尾。
 */
export function outputToSourceTime(cuts: OutputCutsSnapshot | null | undefined, seconds: number, bias: OutputCutTimeBias = 'next'): number | null {
  if (!Number.isFinite(seconds)) return null
  if (!cuts) return seconds
  const segments = retainedOutputSegments(cuts)
  if (!segments.length) return 0
  const outputDuration = segments[segments.length - 1]!.output_end
  const outputTime = Math.max(0, Math.min(outputDuration, seconds))
  for (let index = 0; index < segments.length; index++) {
    const segment = segments[index]!
    if (outputTime < segment.output_start - OUTPUT_CUT_EPSILON) return segment.start
    if (outputTime < segment.output_end - OUTPUT_CUT_EPSILON) {
      return segment.start + Math.max(0, Math.min(segment.end - segment.start, outputTime - segment.output_start))
    }
    if (Math.abs(outputTime - segment.output_end) <= OUTPUT_CUT_EPSILON) {
      const next = segments[index + 1]
      if (next && bias === 'next') return next.start
      return segment.end
    }
  }
  return segments[segments.length - 1]!.end
}

/** 用成片自己的剪辑快照映射章节；整章被删时移除，不能只折叠章节起点。 */
export function mapOutputCutChapters<T extends OutputCutChapter>(chapters: readonly T[], cuts: OutputCutsSnapshot | null | undefined): T[] {
  if (!cuts) return chapters.map(row => ({ ...row }))
  const epsilon = .000001
  const kept: Array<OutputCutInterval & { outputStart: number }> = []
  let cursor = 0, output = 0
  for (const cut of cuts.ranges) {
    const left = Math.max(0, cut.start), right = Math.min(cuts.source_duration, cut.end)
    if (right <= left) continue
    if (left > cursor + epsilon) {
      kept.push({ start: cursor, end: left, outputStart: output })
      output += left - cursor
    }
    cursor = Math.max(cursor, right)
  }
  if (cursor < cuts.source_duration - epsilon) kept.push({ start: cursor, end: cuts.source_duration, outputStart: output })
  const sourceStart = (row: T) => Number(row.start_time ?? row.start ?? 0)
  const ordered = [...chapters].sort((a, b) => sourceStart(a) - sourceStart(b))
  const result: T[] = []
  for (let index = 0; index < ordered.length; index++) {
    const row = ordered[index]!, start = sourceStart(row)
    const nextStart = index + 1 < ordered.length ? sourceStart(ordered[index + 1]!) : cuts.source_duration
    const end = Number((row.end_time !== undefined ? row.end_time : row.end) || nextStart)
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue
    const intersections = kept.flatMap(segment => {
      const left = Math.max(start, segment.start), right = Math.min(end, segment.end)
      return right > left + epsilon ? [{ start: segment.outputStart + left - segment.start, end: segment.outputStart + right - segment.start }] : []
    })
    if (!intersections.length) continue
    const left = Math.round(intersections[0]!.start * 1e6) / 1e6
    const right = Math.round(intersections[intersections.length - 1]!.end * 1e6) / 1e6
    const previous = result[result.length - 1]
    if (previous && Math.abs(Number(previous.start_time) - left) <= epsilon) continue
    result.push({ ...row, start_time: left, end_time: right,
      ...('start' in row ? { start: left } : {}), ...('end' in row ? { end: right } : {}) })
  }
  return result
}
