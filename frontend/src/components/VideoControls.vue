<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NSelect, NSlider, NText } from 'naive-ui'
import { listeningAudioState, type ListeningAudioState } from '../utils/listeningAudio'
import type { CutPreviewTimeline } from '../utils/cutPreviewTimeline'
import { formatTimecode } from '../utils/timecode'

export type VideoControlChapter = {
  title?: string
  start_time?: number
  end_time?: number | null
}

const props = withDefaults(defineProps<{
  media: HTMLVideoElement | null
  chapters?: VideoControlChapter[]
  duration?: number
  /** 可选的剪后逻辑时间轴；所有定位先映回保留帧，再写入真实媒体。 */
  timeline?: CutPreviewTimeline | null
  /** 源视频帧率（支持 ffprobe 的 num/den 字符串）；未传入时按常见的 30fps 估算。 */
  frameRate?: number | string | null
  /** 可选的播放器外壳；未提供时使用视频的最近祖先。 */
  container?: HTMLElement | null
  /** 多音轨播放器提供真实监听状态；省略时沿用原生视频音量。 */
  audioControl?: ListeningAudioState
}>(), { chapters: () => [], duration: 0, frameRate: 30, container: null })
const emit = defineEmits<{
  'audio-change': [value: ListeningAudioState]
  /** 用户明确点击/按键切换播放状态；父级联动播放器可据此取消迟到的镜像事件。 */
  'playback-intent': [playing: boolean]
  /** 用户拖动/键盘定位；父级可据此锁定双播放器的联动来源。 */
  seek: [value: number]
  'seek-start': []
  'seek-end': []
}>()

const root = ref<HTMLElement | null>(null)
const keyboardHint = computed(() => `${props.timeline ? '剪后时间轴；' : ''}点击画面后：←/→ 后退/前进 5 秒，逗号/句号逐帧后退/前进（未提供源帧率时按约30fps），↑/↓ 调整音量 5%，空格/K 播放暂停，M 静音，F 全屏，Home/End 跳到开头/结尾${props.timeline ? '；所有定位及逐帧均跳过删除段' : ''}`)
const playing = ref(false)
const current = ref(0)
const durationValue = ref(0)
const buffered = ref(0)
const volume = ref(1)
const muted = ref(false)
const listeningVolume = computed(() => props.audioControl ? listeningAudioState(props.audioControl).volume : volume.value)
const listeningMuted = computed(() => props.audioControl ? !!props.audioControl.muted : muted.value)
const rate = ref(1)
const hover = ref<{ left: number; time: number; chapter: string } | null>(null)
const dragging = ref(false)
const controlMessage = ref('')
const fullscreenState = ref(false)
// 播放时自动隐藏控制条，暂停或移动指针后重新显示。
const controlsHidden = ref(false)
// 指针停留在播放器底部时保持控制条可见；否则自动隐藏计时器会在
// 指针停止移动后把控制条收起，用户无法像常见播放器那样继续操作。
const pointerNearControls = ref(false)
const fallbackTarget = ref<HTMLElement | null>(null)
let clearMessageTimer: ReturnType<typeof setTimeout> | undefined
let mediaClickTimer: ReturnType<typeof setTimeout> | undefined
let hideControlsTimer: ReturnType<typeof setTimeout> | undefined
let cueRefreshTimers: ReturnType<typeof setTimeout>[] = []
let cueLayoutKey = ''
let cueLayoutTracks: { track: TextTrack; cues: TextTrackCueList | null; count: number }[] = []
let detach: (() => void) | undefined
const fallbackFullscreenClass = 'video-controls-fallback-fullscreen'

const timelinePlayable = computed(() => !props.timeline || (props.timeline.valid && !props.timeline.empty))
const timelineLabel = computed(() => props.timeline ? '剪后视频进度（左右键每次剪后5秒，逗号和句号逐帧，跳过删除段）' : '视频进度（左右键每次5秒，逗号和句号逐帧）')
const duration = computed(() => {
  if (props.timeline) return timelinePlayable.value ? props.timeline.duration : 0
  const value = Number(durationValue.value) || Number(props.duration)
  return Number.isFinite(value) && value > 0 ? value : 0
})
// 浏览器 seek 尚未完成时，currentTime 可能仍是旧值或前一次命令的位置。
// 连续按键使用最后目标继续，避免每次都从同一帧重新计算。
let pendingSeek: { target: number; observed: number; previousTargets: number[]; expires: number } | null = null
function rawMediaPosition() { return Number(props.media?.currentTime) || 0 }
function mediaPosition() {
  const actual = rawMediaPosition(), pending = pendingSeek
  if (!pending) return actual
  const tolerance = seekPositionTolerance.value
  if (Date.now() > pending.expires) { pendingSeek = null; return actual }
  if (Math.abs(actual - pending.target) <= tolerance) {
    pendingSeek = null
    // Chrome 可将 currentTime 舍入到微秒；这一轮继续使用刚请求的精确帧点，
    // 不能把量化误差误认为外部定位，或再次落入接缝前的删除帧。
    return pending.target
  }
  if (Math.abs(actual - pending.observed) <= tolerance || pending.previousTargets.some(value => Math.abs(actual - value) <= tolerance)) return pending.target
  // 原生控件或父级发起了另一定位，以该实时媒体值为准。
  pendingSeek = null
  return actual
}
function logicalPosition(mediaTime: number) {
  const mapped = props.timeline ? props.timeline.fromMedia(mediaTime) : mediaTime
  return Number.isFinite(mapped) ? Math.max(0, Math.min(duration.value, mapped)) : 0
}
function parseFrameRate(raw: unknown): number | null {
  let value = Number(raw)
  if (typeof raw === 'string' && raw.includes('/')) {
    const [numerator, denominator] = raw.split('/', 2).map(Number)
    if (Number.isFinite(numerator) && Number.isFinite(denominator) && denominator > 0) value = numerator / denominator
  }
  return Number.isFinite(value) && value > 0 ? Math.min(240, value) : null
}
const configuredFrameRate = computed(() => parseFrameRate(props.timeline?.fps) || parseFrameRate(props.frameRate))
const effectiveFrameRate = computed(() => {
  return configuredFrameRate.value || 30
})
const frameStep = computed(() => 1 / effectiveFrameRate.value)
// 只容忍千分之一帧（至少一微秒）的媒体时间量化，不能吞掉用户真正的逐帧定位。
const seekPositionTolerance = computed(() => Math.max(1e-6, frameStep.value * .001))
const frameStepLabel = computed(() => {
  const fps = effectiveFrameRate.value
  return `${configuredFrameRate.value ? '' : '约 '}1/${Number.isInteger(fps) ? fps : fps.toFixed(2)} 秒`
})
const normalizedChapters = computed(() => (Array.isArray(props.chapters) ? props.chapters : [])
  .map((row, index) => ({
    title: String(row?.title || '').trim() || `章节 ${index + 1}`,
    start: props.timeline ? props.timeline.fromMedia(Number(row?.start_time)) : Number(row?.start_time),
    end: row?.end_time == null ? null : props.timeline ? props.timeline.fromMedia(Number(row.end_time)) : Number(row.end_time),
  }))
  .filter(row => Number.isFinite(row.start) && row.start >= 0)
  .sort((a, b) => a.start - b.start)
  .map((row, index, rows) => ({ ...row, end: row.end && row.end > row.start ? row.end : (rows[index + 1]?.start ?? null) })))
const chapterMarkerStyle = (start: number) => ({
  left: (duration.value ? Math.max(0, Math.min(1, start / duration.value)) * 100 : 0) + '%',
  opacity: duration.value ? 1 : 0,
})
const percentage = computed(() => duration.value ? Math.max(0, Math.min(100, current.value / duration.value * 100)) : 0)
const bufferedPercentage = computed(() => duration.value ? Math.max(0, Math.min(100, buffered.value / duration.value * 100)) : 0)
const fullscreen = computed(() => fullscreenState.value)
const trackCount = computed(() => {
  const media = props.media
  return media?.textTracks?.length || 0
})
const trackShowing = ref(false)
const rateOptions = [0.5, 0.75, 1, 1.25, 1.5, 2].map(value => ({ label: `${value}×`, value }))

function formatTime(value: number): string {
  return formatTimecode(Math.floor(value))
}
function showMessage(value: string) {
  controlMessage.value = value
  if (clearMessageTimer) clearTimeout(clearMessageTimer)
  clearMessageTimer = setTimeout(() => { controlMessage.value = '' }, 4000)
}
function clearHideControlsTimer() {
  if (hideControlsTimer) clearTimeout(hideControlsTimer)
  hideControlsTimer = undefined
}
function scheduleCueRefresh() {
  cueRefreshTimers.forEach(timer => clearTimeout(timer))
  cueRefreshTimers = [100, 400, 1000].map(delay => setTimeout(() => updateCuePosition(true), delay))
}
function updateCuePosition(force = false) {
  const media = props.media
  if (!media) return
  // 原生 WebVTT 字幕默认贴近画面底部，容易被自定义控制条遮住。
  // 控制条出现时字幕上移；控制条隐藏后再下移，保持画面空间。
  const line = controlsHidden.value ? 87 : 68
  const tracks = Array.from(media.textTracks, track => {
    const cues = track.cues
    return { track, cues, count: cues?.length || 0 }
  })
  const layoutKey = `${line}:${tracks.map(track => track.count).join(',')}`
  // 高频时钟只检查轨道身份与条目数；文本只在重排或字幕载入时读取。
  if (!force && layoutKey === cueLayoutKey && tracks.length === cueLayoutTracks.length
    && tracks.every((entry, index) => entry.track === cueLayoutTracks[index].track && entry.cues === cueLayoutTracks[index].cues)) return
  cueLayoutKey = layoutKey
  cueLayoutTracks = tracks
  for (const { cues } of tracks) {
    if (!cues) continue
    for (let j = 0; j < cues.length; j++) {
      const cue = cues[j] as VTTCue
      try {
        const cueLines = Math.max(1, String(cue.text || '').split(/\r?\n/).length)
        // 多行 cue 需要再向上留出行高，否则显式换行后最后一行仍可能落入
        // 自定义控制条或视频容器边界；单/双行保持既有安全区。
        const cueLine = Math.max(46, line - Math.max(0, cueLines - 2) * 7)
        cue.snapToLines = false
        cue.line = cueLine
        cue.lineAlign = 'center'
        cue.position = 50
        cue.positionAlign = 'center'
        cue.size = 90
        cue.align = 'center'
      } catch {
        // 某些浏览器只读部分 cue 属性，保留浏览器默认字幕即可。
      }
    }
  }
}
function hideControlsSoon() {
  clearHideControlsTimer()
  if (!playing.value || dragging.value || pointerNearControls.value) return
  hideControlsTimer = setTimeout(() => {
    if (playing.value && !dragging.value && !pointerNearControls.value) {
      controlsHidden.value = true
      updateCuePosition()
    }
  }, 2400)
}
function showControls() {
  if (controlsHidden.value) {
    controlsHidden.value = false
    updateCuePosition()
  }
  hideControlsSoon()
}
function updatePointerProximity(event: MouseEvent | PointerEvent, container: HTMLElement | null) {
  const rect = container?.getBoundingClientRect()
  if (!rect || !Number.isFinite(event.clientY)) return
  // 使用容器而不是视频本身计算，兼容全屏时的黑边/留白区域；
  // 只把最底部一小段作为唤醒区，避免鼠标在进度条上方时闪烁。
  const zone = Math.min(72, Math.max(48, rect.height * .08))
  pointerNearControls.value = event.clientY >= rect.bottom - zone
}
function showFromPointer(event: MouseEvent | PointerEvent, container: HTMLElement | null) {
  updatePointerProximity(event, container)
  // 控制条已经隐藏时，只允许底部区域唤醒；控制条可见时正常刷新隐藏计时。
  if (!controlsHidden.value || pointerNearControls.value) showControls()
}
function onMediaPointerActivity(event: PointerEvent) {
  showFromPointer(event, props.container || (event.currentTarget as HTMLElement | null)?.parentElement || null)
}
function syncFromMedia() {
  const media = props.media
  if (!media) return
  durationValue.value = Number(media.duration) || 0
  current.value = logicalPosition(mediaPosition())
  volume.value = Number.isFinite(media.volume) ? media.volume : 1
  muted.value = media.muted
  rate.value = media.playbackRate || 1
  playing.value = !media.paused && !media.ended
  trackShowing.value = Array.from(media.textTracks || []).some(track => track.mode === 'showing')
  updateCuePosition()
  try {
    const range = media.buffered
    buffered.value = range.length ? logicalPosition(range.end(range.length - 1)) : 0
  } catch { buffered.value = 0 }
}
function clearMediaClickTimer() {
  if (mediaClickTimer) clearTimeout(mediaClickTimer)
  mediaClickTimer = undefined
}
function onMediaClick() {
  // 双击也会先产生两次 click；延迟单击动作，避免双击时短暂暂停/播放一次。
  clearMediaClickTimer()
  mediaClickTimer = setTimeout(() => {
    mediaClickTimer = undefined
    togglePlay()
  }, 220)
}
function onMediaDoubleClick(event: MouseEvent) {
  clearMediaClickTimer()
  event.preventDefault()
  void toggleFullscreen()
}
function bindMedia(media: HTMLVideoElement | null) {
  detach?.(); detach = undefined
  pendingSeek = null
  clearMediaClickTimer()
  clearHideControlsTimer()
  cueRefreshTimers.forEach(timer => clearTimeout(timer))
  cueRefreshTimers = []
  cueLayoutKey = ''
  cueLayoutTracks = []
  controlsHidden.value = false
  pointerNearControls.value = false
  if (!media) return
  const events = ['seeked', 'loadedmetadata', 'durationchange', 'timeupdate', 'progress', 'ratechange', 'volumechange', 'emptied']
  events.forEach(event => media.addEventListener(event, syncFromMedia))
  const onPlay = () => { pendingSeek = null; syncFromMedia(); showControls() }
  const onPause = () => { syncFromMedia(); clearHideControlsTimer(); controlsHidden.value = false; updateCuePosition() }
  const onEnded = () => { syncFromMedia(); clearHideControlsTimer(); controlsHidden.value = false; updateCuePosition() }
  media.addEventListener('play', onPlay)
  media.addEventListener('pause', onPause)
  media.addEventListener('ended', onEnded)
  media.addEventListener('click', onMediaClick)
  media.addEventListener('dblclick', onMediaDoubleClick)
  media.addEventListener('pointermove', onMediaPointerActivity)
  media.addEventListener('touchstart', onMediaPointerActivity, { passive: true })
  const onAddTrack = () => updateCuePosition(true)
  const onRemoveTrack = () => updateCuePosition(true)
  // 替换同条字幕文件可能复用 cue 列表且条数相同，加载完成时显式作废缓存。
  const onTrackLoad = (event: Event) => { if ((event.target as Element | null)?.tagName === 'TRACK') updateCuePosition(true) }
  const trackLoadOptions = { capture: true }
  media.addEventListener('load', onTrackLoad, trackLoadOptions)
  media.textTracks?.addEventListener?.('addtrack', onAddTrack)
  media.textTracks?.addEventListener?.('removetrack', onRemoveTrack)
  syncFromMedia()
  scheduleCueRefresh()
  detach = () => {
    events.forEach(event => media.removeEventListener(event, syncFromMedia))
    media.removeEventListener('play', onPlay)
    media.removeEventListener('pause', onPause)
    media.removeEventListener('ended', onEnded)
    media.removeEventListener('click', onMediaClick)
    media.removeEventListener('dblclick', onMediaDoubleClick)
    media.removeEventListener('pointermove', onMediaPointerActivity)
    media.removeEventListener('touchstart', onMediaPointerActivity)
    media.removeEventListener('load', onTrackLoad, trackLoadOptions)
    media.textTracks?.removeEventListener?.('addtrack', onAddTrack)
    media.textTracks?.removeEventListener?.('removetrack', onRemoveTrack)
    cueRefreshTimers.forEach(timer => clearTimeout(timer))
    cueRefreshTimers = []
  }
}
watch(() => props.media, bindMedia, { immediate: true })
watch(() => props.timeline, () => { pendingSeek = null; syncFromMedia() })
// 焦点留在播放器内时才接收快捷键；离开后立即恢复页面和表单自己的键盘行为。
watch([() => props.media, () => props.container, root, () => props.timeline], ([media, container, controls], _, onCleanup) => {
  if (!media) return
  const target = container || controls?.parentElement || media.parentElement
  if (!target) return
  const attributes = { tabindex: '0', role: 'group', 'aria-label': '视频播放器', 'aria-description': keyboardHint.value,
    'aria-keyshortcuts': 'ArrowLeft ArrowRight ArrowUp ArrowDown Comma Period Space K M F Home End' }
  const added = Object.entries(attributes).filter(([name]) => !target.hasAttribute(name))
  added.forEach(([name, value]) => target.setAttribute(name, value))
  const addedClass = !target.classList.contains('video-keyboard-scope')
  target.classList.add('video-keyboard-scope')
  const focusPlayer = (event: PointerEvent) => {
    if (event.target === media || event.target === target) target.focus({ preventScroll: true })
  }
  // 控制条隐藏时本身会暂时关闭 pointer-events，不能只监听控制条自身的移动。
  // 将活动监听放在播放器外壳上，覆盖视频的黑边/留白区以及原生全屏状态，
  // 用户把指针移回画面任意位置都可以重新唤起控制条。
  const onContainerPointerActivity = (event: PointerEvent) => {
    showFromPointer(event, target)
  }
  const onContainerPointerLeave = () => {
    pointerNearControls.value = false
    hideControlsSoon()
  }
  target.addEventListener('pointerdown', focusPlayer)
  target.addEventListener('pointermove', onContainerPointerActivity)
  target.addEventListener('pointerenter', onContainerPointerActivity)
  target.addEventListener('pointerleave', onContainerPointerLeave)
  target.addEventListener('keydown', onPlayerKeydown)
  onCleanup(() => {
    target.removeEventListener('pointerdown', focusPlayer)
    target.removeEventListener('pointermove', onContainerPointerActivity)
    target.removeEventListener('pointerenter', onContainerPointerActivity)
    target.removeEventListener('pointerleave', onContainerPointerLeave)
    target.removeEventListener('keydown', onPlayerKeydown)
    added.forEach(([name, value]) => { if (target.getAttribute(name) === value) target.removeAttribute(name) })
    if (addedClass) target.classList.remove('video-keyboard-scope')
  })
}, { immediate: true, flush: 'post' })

function togglePlay() {
  // 键盘或控制按钮的新操作取代尚未判定完的画面单击，避免稍后反向切换。
  clearMediaClickTimer()
  const media = props.media
  if (!media || !timelinePlayable.value) return
  if (media.paused || media.ended) {
    emit('playback-intent', true)
    void media.play().catch(() => showMessage('浏览器阻止了自动播放，请点击播放按钮继续'))
  } else {
    emit('playback-intent', false)
    media.pause()
  }
}
function applyMediaSeek(target: number) {
  const media = props.media
  if (!media || !timelinePlayable.value || !Number.isFinite(target)) return
  const actual = rawMediaPosition(), previous = pendingSeek
  const maximum = Number(media.duration) || (!props.timeline ? duration.value : Infinity)
  target = Math.max(0, Math.min(maximum, target))
  pendingSeek = { target, observed: actual, previousTargets: previous ? [...previous.previousTargets, previous.target].slice(-8) : [], expires: Date.now() + 2000 }
  // 唯一的媒体写入点：映射先完成，不能先落入删除段再交给父级纠正。
  media.currentTime = target
  current.value = logicalPosition(target)
  emit('seek', target)
  if (!dragging.value) emit('seek-end')
}
function seek(value: number, bias: 'next' | 'previous' = 'next') {
  if (!props.media || !duration.value || !timelinePlayable.value || !Number.isFinite(value)) return
  const logical = Math.max(0, Math.min(duration.value, value))
  const target = props.timeline ? props.timeline.toMedia(logical, bias) : logical
  if (target === null || !Number.isFinite(target)) return
  applyMediaSeek(target)
}
function jumpBy(seconds: number) {
  if (!props.media || !duration.value || !timelinePlayable.value) return
  seek(logicalPosition(mediaPosition()) + seconds, seconds < 0 ? 'previous' : 'next')
  showControls()
  showMessage(`${props.timeline ? '剪后' : ''}${seconds < 0 ? '后退' : '前进'} ${Math.abs(seconds)} 秒 · ${formatTime(current.value)}`)
}
/**
 * 将媒体停在相邻的源视频帧上。
 *
 * HTMLVideoElement 没有跨浏览器可用的“下一帧”方法，使用源 FPS 的时间步长
 * 设置 currentTime 是最稳定且可复用的做法。逐帧检查时自动暂停，避免播放时钟
 * 在用户定位过程中继续前进；实际解码帧到达后仍由 timeupdate 校正显示位置。
 */
function stepFrame(direction: -1 | 1) {
  const media = props.media
  if (!media || !duration.value || !timelinePlayable.value) return
  if (!media.paused) media.pause()
  const position = mediaPosition()
  // 以帧序号计算目标，避免在连续点击时不断累积浮点误差；对于浏览器
  // 返回的略偏离帧边界的 currentTime，也会先归一到相邻帧再移动一帧。
  const step = frameStep.value
  // currentTime 可能落在两帧之间；先取最近的已知帧，再按方向移动一帧，
  // 避免 29.97fps 等分数帧率在正向移动时跳过当前位置附近的真实帧。
  const frame = Math.round(position / step) + direction
  if (props.timeline) {
    const target = props.timeline.stepMedia(position, direction)
    if (target !== null && Number.isFinite(target)) applyMediaSeek(target)
  } else seek(frame * step)
  showControls()
  showMessage(`${props.timeline ? '剪后' : ''}${direction < 0 ? '上一帧' : '下一帧'} · ${formatTime(current.value)} · ${frameStepLabel.value}`)
}
function positionFromEvent(event: PointerEvent | MouseEvent): number {
  const element = event.currentTarget as HTMLElement
  const rect = element.getBoundingClientRect()
  return Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)))
}
function beginScrub(event: PointerEvent) {
  if (!props.media || !duration.value || !timelinePlayable.value) return
  const target = event.currentTarget as HTMLElement
  controlsHidden.value = false
  updateCuePosition()
  clearHideControlsTimer()
  dragging.value = true
  emit('seek-start')
  target.setPointerCapture?.(event.pointerId)
  seek(positionFromEvent(event) * duration.value)
}
function moveScrub(event: PointerEvent) {
  if (!dragging.value) return
  seek(positionFromEvent(event) * duration.value)
}
function endScrub(event: PointerEvent) {
  dragging.value = false
  const target = event.currentTarget as HTMLElement
  if (target.hasPointerCapture?.(event.pointerId)) target.releasePointerCapture(event.pointerId)
  emit('seek-end')
  hideControlsSoon()
}
function updateHover(event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const left = Math.max(0, Math.min(100, (event.clientX - rect.left) / Math.max(1, rect.width) * 100))
  const time = left / 100 * duration.value
  const chapter = normalizedChapters.value.find(row => time >= row.start && (row.end == null || time < row.end))
  hover.value = { left: Math.max(4, Math.min(96, left)), time, chapter: chapter?.title || '' }
}
function clearHover() { if (!dragging.value) hover.value = null }
function onProgressKey(event: KeyboardEvent) {
  onPlayerKeydown(event)
}
function onPlayerKeydown(event: KeyboardEvent) {
  if (!props.media || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return
  const target = event.target as HTMLElement | null
  if (target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"], [role="spinbutton"]')) return
  const slider = target?.closest?.('[role="slider"]')
  if (slider && !slider.classList.contains('timeline')) return
  const key = event.key.toLowerCase()
  // 按钮上的空格仍激活该按钮，避免静音、全屏按钮同时切换播放状态。
  if (key === ' ' && target?.closest?.('button, a[href], [role="button"]')) return
  const directional = ['arrowleft', 'arrowright', 'arrowup', 'arrowdown', 'home', 'end'].includes(key)
  const frameKey = key === ',' || key === '.'
  if (!directional && !frameKey && ![' ', 'k', 'm', 'f'].includes(key)) return
  event.preventDefault()
  event.stopPropagation()
  showControls()
  if (event.repeat && !directional && !frameKey) return
  if (key === ',' || key === '.') {
    if (!duration.value) return
    stepFrame(key === ',' ? -1 : 1)
  } else if (key === 'arrowleft' || key === 'arrowright' || key === 'home' || key === 'end') {
    if (!duration.value) return
    if (key === 'arrowleft' || key === 'arrowright') jumpBy(key === 'arrowleft' ? -5 : 5)
    else {
      seek(key === 'home' ? 0 : duration.value, key === 'home' ? 'next' : 'previous')
      showMessage(`${props.timeline ? '剪后' : ''}${key === 'home' ? '跳到开头' : '跳到结尾'} · ${formatTime(current.value)}`)
    }
  } else if (key === 'arrowup' || key === 'arrowdown') {
    const value = listeningMuted.value ? 0 : listeningVolume.value
    const next = Math.max(0, Math.min(100, Math.round(value * 100) + (key === 'arrowup' ? 5 : -5)))
    setVolume(next / 100)
    showMessage(`音量 ${next}%`)
  } else if (key === ' ' || key === 'k') togglePlay()
  else if (key === 'm') toggleMute()
  else if (key === 'f') void toggleFullscreen()
}
function setVolume(value: number | [number, number]) {
  const media = props.media
  const next = Number(Array.isArray(value) ? value[0] : value)
  if (!media || !Number.isFinite(next)) return
  if (props.audioControl) {
    emit('audio-change', listeningAudioState({ volume: next, muted: next <= 0 }))
    return
  }
  media.volume = Math.max(0, Math.min(1, next))
  media.muted = next <= 0
  syncFromMedia()
}
function toggleMute() {
  const media = props.media
  if (!media) return
  if (props.audioControl) {
    const nextMuted = !listeningMuted.value
    emit('audio-change', { volume: !nextMuted && listeningVolume.value === 0 ? 0.7 : listeningVolume.value, muted: nextMuted })
    return
  }
  media.muted = !media.muted
  if (!media.muted && media.volume === 0) media.volume = 0.7
  syncFromMedia()
}
function setRate(value: number) {
  if (!props.media || !Number.isFinite(Number(value))) return
  props.media.playbackRate = Number(value); syncFromMedia()
}
function toggleTracks() {
  const media = props.media
  if (!media?.textTracks?.length) return
  trackShowing.value = !trackShowing.value
  for (let i = 0; i < media.textTracks.length; i++) media.textTracks[i].mode = trackShowing.value ? 'showing' : 'disabled'
}
async function toggleFullscreen() {
  const target = props.container || root.value?.closest('.video-preview') || root.value?.parentElement
  if (!target) return
  if (fallbackTarget.value === target || target.classList.contains(fallbackFullscreenClass)) {
    target.classList.remove(fallbackFullscreenClass)
    fallbackTarget.value = null
    fullscreenState.value = !!document.fullscreenElement
    return
  }
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else if (document.fullscreenEnabled !== false && typeof target.requestFullscreen === 'function') await target.requestFullscreen()
    else throw new Error('fullscreen-unavailable')
  } catch {
    // 某些嵌入页面或自动化浏览器禁用 Fullscreen API，仍提供占满当前视口的可退出模式。
    if (document.fullscreenElement) { showMessage('当前浏览器不允许全屏播放'); return }
    target.classList.add(fallbackFullscreenClass)
    fallbackTarget.value = target
    fullscreenState.value = true
  }
}
function onFullscreenChange() { fullscreenState.value = !!document.fullscreenElement || !!fallbackTarget.value }
// 某些浏览器的原生全屏会把指针事件直接交给全屏顶层的视频层，
// 外壳和视频上的监听不一定都能收到。全屏期间从 document 兜底接收，
// 仍由当前全屏元素计算底部悬停区域，避免依赖具体的全屏实现。
function onFullscreenPointerActivity(event: MouseEvent | PointerEvent) {
  const target = fallbackTarget.value || (document.fullscreenElement as HTMLElement | null)
  const ownTarget = props.container || root.value?.closest('.video-preview') || root.value?.parentElement
  // 页面可能同时挂着多个播放器；只有当前真正进入全屏的实例可以响应
  // document 级鼠标事件，避免隐藏的其他播放器被一起唤醒。
  if (!target || !ownTarget || target !== ownTarget) return
  showFromPointer(event, target)
}
function onFullscreenKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !fallbackTarget.value) return
  event.preventDefault()
  const target = fallbackTarget.value
  target.classList.remove(fallbackFullscreenClass)
  fallbackTarget.value = null
  fullscreenState.value = !!document.fullscreenElement
}
function onRevealPointerActivity(event: PointerEvent) {
  const target = root.value?.parentElement
  showFromPointer(event, target || null)
}
function onRevealPointerLeave() {
  // 控制条出现时，唤醒层会主动让位并触发 pointerleave；不能在这里
  // 立刻重新启动隐藏计时，否则会造成显示/隐藏闪烁。
  if (!controlsHidden.value) return
  pointerNearControls.value = false
}
onMounted(() => {
  document.addEventListener('fullscreenchange', onFullscreenChange)
  document.addEventListener('pointermove', onFullscreenPointerActivity)
  document.addEventListener('mousemove', onFullscreenPointerActivity)
  document.addEventListener('keydown', onFullscreenKeydown)
})
onBeforeUnmount(() => {
  detach?.(); clearMediaClickTimer(); clearHideControlsTimer(); cueRefreshTimers.forEach(timer => clearTimeout(timer)); cueRefreshTimers = []; if (clearMessageTimer) clearTimeout(clearMessageTimer)
  fallbackTarget.value?.classList.remove(fallbackFullscreenClass)
  fallbackTarget.value = null
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  document.removeEventListener('pointermove', onFullscreenPointerActivity)
  document.removeEventListener('mousemove', onFullscreenPointerActivity)
  document.removeEventListener('keydown', onFullscreenKeydown)
})
</script>

<template>
  <div ref="root" class="video-controls-layer" :class="{ 'controls-hidden': controlsHidden }">
    <div class="controls-reveal-zone" aria-hidden="true"
         @pointermove="onRevealPointerActivity" @pointerenter="onRevealPointerActivity" @pointerleave="onRevealPointerLeave" />
    <div class="video-controls" :class="{ dragging, hidden: controlsHidden }" aria-label="视频播放控制" :title="keyboardHint"
         @pointermove="showControls" @pointerenter="showControls" @focusin="showControls">
    <div v-if="hover" class="timeline-tooltip" :style="{ left: hover.left + '%' }">
      {{ formatTime(hover.time) }}<span v-if="hover.chapter"> · {{ hover.chapter }}</span>
    </div>
    <div class="timeline" role="slider" tabindex="0" :aria-valuemin="0" :aria-valuemax="duration" :aria-valuenow="current"
         :aria-valuetext="formatTime(current)" :aria-label="timelineLabel" :aria-disabled="!timelinePlayable"
         @pointerdown="beginScrub" @pointermove="moveScrub" @pointerup="endScrub" @pointercancel="endScrub"
         @mousemove="updateHover" @mouseleave="clearHover" @keydown="onProgressKey">
      <div class="timeline-buffer" :style="{ width: bufferedPercentage + '%' }" />
      <div class="timeline-played" :style="{ width: percentage + '%' }" />
      <span v-for="(chapter, index) in normalizedChapters" :key="chapter.start + ':' + index" class="timeline-chapter"
            :style="chapterMarkerStyle(chapter.start)" :title="chapter.title" />
      <span class="timeline-thumb" :style="{ left: percentage + '%' }" />
    </div>
    <div class="control-row">
      <n-button text size="small" class="control-button" :disabled="!media || !timelinePlayable" :aria-label="playing ? '暂停' : '播放'" title="播放/暂停（空格 / K）" @click="togglePlay">
        <svg class="control-icon" viewBox="0 0 20 20" aria-hidden="true"><path v-if="playing" d="M5 4h3v12H5zM12 4h3v12h-3z"/><path v-else d="M6 4.5v11l9-5.5z"/></svg>
      </n-button>
      <n-button text size="small" class="control-button jump-button" :disabled="!media || !duration" :aria-label="timeline ? '剪后后退5秒' : '后退5秒'" :title="`后退5秒（←）${timeline ? ' · 按剪后时间跳过删除段' : ''}`" @click="jumpBy(-5)"><svg class="control-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M4.5 6.5A7 7 0 1 1 3 12M4.5 2.5v4h4"/><text x="10" y="13.5" text-anchor="middle" fill="currentColor" stroke="none" font-size="8">5</text></svg></n-button>
      <n-button text size="small" class="control-button frame-button" :disabled="!media || !duration" :aria-label="timeline ? '剪后上一帧' : '上一帧'" :title="`上一帧（逗号）· ${frameStepLabel}`" @click="stepFrame(-1)">
        <span aria-hidden="true">|◀</span>
      </n-button>
      <span class="timecode">{{ formatTime(current) }} / {{ formatTime(duration) }}</span>
      <n-button text size="small" class="control-button frame-button" :disabled="!media || !duration" :aria-label="timeline ? '剪后下一帧' : '下一帧'" :title="`下一帧（句号）· ${frameStepLabel}`" @click="stepFrame(1)">
        <span aria-hidden="true">▶|</span>
      </n-button>
      <n-button text size="small" class="control-button jump-button" :disabled="!media || !duration" :aria-label="timeline ? '剪后前进5秒' : '前进5秒'" :title="`前进5秒（→）${timeline ? ' · 按剪后时间跳过删除段' : ''}`" @click="jumpBy(5)"><svg class="control-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M15.5 6.5A7 7 0 1 0 17 12M15.5 2.5v4h-4"/><text x="10" y="13.5" text-anchor="middle" fill="currentColor" stroke="none" font-size="8">5</text></svg></n-button>
      <n-button text size="small" class="control-button" :disabled="!media" :aria-label="listeningMuted ? '取消静音' : '静音'" title="静音/恢复（M）" @click="toggleMute">
        <svg class="control-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 8h3l4-3v10l-4-3H3z"/><path v-if="!(listeningMuted || listeningVolume === 0)" d="M13 7.5a4 4 0 0 1 0 5M15.5 5a7 7 0 0 1 0 10"/><path v-else d="m13 8 4 4m0-4-4 4"/></svg>
      </n-button>
      <n-slider class="volume" :value="listeningMuted ? 0 : listeningVolume" :min="0" :max="1" :step="0.01" :disabled="!media" aria-label="监听总音量" title="仅调整本机播放音量，不修改成片设置；播放器聚焦时 ↑/↓ 每次调整 5%" @update:value="setVolume" />
      <n-select v-if="media" class="rate" size="tiny" :value="rate" :options="rateOptions" aria-label="播放速度" @update:value="setRate" />
      <n-button v-if="trackCount" text size="small" class="control-button" :class="{ selected: trackShowing }" aria-label="字幕" @click="toggleTracks">CC</n-button>
      <n-text v-if="controlMessage" class="control-message" depth="3">{{ controlMessage }}</n-text>
      <span class="control-spacer" aria-hidden="true" />
      <n-button text size="small" class="control-button" :aria-label="fullscreen ? '退出全屏' : '全屏'" title="全屏/恢复（F）" @click="toggleFullscreen">
        <svg class="control-icon" viewBox="0 0 20 20" aria-hidden="true"><path v-if="!fullscreen" d="M3 8V3h5M12 3h5v5M17 12v5h-5M8 17H3v-5"/><path v-else d="M8 3v5H3M17 8h-5V3M12 17v-5h5M3 12h5v5"/></svg>
      </n-button>
    </div>
    </div>
  </div>
</template>

<style scoped>
.video-controls-layer{position:absolute;z-index:4;inset:0;pointer-events:none}.video-controls{position:absolute;z-index:4;left:0;right:0;bottom:0;padding:10px 12px 8px;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.9));opacity:.82;transition:opacity .2s;user-select:none;pointer-events:auto}.video-controls-layer.controls-hidden .controls-reveal-zone{pointer-events:auto}.controls-reveal-zone{position:absolute;z-index:3;left:0;right:0;bottom:0;height:64px;pointer-events:none;background:transparent}.video-controls:hover,.video-controls:focus-within,.video-controls.dragging{opacity:1}.video-controls.hidden{opacity:0;pointer-events:none}.timeline{position:relative;height:14px;margin:0 1px 4px;border-radius:5px;cursor:pointer;touch-action:none}.timeline:before{content:'';position:absolute;left:0;right:0;top:5px;height:4px;border-radius:3px;background:rgba(255,255,255,.35)}.timeline-buffer,.timeline-played{position:absolute;left:0;top:5px;height:4px;border-radius:3px;pointer-events:none}.timeline-buffer{background:rgba(255,255,255,.58)}.timeline-played{background:#55a9ff}.timeline-thumb{position:absolute;top:2px;width:10px;height:10px;transform:translateX(-50%);border-radius:50%;background:#fff;box-shadow:0 0 3px #000;pointer-events:none}.timeline-chapter{position:absolute;top:2px;width:3px;height:10px;transform:translateX(-50%);border-radius:2px;background:#ffd166;box-shadow:0 0 2px #000;pointer-events:none}.timeline-tooltip{position:absolute;bottom:39px;transform:translateX(-50%);padding:3px 6px;border-radius:4px;background:rgba(0,0,0,.86);font-size:11px;white-space:nowrap;pointer-events:none}.control-row{display:flex;align-items:center;gap:5px;min-height:24px}.control-button{color:#fff!important;min-width:26px}.control-button:hover,.control-button.selected{color:#8bc8ff!important}.control-icon{display:block;width:16px;height:16px;fill:currentColor;stroke:currentColor;stroke-width:1.35;stroke-linecap:round;stroke-linejoin:round}.jump-button{font-size:11px;font-variant-numeric:tabular-nums}.frame-button{font-size:11px;letter-spacing:-.08em;padding-inline:2px}.timecode{font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap}.volume{width:78px;margin:0 5px}.rate{width:67px}.control-message{max-width:35%;font-size:11px;color:#ffd166;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.control-spacer{flex:1;min-width:8px}
/* 全屏时由容器占满视口，视频在其中保持比例并完整显示。 */
:global(.video-shell:fullscreen),:global(.video-preview:fullscreen),:global(.cuts-player:fullscreen),:global(.target-reference-player:fullscreen),:global(.video-shell:-webkit-full-screen),:global(.video-preview:-webkit-full-screen),:global(.cuts-player:-webkit-full-screen),:global(.target-reference-player:-webkit-full-screen){display:flex!important;align-items:center;justify-content:center;width:100vw!important;height:100vh!important;max-width:none!important;max-height:none!important;background:#000!important}
:global(.video-shell:fullscreen > video),:global(.video-preview:fullscreen > video),:global(.cuts-player:fullscreen > video),:global(.target-reference-player:fullscreen > video),:global(.video-shell:-webkit-full-screen > video),:global(.video-preview:-webkit-full-screen > video),:global(.cuts-player:-webkit-full-screen > video),:global(.target-reference-player:-webkit-full-screen > video){display:block;width:100%!important;height:100%!important;max-width:100%!important;max-height:100%!important;aspect-ratio:auto!important;object-fit:contain!important;border-radius:0!important}
:global(.target-reference-player:fullscreen > .video-controls-layer),:global(.target-reference-player:-webkit-full-screen > .video-controls-layer){position:absolute!important;inset:0!important;width:100%!important;height:100%!important;max-width:none!important;max-height:none!important;pointer-events:none!important}
:global(.video-controls-fallback-fullscreen){position:fixed!important;z-index:2147483640!important;inset:0!important;display:flex!important;align-items:center;justify-content:center;width:100vw!important;height:100vh!important;max-width:none!important;max-height:none!important;background:#000!important}
:global(.video-controls-fallback-fullscreen > video){display:block;width:100%!important;height:100%!important;max-width:100%!important;max-height:100%!important;aspect-ratio:auto!important;object-fit:contain!important;border-radius:0!important}
:global(.video-shell > video),:global(.video-preview > video){cursor:pointer}
:global(.video-keyboard-scope:focus-visible){outline:2px solid #55a9ff;outline-offset:3px;border-radius:8px}
@media(max-width:500px){.volume{width:54px}.rate{width:60px}.control-row{gap:2px}.control-message{display:none}}
</style>
