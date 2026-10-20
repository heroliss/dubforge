import { computed, onUnmounted, readonly, ref, watch, type Ref } from 'vue'
import { useMessage } from 'naive-ui'
import { DubbingTimeline, voiceFor, type PlaybackPosition, type TimelineLine, type VoiceChoice } from '../utils/dubbingTimeline'
import { playbackErrorMessage } from '../utils/playbackFeedback'
import { releaseMedia } from '../utils/preview'
import { listeningAudioState, type ListeningAudioState } from '../utils/listeningAudio'
import { MediaListeningGain } from '../utils/mediaListeningGain'

export function useDubbingPreview(options: {
  state: () => any; video: Ref<HTMLVideoElement | undefined>; choices: () => Record<number, VoiceChoice>
}) {
  const message = useMessage()
  const volDub = ref(1), volBg = ref(1), solo = ref(false), playing = ref(false), time = ref(0)
  const localMonitor = ref<ListeningAudioState>({ volume: 1, muted: false })
  const monitorAudio = computed(() => ({ ...localMonitor.value }))
  const setMonitorAudio = (value: ListeningAudioState) => { localMonitor.value = listeningAudioState(value) }
  const cursor = ref<PlaybackPosition>(null)
  const playbackError = ref('')
  const listeningGain = new MediaListeningGain(warning => { playbackError.value = warning; message.error(warning) })
  const finished = ref<{ serial: number; lineId: number | null }>({ serial: 0, lineId: null })
  const interrupted = ref(0)
  let positioning = 0
  const player = new DubbingTimeline({
    video: () => options.video.value,
    lines: () => options.state()?.lines ?? [], choices: options.choices,
    sources: () => {
      const s = options.state()
      return { vocal: s?.vocal?.found ? s.vocal.url : undefined,
        original: s?.original?.found ? s.original.url : undefined,
        background: s?.bg?.found ? s.bg.url : undefined }
    },
    createAudio: url => { const audio = new Audio(url); audio.preload = 'auto'; return audio },
    now: () => performance.now() / 1000,
    error: (error, source) => {
      playbackError.value = playbackErrorMessage(error, source)
      message.error(playbackError.value)
    },
    onEnded: lineId => { finished.value = { serial: finished.value.serial + 1, lineId } },
    onChanged: () => updatePosition(),
    applyGain: (media, gain) => listeningGain.set(media as unknown as HTMLMediaElement, gain),
    releaseGain: media => listeningGain.release(media as unknown as HTMLMediaElement),
  })
  let frame = 0, disposed = false
  let playbackRequest = 0
  // 连续播放也预先考虑后续句子的增益；默认 100% 监听仍使用原生输出。
  const requiredGain = (line?: TimelineLine) => {
    const state = options.state()
    const lines: TimelineLine[] = line ? [line] : state?.lines ?? []
    const choices = options.choices()
    let gain = !solo.value && state?.bg?.found ? volBg.value : 0
    for (const item of lines) if (voiceFor(item, choices) !== 'silent') {
      const db = Number(item.audio_gain_db)
      gain = Math.max(gain, volDub.value * (Number.isFinite(db) ? 10 ** (Math.max(-12, Math.min(12, db)) / 20) : 1))
    }
    return gain * localMonitor.value.volume
  }
  const startPlayback = async (line?: TimelineLine) => {
    // 等待输出期间先冻结旧时钟。迟到的恢复只允许本次操作继续，不能推翻暂停或跳转。
    player.pause(); updatePosition()
    const request = ++playbackRequest
    playbackError.value = ''
    const ready = await listeningGain.prepare(requiredGain(line))
    if (!ready || disposed || request !== playbackRequest) return
    return player.play(line, solo.value)
  }
  // Chromium 在原生全屏切换时偶尔会让 video 短暂触发 pause。这个 pause
  // 是渲染上下文切换，不是用户要求停止试听；若按普通 pause 处理，会
  // 先停掉整条配音轨，随后即使视频恢复播放也只剩画面。
  let fullscreenActive = false
  let fullscreenWasPlaying = false
  let holdingFullscreenPause = false
  let restoringFullscreenPlay = false
  let fullscreenResumeTimer: ReturnType<typeof setTimeout> | undefined
  let fullscreenPauseCandidate: { video: HTMLVideoElement; at: number } | null = null
  let fullscreenChangedAt = -Infinity
  // 全屏切换本身通常在一个渲染帧内完成；窗口过长会把用户紧接着
  // 点击的普通暂停误判成浏览器过渡。
  const fullscreenWindowMs = 240
  // 原生控件在全屏切换时 pause/fullscreenchange 的先后顺序不固定；给
  // fullscreenchange 留出一小段窗口，但普通点击暂停仍应很快生效。
  const fullscreenPauseGraceMs = 120
  const readFullscreen = (video: HTMLVideoElement) => {
    const documentElement = typeof document !== 'undefined' ? document.fullscreenElement : null
    return documentElement === video || !!documentElement?.contains(video)
      || !!(video as HTMLVideoElement & { webkitDisplayingFullscreen?: boolean }).webkitDisplayingFullscreen
  }
  const fullscreenChanging = (event?: Event) => {
    const video = options.video.value
    if (!video) return
    // WebKit 移动端事件有时不暴露 document.fullscreenElement；事件名本身
    // 就是可靠的状态来源。
    const nativeState = event?.type === 'webkitbeginfullscreen' ? true
      : event?.type === 'webkitendfullscreen' ? false : null
    const active = nativeState ?? readFullscreen(video)
    if (active === fullscreenActive) return
    const now = performance.now()
    fullscreenActive = active
    fullscreenWasPlaying = playing.value || player.playing
    fullscreenChangedAt = now
    // Chromium 有时先触发 pause，稍后才派发 fullscreenchange。此时
    // onPause 已经暂存了候选事件，直接恢复同一媒体节点，避免只剩画面。
    const candidate = fullscreenPauseCandidate
    if (candidate && candidate.video === video && now - candidate.at <= fullscreenWindowMs) {
      fullscreenPauseCandidate = null
      fullscreenWasPlaying = false
      if (fullscreenResumeTimer) { clearTimeout(fullscreenResumeTimer); fullscreenResumeTimer = undefined }
      holdingFullscreenPause = true
      restoringFullscreenPlay = true
      void video.play().catch(() => {
        restoringFullscreenPlay = false
        holdingFullscreenPause = false
        if (!disposed) { player.videoPaused(); updatePosition() }
      })
      return
    }
  }
  function updatePosition() {
    if (!holdingFullscreenPause) playing.value = player.playing
    time.value = player.time
    const position = player.cursor
    if (position?.lineId !== cursor.value?.lineId || position?.phase !== cursor.value?.phase) cursor.value = position
  }
  function tick() {
    if (disposed) return
    // 全屏切换造成的短暂 pause 期间不要让时钟把所有配音轨标记为不需要。
    if (!holdingFullscreenPause) player.tick()
    updatePosition()
    frame = requestAnimationFrame(tick)
  }
  watch([volDub, volBg, solo, localMonitor], () => {
    player.voiceVolume = volDub.value; player.backgroundVolume = volBg.value
    player.monitorVolume = localMonitor.value.volume; player.monitorMuted = localMonitor.value.muted
    player.setSolo(solo.value)
    // 即使标签页帧循环被节流，用户操作也应立即控制实际所有声轨。
    player.tick(); updatePosition()
  }, { immediate: true, flush: 'sync' })
  tick()
  // 原生视频按钮、拖动和变速也走同一个同步入口。
  const update = () => { player.tick(); updatePosition() }
  const onPlay = () => {
    const readiness = listeningGain.prepare(requiredGain())
    // 原生视频按钮也可能先启动媒体，再异步恢复 Web Audio。先暂停原地等待，
    // 恢复后沿用已有单句位置；首次原生播放则接管为连续预览。
    if (!listeningGain.ready) {
      if (player.activate() && !restoringFullscreenPlay) interrupted.value++
      player.pause(); updatePosition()
      const request = ++playbackRequest
      void readiness.then(ready => {
        if (!ready || disposed || request !== playbackRequest) return
        if (!player.resume()) void player.play(undefined, solo.value)
        updatePosition()
      })
      return
    }
    playbackError.value = ''
    const restoring = restoringFullscreenPlay
    restoringFullscreenPlay = false
    // 快速恢复播放时清除尚未判定的 pause，避免旧候选干扰后续状态。
    if (!restoring && (fullscreenPauseCandidate || fullscreenResumeTimer)) {
      fullscreenPauseCandidate = null
      if (fullscreenResumeTimer) { clearTimeout(fullscreenResumeTimer); fullscreenResumeTimer = undefined }
      holdingFullscreenPause = false
    }
    if (player.activate() && !restoring) { playbackRequest++; interrupted.value++ }
    if (restoring) {
      holdingFullscreenPause = false
      player.tick()
    }
    update()
  }
  const onPause = () => {
    if (player.videoPauseIsManual()) playbackRequest++
    const now = performance.now()
    const video = options.video.value
    // 原生全屏按钮可能在 fullscreenchange 之前派发 pause。先暂存这个
    // pause，若没有全屏事件再按普通用户暂停处理；测试媒体没有
    // requestFullscreen，因此仍走原有的同步路径。
    const deferForFullscreen = !!video && typeof video.requestFullscreen === 'function'
    if (deferForFullscreen && player.videoPauseIsManual()) {
      const candidate = { video, at: now }
      fullscreenPauseCandidate = candidate
      holdingFullscreenPause = true
      if (fullscreenResumeTimer) clearTimeout(fullscreenResumeTimer)
      fullscreenResumeTimer = setTimeout(() => {
        fullscreenResumeTimer = undefined
        if (fullscreenPauseCandidate !== candidate || disposed || options.video.value !== video) return
        fullscreenPauseCandidate = null
        // fullscreenchange 先到时由 transition 窗口识别；事件迟到时
        // fullscreenChangedAt 也能覆盖 pause → fullscreenchange 顺序。
        const transition = fullscreenChangedAt >= candidate.at
          || (fullscreenWasPlaying && candidate.at >= fullscreenChangedAt
            && candidate.at - fullscreenChangedAt <= fullscreenPauseGraceMs)
        if (transition) {
          fullscreenWasPlaying = false
          restoringFullscreenPlay = true
          void video.play().catch(() => {
            restoringFullscreenPlay = false
            holdingFullscreenPause = false
            if (!disposed) { player.videoPaused(); updatePosition() }
          })
          return
        }
        holdingFullscreenPause = false
        if (player.videoPaused()) interrupted.value++
        updatePosition()
      }, fullscreenPauseGraceMs)
      updatePosition()
      return
    }
    // 某些 Chromium 版本在进入/退出全屏时先 pause 再异步恢复。保留
    // DubbingTimeline 的 generation 和音轨，不把这次事件当成用户暂停。
    if (fullscreenWasPlaying && fullscreenChangedAt > -Infinity
      && now - fullscreenChangedAt <= fullscreenPauseGraceMs) {
      // 每次全屏状态变化最多接管一次 pause；恢复后用户立即点击暂停
      // 仍应按正常手动暂停处理，不能被这次过渡保护误恢复。
      fullscreenWasPlaying = false
      const video = options.video.value
      holdingFullscreenPause = true
      if (fullscreenResumeTimer) clearTimeout(fullscreenResumeTimer)
      fullscreenResumeTimer = setTimeout(() => {
        fullscreenResumeTimer = undefined
        if (disposed || !video || options.video.value !== video || !video.paused) {
          holdingFullscreenPause = false
          return
        }
        restoringFullscreenPlay = true
        void video.play().catch(() => {
          restoringFullscreenPlay = false
          holdingFullscreenPause = false
          if (!disposed) { player.videoPaused(); updatePosition() }
        })
      }, 0)
      updatePosition()
      return
    }
    if (player.videoPaused()) interrupted.value++
    updatePosition()
  }
  const onSeeking = () => { if (player.videoSeeking()) { playbackRequest++; interrupted.value++ }; updatePosition() }
  const onSeeked = () => { player.videoSeeked(); updatePosition() }
  // seeked 及时恢复拖动后的同步；timeupdate 在后台标签页的帧循环被节流时仍可核对音轨。
  const events = ['ratechange', 'waiting', 'playing', 'ended', 'timeupdate', 'emptied', 'loadedmetadata', 'canplay', 'error']
  const fullscreenEvents = ['fullscreenchange', 'webkitbeginfullscreen', 'webkitendfullscreen']
  let removeRestore = () => {}
  let removeDocumentFullscreen = () => {}
  watch(options.video, (video, previous) => {
    playbackRequest++
    // beforeUnmount 的媒体释放可能已把旧元素归零，此时使用最后一次有效定位。
    const position = previous && previous.readyState > 0 ? previous.currentTime : time.value
    const revision = positioning
    removeRestore()
    previous?.removeEventListener('play', onPlay)
    previous?.removeEventListener('pause', onPause)
    previous?.removeEventListener('seeking', onSeeking)
    previous?.removeEventListener('seeked', onSeeked)
    events.forEach(name => previous?.removeEventListener(name, update))
    fullscreenEvents.forEach(name => previous?.removeEventListener(name, fullscreenChanging))
    removeDocumentFullscreen(); removeDocumentFullscreen = () => {}
    fullscreenPauseCandidate = null
    if (fullscreenResumeTimer) { clearTimeout(fullscreenResumeTimer); fullscreenResumeTimer = undefined }
    fullscreenActive = false; fullscreenWasPlaying = false; holdingFullscreenPause = false
    // 替换 DOM 时先停止整组媒体，不能让旧 video 或其在途播放请求继续出声。
    player.release(position)
    if (previous) releaseMedia(previous)
    if (previous) listeningGain.release(previous)
    playing.value = false; cursor.value = null; time.value = position
    video?.addEventListener('play', onPlay)
    video?.addEventListener('pause', onPause)
    video?.addEventListener('seeking', onSeeking)
    video?.addEventListener('seeked', onSeeked)
    events.forEach(name => video?.addEventListener(name, update))
    fullscreenEvents.forEach(name => video?.addEventListener(name, fullscreenChanging))
    if (video && typeof document !== 'undefined') {
      // fullscreenchange 的目标在不同浏览器实现中可能是 document 或 video；
      // 捕获阶段同时覆盖两种实现。
      const handler = () => fullscreenChanging()
      document.addEventListener('fullscreenchange', handler, true)
      document.addEventListener('webkitfullscreenchange', handler, true)
      removeDocumentFullscreen = () => {
        document.removeEventListener('fullscreenchange', handler, true)
        document.removeEventListener('webkitfullscreenchange', handler, true)
      }
    }
    if (video) {
      const restore = () => {
        if (options.video.value === video && revision === positioning && position > 0 && Math.abs(video.currentTime - position) > .1) {
          const target = Math.min(position, Number.isFinite(video.duration) ? video.duration : position)
          player.expectVideoSeek(video, target)
          video.currentTime = target
        }
      }
      removeRestore = () => video.removeEventListener('loadedmetadata', restore)
      if (video.readyState > 0) restore()
      else video.addEventListener('loadedmetadata', restore, { once: true })
    }
  }, { immediate: true, flush: 'sync' })
  onUnmounted(() => {
    disposed = true
    playbackRequest++
    removeRestore()
    options.video.value?.removeEventListener('play', onPlay)
    options.video.value?.removeEventListener('pause', onPause)
    options.video.value?.removeEventListener('seeking', onSeeking)
    options.video.value?.removeEventListener('seeked', onSeeked)
    events.forEach(name => options.video.value?.removeEventListener(name, update))
    fullscreenEvents.forEach(name => options.video.value?.removeEventListener(name, fullscreenChanging))
    removeDocumentFullscreen()
    if (fullscreenResumeTimer) clearTimeout(fullscreenResumeTimer)
    fullscreenPauseCandidate = null
    cancelAnimationFrame(frame); player.dispose()
    listeningGain.dispose()
    if (options.video.value) releaseMedia(options.video.value)
    playing.value = false; cursor.value = null
  })
  return { volDub, volBg, solo, monitorAudio, setMonitorAudio, playing, time, cursor, playbackError,
    finished: readonly(finished), interrupted: readonly(interrupted),
    preview: (line: TimelineLine) => { positioning++; return startPlayback(line) },
    playAll: () => startPlayback(),
    resume: (lineId?: number) => {
      if (!player.canResume(lineId)) return false
      const request = ++playbackRequest
      void listeningGain.prepare(requiredGain()).then(ready => {
        if (!ready || disposed || request !== playbackRequest) return
        player.resume(lineId); updatePosition()
      })
      return true
    },
    pause: () => { playbackRequest++; player.pause(); updatePosition() }, seek: (t: number) => { playbackRequest++; positioning++; player.seek(t); updatePosition() },
    stopAll: () => { playbackRequest++; player.release(); updatePosition(); playbackError.value = '' },
  }
}
