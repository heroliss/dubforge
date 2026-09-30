import { onUnmounted, ref, watch, type Ref } from 'vue'
import { useMessage } from 'naive-ui'
import { DubbingTimeline, type PlaybackPosition, type TimelineLine, type VoiceChoice } from '../utils/dubbingTimeline'
import { playbackErrorMessage } from '../utils/playbackFeedback'

export function useDubbingPreview(options: {
  state: () => any; video: Ref<HTMLVideoElement | undefined>; choices: () => Record<number, VoiceChoice>
}) {
  const message = useMessage()
  const volDub = ref(1), volBg = ref(1), solo = ref(false), playing = ref(false), time = ref(0)
  const cursor = ref<PlaybackPosition>(null)
  const playbackError = ref('')
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
  })
  let frame = 0
  function updatePosition() {
    playing.value = player.playing; time.value = player.time
    const position = player.cursor
    if (position?.lineId !== cursor.value?.lineId || position?.phase !== cursor.value?.phase) cursor.value = position
  }
  function tick() {
    player.voiceVolume = volDub.value; player.backgroundVolume = volBg.value
    player.tick(); updatePosition()
    frame = requestAnimationFrame(tick)
  }
  tick()
  // 原生视频按钮、拖动和变速也走同一个同步入口。
  const update = () => { player.tick(); updatePosition() }
  const onPlay = () => { playbackError.value = ''; player.activate(); update() }
  const events = ['pause', 'ratechange', 'waiting', 'playing', 'ended', 'seeking']
  let removeRestore = () => {}
  watch(options.video, (video, previous) => {
    removeRestore()
    previous?.removeEventListener('play', onPlay)
    video?.addEventListener('play', onPlay)
    events.forEach(name => previous?.removeEventListener(name, update))
    events.forEach(name => video?.addEventListener(name, update))
    if (video) {
      const position = time.value
      const restore = () => {
        if (options.video.value === video && position > 0 && Math.abs(video.currentTime - position) > .1)
          video.currentTime = Math.min(position, Number.isFinite(video.duration) ? video.duration : position)
      }
      removeRestore = () => video.removeEventListener('loadedmetadata', restore)
      if (video.readyState > 0) restore()
      else video.addEventListener('loadedmetadata', restore, { once: true })
    }
  })
  onUnmounted(() => {
    removeRestore()
    cancelAnimationFrame(frame); player.dispose()
    options.video.value?.removeEventListener('play', onPlay)
    events.forEach(name => options.video.value?.removeEventListener(name, update))
  })
  return { volDub, volBg, solo, playing, time, cursor, playbackError,
    preview: (line: TimelineLine) => { playbackError.value = ''; return player.play(line, solo.value) },
    playAll: () => { playbackError.value = ''; solo.value = false; return player.play() },
    pause: () => player.pause(), seek: (t: number) => player.seek(t),
    stopAll: () => { player.release(); playbackError.value = '' },
  }
}
