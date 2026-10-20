export type PlaybackTimedLine = {
  start: number; end: number; playback_start?: number | null; playback_end?: number | null
}

// 源时间用于校对、行身份和原声参考；播放时间由服务端统一排布，原文/译文共用。
export function playbackTime(line: PlaybackTimedLine) {
  const start = line.playback_start, end = line.playback_end
  return typeof start === 'number' && Number.isFinite(start) && start >= 0
    && typeof end === 'number' && Number.isFinite(end) && end > start
    ? { start, end } : { start: line.start, end: line.end }
}

export function withPlaybackTime<T extends PlaybackTimedLine>(line: T): T {
  const time = playbackTime(line)
  return time.start === line.start && time.end === line.end ? line : { ...line, ...time }
}
