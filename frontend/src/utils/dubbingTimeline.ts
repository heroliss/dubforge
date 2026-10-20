// 单句和整段共用同一时间轴；媒体只跟随时钟，不各自决定播放位置。
import { withPlaybackTime } from './dubbingPlaybackTime.ts'
export type VoiceChoice = 'dub' | 'original' | 'silent'
export interface TimelineLine {
  id: number; start: number; end: number
  audio_gain_db?: number
  playback_start?: number | null; playback_end?: number | null
  wav: { exists: boolean; url: string; seconds?: number | null }
  selection?: { source: 'dub' | 'original'; take: number | null }
}
export interface MediaClock {
  currentTime: number; paused: boolean; playbackRate: number; muted: boolean; volume: number
  readyState: number; duration: number; src: string; ended: boolean; seeking?: boolean
  error?: { code: number; message?: string } | null
  play(): Promise<void>; pause(): void; load(): void; removeAttribute(name: string): void
  addEventListener?(name: string, listener: () => void): void
  removeEventListener?(name: string, listener: () => void): void
}
export function voiceFor(line: TimelineLine, choices: Record<number, VoiceChoice>): VoiceChoice {
  if (choices[line.id]) return choices[line.id] === 'dub' && !line.wav.exists ? 'silent' : choices[line.id]
  if (line.selection) return line.selection.source === 'original' ? 'original' : line.wav.exists && line.selection.take !== null ? 'dub' : 'silent'
  return choices[line.id] === 'original' || !line.wav.exists ? 'original' : 'dub'
}
export function frameAt(lines: TimelineLine[], time: number, choices: Record<number, VoiceChoice>,
  duration = (line: TimelineLine) => line.wav.seconds ?? line.end - line.start) {
  lines = lines.map(withPlaybackTime)
  const active = lines.filter(l => l.start <= time && time < l.end)
  const dubbed = lines.filter(l => voiceFor(l, choices) === 'dub' && l.start <= time
    && time < l.start + duration(l))
  return { active, dubbed, original: active.some(l => voiceFor(l, choices) === 'original') }
}

type Track = { media: MediaClock; pending: number | null; revision: number; wanted: boolean; error: boolean; positioned: boolean; detach: () => void }
export type PlaybackPosition = { lineId: number; phase: 'playing' | 'gap' | 'silent' } | null

export type PlacementIssue = {
  kind: 'target_overrun' | 'overlap' | 'video_overrun'
  id: number; next_id: number | null; available: number; excess: number
}
export interface DubbingPlacement {
  schema: number; signature: unknown[][]; video_duration: number | null; issues: PlacementIssue[]
}
export type TimingWarning = {
  nextId: number | null; available: number; excess: number; kind: PlacementIssue['kind']
}

export function timingWarnings(lines: TimelineLine[], choices: Record<number, VoiceChoice>,
  placement?: DubbingPlacement | null, videoDuration?: number) {
  lines = lines.map(withPlaybackTime)
  const rows = [...lines].sort((a, b) => a.start - b.start || a.id - b.id).map(line => {
    const source = voiceFor(line, choices)
    const seconds = source === 'original' ? line.end - line.start : source === 'silent' ? 0
      : line.wav.seconds != null && Number.isFinite(line.wav.seconds) ? line.wav.seconds : null
    return { line, source, seconds, audioEnd: seconds == null ? null : line.start + seconds }
  })
  const signature = rows.map(({ line, source, seconds }) => [line.id, line.start, line.end, source,
    line.selection?.take ?? (line.selection ? null : source === 'dub' ? 1 : null), seconds, line.wav.url ?? null])
  const total = videoDuration != null && Number.isFinite(videoDuration) && videoDuration > 0
    ? videoDuration : placement?.video_duration ?? null
  let issues: PlacementIssue[]
  // 服务端计划与句子、声源、所选版本及媒体一致时直接使用；乐观编辑尚未保存时临时计算。
  if (placement?.schema === 1 && JSON.stringify(signature) === JSON.stringify(placement.signature)
    && (total == null || placement.video_duration != null && Math.abs(total - placement.video_duration) <= .05)) {
    issues = placement.issues
  } else {
    issues = []
    rows.forEach(({ line, source, seconds, audioEnd }, index) => {
      const next = rows[index + 1]?.line
      if (source === 'dub' && audioEnd != null) {
        const target = next?.start ?? line.end, excess = audioEnd - target
        if (excess > .05 + 1e-9) issues.push({kind: 'target_overrun', id: line.id, next_id: next?.id ?? null,
          available: Math.max(0, target - line.start), excess})
      }
      if (total != null && audioEnd != null && seconds! > 0 && audioEnd - total > .05 + 1e-9)
        issues.push({kind: 'video_overrun', id: line.id, next_id: null,
          available: Math.max(0, total - line.start), excess: audioEnd - total})
    })
    const audible = rows.filter(row => row.seconds != null && row.seconds > 0)
    audible.forEach((row, index) => {
      for (let next = index + 1; next < audible.length; next++) {
        const other = audible[next]
        if (other.line.start >= row.audioEnd! - .05) break
        const excess = Math.min(row.audioEnd!, other.audioEnd!) - other.line.start
        if (excess > .05 + 1e-9) issues.push({kind: 'overlap', id: row.line.id, next_id: other.line.id,
          available: Math.max(0, other.line.start - row.line.start), excess})
      }
    })
  }
  const result: Record<number, TimingWarning> = {}
  const priority = {target_overrun: 0, overlap: 1, video_overrun: 2}
  for (const issue of issues) {
    const previous = result[issue.id]
    if (!previous || priority[issue.kind] > priority[previous.kind])
      result[issue.id] = {nextId: issue.next_id, available: issue.available, excess: issue.excess, kind: issue.kind}
  }
  return result
}

export class DubbingTimeline {
  private tracks = new Map<string, Track>()
  private stopped = true
  private position = 0
  private anchor = 0
  private segment: number | null = null
  private segmentStarted = false
  private segmentTail: { generation: number; since: number; timer: ReturnType<typeof setTimeout> } | null = null
  private solo = false
  private disposed = false
  private enabled = false
  private generation = 0
  private videoRequest: { media: MediaClock; generation: number } | null = null
  private videoSeek: { media: MediaClock; time: number } | null = null
  private pendingStart: { lineId: number; generation: number; positioned: boolean } | null = null
  voiceVolume = 1
  backgroundVolume = 1
  monitorVolume = 1
  monitorMuted = false
  private options: {
    video: () => MediaClock | undefined; lines: () => TimelineLine[]
    choices: () => Record<number, VoiceChoice>
    sources: () => { vocal?: string; original?: string; background?: string }
    createAudio: (url: string) => MediaClock; now: () => number; error: (error: unknown, source: 'audio' | 'video') => void
    onEnded?: (lineId: number | null) => void
    onChanged?: () => void
    applyGain?: (media: MediaClock, gain: number) => void
    releaseGain?: (media: MediaClock) => void
  }
  constructor(options: DubbingTimeline['options']) { this.options = options }
  setSolo(value: boolean) {
    if (this.solo === value) return
    this.solo = value
    this.tick()
  }
  private lines() { return this.options.lines().map(withPlaybackTime) }
  private previewLine(line: TimelineLine) {
    if (this.segment !== line.id || voiceFor(line, this.options.choices()) !== 'original') return line
    const duration = this.options.video()?.duration
    // 原声单句试听保留字头和尾音上下文；正式时间轴及连续预览不受影响。
    return { ...line, start: Math.max(0, line.start - .15),
      end: Number.isFinite(duration) && duration! > 0 ? Math.min(duration!, line.end + .25) : line.end + .25,
      playback_start: null, playback_end: null }
  }
  private cancelTail() {
    if (this.segmentTail) clearTimeout(this.segmentTail.timer)
    this.segmentTail = null
  }
  private completeSegment() {
    const lineId = this.segment
    this.segment = null; this.pause()
    this.options.onEnded?.(lineId)
  }
  get time() {
    if (this.pendingStart) return this.position
    const video = this.options.video()
    return video ? video.readyState > 0 ? video.currentTime : this.position
      : this.stopped ? this.position : this.position + this.options.now() - this.anchor
  }
  get playing() {
    const video = this.options.video()
    return !this.stopped && !this.pendingStart && (video ? !video.paused || !!video.ended
      && (!!this.segmentTail || [...this.tracks.values()].some(t => t.wanted && !t.media.ended)) : true)
  }
  get cursor(): PlaybackPosition {
    if (!this.enabled || !this.playing) return null
    const time = this.time
    const line = this.lines().map(item => this.previewLine(item)).reduce<TimelineLine | undefined>((current, item) =>
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
  private voiceGain(line?: TimelineLine) {
    const db = Number(line?.audio_gain_db)
    return this.voiceVolume * (Number.isFinite(db) ? 10 ** (Math.max(-12, Math.min(12, db)) / 20) : 1)
  }
  private applyGain(media: MediaClock, gain: number) {
    if (this.options.applyGain) this.options.applyGain(media, gain)
    else media.volume = Math.max(0, Math.min(1, gain))
  }
  private track(url: string) {
    let t = this.tracks.get(url)
    if (!t) {
      const media = this.options.createAudio(url)
      t = { media, pending: null, revision: 0, wanted: false, error: false, positioned: false, detach: () => {} }
      this.tracks.set(url, t)
      const current = t
      const changed = () => {
        // 后台标签页会暂停帧循环；声音就绪和结束应由媒体事件及时推动。
        if (this.disposed || this.tracks.get(url) !== current) return
        this.tick(); this.options.onChanged?.()
      }
      const events = ['loadedmetadata', 'canplay', 'seeked', 'ended', 'error']
      events.forEach(name => media.addEventListener?.(name, changed))
      t.detach = () => events.forEach(name => media.removeEventListener?.(name, changed))
    }
    return t
  }
  private positionAt(time: number) {
    for (const t of this.tracks.values()) {
      this.stopTrack(t)
      if (t.media.ended) t.media.currentTime = 0
    }
    this.position = Math.max(0, time)
    this.anchor = this.options.now()
    const video = this.options.video()
    if (video) {
      this.videoSeek = { media: video, time: this.position }
      if (Math.abs(video.currentTime - this.position) > .001) video.currentTime = this.position
    }
  }
  seek(time: number) {
    // 仅定位；保持视频播放状态，旧音轨的异步请求必须先失效。
    this.pendingStart = null
    this.cancelTail()
    this.segment = null
    this.positionAt(time)
    this.tick()
  }
  async play(line?: TimelineLine, solo = false) {
    if (line) line = withPlaybackTime(line)
    const generation = ++this.generation
    this.cancelTail()
    this.pendingStart = null
    this.retryFailedTracks()
    for (const t of this.tracks.values()) if (t.pending !== null) this.stopTrack(t)
    this.enabled = true
    this.segmentStarted = false
    this.solo = solo
    if (line) {
      this.segment = line.id
      this.positionAt(this.previewLine(line).start)
    } else { this.segment = null }
    this.stopped = false
    this.anchor = this.options.now()
    const video = this.options.video()
    if (!line && video?.ended) this.positionAt(0)
    if (line) {
      // 单句先定位并等人声真实启动，再启视频；不能让视频先走而后追播配音。
      this.pendingStart = { lineId: line.id, generation, positioned: !video || video.readyState > 0 }
      video?.pause()
      for (const t of this.tracks.values()) this.stopTrack(t)
      this.tick()
      return
    }
    this.tick(true)
    await this.startVideo(generation, video)
  }
  private async startVideo(generation: number, video = this.options.video()) {
    if (this.stopped || generation !== this.generation) return
    const request = video ? { media: video, generation } : null
    this.videoRequest = request
    try { if (video) await video.play() }
    catch (error) {
      if (generation === this.generation) {
        this.pause()
        if ((error as Error)?.name !== 'AbortError') this.options.error(error, 'video')
      }
      return
    }
    finally { if (this.videoRequest === request) this.videoRequest = null }
    if (generation !== this.generation) {
      // 旧节点或已暂停的请求不能迟到后重新开声；同一节点的新播放仍可继续。
      if (video && (this.options.video() !== video || this.stopped || this.disposed || this.pendingStart)) video.pause()
      return
    }
    this.tick()
  }
  private startPrepared(pending: NonNullable<DubbingTimeline['pendingStart']>, video?: MediaClock) {
    if (this.pendingStart !== pending || pending.generation !== this.generation || this.stopped || this.disposed) return
    this.pendingStart = null
    this.segmentStarted = true
    this.anchor = this.options.now()
    void this.startVideo(pending.generation, video)
    this.options.onChanged?.()
  }
  private stopTrack(t: Track, invalidate = true) {
    if (invalidate) { t.revision++; t.pending = null }
    t.wanted = false; t.positioned = false
    t.media.pause()
  }
  private halt(invalidate = true) {
    this.cancelTail()
    this.generation++
    this.position = this.time
    this.stopped = true
    this.pendingStart = null
    this.options.video()?.pause()
    for (const t of this.tracks.values()) this.stopTrack(t, invalidate)
  }
  pause() { this.halt() }
  /**
   * 判断原生 video 的 pause 是否来自用户接管。
   *
   * 全屏切换在部分浏览器中会短暂触发 pause。调用方需要先观察
   * fullscreenchange，再决定是否真正停止时间轴，因此这里不能直接调用
   * videoPaused()（它会立刻 halt）。
   */
  videoPauseIsManual() {
    const video = this.options.video()
    return !!video && !video.ended && !this.stopped && !this.pendingStart
  }
  videoPaused() {
    // 原生暂停也取消在途请求；视频自然结束触发的 pause 仍允许尾音播完。
    // 上句的 pause 事件可能排在下一句 play 之后，不能暂停刚启动的新句。
    if (this.options.video() && !this.options.video()!.paused) return false
    const manual = !this.stopped && !this.pendingStart && !this.options.video()?.ended
    if (manual) this.halt()
    this.tick()
    return manual
  }
  videoSeeking() {
    const video = this.options.video(), requested = this.videoSeek
    const manual = !!video && !(requested?.media === video && Math.abs(video.currentTime - requested.time) <= .1)
    if (manual) {
      // 用户拖动接管连续预览，不能继续被旧的单句范围限制。
      this.cancelTail()
      this.pendingStart = null
      this.segment = null
      for (const t of this.tracks.values()) this.stopTrack(t)
    }
    this.tick()
    return manual
  }
  videoSeeked() {
    this.videoSeek = null
    this.tick()
  }
  expectVideoSeek(video: MediaClock, time: number) {
    // DOM 重建后的定位由上层负责，也要与真正的手动拖动区分。
    this.videoSeek = { media: video, time }
  }
  private retryFailedTracks() {
    // 仅用户再次播放时重建失败媒体；不能在帧循环里自动重试并持续刷请求。
    for (const [url, track] of this.tracks) if (track.error) {
      this.retireTrack(url, track)
    }
  }
  private retireTrack(url: string, track: Track) {
    // 先解绑并注销，再清空资源，load() 产生的旧事件不能影响新播放。
    track.detach()
    if (this.tracks.get(url) === track) this.tracks.delete(url)
    this.stopTrack(track)
    this.options.releaseGain?.(track.media)
    track.media.removeAttribute('src'); track.media.load()
  }
  activate(allowCancelledRequest = false) {
    if (this.disposed) return false
    const video = this.options.video(), request = this.videoRequest
    if (!allowCancelledRequest && (this.stopped || this.pendingStart)
      && request && request.media === video && request.generation !== this.generation) {
      video?.pause(); return false
    }
    const manual = !(request && request.media === video && request.generation === this.generation)
    if (manual) this.pendingStart = null
    // 无视频时由本地时钟驱动；恢复前重设锚点，不能把暂停前已走的时间再累加一次。
    if (!video && this.stopped) this.anchor = this.options.now()
    this.retryFailedTracks(); this.enabled = true; this.stopped = false
    return manual
  }
  /**
   * 恢复已经暂停的单句试听。暂停只停媒体，不清除 segment，因此恢复时
   * 应沿用视频当前时间，而不是再次调用 play(line) 把视频和配音定位到句首。
   * 返回 false 表示时间轴已经被其它操作接管，调用方应按新句重新启动。
   */
  canResume(lineId?: number) {
    return !this.disposed && this.enabled && this.segment != null && this.segmentStarted
      && (lineId == null || this.segment === lineId)
  }
  resume(lineId?: number) {
    if (!this.canResume(lineId)) return false
    // 跟随控制器接管正在试听的同一句时无需再次 play，保留媒体和时间位置。
    if (this.playing) return true
    this.activate(true)
    const video = this.options.video()
    if (video) void this.startVideo(this.generation, video)
    else this.tick(true)
    return true
  }
  private follow(url: string | undefined, time: number, volume: number, running: boolean, rate: number,
    finishing = false, onStarted?: () => void) {
    if (!url) return
    const t = this.track(url), media = t.media
    const wanted = running && !this.stopped && volume > 0 && !t.error
    if (!wanted && (t.wanted || t.pending !== null)) this.stopTrack(t)
    t.wanted = wanted
    // 静音和监听总音量不改变声源、播放位置或轨道时钟。
    this.applyGain(media, volume * this.monitorVolume)
    media.muted = this.monitorMuted
    if (media.ended && running) { t.wanted = false; return }
    if (media.error && t.wanted) {
      t.error = true
      this.halt(false)
      this.options.error(new Error('音频加载失败，请重新试听。'), 'audio')
      return
    }
    // 视频是唯一时钟。首次启动/拖动准确定位，小漂移缓慢追齐，避免频繁跳音。
    const drift = time - media.currentTime
    // 同次启动完成定位后，即使媒体仍 paused，也先开声并用变速追齐
    // 小漂移；否则视频在 seek 期间前进，会造成 seeked → 再 seek 循环。
    const realign = media.paused && !t.positioned || !running || Math.abs(drift) > .35
    // 到时间轴边界后，让已开始的音频自然播完，不再 seek 或按估算时长截断尾音。
    // 音频定位是异步操作；定位期间 currentTime 可能仍返回旧位置。
    // 每帧追加 seek 会不断取消浏览器正在完成的定位，形成句首循环。
    // 等 seeked 后再按最新视频时间核对，暂停、版本替换仍可及时停音。
    if (!finishing && !media.seeking && media.readyState > 0 && realign && Math.abs(drift) > .04) {
      t.positioned = true
      media.currentTime = Math.max(0, time)
    }
    const speed = rate * (finishing || realign || Math.abs(drift) < .04 ? 1 : 1 + Math.max(-.03, Math.min(.03, drift * .15)))
    if (Math.abs(media.playbackRate - speed) > .001) media.playbackRate = speed
    // 运行中刚发布的音频先等元数据，再按当前时钟接入，不能从零追播旧台词。
    if (!t.wanted || media.readyState === 0) {
      // 元数据尚未就绪时此前的定位不代表可播放位置，加载后重新对齐。
      if (media.readyState === 0) t.positioned = false
      media.pause(); return
    }
    if (media.seeking) return
    if (media.paused && t.pending === null && (finishing || !Number.isFinite(media.duration) || time < media.duration)) {
      const revision = ++t.revision
      t.pending = revision
      const failed = (error: Error) => {
        if (revision !== t.revision || this.tracks.get(url) !== t || this.disposed) return
        if (error.name !== 'AbortError') {
          // 一组请求同时失败时仍记录各轨；用户暂停、跳转或新播放后的旧失败则丢弃。
          t.error = true
          if (t.wanted) { this.halt(false); this.options.error(error, 'audio') }
        }
      }
      let request: Promise<void>
      try { request = media.play() }
      catch (error) { failed(error as Error); if (t.pending === revision) t.pending = null; return }
      void request.then(() => {
        if (!t.wanted || this.disposed) media.pause()
        else if (revision === t.revision && this.tracks.get(url) === t) onStarted?.()
      }).catch(failed).finally(() => { if (t.pending === revision) t.pending = null })
    }
  }
  tick(forcePlay = false) {
    if (this.disposed || !this.enabled) return
    if (this.pendingStart) {
      const pending = this.pendingStart, video = this.options.video()
      const original = this.lines().find(l => l.id === pending.lineId)
      const line = original && this.previewLine(original)
      if (!line) { this.pause(); return }
      const voice = voiceFor(line, this.options.choices()), sources = this.options.sources()
      const url = voice === 'dub' ? line.wav.url : voice === 'original' ? sources.vocal || sources.original : undefined
      const track = url ? this.track(url) : undefined
      const failed = video?.error ? 'video' : track?.media.error ? 'audio' : null
      if (failed) {
        if (track && failed === 'audio') track.error = true
        this.pause(); this.options.error(new Error(failed === 'audio' ? '音频加载失败，请重新试听。' : '视频加载失败，请重新试听。'), failed)
        return
      }
      if (video?.readyState === 0 || this.voiceVolume > 0 && track && track.media.readyState < 3) return
      if (!pending.positioned) {
        pending.positioned = true
        this.positionAt(line.start)
      }
      // 设置 currentTime 后还要等 seeked，避免音频被 seeking 禁止而视频先启动。
      if (video?.seeking) return
      if (track && this.voiceVolume > 0) {
        // 原声外部轨也必须定位、开声后才启视频；长人声轨的异步 seek 不能吞掉句首。
        if (track.pending === null) this.follow(url, voice === 'original' ? line.start : 0,
          this.voiceGain(line), true, video?.playbackRate ?? 1,
          false, () => this.startPrepared(pending, video))
      } else this.startPrepared(pending, video)
      return
    }
    const time = this.time, video = this.options.video()
    const lines = this.lines(), sources = this.options.sources()
    const choices = this.options.choices()
    const selected = this.segment == null ? lines : lines.filter(l => l.id === this.segment).map(line => this.previewLine(line))
    const frame = frameAt(selected, time, choices, l => this.duration(l))
    const unfinished = selected.filter(l => {
      const t = this.tracks.get(l.wav.url)
      if (video?.seeking || l.start > time || voiceFor(l, choices) !== 'dub'
        || !t?.wanted || t.error || t.media.ended) return false
      const audioEnd = l.start + this.duration(l)
      if (time < audioEnd || this.segment != null || video?.ended) return true
      // 单句与片尾保留完整尾音。连续预览只容忍短暂漂移，下一句开声前结束旧续播。
      const newerVoice = frame.dubbed.some(next => next.start > l.start)
        || frame.active.some(next => next.start > l.start && voiceFor(next, choices) === 'original')
      return !newerVoice && time - audioEnd <= .35
    })
    const end = this.segment != null || !video ? Math.max(0, ...selected.map(l =>
      this.segment != null && voiceFor(l, choices) === 'dub' ? l.start + this.duration(l)
        : Math.max(l.end, l.start + (voiceFor(l, choices) === 'dub' ? this.duration(l) : 0)))) : Infinity
    const spoken = this.segment != null && selected.length > 0 && selected.every(l =>
      voiceFor(l, choices) === 'dub' && this.tracks.get(l.wav.url)?.media.ended)
    if (spoken && !this.stopped && !this.segmentTail) {
      // ended 后只保留短暂听后空隙，不能等待字幕长留白，也不重播已经结束的人声。
      const generation = this.generation
      const tail = { generation, since: this.options.now(), timer: setTimeout(() => {
        if (this.segmentTail !== tail || this.generation !== generation
          || this.stopped || this.disposed) return
        this.completeSegment(); this.options.onChanged?.()
      }, 200) }
      this.segmentTail = tail
    } else if (!spoken) this.cancelTail()
    if (!this.stopped && (this.segmentTail ? this.options.now() - this.segmentTail.since >= .2 - 1e-9
      : (time >= end || video?.ended) && !unfinished.length)) {
      // 先停整组媒体再通知上层；重复帧和自然 pause 事件都不会重复报告。
      this.completeSegment()
      return
    }
    const running = (forcePlay || this.playing) && (!video || video.ended || (video.readyState >= 3 && !video.seeking))
    const rate = video?.playbackRate ?? 1
    for (const line of unfinished) if (!frame.dubbed.includes(line)) frame.dubbed.push(line)
    // 分离人声优先；退回完整原声时不叠加第二份背景。
    const originalUrl = sources.vocal || sources.original
    const originalOn = frame.original
    const wanted = new Set([
      ...(originalOn && !video?.ended ? [originalUrl] : []),
      ...(!this.solo && !video?.ended && (!originalOn || sources.vocal) ? [sources.background] : []),
      ...frame.dubbed.map(l => l.wav.url),
    ])
    // 先停旧声源，再启动新声源，原声回退和配音版本刷新不产生瞬间双播。
    for (const [url, t] of this.tracks) if (!wanted.has(url) && (t.wanted || t.pending !== null)) this.stopTrack(t)
    if (video) {
      const muted = this.monitorMuted || !!originalUrl || !originalOn
      if (video.muted !== muted) video.muted = muted
      const originalLine = frame.active.find(line => voiceFor(line, choices) === 'original')
      if (!muted) this.applyGain(video, this.voiceGain(originalLine) * this.monitorVolume)
    }
    if (originalOn && !video?.ended) this.follow(originalUrl, time,
      this.voiceGain(frame.active.find(line => voiceFor(line, choices) === 'original')), running, rate)
    if (!this.solo && !video?.ended && (!originalOn || sources.vocal)) this.follow(sources.background, time, this.backgroundVolume, running, rate)
    for (const line of frame.dubbed) this.follow(line.wav.url, time - line.start, this.voiceGain(line), running, rate,
      !!video?.ended || time >= line.start + this.duration(line))
    for (const t of this.tracks.values()) if (!t.wanted) t.media.pause()
    // 只预载附近两句；长音轨使用流式媒体，避免整条视频音频解码进内存。
    const next = lines.filter(l => l.start > time && voiceFor(l, this.options.choices()) === 'dub').slice(0, 2)
    for (const l of next) this.track(l.wav.url)
    // 句子区间内保留已结束媒体的实际时长，防止释放后又按接口的四舍五入时长重播。
    const current = selected.filter(l => voiceFor(l, choices) === 'dub' && l.start <= time
      && time < Math.max(l.end, l.start + this.duration(l), l.start + (l.wav.seconds ?? 0)))
    const keep = new Set([sources.vocal, sources.original, sources.background,
      ...(this.segmentTail ? selected.map(line => line.wav.url) : []),
      ...frame.dubbed.map(l => l.wav.url), ...current.map(l => l.wav.url), ...next.map(l => l.wav.url)])
    for (const [url, t] of this.tracks) if (!keep.has(url)) {
      this.retireTrack(url, t)
    }
  }
  release(position?: number) {
    this.enabled = false
    this.pause()
    this.videoSeek = null
    if (position != null) this.position = Math.max(0, position)
    for (const [url, t] of this.tracks) this.retireTrack(url, t)
  }
  dispose() { this.release(); this.disposed = true }
}
