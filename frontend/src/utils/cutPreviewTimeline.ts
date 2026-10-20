export type CutPreviewBias = 'next' | 'previous'
export type CutPreviewDeleteRange = { start: number; end: number; enabled?: boolean }
export type CutPreviewFrameRange = Readonly<{
  startFrame: number; endFrame: number; start: number; end: number
}>
export type CutPreviewKeptRange = CutPreviewFrameRange & Readonly<{
  previewStartFrame: number; previewEndFrame: number; previewStart: number; previewEnd: number
}>
export type CutPreviewTimeline = Readonly<{
  valid: boolean; empty: boolean; error: string | null
  mediaDuration: number; fps: number; frameCount: number; keptFrameCount: number; duration: number
  deletedRanges: readonly CutPreviewFrameRange[]; keptRanges: readonly CutPreviewKeptRange[]
  fromMedia: (mediaSeconds: number) => number
  toMedia: (previewSeconds: number, bias?: CutPreviewBias) => number
  stepMedia: (mediaSeconds: number, direction: number) => number
}>
export type CutPreviewTimelineInput = {
  mediaDuration: number; fps: number; deleteRanges?: readonly CutPreviewDeleteRange[]
}

// 容差以帧为单位，只消除浮点误差，不把落在帧内部的删除边界取回去。
const FRAME_EPSILON = 1e-7
const MEDIA_FRAME_EPSILON = .001
const finite = (value: number) => typeof value === 'number' && Number.isFinite(value)
const clamp = (value: number, lower: number, upper: number) => Math.min(upper, Math.max(lower, value))

/**
 * 单一媒体的剪后逻辑时间轴。删除向外取整，区间均为 [startFrame,endFrame)。
 *
 * 播放进度在保留区内连续，删除区折叠成接缝；定位接缝默认进入下一保留帧。
 * previous 选择上一保留帧，逻辑终点选择最后保留帧，避免 seek 落入删除区。
 * 无效或全删除时映射返回 0，调用者须先检查 valid && !empty。
 */
export function buildCutPreviewTimeline(input: CutPreviewTimelineInput): CutPreviewTimeline {
  const mediaDuration = finite(input.mediaDuration) && input.mediaDuration > 0 ? input.mediaDuration : 0
  const fps = finite(input.fps) && input.fps > 0 ? input.fps : 0
  const rawFrames = mediaDuration * fps
  // ffprobe/浏览器时长常以六位小数传输，不能把 114967.99998 误作少一帧。
  // 这个容差只用于媒体总帧数，删除起止仍使用严格的帧边界容差。
  const nearestFrame = Math.round(rawFrames)
  const rawCount = Math.abs(rawFrames - nearestFrame) <= MEDIA_FRAME_EPSILON ? nearestFrame : Math.floor(rawFrames + FRAME_EPSILON)
  const valid = mediaDuration > 0 && fps > 0 && Number.isSafeInteger(rawCount) && rawCount > 0
  const frameCount = valid ? rawCount : 0
  const error = valid ? null : '视频时长和帧率就绪后才能按帧预览剪辑。'
  const deletedFrames: Array<{ startFrame: number; endFrame: number }> = []
  if (valid) {
    for (const range of input.deleteRanges || []) {
      if (range.enabled === false || !finite(range.start) || !finite(range.end) || range.end <= range.start) continue
      const startFrame = clamp(Math.floor(range.start * fps + FRAME_EPSILON), 0, frameCount)
      const endFrame = clamp(Math.ceil(range.end * fps - FRAME_EPSILON), 0, frameCount)
      if (endFrame > startFrame) deletedFrames.push({ startFrame, endFrame })
    }
    deletedFrames.sort((a, b) => a.startFrame - b.startFrame || a.endFrame - b.endFrame)
  }
  const merged: Array<{ startFrame: number; endFrame: number }> = []
  for (const range of deletedFrames) {
    const previous = merged[merged.length - 1]
    if (previous && range.startFrame <= previous.endFrame) previous.endFrame = Math.max(previous.endFrame, range.endFrame)
    else merged.push({ ...range })
  }
  const deletedRanges = Object.freeze(merged.map(range => Object.freeze({ ...range,
    start: range.startFrame / fps, end: range.endFrame / fps })))
  const kept: CutPreviewKeptRange[] = []
  let mediaCursor = 0, previewCursor = 0
  const addKept = (startFrame: number, endFrame: number) => {
    if (endFrame <= startFrame) return
    const previewEndFrame = previewCursor + endFrame - startFrame
    kept.push(Object.freeze({ startFrame, endFrame, start: startFrame / fps, end: endFrame / fps,
      previewStartFrame: previewCursor, previewEndFrame, previewStart: previewCursor / fps, previewEnd: previewEndFrame / fps }))
    previewCursor = previewEndFrame
  }
  for (const range of deletedRanges) { addKept(mediaCursor, range.startFrame); mediaCursor = range.endFrame }
  addKept(mediaCursor, frameCount)
  const keptRanges = Object.freeze(kept)
  const keptFrameCount = previewCursor
  const duration = keptFrameCount && fps ? keptFrameCount / fps : 0
  const empty = keptFrameCount === 0

  const fromMedia = (mediaSeconds: number): number => {
    if (empty) return 0
    const time = finite(mediaSeconds) ? clamp(mediaSeconds, 0, mediaDuration) : 0
    for (const range of keptRanges) {
      if (time < range.start) return range.previewStart
      if (time < range.end) return clamp(range.previewStart + time - range.start, 0, duration)
    }
    return duration
  }

  const mediaFrame = (ordinal: number): number => {
    const target = clamp(ordinal, 0, keptFrameCount - 1)
    for (const range of keptRanges) {
      if (target < range.previewEndFrame) return (range.startFrame + target - range.previewStartFrame) / fps
    }
    return 0
  }

  const toMedia = (previewSeconds: number, bias: CutPreviewBias = 'next'): number => {
    if (empty) return 0
    const time = finite(previewSeconds) ? clamp(previewSeconds, 0, duration) : 0
    if (time >= duration - FRAME_EPSILON / fps) return mediaFrame(keptFrameCount - 1)
    for (const range of keptRanges) {
      if (Math.abs(time - range.previewStart) <= FRAME_EPSILON / fps) {
        return bias === 'previous' && range.previewStartFrame > 0
          ? mediaFrame(range.previewStartFrame - 1) : range.start
      }
      if (time < range.previewEnd - FRAME_EPSILON / fps) return range.start + time - range.previewStart
    }
    return mediaFrame(keptFrameCount - 1)
  }

  const stepMedia = (mediaSeconds: number, direction: number): number => {
    if (empty) return 0
    const time = finite(mediaSeconds) ? clamp(mediaSeconds, 0, mediaDuration) : 0
    // currentTime 的微秒舍入也可能把下一保留帧读成接缝内的前一帧。
    // 只校正贴近整数的媒体读数；删除范围本身仍严格向外取整。
    const rawFrame = time * fps, nearest = Math.round(rawFrame)
    const frame = clamp(Math.abs(rawFrame - nearest) <= MEDIA_FRAME_EPSILON
      ? nearest : Math.floor(rawFrame + FRAME_EPSILON), 0, frameCount)
    const steps = finite(direction) ? Math.trunc(direction) : 0
    if (!steps) return toMedia(fromMedia(time))
    for (const range of keptRanges) {
      if (frame < range.startFrame) {
        // 当前位于删除区：第一步就是接缝另一侧，不能额外跳过一帧。
        return mediaFrame(range.previewStartFrame + (steps > 0 ? steps - 1 : steps))
      }
      if (frame < range.endFrame) return mediaFrame(range.previewStartFrame + frame - range.startFrame + steps)
    }
    return mediaFrame(keptFrameCount + (steps > 0 ? steps - 1 : steps))
  }

  return Object.freeze({ valid, empty, error, mediaDuration, fps, frameCount, keptFrameCount, duration,
    deletedRanges, keptRanges, fromMedia, toMedia, stepMedia })
}
