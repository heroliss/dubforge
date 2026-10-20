import type { CutPreviewKeptRange, CutPreviewTimeline } from './cutPreviewTimeline.ts'

export type CutPreviewMedia = {
  currentTime: number; paused: boolean; seeking: boolean; playbackRate: number
  readyState: number; ended?: boolean; pause: () => void
  requestVideoFrameCallback?: (callback: (now: number, metadata: { mediaTime: number }) => void) => number
  cancelVideoFrameCallback?: (handle: number) => void
}
export type CutPreviewJump = {
  media: CutPreviewMedia; timeline: CutPreviewTimeline; from: number; to: number; reason: 'cut' | 'tail'
}
export type CutPreviewPlaybackOptions = {
  getMedia: () => CutPreviewMedia | null | undefined
  getTimeline: () => CutPreviewTimeline | null | undefined
  onPrepareJump?: (event: CutPreviewJump) => void
  onJump?: (event: CutPreviewJump) => void
  onSettled?: (event: CutPreviewJump) => void
  clock?: { setTimeout: (callback: () => void, delay: number) => unknown; clearTimeout: (handle: unknown) => void }
}

/**
 * 单播放器快速试听：逐视频帧观察，另以一个截止计时器守住删除接缝。
 * 播放、暂停、seeked、速率或清单变化后调用 refresh；无需等待 timeupdate。
 * 真正无缝播放仍应生成连续预览文件，这里允许寻址缓冲并提供冻结画面钩子。
 */
export function createCutPreviewPlayback(options: CutPreviewPlaybackOptions) {
  const clock = options.clock || {
    setTimeout: (callback: () => void, delay: number) => globalThis.setTimeout(callback, delay),
    clearTimeout: (handle: unknown) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  }
  let destroyed = false, generation = 0
  let deadline: unknown = null, frameHandle: number | null = null
  let frameMedia: CutPreviewMedia | null = null
  let lastMedia: CutPreviewMedia | null = null, lastTimeline: CutPreviewTimeline | null = null
  let preparedEnd: number | null = null
  let pending: (CutPreviewJump & { sawSeeking: boolean }) | null = null

  const cancelScheduled = () => {
    generation++
    if (deadline !== null) clock.clearTimeout(deadline)
    if (frameHandle !== null) frameMedia?.cancelVideoFrameCallback?.(frameHandle)
    deadline = null; frameHandle = null; frameMedia = null
  }
  // 浏览器 currentTime 可按微秒舍入；仅容忍千分之一帧的媒体读数误差，
  // 否则已跳到下一保留帧仍会被判作删除区，遮罩也无法在 seeked 后撤下。
  const epsilon = (timeline: CutPreviewTimeline) => 1e-3 / timeline.fps
  const allowed = (timeline: CutPreviewTimeline, time: number) => timeline.keptRanges.some(range =>
    time >= range.start - epsilon(timeline) && time < range.end - epsilon(timeline))
  const targetEvent = (media: CutPreviewMedia, timeline: CutPreviewTimeline, range: CutPreviewKeptRange): CutPreviewJump => {
    const index = timeline.keptRanges.indexOf(range), next = timeline.keptRanges[index + 1]
    return { media, timeline, from: media.currentTime,
      to: next?.start ?? timeline.toMedia(timeline.duration), reason: next ? 'cut' : 'tail' }
  }
  const prepare = (event: CutPreviewJump, boundary: number) => {
    if (preparedEnd === boundary) return
    preparedEnd = boundary
    // 迟到回调或用户直接定位进删除区时，不能把禁用画面抓成冻结遮罩。
    if (allowed(event.timeline, event.media.currentTime)) options.onPrepareJump?.(event)
  }
  const settle = () => {
    if (!pending) return
    const event = pending
    pending = null; preparedEnd = null
    options.onSettled?.(event)
  }

  const armFrame = (media: CutPreviewMedia, token: number) => {
    if (!media.requestVideoFrameCallback) return
    frameMedia = media
    frameHandle = media.requestVideoFrameCallback((_now, metadata) => {
      if (destroyed || token !== generation) return
      frameHandle = null; frameMedia = null
      refreshWithFrame(metadata.mediaTime)
    })
  }
  const armDeadline = (delay: number, token: number) => {
    deadline = clock.setTimeout(() => {
      if (destroyed || token !== generation) return
      deadline = null
      refreshWithFrame()
    }, Math.max(1, delay))
  }
  const jump = (event: CutPreviewJump) => {
    pending = { ...event, sawSeeking: false }
    options.onJump?.(event)
    if (event.reason === 'tail' && !event.media.paused) event.media.pause()
    // pending 先登记；seeking/暂停事件即使同步回调也不会重复写寻址目标。
    if (Math.abs(event.media.currentTime - event.to) > epsilon(event.timeline)) event.media.currentTime = event.to
    if (pending) pending.sawSeeking ||= event.media.seeking
  }

  function refreshWithFrame(presentedTime?: number) {
    cancelScheduled()
    if (destroyed) return
    const media = options.getMedia() || null, timeline = options.getTimeline() || null
    if (media !== lastMedia || timeline !== lastTimeline) { pending = null; preparedEnd = null }
    lastMedia = media; lastTimeline = timeline
    if (!media || !timeline?.valid) return
    if (timeline.empty) { if (!media.paused) media.pause(); return }
    const token = generation
    if (pending) {
      pending.sawSeeking ||= media.seeking
      if (media.seeking) return
      const currentAllowed = Number.isFinite(media.currentTime) && allowed(timeline, media.currentTime)
      const paintedTarget = typeof presentedTime === 'number' && Number.isFinite(presentedTime)
        && allowed(timeline, presentedTime) && Math.abs(presentedTime - pending.to) < 1.1 / timeline.fps
      if (media.readyState >= 2 && currentAllowed && (pending.sawSeeking || paintedTarget || !media.requestVideoFrameCallback)) settle()
      else { armFrame(media, token); return }
    }
    if (media.paused || media.ended || media.seeking || !Number.isFinite(media.currentTime)
        || !Number.isFinite(media.playbackRate) || media.playbackRate <= 0) return
    // currentTime 可能已比待呈现帧稍晚；两者取较大值，不能被迟到的帧回调拖回删除区。
    const time = Math.max(media.currentTime, Number.isFinite(presentedTime) ? presentedTime! : media.currentTime)
    const range = timeline.keptRanges.find(item => time < item.end - epsilon(timeline))
    if (!range || time < range.start - epsilon(timeline)) {
      const event: CutPreviewJump = { media, timeline, from: media.currentTime,
        to: range?.start ?? timeline.toMedia(timeline.duration), reason: range ? 'cut' : 'tail' }
      prepare(event, range?.start ?? timeline.mediaDuration)
      jump(event)
      if (!media.seeking) armFrame(media, token)
      return
    }
    const event = targetEvent(media, timeline, range)
    const prepareAt = Math.max(range.start, range.end - 2 / timeline.fps)
    if (time < prepareAt - epsilon(timeline)) preparedEnd = null
    else prepare(event, range.end)
    const deadlineAt = preparedEnd === range.end ? range.end : prepareAt
    armDeadline((deadlineAt - time) / media.playbackRate * 1000, token)
    armFrame(media, token)
  }

  return {
    refresh: () => refreshWithFrame(),
    stop: () => { cancelScheduled(); pending = null; preparedEnd = null },
    destroy: () => { destroyed = true; cancelScheduled(); pending = null; preparedEnd = null; lastMedia = null; lastTimeline = null },
  }
}
