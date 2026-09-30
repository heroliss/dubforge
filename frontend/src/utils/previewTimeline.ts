export type PreviewCue = { start: number; end: number; label: string }

// 相同起点合并提示，越界/损坏的数据不产生虚假刻度；不改变原始字幕或评论。
export function timelineCues(cues: PreviewCue[], max: number): PreviewCue[] {
  const grouped = new Map<number, PreviewCue>()
  for (const cue of cues) {
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.start < 0 || cue.start > max || cue.end <= cue.start) continue
    const previous = grouped.get(cue.start)
    grouped.set(cue.start, previous
      ? { start: cue.start, end: Math.min(previous.end, cue.end), label: `${previous.label}\n${cue.label}` }
      : { ...cue })
  }
  return [...grouped.values()].sort((a, b) => a.start - b.start)
}

export function cuePosition(cue: PreviewCue, max: number) {
  // 稍进入内容区间，避开 ASS 百分之一秒取整导致的边界空帧。
  return Math.min(max, cue.start + Math.min(.1, (cue.end - cue.start) / 2))
}

export function adjacentCue(cues: PreviewCue[], value: number, direction: -1 | 1, max: number) {
  return direction === 1 ? cues.find(cue => cuePosition(cue, max) > value + .001)
    : [...cues].reverse().find(cue => cuePosition(cue, max) < value - .001)
}
