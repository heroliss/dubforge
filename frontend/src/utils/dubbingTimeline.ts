// 单句和整段共用同一时间轴；媒体只跟随时钟，不各自决定播放位置。
export type VoiceChoice = 'dub' | 'original' | 'silent'
export interface TimelineLine {
  id: number; start: number; end: number
  wav: { exists: boolean; url: string; seconds?: number | null }
  selection?: { source: 'dub' | 'original'; take: number | null }
}
export interface MediaClock {
  currentTime: number; paused: boolean; playbackRate: number; muted: boolean; volume: number
  readyState: number; duration: number; src: string; ended: boolean; seeking?: boolean
  play(): Promise<void>; pause(): void; load(): void; removeAttribute(name: string): void
}
export function voiceFor(line: TimelineLine, choices: Record<number, VoiceChoice>): VoiceChoice {
  if (choices[line.id]) return choices[line.id] === 'dub' && !line.wav.exists ? 'silent' : choices[line.id]
  if (line.selection) return line.selection.source === 'original' ? 'original' : line.wav.exists && line.selection.take !== null ? 'dub' : 'silent'
  return choices[line.id] === 'original' || !line.wav.exists ? 'original' : 'dub'
}
export function frameAt(lines: TimelineLine[], time: number, choices: Record<number, VoiceChoice>,
  duration = (line: TimelineLine) => line.wav.seconds ?? line.end - line.start) {
  const active = lines.filter(l => l.start <= time && time < l.end)
  const dubbed = lines.filter(l => voiceFor(l, choices) === 'dub' && l.start <= time
    && time < l.start + duration(l))
  return { active, dubbed, original: active.some(l => voiceFor(l, choices) === 'original') }
}

type Track = { media: MediaClock; pending: boolean; wanted: boolean; error: boolean }
export type PlaybackPosition = { lineId: number; phase: 'playing' | 'gap' | 'silent' } | null

export function timingWarnings(lines: TimelineLine[], choices: Record<number, VoiceChoice>) {
  const result: Record<number, { nextId: number | null; available: number; excess: number }> = {}
  const ordered = [...lines].sort((a, b) => a.start - b.start)
  ordered.forEach((line, index) => {
    const next = ordered[index + 1], seconds = line.wav.seconds
    if (voiceFor(line, choices) !== 'dub' || seconds == null || !Number.isFinite(seconds)) return
    const available = (next?.start ?? line.end) - line.start, excess = seconds - available
    // 忽略媒体时长取整产生的毫秒级误差；这里只提示，不裁剪或移动音频。
    if (excess > .05) result[line.id] = { nextId: next?.id ?? null, available, excess }
  })
  return result
}

export class DubbingTimeline {
  private tracks = new Map<string, Track>()
  private stopped = true
  private position = 0
  private anchor = 0
  private segment: number | null = null
  private solo = false
  private disposed = false
  private enabled = false
  private generation = 0
  voiceVolume = 1
  backgroundVolume = 1
  private options: {
    video: () => MediaClock | undefined; lines: () => TimelineLine[]
    choices: () => Record<number, VoiceChoice>
    sources: () => { vocal?: string; original?: string; background?: string }
    createAudio: (url: string) => MediaClock; now: () => number; error: (error: unknown, source: 'audio' | 'video') => void
  }
  constructor(options: DubbingTimeline['options']) { this.options = options }
  get time() { return this.options.video()?.currentTime ?? (this.stopped ? this.position : this.position + this.options.now() - this.anchor) }
  get playing() {
    const video = this.options.video()
    return video ? !video.paused || (!!video.ended && !this.stopped
      && [...this.tracks.values()].some(t => t.wanted && !t.media.ended)) : !this.stopped
  }
  get cursor(): PlaybackPosition {
    if (!this.enabled || !this.playing) return null
    const time = this.time
    const line = this.options.lines().reduce<TimelineLine | undefined>((current, item) =>
      item.start <= time && (this.segment == null || item.id === this.segment)
        && (!current || item.start >= current.start) ? item : current, undefined)
    if (!line) return null
    const source = voiceFor(line, this.options.choices())
    if (source === 'silent') return { lineId: line.id, phase: 'silent' }
    const track = this.tracks.get(line.wav.url)
    const speaking = source === 'original' ? time < line.end
      : !track?.media.ended && !track?.error
        && (!!track?.wanted || time < line.start + this.duration(line))
    return { lineId: line.id, phase: speaking ? 'playing' : 'gap' }
  }
  private duration(line: TimelineLine) {
    const actual = this.tracks.get(line.wav.url)?.media.duration
    return actual != null && Number.isFinite(actual) && actual > 0 ? actual : line.wav.seconds ?? line.end - line.start
  }
  private track(url: string) {
    let t = this.tracks.get(url)
    if (!t) {
      t = { media: this.options.createAudio(url), pending: false, wanted: false, error: false }
      this.tracks.set(url, t)
    }
    return t
  }
  seek(time: number) {
    // 仅定位；不调用 play/pause，保持用户原来的播放状态。
    this.segment = null
    for (const t of this.tracks.values()) {
      t.wanted = false
      if (t.media.ended) t.media.currentTime = 0
    }
    this.position = Math.max(0, time)
    this.anchor = this.options.now()
    const video = this.options.video()
    if (video) video.currentTime = this.position
    this.tick()
  }
  async play(line?: TimelineLine, solo = false) {
    const generation = ++this.generation
    this.retryFailedTracks()
    this.enabled = true
    this.solo = solo
    if (line) {
      this.seek(line.start)
      this.segment = line.id
    } else { this.segment = null }
    this.stopped = false
    this.anchor = this.options.now()
    const video = this.options.video()
    this.tick(true)
    try { if (video) await video.play() }
    catch (error) {
      if (generation === this.generation) { this.pause(); this.options.error(error, 'video') }
      return
    }
    if (generation !== this.generation) return
    this.tick()
  }
  pause() {
    this.generation++
    this.position = this.time
    this.stopped = true
    this.options.video()?.pause()
    for (const t of this.tracks.values()) { t.wanted = false; t.media.pause() }
  }
  private retryFailedTracks() {
    // 仅用户再次播放时重建失败媒体；不能在帧循环里自动重试并持续刷请求。
    for (const [url, track] of this.tracks) if (track.error) {
      track.wanted = false
      track.media.pause(); track.media.removeAttribute('src'); track.media.load()
      this.tracks.delete(url)
    }
  }
  activate() { this.retryFailedTracks(); this.enabled = true; this.stopped = false }
  private follow(url: string | undefined, time: number, volume: number, running: boolean, rate: number, finishing = false) {
    if (!url) return
    const t = this.track(url), media = t.media
    t.wanted = running && volume > 0 && !t.error
    media.volume = Math.max(0, Math.min(1, volume))
    if (media.ended && running) { t.wanted = false; return }
    // 视频是唯一时钟。首次启动/拖动准确定位，小漂移缓慢追齐，避免频繁跳音。
    const drift = time - media.currentTime
    const realign = media.paused || !running || Math.abs(drift) > .35
    // 到时间轴边界后，让已开始的音频自然播完，不再 seek 或按估算时长截断尾音。
    if (!finishing && media.readyState > 0 && realign && Math.abs(drift) > .04) media.currentTime = Math.max(0, time)
    const speed = rate * (finishing || realign || Math.abs(drift) < .04 ? 1 : 1 + Math.max(-.03, Math.min(.03, drift * .15)))
    if (Math.abs(media.playbackRate - speed) > .001) media.playbackRate = speed
    if (!t.wanted) { media.pause(); return }
    if (media.paused && !t.pending && (finishing || !Number.isFinite(media.duration) || time < media.duration)) {
      t.pending = true
      void media.play().then(() => {
        if (!t.wanted || this.disposed) media.pause()
      }).catch((error: Error) => {
        if (error.name !== 'AbortError' && !this.disposed) {
          // 同时失败的其他音轨也要标记，不能因第一条已暂停整组而漏掉恢复。
          t.error = true
          if (t.wanted) { this.pause(); this.options.error(error, 'audio') }
        }
      }).finally(() => { t.pending = false })
    }
  }
  tick(forcePlay = false) {
    if (this.disposed || !this.enabled) return
    const time = this.time, video = this.options.video()
    const lines = this.options.lines(), sources = this.options.sources()
    const choices = this.options.choices()
    const selected = this.segment == null ? lines : lines.filter(l => l.id === this.segment)
    const unfinished = selected.filter(l => {
      const t = this.tracks.get(l.wav.url)
      return !video?.seeking && l.start <= time && voiceFor(l, choices) === 'dub'
        && t?.wanted && !t.error && !t.media.ended
    })
    const end = this.segment != null || !video ? Math.max(0, ...selected.map(l =>
      Math.max(l.end, l.start + (voiceFor(l, choices) === 'dub' ? this.duration(l) : 0)))) : Infinity
    if ((time >= end || video?.ended) && !unfinished.length) {
      this.segment = null; this.solo = false; this.pause(); return
    }
    const running = forcePlay || (this.playing && (!video || video.ended || (video.readyState >= 3 && !video.seeking)))
    const rate = video?.playbackRate ?? 1
    const frame = frameAt(selected, time, choices, l => this.duration(l))
    for (const line of unfinished) if (!frame.dubbed.includes(line)) frame.dubbed.push(line)
    for (const t of this.tracks.values()) t.wanted = false
    // 分离人声优先；退回完整原声时不叠加第二份背景。
    const originalUrl = sources.vocal || sources.original
    const originalOn = frame.original
    if (video) {
      const muted = !!originalUrl || !originalOn
      if (video.muted !== muted) video.muted = muted
      if (!muted && video.volume !== this.voiceVolume) video.volume = this.voiceVolume
    }
    if (originalOn && !video?.ended) this.follow(originalUrl, time, this.voiceVolume, running, rate)
    if (!this.solo && !video?.ended && (!originalOn || sources.vocal)) this.follow(sources.background, time, this.backgroundVolume, running, rate)
    for (const line of frame.dubbed) this.follow(line.wav.url, time - line.start, this.voiceVolume, running, rate,
      !!video?.ended || time >= line.start + this.duration(line))
    for (const t of this.tracks.values()) if (!t.wanted) t.media.pause()
    // 只预载附近两句；长音轨使用流式媒体，避免整条视频音频解码进内存。
    const next = lines.filter(l => l.start > time && voiceFor(l, this.options.choices()) === 'dub').slice(0, 2)
    for (const l of next) this.track(l.wav.url)
    // 句子区间内保留已结束媒体的实际时长，防止释放后又按接口的四舍五入时长重播。
    const current = selected.filter(l => voiceFor(l, choices) === 'dub' && l.start <= time
      && time < Math.max(l.end, l.start + this.duration(l), l.start + (l.wav.seconds ?? 0)))
    const keep = new Set([sources.vocal, sources.original, sources.background,
      ...frame.dubbed.map(l => l.wav.url), ...current.map(l => l.wav.url), ...next.map(l => l.wav.url)])
    for (const [url, t] of this.tracks) if (!keep.has(url)) {
      t.wanted = false; t.media.pause(); t.media.removeAttribute('src'); t.media.load(); this.tracks.delete(url)
    }
  }
  release() {
    this.enabled = false
    this.pause()
    for (const t of this.tracks.values()) { t.media.removeAttribute('src'); t.media.load() }
    this.tracks.clear()
  }
  dispose() { this.release(); this.disposed = true }
}
