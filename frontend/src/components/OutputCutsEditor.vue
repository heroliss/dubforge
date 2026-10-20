<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NProgress, NSelect, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import VideoControls from './VideoControls.vue'
import TimecodeInput from './TimecodeInput.vue'
import { vReleaseMedia } from '../utils/preview'
import { buildCutPreviewTimeline } from '../utils/cutPreviewTimeline'
import { createCutPreviewPlayback } from '../utils/cutPreviewPlayback'
import { buildVirtualPreviewSegments, cloneCutRanges, formatCutTime, mapSourceIntervalsToOutput, mergeCutRanges, nextRetainedTime, outputToSourceTime, parseCutTime, sourceToOutputTime, subtractCutIntervals, validateCutRanges, type OutputCutRange, type OutputCutsSnapshot, type OutputCutsState, type VirtualPreviewSegment } from '../utils/outputCuts'

type CutBaselineOption = { id: string; name: string; generated_at?: string | null; status?: string; fresh?: boolean; output_cuts?: any; artifact_id?: number | string | null; artifact_name?: string; artifact_hash?: string }
type LinkedAudioSource = 'target' | 'source' | 'none'
const props = defineProps<{ uid: string; versionId?: string | null; versionName?: string; baselineOptions?: CutBaselineOption[]; targetArtifactName?: string; targetArtifactHash?: string | null; targetArtifactFresh?: boolean; disabled?: boolean; running?: boolean; applyDisabled?: boolean }>()
const emit = defineEmits<{ 'dirty-change': [value: boolean]; 'busy-change': [value: boolean]; 'open-change': [value: boolean]; saved: [value: OutputCutsState]; 'timeline-created': [value: any] }>()
const dialog = useDialog(), message = useMessage()
const saved = ref<OutputCutsState | null>(null), enabled = ref(false), ranges = ref<OutputCutRange[]>([])
const loading = ref(false), saving = ref(false), creatingTimeline = ref(false), error = ref('')
const timelinePlanPreview = ref<any>(null)
const artifactContext = ref<any>(null), artifactContextLoading = ref(false)
let contextRead: AbortController | null = null, artifactContextGeneration = 0
// `video` 仅作为旧状态/测试兼容引用；模板已不再挂载第二个 source <video>。
// 所有真实试听均走 referenceVideo 的单一虚拟试听播放器。
const video = ref<HTMLVideoElement | null>(null), current = ref(0), markStart = ref<number | null>(0), markEnd = ref<number | null>(null)
const referenceVideo = ref<HTMLVideoElement | null>(null), referenceCurrent = ref(0), referenceDuration = ref(0), referenceMediaError = ref('')
const seekFreezeCanvas = ref<HTMLCanvasElement | null>(null), previewTransitioning = ref(false)
const previewScope = ref<'seam' | 'full'>('seam')
const previewScopeOptions = [{label:'当前接缝附近',value:'seam'}, {label:'完整剪辑清单',value:'full'}]
let preparedSeekFrame = false
let virtualResumeSource: number | null = null
const referenceSync = ref(true)
// 联动时只让一条音轨发声，避免原视频和成片配音叠加。切换来源不会改变
// 两个播放器的音量数值，只切换 muted 状态，方便来回对比试听。
const audioSource = ref<LinkedAudioSource>('target')
const audioSourceOptions = [
  { label: '当前成片（默认）', value: 'target' as LinkedAudioSource },
  { label: '原视频', value: 'source' as LinkedAudioSource },
  { label: '静音', value: 'none' as LinkedAudioSource },
]
let syncingPlayback = false
type SeekOrigin = 'source' | 'reference'
let seekSyncOrigin: SeekOrigin | null = null
let seekSyncTimer: ReturnType<typeof setTimeout> | undefined
// VideoControls 的显式拖动生命周期。原生 seeking/timeupdate 事件无法区分
// 用户拖动和联动代码写入；拖动期间以发起方为唯一权威，避免另一播放器的
// 播放时钟把正在拖动的位置抢回去。
let activeSeekOrigin: SeekOrigin | null = null
// 播放期间只允许发起播放的一侧作为时钟主端。另一侧只做有限的漂移校正，
// 避免每个 timeupdate 都写 currentTime，导致浏览器反复解码、卡顿或越播越偏。
let playbackOrigin: SeekOrigin | null = null
let playbackSyncTimer: ReturnType<typeof setTimeout> | undefined
let playbackRetryTimer: ReturnType<typeof setTimeout> | undefined
let pendingPlaybackOrigin: SeekOrigin | null = null
let suppressedPauseOrigin: SeekOrigin | null = null
// 播放/暂停事件是异步派发的：调用 follower.play()/pause() 后，原生事件
// 可能在当前函数返回、甚至 Promise settled 之后才到达。没有这层命令账本时，
// follower 的 pause 会被误认为用户暂停，继而反向暂停 master；切换联动开关
// 后的迟到 play 还可能重新夺回主时钟。
type PlaybackAction = 'play' | 'pause'
type PlaybackCommand = { side: SeekOrigin; action: PlaybackAction; epoch: number; expires: number }
let playbackEpoch = 0
let playbackCommands: PlaybackCommand[] = []
let lastPlaybackOrigin: SeekOrigin | null = null
const playbackLinkState = ref<'idle' | 'syncing' | 'waiting'>('idle')
let lastPlaybackHardSeekAt = 0
let playbackBaseRates: { source: number; reference: number } | null = null
const playbackDriftTolerance = 0.14
const playbackHardSeekThreshold = 0.75
const playbackCommandLifetime = 450
// 上一次由联动逻辑写入的目标值。原生 seeking/timeupdate 事件不带“用户还是代码”
// 的来源信息，因此用它识别目标播放器是否被用户再次拖到另一个位置。
let linkedSourceTime: number | null = null
let linkedReferenceTime: number | null = null
const seekDriftTolerance = 0.22
function holdSeekOrigin(origin: SeekOrigin) {
  if (!referenceSync.value) return false
  if (seekSyncOrigin && seekSyncOrigin !== origin) return false
  seekSyncOrigin = origin
  if (seekSyncTimer) clearTimeout(seekSyncTimer)
  // 拖动会连续产生 seeking/timeupdate；短暂保持发起方为主，避免另一边
  // 的异步事件反向抢回时间轴。停止拖动后自动恢复普通双向同步。
  seekSyncTimer = setTimeout(() => { seekSyncOrigin = null; seekSyncTimer = undefined; linkedSourceTime = null; linkedReferenceTime = null }, 320)
  return true
}
function clearSeekSync() {
  if (seekSyncTimer) clearTimeout(seekSyncTimer)
  seekSyncTimer = undefined; seekSyncOrigin = null; activeSeekOrigin = null
  linkedSourceTime = null; linkedReferenceTime = null
}
function clearPlaybackSync() {
  // 使尚未完成的 play() / retry 回调失效。尤其是“关闭联动后马上
  // 重新打开”的场景，旧 Promise 不能再在新会话里调用 startPlaybackSync。
  playbackEpoch += 1
  playbackCommands = []
  if (playbackSyncTimer) clearTimeout(playbackSyncTimer)
  if (playbackRetryTimer) clearTimeout(playbackRetryTimer)
  playbackSyncTimer = undefined
  playbackRetryTimer = undefined
  pendingPlaybackOrigin = null
  suppressedPauseOrigin = null
  playbackOrigin = null
  lastPlaybackHardSeekAt = 0
  playbackLinkState.value = 'idle'
  // 漂移校正会临时修改从播放器的 playbackRate；结束联动后恢复用户原先
  // 选择的倍速，而不是强行重置为 1×。
  if (playbackBaseRates) {
    if (video.value) video.value.playbackRate = playbackBaseRates.source
    if (referenceVideo.value) referenceVideo.value.playbackRate = playbackBaseRates.reference
  }
  playbackBaseRates = null
}
function issuePlaybackCommand(side: SeekOrigin, action: PlaybackAction) {
  playbackCommands.push({ side, action, epoch: playbackEpoch, expires: Date.now() + playbackCommandLifetime })
}
function consumePlaybackCommand(side: SeekOrigin, action: PlaybackAction) {
  const now = Date.now()
  playbackCommands = playbackCommands.filter(command => command.epoch === playbackEpoch && command.expires > now)
  const index = playbackCommands.findIndex(command => command.side === side && command.action === action)
  if (index < 0) return false
  playbackCommands.splice(index, 1)
  return true
}
function cancelPendingPlaybackTransition() {
  playbackEpoch += 1
  playbackCommands = []
  pendingPlaybackOrigin = null
  suppressedPauseOrigin = null
  if (playbackSyncTimer) clearTimeout(playbackSyncTimer)
  playbackSyncTimer = undefined
  if (playbackRetryTimer) clearTimeout(playbackRetryTimer)
  playbackRetryTimer = undefined
  syncingPlayback = false
  playbackLinkState.value = 'idle'
}
function playbackExpectedTime(origin: SeekOrigin): number | null {
  if (origin === 'source') {
    const source = video.value
    return source ? sourceToOutputTime(referenceCuts.value, source.currentTime) : null
  }
  const target = referenceVideo.value
  return target ? outputToSourceTime(referenceCuts.value, target.currentTime, 'next') : null
}
function schedulePlaybackSync() {
  if (playbackSyncTimer) clearTimeout(playbackSyncTimer)
  if (!playbackOrigin) return
  playbackSyncTimer = setTimeout(() => {
    playbackSyncTimer = undefined
    syncPlaybackClock()
  }, 100)
}
function syncPlaybackClock(force = false) {
  const origin = playbackOrigin
  if (!origin || !referenceSync.value) return
  const master = origin === 'source' ? video.value : referenceVideo.value
  const slave = origin === 'source' ? referenceVideo.value : video.value
  if (!master || !slave || master.paused || master.ended) {
    if (pendingPlaybackOrigin) {
      playbackLinkState.value = 'waiting'
      schedulePlaybackRetry()
    } else if (master?.paused || master?.ended) clearPlaybackSync()
    return
  }
  const expected = playbackExpectedTime(origin)
  if (expected === null || !Number.isFinite(expected) || slave.readyState < 1) {
    schedulePlaybackSync()
    return
  }
  const drift = slave.currentTime - expected
  const now = typeof performance !== 'undefined' ? performance.now() : Date.now()
  // 大偏差只在有冷却时间时硬定位一次；否则会在目标媒体尚未解码时连续
  // seek，表现为播放器卡住数秒。小偏差用轻微变速收敛，不触发重新解码。
  if ((force || Math.abs(drift) >= playbackHardSeekThreshold) && (force || lastPlaybackHardSeekAt === 0 || now - lastPlaybackHardSeekAt >= 500)) {
    try { slave.currentTime = Math.max(0, expected) } catch { /* 媒体尚未可定位 */ }
    slave.playbackRate = master.playbackRate || 1
    lastPlaybackHardSeekAt = now
  } else if (Math.abs(drift) >= playbackDriftTolerance) {
    const correction = Math.max(-0.08, Math.min(0.08, drift * 0.18))
    slave.playbackRate = Math.max(0.85, Math.min(1.15, (master.playbackRate || 1) - correction))
  } else {
    slave.playbackRate = master.playbackRate || 1
  }
  schedulePlaybackSync()
}
function startPlaybackSync(origin: SeekOrigin) {
  if (!referenceSync.value) return
  if (!playbackBaseRates) playbackBaseRates = {
    source: video.value?.playbackRate || 1,
    reference: referenceVideo.value?.playbackRate || 1,
  }
  playbackOrigin = origin
  lastPlaybackOrigin = origin
  playbackLinkState.value = 'syncing'
  syncPlaybackClock()
}
function schedulePlaybackRetry() {
  if (playbackRetryTimer || !pendingPlaybackOrigin) return
  playbackRetryTimer = setTimeout(() => {
    playbackRetryTimer = undefined
    retryPendingPlayback()
  }, 180)
}
function retryPendingPlayback() {
  const origin = pendingPlaybackOrigin
  if (!origin || !referenceSync.value) return
  const epoch = playbackEpoch
  const master = origin === 'source' ? video.value : referenceVideo.value
  const follower = origin === 'source' ? referenceVideo.value : video.value
  if (!master || !follower || master.ended) { clearPlaybackSync(); return }
  if (follower.readyState < 2) {
    playbackLinkState.value = 'waiting'
    schedulePlaybackRetry()
    return
  }
  pendingPlaybackOrigin = null
  playbackLinkState.value = 'syncing'
  syncingPlayback = true
  const tasks: Promise<unknown>[] = []
  if (master.paused) {
    issuePlaybackCommand(origin, 'play')
    tasks.push(Promise.resolve(master.play()))
  }
  issuePlaybackCommand(origin === 'source' ? 'reference' : 'source', 'play')
  tasks.push(Promise.resolve(follower.play()))
  void Promise.all(tasks).catch(() => {
    if (epoch !== playbackEpoch) return
    pendingPlaybackOrigin = origin
    playbackLinkState.value = 'waiting'
    // 本轮至少有一个 play 失败；旧的 play 命令不应留到下一次 retry，
    // 否则下一次真实用户点击可能被误消费成迟到的镜像事件。
    playbackCommands = []
    issuePlaybackCommand(origin, 'pause')
    try { master.pause() } catch { /* 媒体已卸载 */ }
    schedulePlaybackRetry()
  }).finally(() => {
    if (epoch !== playbackEpoch) return
    syncingPlayback = false
    if (!pendingPlaybackOrigin) startPlaybackSync(origin)
  })
}
function releaseSeekSyncSoon() {
  if (!seekSyncOrigin) return
  if (seekSyncTimer) clearTimeout(seekSyncTimer)
  seekSyncTimer = setTimeout(() => { seekSyncOrigin = null; seekSyncTimer = undefined; linkedSourceTime = null; linkedReferenceTime = null }, 180)
}
function beginSeek(origin: SeekOrigin) {
  if (!referenceSync.value) return
  // 另一侧明确发出 seek-start/seek 代表用户已切换操作对象，立即切换
  // 主导权；联动产生的原生 seeking 不会调用此函数。
  if (seekSyncOrigin && seekSyncOrigin !== origin) {
    // 清掉上一轮联动的迟到事件标记，但不要调用 clearSeekSync()：它还会
    // 清除刚刚要建立的新 activeSeekOrigin，导致切换到另一侧的第一个
    // seek 被当成程序性定位而丢失。
    if (seekSyncTimer) clearTimeout(seekSyncTimer)
    seekSyncTimer = undefined
    seekSyncOrigin = null
    linkedSourceTime = null
    linkedReferenceTime = null
  }
  activeSeekOrigin = origin
  seekSyncOrigin = origin
  if ((video.value && !video.value.paused) || (referenceVideo.value && !referenceVideo.value.paused)) playbackOrigin = origin
}
function endSeek(origin: SeekOrigin) {
  if (activeSeekOrigin !== origin) return
  // seek-end 仍用最终媒体时间再同步一次，避免最后一个 pointermove 还未
  // 触发 timeupdate 就释放锁，随后由另一播放器的时钟抢回位置。
  if (origin === 'source') syncReferenceToSource()
  else syncSourceToReference()
  activeSeekOrigin = null
  if (playbackOrigin === origin) syncPlaybackClock()
  releaseSeekSyncSoon()
}
// 时间标记使用独立文本草稿，避免 NInputNumber 在自定义“时:分:秒”解析期间
// 因父组件回写旧数值而覆盖用户尚未输入完的内容（典型表现是起止点都跳回
// 同一个当前播放时间）。提交或离开输入框时才解析为秒数。
const markStartText = ref('00:00:00.000'), markEndText = ref('')
const markReason = ref(''), skipCuts = ref(false)
const baselineId = ref<string | null>(null)
let disposed = false, generation = 0, controller: AbortController | null = null, confirmation: any
const duration = computed(() => saved.value?.duration ?? null)
const frameRate = computed(() => {
  const value = Number((saved.value as any)?.frame_rate)
  return Number.isFinite(value) && value > 0 ? value : undefined
})
const invalidRangeInputs = ref<Record<string, boolean>>({})
const rangeEditorGeneration = ref(0)
const hasInvalidRangeInput = computed(() => Object.values(invalidRangeInputs.value).some(Boolean))
const markerSignature = computed(() => JSON.stringify([markStartText.value, markEndText.value, markReason.value]))
const markerBaselineSignature = ref(markerSignature.value)
const hasPendingMarker = computed(() => !!saved.value && markerSignature.value !== markerBaselineSignature.value)
const markerActionError = ref('')
const draftSignature = computed(() => JSON.stringify({ enabled: enabled.value, ranges: cloneCutRanges(ranges.value), baseline_id: baselineId.value }))
const savedBaselineId = () => (saved.value as any)?.baseline?.version_id || (saved.value as any)?.cut_baseline?.version_id || (saved.value as any)?.base_version_id || null
const listDirty = computed(() => !!saved.value && (hasInvalidRangeInput.value || draftSignature.value !== JSON.stringify({ enabled: saved.value.enabled, ranges: cloneCutRanges(saved.value.ranges), baseline_id: savedBaselineId() })))
const dirty = computed(() => listDirty.value || hasPendingMarker.value)
const effective = computed(() => mergeCutRanges(ranges.value, duration.value, enabled.value))
const restoredRanges = computed(() => ranges.value.filter(row => row.enabled === false))
const removedDuration = computed(() => effective.value.reduce((total, row) => total + row.end - row.start, 0))
const outputDuration = computed(() => duration.value === null ? null : Math.max(0, duration.value - removedDuration.value))
const validation = computed(() => [
  ...(hasInvalidRangeInput.value ? ['清单中有未完成或无效的时间输入，请修正后再保存或生成。'] : []),
  ...validateCutRanges(ranges.value, duration.value, enabled.value),
])
const unavailable = computed(() => !saved.value?.source_url || !saved.value?.source_fingerprint || !duration.value)
const locked = computed(() => !!props.disabled || loading.value || saving.value || creatingTimeline.value)
const markerTextError = ref('')
const markerValidation = computed(() => markerTextError.value
  ? [markerTextError.value]
  : validateCutRanges([{ id: 'new', start: parseCutTime(markStartText.value), end: parseCutTime(markEndText.value), reason: '', enabled: true }], duration.value, false))
const pendingMarkerReady = computed(() => hasPendingMarker.value && !markerValidation.value.length && ranges.value.length < 200)
const pendingMarkerBlocked = computed(() => hasPendingMarker.value && !pendingMarkerReady.value)
const pendingMarkerDescription = computed(() => pendingMarkerReady.value
  ? `${formatCutTime(parseCutTime(markStartText.value))}–${formatCutTime(parseCutTime(markEndText.value))} · 尚未加入清单`
  : ranges.value.length >= 200 ? '清单最多保留 200 段，请先移除不需要的片段，或清除当前标记。'
  : '这段标记尚未完成，请补全有效的起点和终点，或清除标记后继续。')
const summary = computed(() => unavailable.value ? '原视频就绪后可编辑'
  : saved.value?.source_changed ? '原视频已更换 · 请核对并重新绑定范围'
  : !enabled.value ? `未启用 · 原片 ${formatCutTime(duration.value)}`
  : `删除 ${effective.value.length} 段 · ${formatCutTime(removedDuration.value)}，保留 ${formatCutTime(outputDuration.value)}`)
const selectedBaselineName = computed(() => props.baselineOptions?.find(option => option.id === baselineId.value)?.name || baselineId.value || '')
// 旧成片只作为只读参考；编辑器的标记仍绑定原视频时间轴，避免将裁剪后的输出时间
// 误当成源时间。artifact_name 由版本工作台从已登记产物中解析而来。
const baselinePreviewUrl = computed(() => {
  const option = props.baselineOptions?.find(row => row.id === baselineId.value)
  if (!option?.artifact_name) return ''
  const stamp = option.artifact_hash ? `?v=${encodeURIComponent(option.artifact_hash)}` : ''
  return `/api/projects/${encodeURIComponent(props.uid)}/steps/assemble/artifacts/${encodeURIComponent(option.artifact_name)}/raw${stamp}`
})
// 当前选中的成片版本的正式产物。默认用于主预览和试听，删除清单仍以
// 原视频时间轴保存；展开源参考后可核对恢复来源，不会修改正式文件。
const targetPreviewUrl = computed(() => {
  if (!props.targetArtifactName) return ''
  const stamp = props.targetArtifactHash ? `?v=${encodeURIComponent(props.targetArtifactHash)}` : ''
  return `/api/projects/${encodeURIComponent(props.uid)}/steps/assemble/artifacts/${encodeURIComponent(props.targetArtifactName)}/raw${stamp}`
})
type VirtualDonorKind = 'source' | 'version'
type VirtualPreviewStatus = 'idle' | 'loading' | 'ready' | 'error'
const virtualDonorKind = ref<VirtualDonorKind>('source')
const virtualDonorVersionId = ref<string | null>(null)
const virtualPreviewStatus = ref<VirtualPreviewStatus>('idle')
const virtualPreviewProgress = ref(0)
const virtualPreviewStage = ref('')
const virtualPreviewMessage = ref('')
const virtualPreviewValidation = ref('')
const virtualPreviewError = ref('')
const virtualPreviewTaskId = ref<string | null>(null)
const virtualPreviewUrl = ref('')
const virtualPreviewSegments = ref<VirtualPreviewSegment[]>([])
const virtualPreviewDuration = ref(0)
const virtualPreviewSourceLabel = ref('原视频')
const virtualPreviewCacheHit = ref(false)
const virtualPreviewFrameRate = ref<number | null>(null)
const previewFrameRate = computed(() => (virtualPreviewStatus.value === 'ready' ? virtualPreviewFrameRate.value
  : targetPreviewUrl.value ? artifactContext.value?.artifact_validation?.frame_rate : null) || frameRate.value || 30)
let virtualPreviewTimer: ReturnType<typeof setTimeout> | undefined
let virtualPreviewGeneration = 0
const virtualDonorOptions = computed(() => [
  { label: '原视频（完整来源）', value: 'source' },
  ...(props.baselineOptions || []).filter(option => option.id !== props.versionId).map(option => ({ label: `成片：${baselineLabel(option)}`, value: `version:${option.id}` })),
])
const hasRestoredIntervals = computed(() => {
  const base = referenceCuts.value?.ranges || []
  const draft = effective.value
  return subtractCutIntervals(base, draft).length > 0
})
const restoredIntervalCount = computed(() => subtractCutIntervals(referenceCuts.value?.ranges || [], effective.value).length)
const artifactMappingReady = computed(() => !targetPreviewUrl.value || (!!artifactContext.value?.artifact_validation?.valid && !!artifactContext.value?.artifact_cuts))
const artifactMappingMessage = computed(() => artifactContextLoading.value ? '正在核对已生成成片的原片时间映射…' : artifactContext.value?.artifact_validation?.message || '尚未取得已生成成片的剪辑记录，请重新核对来源。')
const hasCutChanges = computed(() => !!targetPreviewUrl.value && artifactMappingReady.value && JSON.stringify(effective.value) !== JSON.stringify(referenceCuts.value?.ranges || []))
const virtualPreviewEnabled = computed(() => !!skipCuts.value && !!targetPreviewUrl.value && artifactMappingReady.value && hasCutChanges.value)
const virtualPreviewBusy = computed(() => virtualPreviewStatus.value === 'loading')
const virtualPreviewSummary = computed(() => {
  if (!targetPreviewUrl.value) return '尚无当前成片，按原片试听并跳过删除清单'
  if (!artifactMappingReady.value) return artifactContextLoading.value ? '正在校验成片来源' : '成片来源尚未通过校验'
  if (!skipCuts.value) return '按当前成片原样试听'
  if (virtualPreviewStatus.value === 'loading') return `正在生成恢复试听 · ${Math.round(virtualPreviewProgress.value)}%`
  if (virtualPreviewStatus.value === 'error') return '虚拟试听失败，仍保留当前成片预览'
  if (virtualPreviewStatus.value === 'ready') return `虚拟试听已就绪 · ${virtualPreviewSourceLabel.value}`
  return hasRestoredIntervals.value ? '需要生成恢复试听' : '将跳过当前删除清单'
})
// 试听模式优先作用于当前成片。它已经包含已保存的删除范围；草稿新增范围
// 通过 source→output 映射在浏览器内跳过，未保存的恢复范围无法从旧文件凭空
// 还原，因此会提示用户回到原视频或载入其它成片基准。
const previewModeLabel = computed(() => {
  if (!effective.value.length) {
    return targetPreviewUrl.value ? '连续播放当前成片' : '连续播放原片'
  }
  const scope = dirty.value ? '当前草稿' : '已保存方案'
  if (targetPreviewUrl.value) {
    if (!artifactMappingReady.value) return `${scope} · 成片时间映射待核对`
    if (virtualPreviewStatus.value === 'ready') return `${scope} · 虚拟试听已按当前清单拼接恢复来源`
    if (skipCuts.value && hasRestoredIntervals.value) return `${scope} · 等待生成恢复试听${previewTargetCuts.value.length ? ` · 跳过 ${previewTargetCuts.value.length} 段新增删除` : ''}`
    return skipCuts.value
      ? previewTargetCuts.value.length
        ? `${scope} · 当前成片试听跳过 ${previewTargetCuts.value.length} 段新增删除`
        : `${scope} · 当前成片已包含清单中的删除`
      : `${scope} · 当前成片按已生成版本试听`
  }
  return skipCuts.value ? `${scope} · 原片试听跳过 ${effective.value.length} 段` : `${scope} · 原片试听包含删除段`
})
const previewModeNote = computed(() => {
  if (targetPreviewUrl.value) {
    if (!artifactMappingReady.value) return artifactMappingMessage.value
    if (props.targetArtifactFresh === false && !skipCuts.value) return '现有成片与当前工作稿不同，仍可按其真实时间轴预览；勾选“预览剪辑效果”，或直接生成独立剪辑版。'
    if (!skipCuts.value) return '当前成片是上次已生成的版本；未保存的删除调整只显示在原片时间轴上，不会改变这段试听。'
    if (virtualPreviewStatus.value === 'ready') return '连贯预览已按当前清单拼接；只查看当前预览范围，正式成片保持原样。'
    if (hasRestoredIntervals.value) return `有 ${restoredIntervalCount.value} 段恢复内容尚需拼接；生成连贯预览后可查看。即时预览先跳过本次新增删除。`
    if (previewTargetCuts.value.length) return `即时预览跳过 ${previewTargetCuts.value.length} 段新增删除；如遇寻址缓冲，可生成接缝附近的连贯预览。`
    return '当前成片已经包含已保存的删除范围，不会再次跳过；若要恢复其中片段，需要使用源视频或其他可用成片生成虚拟试听。'
  }
  if (!skipCuts.value) return '原片连续播放；删除范围只显示在时间轴上，不会修改原文件或立即生成成片。'
  return '独立试听原视频时会自动跨过当前删除清单；暂停、定位和标记操作不会被强制跳过。'
})
const playbackLinkLabel = computed(() => playbackLinkState.value === 'waiting'
  ? '等待另一视频缓冲'
  : playbackLinkState.value === 'syncing' ? '联动校正中' : '联动待机')
const playbackLinkTagType = computed(() => playbackLinkState.value === 'waiting' ? 'warning' : playbackLinkState.value === 'syncing' ? 'success' : 'default')
// 时间映射必须来自这个媒体文件生成时的快照；保存的新清单不能反向改变旧文件坐标。
const referenceCuts = computed<OutputCutsSnapshot | null>(() => artifactContext.value?.artifact_validation?.valid
  ? artifactContext.value.artifact_cuts || null : null)
const previewTargetCuts = computed(() => {
  if (!targetPreviewUrl.value || !artifactMappingReady.value || !saved.value?.duration) return []
  return mapSourceIntervalsToOutput(subtractCutIntervals(effective.value, referenceCuts.value?.ranges || []), referenceCuts.value)
})
// 原片编辑坐标、已有成片坐标与剪后播放坐标各有唯一映射。控件先映射再
// seek，不能让逐帧/后退先落入删除段，再被 timeupdate 推回同一接缝。
const playbackTimeline = computed(() => {
  if (!skipCuts.value || virtualPreviewStatus.value === 'ready') return null
  if (!artifactMappingReady.value) return buildCutPreviewTimeline({mediaDuration:0,fps:previewFrameRate.value})
  const mediaDuration = referenceDuration.value || (targetPreviewUrl.value
    ? artifactContext.value?.artifact_validation?.actual_duration || referenceCuts.value?.output_duration
    : duration.value) || 0
  return buildCutPreviewTimeline({ mediaDuration, fps: previewFrameRate.value,
    deleteRanges: targetPreviewUrl.value ? previewTargetCuts.value : effective.value })
})
const previewPosition = computed(() => playbackTimeline.value?.valid
  ? playbackTimeline.value.fromMedia(referenceCurrent.value) : referenceCurrent.value)
const previewPlaybackDuration = computed(() => virtualPreviewStatus.value === 'ready' ? virtualPreviewDuration.value
  : playbackTimeline.value?.duration ?? (referenceDuration.value || duration.value || 0))
const previewSourceWindow = computed(() => {
  if (previewScope.value === 'full' || !duration.value) return null
  const nearest = effective.value.reduce<{start:number;end:number}|null>((best, row) =>
    !best || Math.abs(row.start-current.value)<Math.abs(best.start-current.value) ? row : best, null)
  return { start: Math.max(0, (nearest?.start ?? current.value) - 5),
    end: Math.min(duration.value, (nearest?.end ?? current.value) + 5) }
})
const previewWindowLabel = computed(() => virtualPreviewStatus.value === 'ready' && virtualPreviewSegments.value.length
  ? `原片 ${formatCutTime(virtualPreviewSegments.value[0]!.source_start)}–${formatCutTime(virtualPreviewSegments.value.at(-1)!.source_end)}`
  : previewSourceWindow.value ? `原片 ${formatCutTime(previewSourceWindow.value.start)}–${formatCutTime(previewSourceWindow.value.end)}` : '完整剪辑清单')
function prepareSeekFrame() {
  if (preparedSeekFrame) return
  const media = referenceVideo.value, canvas = seekFreezeCanvas.value
  if (!media || !canvas || media.readyState < 2 || !media.videoWidth) return
  canvas.width = Math.min(1280, media.videoWidth)
  canvas.height = Math.max(1, Math.round(canvas.width * media.videoHeight / media.videoWidth))
  try { canvas.getContext('2d')?.drawImage(media, 0, 0, canvas.width, canvas.height); preparedSeekFrame = true }
  catch { preparedSeekFrame = false }
}
function finishPreviewSeek() { previewTransitioning.value = false; preparedSeekFrame = false }
function onEditorToggle(event: Event) {
  const open = (event.currentTarget as HTMLDetailsElement).open
  emit('open-change', open)
  if (!open) { referenceVideo.value?.pause(); previewClock.stop(); finishPreviewSeek() }
}
const previewClock = createCutPreviewPlayback({
  getMedia: () => referenceVideo.value,
  getTimeline: () => playbackTimeline.value,
  onPrepareJump: prepareSeekFrame,
  onJump: () => { previewTransitioning.value = true },
  onSettled: finishPreviewSeek,
})
function onReferenceSeeked() {
  onReferenceTimeUpdate(); previewClock.refresh()
}
function previewSeam(direction: number) {
  const points = effective.value.map(row => row.start)
  const position = current.value
  const selected = direction > 0 ? points.find(point => Math.max(0, point-2) > position + .05)
    : [...points].reverse().find(point => Math.max(0, point-2) < position - .05)
  if (selected !== undefined) seek(Math.max(0, selected - 2))
}
const virtualPreviewBaseCuts = computed<OutputCutsSnapshot | null>(() => {
  if (!saved.value?.duration || !Number.isFinite(saved.value.duration)) return null
  return referenceCuts.value
})
const virtualPreviewDraftCuts = computed<OutputCutsSnapshot | null>(() => {
  if (!saved.value?.duration || !Number.isFinite(saved.value.duration)) return null
  return { source_duration: saved.value.duration, ranges: effective.value, output_duration: outputDuration.value || undefined }
})
function donorVersionOption() {
  if (virtualDonorKind.value !== 'version' || !virtualDonorVersionId.value) return null
  return props.baselineOptions?.find(option => option.id === virtualDonorVersionId.value) || null
}
function donorArtifactUrl(option: CutBaselineOption | null) {
  if (!option?.artifact_name) return ''
  const stamp = option.artifact_hash ? `?v=${encodeURIComponent(option.artifact_hash)}` : ''
  return `/api/projects/${encodeURIComponent(props.uid)}/steps/assemble/artifacts/${encodeURIComponent(option.artifact_name)}/raw${stamp}`
}
function virtualPreviewRequestBody() {
  return {
    version_id: props.versionId || null,
    base_version_id: props.versionId || null,
    source_fingerprint: saved.value?.source_fingerprint || null,
    draft: { enabled: enabled.value, ranges: cloneCutRanges(ranges.value) },
    donor: virtualDonorKind.value === 'source'
      ? { kind: 'source' }
      : { kind: 'version', version_id: virtualDonorVersionId.value },
  }
}
async function fetchDonorCuts(signal: AbortSignal): Promise<OutputCutsSnapshot | null> {
  const option = donorVersionOption()
  if (!option) return null
  const response = await fetch(`/api/projects/${encodeURIComponent(props.uid)}/output-cuts/virtual-preview/context?version_id=${encodeURIComponent(option.id)}`, { cache: 'no-store', signal })
  const data = await response.json()
  if (!response.ok || !data.artifact_validation?.valid || !data.artifact_cuts) throw new Error(data.artifact_validation?.message || data.message || '读取恢复成片剪辑记录失败')
  if (data.source_fingerprint !== saved.value?.source_fingerprint) throw new Error('所选恢复成片与当前原视频不是同一来源，无法安全拼接。')
  return data.artifact_cuts
}
async function loadArtifactContext() {
  contextRead?.abort(); const token = ++artifactContextGeneration, uid = props.uid, versionId = props.versionId, artifactHash = props.targetArtifactHash, artifactName = props.targetArtifactName
  if (!targetPreviewUrl.value || !versionId) { artifactContext.value = null; artifactContextLoading.value = false; return }
  const read = new AbortController(); contextRead = read; artifactContextLoading.value = true
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/output-cuts/virtual-preview/context?version_id=${encodeURIComponent(versionId)}`, { cache: 'no-store', signal: AbortSignal.any([read.signal, AbortSignal.timeout(15000)]) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '核对成片来源失败')
    if (!disposed && token === artifactContextGeneration && uid === props.uid && versionId === props.versionId && artifactName === props.targetArtifactName && artifactHash === props.targetArtifactHash) {
      artifactContext.value = artifactHash && data.artifact_validation?.artifact_hash !== artifactHash
        ? { artifact_validation: { valid: false, message: '成片文件已变化，剪辑记录与当前播放器不一致，请刷新版本后重新核对。' } } : data
    }
  } catch (e: any) {
    if (!read.signal.aborted && token === artifactContextGeneration) artifactContext.value = { artifact_validation: { valid: false, message: e?.name === 'TimeoutError' ? '核对成片来源超时，请重试。' : e.message } }
  } finally { if (token === artifactContextGeneration) { contextRead = null; artifactContextLoading.value = false } }
}

function virtualPreviewEndpoint(taskId = '') {
  const base = `/api/projects/${encodeURIComponent(props.uid)}/output-cuts/virtual-preview`
  return taskId ? `${base}/${encodeURIComponent(taskId)}` : base
}
function clearVirtualPreviewTimer() {
  if (virtualPreviewTimer) clearTimeout(virtualPreviewTimer)
  virtualPreviewTimer = undefined
}
function resetVirtualPreview(options: { preservePosition?: boolean } = {}) {
  if (options.preservePosition === false) virtualResumeSource = null
  else if (virtualPreviewStatus.value === 'ready' && referenceVideo.value) {
    virtualResumeSource = virtualTimeToSource(referenceVideo.value.currentTime)
  }
  const pendingTask = virtualPreviewStatus.value === 'loading' ? virtualPreviewTaskId.value : null
  clearVirtualPreviewTimer(); virtualPreviewGeneration += 1
  if (pendingTask) void fetch(`${virtualPreviewEndpoint(pendingTask)}/cancel`, { method: 'POST' }).catch(() => {})
  virtualPreviewStatus.value = 'idle'; virtualPreviewProgress.value = 0; virtualPreviewError.value = ''
  virtualPreviewStage.value = ''; virtualPreviewMessage.value = ''
  virtualPreviewValidation.value = ''
  virtualPreviewCacheHit.value = false
  virtualPreviewFrameRate.value = null
  virtualPreviewTaskId.value = null; virtualPreviewUrl.value = ''; virtualPreviewSegments.value = []; virtualPreviewDuration.value = 0
}
function adoptVirtualSegments(rows: any) {
  if (!Array.isArray(rows)) return
  let cursor = 0
  virtualPreviewSegments.value = rows.map((row: any) => {
    const sourceStart = Number(row.source_start), sourceEnd = Number(row.source_end)
    const length = Math.max(0, sourceEnd - sourceStart)
    const virtualStart = Number.isFinite(Number(row.virtual_start)) ? Number(row.virtual_start) : cursor
    const virtualEnd = Number.isFinite(Number(row.virtual_end)) ? Number(row.virtual_end) : virtualStart + length
    cursor = virtualEnd
    return { ...row, source_start: sourceStart, source_end: sourceEnd, virtual_start: virtualStart, virtual_end: virtualEnd, available: row.available !== false }
  }).filter((row: VirtualPreviewSegment) => Number.isFinite(row.source_start) && Number.isFinite(row.source_end) && row.source_end > row.source_start)
  virtualPreviewDuration.value = Number.isFinite(cursor) ? cursor : 0
}
async function pollVirtualPreview(taskId: string, generation: number) {
  if (generation !== virtualPreviewGeneration || !taskId) return
  try {
    const response = await fetch(virtualPreviewEndpoint(taskId), { cache: 'no-store' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '读取虚拟试听进度失败')
    if (generation !== virtualPreviewGeneration) return
    const progress = Number(data.progress)
    if (Number.isFinite(progress)) virtualPreviewProgress.value = Math.max(virtualPreviewProgress.value, Math.max(0, Math.min(100, progress)))
    virtualPreviewStage.value = String(data.stage || '')
    virtualPreviewMessage.value = String(data.message || '')
    virtualPreviewValidation.value = String(data.source_validation?.message || '')
    if (Array.isArray(data.segments)) adoptVirtualSegments(data.segments)
    if (Number.isFinite(Number(data.duration))) virtualPreviewDuration.value = Number(data.duration)
    if (data.status === 'done' || data.status === 'completed') {
      virtualResumeSource = current.value
      virtualPreviewCacheHit.value = !!data.cache_hit
      virtualPreviewFrameRate.value = Number(data.frame_rate) || null
      virtualPreviewStatus.value = 'ready'
      virtualPreviewProgress.value = 100
      virtualPreviewUrl.value = data.output_url || data.url || data.raw_url || `${virtualPreviewEndpoint(taskId)}/raw`
      virtualPreviewSourceLabel.value = virtualDonorKind.value === 'source' ? '原视频恢复段' : (donorVersionOption()?.name || '其他成片恢复段')
      return
    }
    if (['failed', 'error', 'cancelled'].includes(data.status)) throw new Error(data.error_detail || data.error || data.message || '虚拟试听生成失败')
    virtualPreviewTimer = setTimeout(() => { void pollVirtualPreview(taskId, generation) }, 350)
  } catch (error: any) {
    if (generation !== virtualPreviewGeneration) return
    virtualPreviewStatus.value = 'error'; virtualPreviewError.value = error?.message || '虚拟试听生成失败'
  }
}
async function startVirtualPreview() {
  if (locked.value || !targetPreviewUrl.value || !artifactMappingReady.value || !saved.value?.duration || !skipCuts.value) return
  const pendingStart = pendingMarkerReady.value ? parseCutTime(markStartText.value) : null
  if (!includePendingMarker() || validation.value.length) return
  // 手工输入的新标记也以这一段为接缝预览中心，避免仍预览上一段而漏看本次调整。
  if (pendingStart !== null) current.value = pendingStart
  if (virtualPreviewStatus.value === 'ready' && referenceVideo.value) {
    virtualResumeSource = virtualTimeToSource(referenceVideo.value.currentTime)
    if (virtualResumeSource !== null) current.value = virtualResumeSource
  }
  clearVirtualPreviewTimer(); virtualPreviewGeneration += 1
  const generation = virtualPreviewGeneration
  const endpoint = virtualPreviewEndpoint()
  const window = previewSourceWindow.value
  const requestBody = { ...virtualPreviewRequestBody(), ...(window ? { preview_range: window } : {}) }
  virtualPreviewStatus.value = 'loading'; virtualPreviewProgress.value = 5; virtualPreviewStage.value = 'queued'; virtualPreviewMessage.value = '正在校验剪辑范围和恢复来源…'; virtualPreviewValidation.value = ''; virtualPreviewError.value = ''; virtualPreviewUrl.value = ''; virtualPreviewSegments.value = []
  const controller = new AbortController()
  try {
    let donorCuts: OutputCutsSnapshot | null = null
    if (virtualDonorKind.value === 'version') donorCuts = await fetchDonorCuts(controller.signal)
    if (generation !== virtualPreviewGeneration || disposed) return
    // 后端任务负责真正拼接和来源校验；保留本地计划用于展示、失败回退和旧后端兼容。
    const localDonor = virtualDonorKind.value === 'source'
      ? { kind: 'source' as const }
      : { kind: 'version' as const, version_id: virtualDonorVersionId.value, cuts: donorCuts }
    adoptVirtualSegments(buildVirtualPreviewSegments(saved.value.duration, virtualPreviewBaseCuts.value, virtualPreviewDraftCuts.value, localDonor))
    virtualPreviewProgress.value = 18
    if (generation !== virtualPreviewGeneration || disposed) return
    const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(requestBody), signal: controller.signal })
    const data = await response.json()
    if (!response.ok) throw new Error(data.error_detail || data.message || '启动虚拟试听失败')
    if (generation !== virtualPreviewGeneration || disposed) {
      const staleTask = data.task_id || data.id
      if (staleTask && !['done','completed','failed','cancelled'].includes(data.status)) {
        void fetch(`${endpoint}/${encodeURIComponent(staleTask)}/cancel`, {method:'POST'}).catch(()=>{})
      }
      return
    }
    virtualPreviewTaskId.value = data.task_id || data.id || null
    virtualPreviewStage.value = String(data.stage || 'queued')
    virtualPreviewMessage.value = String(data.message || '已提交虚拟试听任务')
    virtualPreviewValidation.value = String(data.source_validation?.message || '')
    if (data.status === 'done' || data.status === 'completed' || data.output_url || data.url || data.raw_url) {
      virtualResumeSource = current.value
      virtualPreviewCacheHit.value = !!data.cache_hit
      virtualPreviewFrameRate.value = Number(data.frame_rate) || null
      virtualPreviewStatus.value = 'ready'; virtualPreviewProgress.value = 100
      virtualPreviewUrl.value = data.output_url || data.url || data.raw_url || (data.task_id ? `${virtualPreviewEndpoint(data.task_id)}/raw` : '')
      if (Array.isArray(data.segments)) adoptVirtualSegments(data.segments)
      if (Number.isFinite(Number(data.duration))) virtualPreviewDuration.value = Number(data.duration)
      virtualPreviewSourceLabel.value = virtualDonorKind.value === 'source' ? '原视频恢复段' : (donorVersionOption()?.name || '其他成片恢复段')
      return
    }
    if (!virtualPreviewTaskId.value) throw new Error('虚拟试听任务未返回任务编号')
    await pollVirtualPreview(virtualPreviewTaskId.value, generation)
  } catch (error: any) {
    if (error?.name === 'AbortError' || generation !== virtualPreviewGeneration) return
    virtualPreviewStatus.value = 'error'; virtualPreviewError.value = error?.message || '虚拟试听生成失败'
  }
}
function cancelVirtualPreview() {
  const taskId = virtualPreviewTaskId.value
  clearVirtualPreviewTimer(); virtualPreviewGeneration += 1
  virtualPreviewStatus.value = 'idle'; virtualPreviewProgress.value = 0; virtualPreviewStage.value = ''; virtualPreviewMessage.value = ''; virtualPreviewValidation.value = ''; virtualPreviewTaskId.value = null; virtualPreviewUrl.value = ''
  if (taskId) void fetch(`${virtualPreviewEndpoint(taskId)}/cancel`, { method: 'POST' }).catch(() => {})
}
function donorChanged(value: string) {
  if (value === 'source') { virtualDonorKind.value = 'source'; virtualDonorVersionId.value = null }
  else { virtualDonorKind.value = 'version'; virtualDonorVersionId.value = value.replace(/^version:/, '') }
  resetVirtualPreview()
}
const percent = (time: number) => `${duration.value ? Math.max(0, Math.min(100, time / duration.value * 100)) : 0}%`
function baselineLabel(option: CutBaselineOption) {
  const suffix: string[] = []
  if (option.generated_at) {
    const date = new Date(option.generated_at)
    if (Number.isFinite(date.getTime())) suffix.push(date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }))
  }
  if (option.fresh === false) suffix.push('需更新')
  return `${option.name}${suffix.length ? ` · ${suffix.join(' · ')}` : ''}`
}
// 将原片时间投影到当前版本的成片时间轴。删除段的起止会收敛到同一个输出位置，
// 便于核对章节、字幕和局部替换时的时间映射。
function mappedOutputTime(time: number | null) {
  if (typeof time !== 'number' || !Number.isFinite(time)) return null
  let offset = 0
  for (const cut of effective.value) {
    if (time <= cut.start) break
    if (time < cut.end) return cut.start - offset
    offset += cut.end - cut.start
  }
  return Math.max(0, time - offset)
}
/** 将当前成片（删除后）时间映射回原视频时间轴，用于双向核对。 */
function mappedSourceTime(time: number | null) {
  if (targetPreviewUrl.value && !artifactMappingReady.value) return null
  return outputToSourceTime(referenceCuts.value, time, 'next')
}
function virtualTimeToSource(time: number) {
  const rows = virtualPreviewSegments.value
  const row = rows.find(segment => time >= segment.virtual_start - .000001 && time < segment.virtual_end - .000001)
  if (!row) return rows.length ? rows[rows.length - 1]!.source_end : null
  const ratio = row.virtual_end > row.virtual_start ? Math.max(0, Math.min(1, (time - row.virtual_start) / (row.virtual_end - row.virtual_start))) : 0
  return row.source_start + (row.source_end - row.source_start) * ratio
}
function sourceToVirtualTime(time: number) {
  const rows = virtualPreviewSegments.value
  const row = rows.find(segment => time >= segment.source_start - .000001 && time < segment.source_end - .000001)
  if (!row) return rows.length && Math.abs(time-rows[rows.length-1]!.source_end)<=.000001 ? rows[rows.length-1]!.virtual_end : null
  const ratio = row.source_end > row.source_start ? Math.max(0, Math.min(1, (time - row.source_start) / (row.source_end - row.source_start))) : 0
  return row.virtual_start + (row.virtual_end - row.virtual_start) * ratio
}
function syncReferenceToSource() {
  if (!holdSeekOrigin('source')) return
  const target = referenceVideo.value
  const output = sourceToOutputTime(referenceCuts.value, current.value)
  if (!target || output === null || !Number.isFinite(output) || Math.abs(target.currentTime - output) < .08) return
  const next = Math.max(0, Math.min(referenceDuration.value || output, output))
  linkedReferenceTime = next
  try { target.currentTime = next } catch { /* 媒体尚未就绪 */ }
}
function syncSourceToReference() {
  if (!holdSeekOrigin('reference')) return
  const source = video.value
  const input = outputToSourceTime(referenceCuts.value, referenceCurrent.value, 'next')
  if (!source || input === null || !Number.isFinite(input) || Math.abs(source.currentTime - input) < .08) return
  const next = Math.max(0, Math.min(duration.value || input, input))
  linkedSourceTime = next
  try { source.currentTime = next; current.value = source.currentTime } catch { /* 媒体尚未就绪 */ }
}
function onReferenceTimeUpdate() {
  const target = referenceVideo.value
  if (!target) return
  referenceCurrent.value = target.currentTime
  if (virtualPreviewStatus.value === 'ready' && virtualPreviewSegments.value.length) {
    const sourceTime = virtualTimeToSource(target.currentTime)
    if (sourceTime !== null && Number.isFinite(sourceTime)) current.value = sourceTime
    return
  }
  // 尚无正式成片时，唯一预览播放器就是原片；仍按当前删除清单试听，
  // 但只在播放中跳过，拖动/取标记不会被强行改写。
  if (video.value && !targetPreviewUrl.value && target && !target.paused && skipCuts.value && enabled.value) {
    const next = nextRetainedTime(target.currentTime, effective.value)
    if (next > target.currentTime + .001) { target.currentTime = next; current.value = next; return }
  }
  // 单视频主预览仍用原视频时间作为编辑坐标，进度条和删除清单因此保持
  // 完整源时长；成片文件本身只提供按已保存方案压缩后的 output 时间。
  const sourceTime = targetPreviewUrl.value
    ? artifactMappingReady.value ? outputToSourceTime(referenceCuts.value, target.currentTime, 'next') : null
    : target.currentTime
  if (sourceTime !== null && Number.isFinite(sourceTime)) current.value = sourceTime
  // 有正式成片时，跳过试听作用在“当前成片 + 本次新增删除”上。已生成
  // 文件自身的删除已经在文件里体现，不应把原片范围再映射/跳过一次。
  if (skipReferenceCutIfNeeded()) return
  if (!video.value) { previewClock.refresh(); return }
  if (!referenceSync.value) return
  // 源播放器正在被用户拖动时，目标播放器只接收定位，不得再把其
  // 播放时钟反向写回源播放器。显式切换到目标拖动由 beginSeek() 接管。
  if (activeSeekOrigin === 'source') return
  if (playbackOrigin === 'source' && !target.paused) {
    syncPlaybackClock()
    return
  }
  // 如果源播放器刚刚驱动了目标播放器，目标的普通 timeupdate 应被忽略；
  // 但目标暂停且明显偏离上次联动值时，说明用户直接拖动了目标播放器，
  // 需要把联动主导权切换过去（也兼容浏览器不派发 seeking 的实现）。
  if (seekSyncOrigin === 'source') {
    const drift = linkedReferenceTime === null ? 0 : Math.abs(target.currentTime - linkedReferenceTime)
    if (!(target.paused && drift > seekDriftTolerance)) return
    seekSyncOrigin = null
  }
  syncSourceToReference()
}
function onSourceTimeUpdate() {
  const source = video.value
  if (!source) return
  current.value = source.currentTime
  // 播放中源时间轴进入删除段时先跳过，再同步成片，避免成片短暂停在接缝。
  if (skipSourceCutIfNeeded()) {
    if (playbackOrigin === 'source') syncPlaybackClock()
    return
  }
  if (referenceSync.value) {
    // 目标播放器正在被用户拖动时，源播放器的播放时钟只会制造抖动；
    // 目标的显式 seek 事件会负责更新源播放器。
    if (activeSeekOrigin === 'reference') return
    if (playbackOrigin === 'source' && !source.paused) {
      syncPlaybackClock()
      return
    }
    if (seekSyncOrigin === 'reference') {
      const drift = linkedSourceTime === null ? 0 : Math.abs(source.currentTime - linkedSourceTime)
      if (source.paused && drift > seekDriftTolerance) seekSyncOrigin = null
    }
    if (seekSyncOrigin !== 'reference') syncReferenceToSource()
  }
}
/**
 * 实时试听的唯一跳过入口。只在媒体正在播放时执行，避免用户拖动到删除
 * 段内取起止点时立即被弹走；范围可以是未保存草稿，保存前就能听到预期结果。
 */
function skipSourceCutIfNeeded() {
  const source = video.value
  if (!source || !duration.value || source.paused || !skipCuts.value || !enabled.value) return false
  // 有当前成片参考时，联动试听的主预览是成片；源视频只保留完整原片时间轴
  // 供标记和恢复使用，避免关闭“跳过”后仍被源端自动跳过造成语义混乱。
  if (targetPreviewUrl.value && referenceSync.value) return false
  const now = Number(source.currentTime)
  const target = nextRetainedTime(now, effective.value)
  if (!Number.isFinite(now) || !Number.isFinite(target) || target <= now + 0.001) return false
  seek(target)
  return true
}
/** 当前成片文件只需要跳过本次编辑新增的删除段。恢复已生成文件中原本缺失的
 * 范围无法通过浏览器 seek 实现，需重新生成或从其它成片/原视频基准恢复。 */
function skipReferenceCutIfNeeded() {
  const target = referenceVideo.value
  const ranges = targetPreviewUrl.value ? previewTargetCuts.value : effective.value
  if (!target || target.paused || !skipCuts.value || virtualPreviewStatus.value === 'ready' || !ranges.length) return false
  if (!video.value && typeof target.playbackRate === 'number') {
    const previous = target.currentTime
    previewClock.refresh()
    return target.currentTime !== previous
  }
  const now = Number(target.currentTime)
  const next = nextRetainedTime(now, ranges)
  if (!Number.isFinite(now) || !Number.isFinite(next) || next <= now + 0.001) return false
  try { target.currentTime = next; referenceCurrent.value = next } catch { /* 媒体尚未可定位 */ }
  return true
}
function skipPrimaryPreviewCutIfNeeded() {
  const target = referenceVideo.value
  if (!target || target.paused || targetPreviewUrl.value || !skipCuts.value || !enabled.value) return false
  if (!video.value && typeof target.playbackRate === 'number') {
    const previous = target.currentTime; previewClock.refresh(); return target.currentTime !== previous
  }
  const next = nextRetainedTime(target.currentTime, effective.value)
  if (!Number.isFinite(next) || next <= target.currentTime + .001) return false
  target.currentTime = next; referenceCurrent.value = next; current.value = next
  return true
}
function onReferenceSeeking() {
  const target = referenceVideo.value
  if (!target) return
  referenceCurrent.value = target.currentTime
  if (!video.value) { previewClock.refresh(); return }
  if (!referenceSync.value) return
  if (activeSeekOrigin === 'source') return
  if (playbackOrigin === 'reference' && !target.paused) {
    syncPlaybackClock(true)
    return
  }
  if (seekSyncOrigin === 'source') {
    const drift = linkedReferenceTime === null ? 0 : Math.abs(target.currentTime - linkedReferenceTime)
    if (drift <= seekDriftTolerance) return
    seekSyncOrigin = null
  }
  syncSourceToReference()
}
function onSourceSeeking() {
  const source = video.value
  if (!source) return
  current.value = source.currentTime
  if (!referenceSync.value) return
  if (activeSeekOrigin === 'reference') return
  if (playbackOrigin === 'source' && !source.paused) {
    syncPlaybackClock(true)
    return
  }
  if (seekSyncOrigin === 'reference') {
    const drift = linkedSourceTime === null ? 0 : Math.abs(source.currentTime - linkedSourceTime)
    if (drift <= seekDriftTolerance) return
    seekSyncOrigin = null
  }
  syncReferenceToSource()
}
function mirrorReferencePlayback(origin: 'source' | 'reference', playing: boolean) {
  if (!referenceSync.value || syncingPlayback) return
  const other = origin === 'source' ? referenceVideo.value : video.value
  const master = origin === 'source' ? video.value : referenceVideo.value
  if (!other || !master) return
  if (!playing) {
    if (pendingPlaybackOrigin === origin) pendingPlaybackOrigin = null
    if (playbackRetryTimer) clearTimeout(playbackRetryTimer)
    playbackRetryTimer = undefined
    const wasOrigin = playbackOrigin === origin
    if (wasOrigin) clearPlaybackSync()
    issuePlaybackCommand(origin === 'source' ? 'reference' : 'source', 'pause')
    try { other.pause() } catch { /* 媒体已卸载 */ }
    return
  }
  const epoch = playbackEpoch
  pendingPlaybackOrigin = origin
  lastPlaybackOrigin = origin
  if (other.readyState < 2) {
    playbackLinkState.value = 'waiting'
    // 跟随媒体尚未能解码时暂停主端，避免两个播放器各自播放后产生数秒偏差。
    syncingPlayback = true
    suppressedPauseOrigin = origin
    issuePlaybackCommand(origin, 'pause')
    try { master.pause() } catch { /* 媒体已卸载 */ }
    syncingPlayback = false
    schedulePlaybackRetry()
    return
  }
  pendingPlaybackOrigin = null
  syncingPlayback = true
  issuePlaybackCommand(origin === 'source' ? 'reference' : 'source', 'play')
  let task: Promise<unknown>
  try { task = Promise.resolve(other.play()) } catch (error) { task = Promise.reject(error) }
  void task.catch(() => {
    if (epoch !== playbackEpoch) return
    pendingPlaybackOrigin = origin
    playbackLinkState.value = 'waiting'
    suppressedPauseOrigin = origin
    playbackCommands = []
    issuePlaybackCommand(origin, 'pause')
    try { master.pause() } catch { /* 媒体已卸载 */ }
    schedulePlaybackRetry()
  }).finally(() => {
    if (epoch !== playbackEpoch) return
    syncingPlayback = false
    if (!pendingPlaybackOrigin) startPlaybackSync(origin)
  })
}
/**
 * VideoControls 在调用 media.play()/pause() 前发出用户意图。它让我们能够
 * 区分用户在“联动校正中”的新操作与上一轮联动命令的迟到原生事件，避免
 * 高频点击时旧 Promise 重新把控制权抢回去。
 */
function onPlaybackIntent(origin: SeekOrigin) {
  if (!referenceSync.value) return
  if (syncingPlayback || pendingPlaybackOrigin || playbackCommands.length) cancelPendingPlaybackTransition()
}
function onSourcePlay() {
  applyLinkedAudioPolicy()
  if (consumePlaybackCommand('source', 'play')) return
  if (!syncingPlayback) {
    mirrorReferencePlayback('source', true)
    if (!pendingPlaybackOrigin) startPlaybackSync('source')
  }
}
function onSourcePause() {
  if (consumePlaybackCommand('source', 'pause')) return
  if (suppressedPauseOrigin === 'source') { suppressedPauseOrigin = null; return }
  if (!syncingPlayback && playbackOrigin === 'source') clearPlaybackSync()
  mirrorReferencePlayback('source', false)
}
function onSourceEnded() {
  if (!syncingPlayback) clearPlaybackSync()
  mirrorReferencePlayback('source', false)
}
function onReferencePlay() {
  // 剪后播放不能遗漏已完成但尚未加入清单的标记；不完整的标记保留待修正。
  if (skipCuts.value && hasPendingMarker.value && (!includePendingMarker() || validation.value.length)) {
    referenceVideo.value?.pause()
    message.warning('请先补全或清除当前标记，再预览剪辑效果')
    return
  }
  if (!video.value) { applyLinkedAudioPolicy(); previewClock.refresh(); return }
  applyLinkedAudioPolicy()
  if (consumePlaybackCommand('reference', 'play')) return
  if (!syncingPlayback) {
    mirrorReferencePlayback('reference', true)
    if (!pendingPlaybackOrigin) startPlaybackSync('reference')
  }
}
function onReferencePause() {
  if (!video.value) { previewClock.refresh(); return }
  if (consumePlaybackCommand('reference', 'pause')) return
  if (suppressedPauseOrigin === 'reference') { suppressedPauseOrigin = null; return }
  if (!syncingPlayback && playbackOrigin === 'reference') clearPlaybackSync()
  mirrorReferencePlayback('reference', false)
}
function onReferenceEnded() {
  if (!video.value) { previewClock.stop(); finishPreviewSeek(); return }
  if (!syncingPlayback) clearPlaybackSync()
  mirrorReferencePlayback('reference', false)
}
/** 联动开关重新打开时，播放事件未必会重新派发；主动收敛一次状态。 */
function reconcilePlaybackLink() {
  if (!referenceSync.value) return
  const source = video.value
  const target = referenceVideo.value
  if (!source || !target) return
  const sourcePlaying = !source.paused && !source.ended
  const targetPlaying = !target.paused && !target.ended
  if (sourcePlaying && targetPlaying) {
    const origin = playbackOrigin || lastPlaybackOrigin || 'source'
    if (!playbackBaseRates) playbackBaseRates = {
      source: source.playbackRate || 1,
      reference: target.playbackRate || 1,
    }
    playbackOrigin = origin
    playbackLinkState.value = 'syncing'
    syncPlaybackClock(true)
    return
  }
  if (sourcePlaying !== targetPlaying) {
    mirrorReferencePlayback(sourcePlaying ? 'source' : 'reference', true)
  }
}
function onReferenceMetadata() {
  const media = referenceVideo.value
  if (!media) return
  referenceDuration.value = Number.isFinite(media.duration) ? media.duration : (referenceCuts.value?.output_duration || 0)
  if (virtualResumeSource !== null) {
    let output = virtualPreviewStatus.value === 'ready' ? sourceToVirtualTime(virtualResumeSource)
      : targetPreviewUrl.value ? sourceToOutputTime(referenceCuts.value, virtualResumeSource) : virtualResumeSource
    if (output === null && virtualPreviewStatus.value === 'ready') {
      output = sourceToVirtualTime(nextRetainedTime(virtualResumeSource, effective.value)) ?? 0
    }
    virtualResumeSource = null
    const timeline = playbackTimeline.value
    if (output !== null && timeline?.valid && !timeline.empty) output = timeline.toMedia(timeline.fromMedia(output))
    if (output !== null && Number.isFinite(output)) media.currentTime = output
  }
  applyLinkedAudioPolicy()
  onReferenceTimeUpdate()
  previewClock.refresh()
  if (playbackOrigin === 'reference') syncPlaybackClock()
}
function onReferenceCanPlay() {
  onReferenceMetadata()
  retryPendingPlayback()
}
function onSourceCanPlay() {
  onTimeUpdate()
  retryPendingPlayback()
}
function applyLinkedAudioPolicy() {
  if (video.value) video.value.muted = audioSource.value !== 'source'
  // 尚无当前成片时，唯一预览会回退到原视频；默认仍应可听，只有明确选静音才静音。
  if (referenceVideo.value) referenceVideo.value.muted = targetPreviewUrl.value
    ? audioSource.value !== 'target'
    : saved.value?.source_url ? audioSource.value === 'none' : audioSource.value !== 'target'
}
function onSourceVolumeChange() {
  if (audioSource.value !== 'source' && video.value) video.value.muted = true
}
function onReferenceVolumeChange() {
  if ((targetPreviewUrl.value ? audioSource.value !== 'target' : saved.value?.source_url ? audioSource.value === 'none' : audioSource.value !== 'target') && referenceVideo.value) referenceVideo.value.muted = true
}
function isCurrent(uid: string, token: number) { return !disposed && uid === props.uid && token === generation }
function adopt(data: OutputCutsState, options: { preserveMarker?: boolean } = {}) {
  saved.value = data; enabled.value = data.enabled; ranges.value = cloneCutRanges(data.ranges)
  invalidRangeInputs.value = {}; rangeEditorGeneration.value += 1
  if ('artifact_cuts' in data) artifactContext.value = data
  baselineId.value = (data as any).baseline?.version_id || (data as any).cut_baseline?.version_id || (data as any).base_version_id || null
  if (!options.preserveMarker) resetMarker()
  markerActionError.value = ''; error.value = ''
}
async function requestForVersion(uid: string, versionId: string | null | undefined, signal: AbortSignal, body?: any, baselineVersionId?: string | null) {
  const params = new URLSearchParams()
  if (versionId) params.set('version_id', versionId)
  if (baselineVersionId) params.set('baseline_version_id', baselineVersionId)
  const query = params.toString() ? `?${params}` : ''
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/output-cuts${query}`, {
    method: body === undefined ? 'GET' : 'PUT', cache: 'no-store', signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) {
    const problem = new Error(data.message || '成片剪辑操作失败') as Error & { code?: string }
    problem.code = data.code; throw problem
  }
  return data as OutputCutsState
}
async function request(uid: string, signal: AbortSignal, body?: any) { return requestForVersion(uid, props.versionId, signal, body) }
function cancelRequest() { controller?.abort(); controller = null }
async function load() {
  if (disposed || loading.value || saving.value) return
  const uid = props.uid, token = generation, read = new AbortController()
  cancelRequest(); controller = read; loading.value = true; error.value = ''
  try {
    const data = await request(uid, AbortSignal.any([read.signal, AbortSignal.timeout(15000)]))
    if (isCurrent(uid, token) && controller === read) { adopt(data); void loadArtifactContext() }
  } catch (e: any) { if (isCurrent(uid, token) && !read.signal.aborted) error.value = e.name === 'TimeoutError' ? '读取剪辑方案超时，请重试。' : e.message }
  finally { if (isCurrent(uid, token) && controller === read) { controller = null; loading.value = false } }
}
function reload() {
  if (locked.value) return
  if (!dirty.value) { void load(); return }
  const uid = props.uid, token = generation
  confirmation = dialog.warning({ title: '放弃未保存的剪辑调整？', content: '重新读取已保存方案；不会生成成片或修改原视频。', positiveText: '读取已保存', negativeText: '继续编辑',
    onPositiveClick: () => { if (isCurrent(uid, token) && !locked.value) return load() } })
}
function restore() {
  if (!saved.value || locked.value) return
  const uid = props.uid, token = generation
  confirmation = dialog.warning({ title: '放弃未保存的剪辑调整？', content: '恢复本页打开时读取的方案，已有成片保持不变。', positiveText: '放弃调整', negativeText: '继续编辑',
    onPositiveClick: () => { if (isCurrent(uid, token) && saved.value && !locked.value) adopt(saved.value) } })
}
async function persist(rebind = false) {
  if (!saved.value || locked.value || unavailable.value) return
  if (!includePendingMarker() || validation.value.length || (!listDirty.value && !saved.value.source_changed)) return
  const uid = props.uid, token = generation, write = new AbortController()
  const body = { revision: saved.value.revision, source_fingerprint: saved.value.source_fingerprint,
    enabled: enabled.value, ranges: cloneCutRanges(ranges.value),
    ...(props.versionId ? { version_id: props.versionId, ...(baselineId.value ? { base_version_id: baselineId.value } : {}) } : {}),
    ...(rebind ? { rebind_source: true } : {}) }
  cancelRequest(); controller = write; saving.value = true; error.value = ''
  try {
    const data = await request(uid, AbortSignal.any([write.signal, AbortSignal.timeout(30000)]), body)
    if (isCurrent(uid, token) && controller === write) {
      adopt(data, { preserveMarker: true }); emit('saved', data); message.success('剪辑清单已保存；预览使用当前清单，下次制作也会使用此方案')
    }
  } catch (e: any) {
    if (isCurrent(uid, token) && !write.signal.aborted) {
      error.value = ['OUTPUT_CUTS_CONFLICT', 'OUTPUT_CUTS_SOURCE_CHANGED'].includes(e.code)
        ? `${e.message}。当前草稿已保留，请记录需要的范围后读取已保存方案，核对后再保存。`
        : e.name === 'TimeoutError' ? '保存超时，当前草稿已保留。请读取已保存方案确认结果后重试。' : e.message
    }
  } finally { if (isCurrent(uid, token) && controller === write) { controller = null; saving.value = false } }
}
function timelineRequestBody() {
  return { base_version_id: props.versionId, source_fingerprint: saved.value?.source_fingerprint,
    draft: { enabled: enabled.value, ranges: cloneCutRanges(ranges.value) },
    donor: virtualDonorKind.value === 'source' ? { kind: 'source' } : { kind: 'version', version_id: virtualDonorVersionId.value } }
}
function encodingSeconds(value: unknown) {
  if (value === null || value === undefined) return '待核对'
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds >= 0 ? formatCutTime(seconds) : '待核对'
}
function timelineEncodingText(encoding: any) {
  if (!encoding) return '编码方式将在生成前核对，不预先承诺直接复用。'
  const title = encoding.method === 'smart_cut' ? '接缝编码，复用主体' : '完整重新编码'
  const audio = encoding.audio_mode === 'none' ? '此版本无音轨' : '音频精确裁剪后编码一次'
  return `${title}\n预计视频重编码 ${encodingSeconds(encoding.encoded_seconds)} · 直接复用 ${encodingSeconds(encoding.copied_seconds)}\n${audio}${encoding.fallback_reason ? `\n需整片编码：${encoding.fallback_reason}` : ''}`
}
const currentTimelinePlan = computed(() => {
  const plan = timelinePlanPreview.value
  return plan && !hasPendingMarker.value && plan.draftSignature === draftSignature.value && plan.donorSignature === JSON.stringify(timelineRequestBody().donor)
    && plan.artifactHash === props.targetArtifactHash && plan.versionId === props.versionId && plan.uid === props.uid ? plan : null
})
async function createTimelineVersion() {
  if (locked.value || props.running || props.applyDisabled || unavailable.value || !props.versionId || !targetPreviewUrl.value || !artifactMappingReady.value) return
  if (!includePendingMarker() || validation.value.length || !hasCutChanges.value) return
  const uid = props.uid, token = generation, requestBody = timelineRequestBody()
  creatingTimeline.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/timeline/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(requestBody), signal: AbortSignal.timeout(30000) })
    const plan = await response.json()
    if (!response.ok) throw new Error(plan.message || '核对成片剪辑失败')
    if (!isCurrent(uid, token)) return
    timelinePlanPreview.value = { ...plan, uid: props.uid, draftSignature: draftSignature.value, donorSignature: JSON.stringify(requestBody.donor), artifactHash: props.targetArtifactHash, versionId: props.versionId }
    creatingTimeline.value = false
    const draftAtConfirm = draftSignature.value, markerAtConfirm = markerSignature.value, donorAtConfirm = JSON.stringify(requestBody.donor)
    confirmation = dialog.warning({ title: '基于当前成片生成剪辑版', content: `基准：${plan.base_name || props.versionName || '当前成片'}\n按当前清单删除 ${effective.value.length} 段，预计保留 ${formatCutTime(plan.output_duration ?? outputDuration.value)}。\n\n${timelineEncodingText(plan.encoding)}${plan.warnings?.length ? `\n\n需留意：\n${plan.warnings.join('\n')}` : ''}\n\n保留现有成片的字幕、配音和评论；恢复段使用所选来源。生成独立版本，旧成片保留。当前清单会固定在新版本中，无需先覆盖旧版方案。`, positiveText: '确认生成剪辑版', negativeText: '返回核对', maskClosable: false,
      onPositiveClick: async () => {
        if (!isCurrent(uid, token) || locked.value || props.running || props.applyDisabled) return false
        if (draftAtConfirm !== draftSignature.value || markerAtConfirm !== markerSignature.value || donorAtConfirm !== JSON.stringify(timelineRequestBody().donor)) { error.value = '删除清单、当前标记或恢复来源已变化，请重新检查并确认。'; return false }
        creatingTimeline.value = true
        try {
          const createdResponse = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/timeline`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...requestBody, fingerprint: plan.fingerprint }), signal: AbortSignal.timeout(30000) })
          const created = await createdResponse.json()
          if (!createdResponse.ok) throw new Error(created.message || '建立成片剪辑版本失败')
          if (!created.version?.id) throw new Error('未收到新剪辑版本，请刷新版本清单后重试。')
          if (isCurrent(uid, token)) { creatingTimeline.value = false; emit('timeline-created', created.version); message.success('剪辑版本已建立，正在准备生成') }
        } catch (e: any) { if (isCurrent(uid, token)) error.value = e?.name === 'TimeoutError' ? '建立剪辑版本超时，请读取版本清单确认结果。' : e.message }
        finally { if (isCurrent(uid, token)) creatingTimeline.value = false }
      } })
  } catch (e: any) { if (isCurrent(uid, token)) error.value = e?.name === 'TimeoutError' ? '核对成片剪辑超时，请重试。' : e.message }
  finally { if (isCurrent(uid, token)) creatingTimeline.value = false }
}
function save() {
  if (locked.value || unavailable.value || !saved.value) return
  if (!includePendingMarker() || validation.value.length) return
  if (!saved.value.source_changed) return persist()
  const uid = props.uid, token = generation
  confirmation = dialog.warning({ title: '将剪辑范围应用到当前原视频？',
    content: '原视频已更换，旧范围暂不生效。请先逐段核对当前原视频；确认后，这些时间将按当前原视频重新绑定，并在下次生成时使用。',
    positiveText: '已核对，绑定并保存', negativeText: '返回核对', onPositiveClick: () => { if (isCurrent(uid, token)) return persist(true) } })
}
function seek(seconds: number) {
  if (!duration.value) return
  if (targetPreviewUrl.value && !artifactMappingReady.value) { message.warning(artifactMappingMessage.value); return }
  const sourceTime = Math.max(0, Math.min(duration.value, seconds))
  current.value = sourceTime
  if (video.value) {
    video.value.currentTime = sourceTime
    return
  }
  // 原视频参考未展开时，仍可在完整源时间轴上定位当前成片；删除接缝处
  // 由统一映射收敛到可播放的 output 时间。
  if (referenceVideo.value) {
    let output = !targetPreviewUrl.value
      ? sourceTime
      : virtualPreviewStatus.value === 'ready' && virtualPreviewSegments.value.length
      ? sourceToVirtualTime(sourceTime)
      : sourceToOutputTime(referenceCuts.value, sourceTime)
    if (output === null && virtualPreviewStatus.value === 'ready' && skipCuts.value) {
      const next = nextRetainedTime(sourceTime, effective.value)
      if (next > sourceTime) { current.value = next; output = sourceToVirtualTime(next) }
    }
    if (output === null && virtualPreviewStatus.value === 'ready') {
      // 短预览不含窗口外的画面；回到完整媒体后再按源位置恢复，不能只移动游标。
      referenceVideo.value.pause()
      resetVirtualPreview()
      virtualResumeSource = sourceTime
      current.value = sourceTime
      message.info('已超出短预览范围，返回即时预览并保留定位。')
      return
    }
    if (output !== null && Number.isFinite(output)) {
      const timeline = playbackTimeline.value
      if (timeline?.valid && !timeline.empty) output = timeline.toMedia(timeline.fromMedia(output))
      referenceVideo.value.currentTime = output
      referenceCurrent.value = output
      previewClock.refresh()
    }
  }
}
function setMarkerValue(key: 'start' | 'end', value: number | null) {
  if (key === 'start') { markStart.value = value; markStartText.value = value === null ? '' : formatCutTime(value) }
  else { markEnd.value = value; markEndText.value = value === null ? '' : formatCutTime(value) }
  markerTextError.value = ''
}
function resetMarker(start = 0) {
  setMarkerValue('start', start); setMarkerValue('end', null); markReason.value = ''
  markerActionError.value = ''; markerBaselineSignature.value = markerSignature.value
}
function clearMarker() { if (!locked.value) resetMarker() }
function includePendingMarker() {
  if (!hasPendingMarker.value) return true
  if (locked.value) return false
  if (!pendingMarkerReady.value) {
    markerActionError.value = ranges.value.length >= 200 ? pendingMarkerDescription.value
      : `${markerValidation.value[0] || '请补全有效的起点和终点'}。标记已保留，请修正或清除后继续。`
    return false
  }
  return addRange()
}
function setPreviewCuts(value: boolean) {
  if (!value) { skipCuts.value = false; return true }
  if (locked.value || unavailable.value || targetPreviewUrl.value && !artifactMappingReady.value) return false
  if (!includePendingMarker() || validation.value.length) return false
  skipCuts.value = true
  return true
}
function previewDraft() {
  const start = pendingMarkerReady.value ? parseCutTime(markStartText.value) : null
  if (!setPreviewCuts(true)) return false
  if (start !== null) seek(Math.max(0, start - 2))
  return true
}
async function changeBaseline(id: string | null, force = false) {
  if (locked.value || id === baselineId.value) return
  if (!force && dirty.value) {
    const uid = props.uid, token = generation
    confirmation = dialog.warning({ title: '替换未保存的剪辑调整？', content: '载入另一成片的删除范围会覆盖当前草稿；已保存的成片和当前版本不会立即改变。', positiveText: '载入基准范围', negativeText: '继续编辑', onPositiveClick: () => { if (isCurrent(uid, token)) return changeBaseline(id, true) } })
    return
  }
  const uid = props.uid, token = generation, read = new AbortController()
  cancelRequest(); controller = read
  loading.value = true; error.value = ''
  try {
    let data = await requestForVersion(uid, props.versionId, AbortSignal.any([read.signal, AbortSignal.timeout(15000)]), undefined, id)
    if (!id) data = await requestForVersion(uid, props.versionId, AbortSignal.any([read.signal, AbortSignal.timeout(15000)]))
    else {
      const returnedBaseline = (data as any).baseline?.version_id || (data as any).cut_baseline?.version_id
      if (returnedBaseline !== id && props.versionId && props.versionId !== id) data = await requestForVersion(uid, id, AbortSignal.any([read.signal, AbortSignal.timeout(15000)]))
    }
    if (!isCurrent(uid, token)) return
    ranges.value = cloneCutRanges(data.ranges || [])
    invalidRangeInputs.value = {}; rangeEditorGeneration.value += 1
    enabled.value = data.enabled !== false && ranges.value.length > 0
    baselineId.value = id
    resetMarker()
    message.info(id ? `已载入「${props.baselineOptions?.find(option => option.id === id)?.name || id}」的剪辑范围，可继续调整后保存` : '已恢复当前版本自己的剪辑范围，可继续调整后保存')
  } catch (e: any) {
    if (isCurrent(uid, token) && e?.name !== 'AbortError') error.value = e?.name === 'TimeoutError' ? '读取基准剪辑超时，请重试。' : e?.message || '读取基准剪辑失败'
  } finally { if (isCurrent(uid, token) && controller === read) { controller = null; loading.value = false } }
}
function commitMarker(key: 'start' | 'end') {
  const text = key === 'start' ? markStartText.value : markEndText.value
  if (!text.trim()) { setMarkerValue(key, null); return }
  const value = parseCutTime(text)
  if (value === null) { markerTextError.value = `请填写有效的${key === 'start' ? '起点' : '终点'}时间，例如 31:40 或 00:31:40.000。`; return }
  if (duration.value !== null && value > duration.value) { markerTextError.value = '标记时间不能超过原视频时长。'; return }
  setMarkerValue(key, value)
}
function onTimeUpdate() {
  onSourceTimeUpdate()
}
function onSourceSeekStart() { beginSeek('source'); onSourceSeeking() }
function onSourceSeek() { if (!activeSeekOrigin) beginSeek('source'); onSourceSeeking() }
function onSourceSeekEnd() { endSeek('source') }
function onReferenceSeekStart() { beginSeek('reference'); onReferenceSeeking() }
function onReferenceSeek(value?: number) {
  if (!video.value && typeof value === 'number') {
    referenceCurrent.value = value
    const source = virtualPreviewStatus.value === 'ready' ? virtualTimeToSource(value)
      : targetPreviewUrl.value ? outputToSourceTime(referenceCuts.value, value, 'next') : value
    if (source !== null) current.value = source
    previewClock.refresh(); return
  }
  if (!activeSeekOrigin) beginSeek('reference'); onReferenceSeeking()
}
function onReferenceSeekEnd() { endSeek('reference') }
function seekTrack(event: MouseEvent) {
  if (!duration.value) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  if (rect.width) seek((event.clientX - rect.left) / rect.width * duration.value)
}
function seekKey(event: KeyboardEvent) {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    seek(event.key === 'Home' ? 0 : event.key === 'End' ? duration.value || 0 : current.value + (event.key === 'ArrowRight' ? 1 : -1))
  }
}
function addRange() {
  if (locked.value || markerValidation.value.length || ranges.value.length >= 200) return false
  const start = parseCutTime(markStartText.value), end = parseCutTime(markEndText.value)
  if (start === null || end === null) return false
  ranges.value.push({ id: `cut-${crypto.randomUUID()}`, start, end, reason: markReason.value.trim(), enabled: true })
  enabled.value = true; resetMarker(end)
  return true
}
function setRangeInputInvalid(id: string, key: 'start' | 'end', invalid: boolean) {
  invalidRangeInputs.value = { ...invalidRangeInputs.value, [`${id}:${key}`]: invalid }
}
function removeRange(id: string) {
  if (locked.value) return
  ranges.value = ranges.value.filter(row => row.id !== id)
  delete invalidRangeInputs.value[`${id}:start`]; delete invalidRangeInputs.value[`${id}:end`]
}
function restoreRange(row: OutputCutRange) { if (!locked.value) row.enabled = true }
function disableRange(row: OutputCutRange) { if (!locked.value) row.enabled = false }
function updateTime(row: OutputCutRange, key: 'start' | 'end', value: number | null) { if (!locked.value) row[key] = value }
watch(dirty, value => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(markerSignature, () => { markerActionError.value = '' })
watch(() => saving.value || creatingTimeline.value, value => emit('busy-change', value), { immediate: true, flush: 'sync' })
watch(() => [props.uid, props.versionId], () => {
  generation++; contextRead?.abort(); artifactContextGeneration++; artifactContext.value = null; cancelRequest(); clearSeekSync(); clearPlaybackSync(); confirmation?.destroy(); saved.value = null; ranges.value = []; enabled.value = false
  invalidRangeInputs.value = {}
  loading.value = false; saving.value = false; error.value = ''; current.value = 0; resetMarker(); skipCuts.value = false; audioSource.value = 'target'; referenceSync.value = true; virtualDonorKind.value = 'source'; virtualDonorVersionId.value = null; resetVirtualPreview({ preservePosition: false })
  void load()
})
watch([audioSource, video, referenceVideo], applyLinkedAudioPolicy, { flush: 'post' })
watch(referenceSync, value => {
  if (!value) { clearSeekSync(); clearPlaybackSync(); return }
  reconcilePlaybackLink()
})
watch(skipCuts, () => {
  // 勾选项可以在播放中切换：当前成片是主预览时，立即检查成片播放头，
  // 不要等下一次 timeupdate 才跳过新增删除段；原视频模式仍沿用同一入口。
  skipReferenceCutIfNeeded()
  skipSourceCutIfNeeded()
  skipPrimaryPreviewCutIfNeeded()
  if (!skipCuts.value) resetVirtualPreview()
})
watch(effective, () => {
  skipSourceCutIfNeeded()
  skipPrimaryPreviewCutIfNeeded()
  if (virtualPreviewStatus.value === 'ready' || virtualPreviewStatus.value === 'loading') resetVirtualPreview()
}, { deep: true, flush: 'sync' })
watch([referenceVideo, playbackTimeline], () => { finishPreviewSeek(); previewClock.refresh() }, { flush: 'post' })
watch(targetPreviewUrl, () => {
  artifactContext.value = null
  referenceCurrent.value = 0
  referenceDuration.value = 0
  referenceMediaError.value = ''
  resetVirtualPreview()
  void loadArtifactContext()
})
onMounted(load)
onBeforeUnmount(() => emit('open-change', false))
onBeforeUnmount(() => { disposed = true; generation++; previewClock.destroy(); contextRead?.abort(); artifactContextGeneration++; cancelRequest(); clearSeekSync(); clearPlaybackSync(); cancelVirtualPreview(); confirmation?.destroy(); emit('dirty-change', false); emit('busy-change', false) })
</script>

<template>
  <details class="output-cuts" @toggle="onEditorToggle">
    <summary><span class="cuts-heading"><strong>成片剪辑</strong><span>{{ summary }}</span></span><n-tag v-if="hasPendingMarker" type="warning" size="small">片段待加入</n-tag><n-tag v-if="listDirty" type="warning" size="small">清单未保存</n-tag><span v-if="!dirty" class="scope-label">{{ versionId ? (saved?.scope === 'version' ? '当前版本独立方案' : '当前版本 · 项目默认') : '项目级方案' }}</span></summary>
    <div class="cuts-body">
      <p class="cuts-description"><strong>{{ versionName || (versionId ? '当前成片版本' : '项目成片') }}</strong> · 按原片时间标记删除或恢复，预览后另存为新成片。</p>
      <n-alert v-if="error" type="error" :show-icon="false">{{ error }} <n-button v-if="!saved" size="tiny" @click="load">重试</n-button></n-alert>
      <p v-if="loading" class="muted" role="status">正在读取剪辑方案…</p>
      <template v-if="saved">
        <div class="cuts-toolbar"><label class="enable-control">启用成片剪辑<n-switch v-model:value="enabled" :disabled="locked || unavailable" aria-label="启用成片剪辑" /></label><span class="muted">预览使用当前清单，无需先保存；保存后供下次制作使用</span></div>
        <details v-if="versionId" class="cuts-range-source"><summary>载入其他版本的删除范围</summary><div class="cuts-baseline" role="group" aria-label="载入旧成片的删除范围"><div><strong>删除范围来源</strong><small>仅载入清单，编辑对象仍为当前版本</small></div><n-select :value="baselineId" aria-label="删除范围来源" :options="[{label:'当前版本独立时间轴',value:null}, ...(baselineOptions || []).map(option => ({label:baselineLabel(option),value:option.id}))]" :disabled="locked || !baselineOptions?.length" placeholder="载入旧成片范围（可选）" clearable @update:value="changeBaseline" /><n-button size="tiny" quaternary :disabled="locked || !baselineId" title="保留当前范围，但不再关联旧成片" @click="changeBaseline(null)">取消范围来源</n-button></div></details>
        <n-alert v-if="baselineId" type="info" :show-icon="false" class="baseline-note">已载入“{{ selectedBaselineName }}”的删除范围；视频仍按原视频时间轴预览，保存后只用于当前编辑对象。</n-alert>
        <n-alert v-if="running" type="info" :show-icon="false">当前制作使用启动时的方案；这里仍可编辑，保存后供下次生成使用。</n-alert>
        <n-alert v-for="warning in saved.warnings" :key="warning" type="warning" :show-icon="false">{{ warning }}</n-alert>
        <template v-if="!unavailable">
          <section v-if="targetPreviewUrl || saved.source_url" class="target-reference" aria-label="成片试听">
            <header class="target-reference-heading">
              <div><strong>{{ virtualPreviewStatus === 'ready' ? '连贯剪辑预览' : skipCuts ? '剪后效果预览' : targetPreviewUrl ? '当前成片原样' : '原片预览' }}</strong><small>{{ virtualPreviewStatus === 'ready' ? previewWindowLabel : skipCuts ? '进度、快进快退和逐帧统一使用剪后时间，自动跨过计划删除段。' : '查看删除前的画面；下方原片时间轴仍可标记范围。' }}</small></div>
              <span class="target-reference-badges"><n-tag size="small" type="info" :bordered="false">{{ versionName || '当前版本' }}</n-tag><n-tag v-if="targetArtifactFresh === false" size="small" type="warning" :bordered="false" title="当前工作稿与此成片不同；不影响对现有成片剪辑">工作稿有改动</n-tag><n-tag v-if="virtualPreviewStatus === 'ready'" size="small" type="success" :bordered="false">虚拟试听</n-tag></span>
            </header>
            <div class="target-reference-toolbar">
              <n-checkbox :checked="skipCuts" :disabled="locked || (!skipCuts && (pendingMarkerBlocked || validation.length>0 || !!(targetPreviewUrl && !artifactMappingReady)))" title="按当前清单跳过删除段，无需先保存；完整的待加入标记会一并收录。取消后可查看删除前的画面。" @update:checked="setPreviewCuts">预览剪辑效果</n-checkbox>
              <small class="preview-draft-state">{{ listDirty ? '当前草稿 · 尚未保存' : '当前清单' }} · 无需先保存</small>
              <n-tag size="small" :type="virtualPreviewStatus === 'ready' ? 'success' : 'default'" :bordered="false">{{ virtualPreviewStatus === 'ready' ? (virtualPreviewCacheHit ? '已复用预览缓存' : '连贯预览已就绪') : '即时预览' }}</n-tag>
              <n-button v-if="virtualPreviewStatus === 'ready'" size="small" quaternary @click="resetVirtualPreview">返回即时预览</n-button>
            </div>
            <p v-if="hasPendingMarker" class="preview-pending-marker" role="status">{{ pendingMarkerReady ? '下方还有一段完整标记；开启剪后预览或播放时会自动加入清单。' : '下方标记尚未完成，请补全或清除后再预览剪辑效果。' }}</p>
            <div v-if="skipCuts && targetPreviewUrl && hasCutChanges" class="preview-build-toolbar" role="group" aria-label="准备连贯剪辑预览">
              <div><strong>检查接缝</strong><small>即时跳转可能短暂缓冲；生成预览可连续检查画面与声音。</small></div>
              <n-select :value="previewScope" :options="previewScopeOptions" size="small" aria-label="连贯预览范围" :disabled="virtualPreviewBusy" @update:value="previewScope=$event" />
              <n-button v-if="virtualPreviewStatus === 'loading'" size="small" quaternary @click="cancelVirtualPreview">取消预览任务</n-button>
              <n-button v-else size="small" type="primary" secondary :disabled="pendingMarkerBlocked || validation.length>0 || !artifactMappingReady" @click="startVirtualPreview">{{ virtualPreviewStatus === 'ready' ? '重新准备预览' : '生成连贯预览' }}</n-button>
              <small class="preview-window-label">{{ previewWindowLabel }} · 只生成预览，不修改正式成片</small>
            </div>
            <div v-if="skipCuts && hasRestoredIntervals" class="restore-source"><span>恢复片段取自</span><n-select :value="virtualDonorKind === 'source' ? 'source' : `version:${virtualDonorVersionId}`" size="small" :options="virtualDonorOptions" aria-label="恢复片段试听来源" :disabled="virtualPreviewBusy" @update:value="donorChanged" /></div>
            <div v-if="targetPreviewUrl" class="cut-snapshot-summary"><span>已生成成片删除 <strong>{{ referenceCuts ? referenceCuts.ranges.length : '—' }}</strong> 段</span><span>当前清单删除 <strong>{{ effective.length }}</strong> 段</span><n-tag v-if="artifactMappingReady && hasCutChanges" size="tiny" type="warning">剪辑待应用</n-tag><n-tag v-else-if="artifactMappingReady" size="tiny" type="success">剪辑一致</n-tag></div>
            <n-alert v-if="targetPreviewUrl && !artifactMappingReady" type="warning" :show-icon="false">{{ artifactMappingMessage }} <n-button size="tiny" text :loading="artifactContextLoading" @click="loadArtifactContext">重新核对来源</n-button></n-alert>
            <n-progress v-if="virtualPreviewStatus === 'loading'" type="line" :percentage="Math.round(virtualPreviewProgress)" :show-indicator="true" status="info" />
            <small v-if="virtualPreviewStatus === 'loading'" class="virtual-preview-progress-note" role="status">{{ virtualPreviewMessage }} · {{ Math.round(virtualPreviewProgress) }}%</small>
            <n-alert v-if="virtualPreviewStatus === 'error'" type="error" :show-icon="false">{{ virtualPreviewError }} 当前成片预览仍可继续使用；请核对删除/恢复来源后重试。</n-alert>
            <div class="target-reference-player">
              <video v-release-media ref="referenceVideo" :key="virtualPreviewUrl || targetPreviewUrl || saved.source_url || ''" :src="virtualPreviewUrl || targetPreviewUrl || saved.source_url || ''" preload="metadata" :aria-label="virtualPreviewStatus === 'ready' ? '虚拟试听预览' : targetPreviewUrl ? '当前成片预览' : '原片试听预览'"
                :muted="targetPreviewUrl ? audioSource !== 'target' : saved?.source_url ? audioSource === 'none' : audioSource !== 'target'" @seeking="onReferenceSeeking" @seeked="onReferenceSeeked" @ratechange="previewClock.refresh()" @timeupdate="onReferenceTimeUpdate" @loadedmetadata="onReferenceMetadata" @canplay="onReferenceCanPlay" @play="onReferencePlay" @pause="onReferencePause" @ended="onReferenceEnded" @volumechange="onReferenceVolumeChange" @error="referenceMediaError=targetPreviewUrl ? '当前成片暂时无法播放，请确认正式产物仍在项目目录。' : '原片暂时无法播放，请检查源文件后重新读取。'" />
              <canvas ref="seekFreezeCanvas" v-show="previewTransitioning" class="cut-seek-freeze" aria-hidden="true" />
              <span v-if="previewTransitioning" class="cut-seek-status" role="status">正在定位保留画面…</span>
              <VideoControls :media="referenceVideo" :duration="virtualPreviewDuration || referenceDuration || saved.output_duration || duration || 0" :frame-rate="previewFrameRate"
                :timeline="playbackTimeline"
                @seek-start="onReferenceSeekStart" @seek="onReferenceSeek" @seek-end="onReferenceSeekEnd" @playback-intent="onPlaybackIntent('reference')" />
            </div>
            <div class="preview-clock-row"><span>{{ skipCuts ? '剪后预览' : '媒体位置' }} <strong>{{ formatCutTime(previewPosition) }}</strong> / {{ formatCutTime(previewPlaybackDuration) }}</span><span>对应原片 <strong>{{ formatCutTime(current) }}</strong></span><span class="seam-actions"><n-button size="tiny" quaternary :disabled="!effective.length" @click="previewSeam(-1)">上一接缝</n-button><n-button size="tiny" quaternary :disabled="!effective.length" @click="previewSeam(1)">下一接缝</n-button></span></div>
            <p v-if="referenceMediaError" class="media-error" role="status">{{ referenceMediaError }}</p>
          </section>
          <!-- 原片参考播放器已移除；下方时间轴仍使用统一源时间坐标。 -->
          <details v-if="baselinePreviewUrl" class="baseline-reference">
            <summary>恢复来源校验信息<small>旧成片不会再打开第二个播放器</small></summary>
            <small class="muted">已选择「{{ selectedBaselineName }}」作为可选恢复来源。生成虚拟试听时，后端会校验它与当前原视频的指纹、时长和可用时间段；不满足条件会拒绝拼接。</small>
          </details>
          <div class="cuts-timeline-heading"><strong>原片完整时间轴</strong><div class="preview-mode-summary"><span><i class="legend-keep" />保留<i class="legend-cut" />删除</span><small>用于标记和恢复片段</small></div></div>
          <n-alert v-if="skipCuts && dirty && targetPreviewUrl && referenceSync" type="warning" :show-icon="false" class="preview-draft-note">
            {{ previewModeNote }}
          </n-alert>
          <p v-else-if="effective.length || skipCuts" class="preview-mode-note muted">{{ previewModeNote }}</p>
          <div class="cuts-track" role="slider" tabindex="0" aria-label="原视频剪辑时间轴" :aria-valuemin="0" :aria-valuemax="duration || 0" :aria-valuenow="current" :aria-valuetext="formatCutTime(current)" @click="seekTrack" @keydown="seekKey">
            <span v-for="(range,index) in effective" :key="index" class="cut-region" :style="{left:percent(range.start),width:percent(range.end-range.start)}" :title="`删除 ${formatCutTime(range.start)}–${formatCutTime(range.end)}`" />
            <span class="playhead" :style="{left:percent(current)}" />
          </div>
          <div class="timeline-endpoints"><span>原片 00:00:00</span><span>{{ formatCutTime(duration) }}</span></div>
          <div class="marker-panel">
            <div class="marker-title"><strong>标记要删除的片段</strong><n-tag v-if="hasPendingMarker" size="small" type="warning" :bordered="false">待加入清单</n-tag><span class="muted">当前位置 {{ formatCutTime(current) }}</span></div>
            <div class="marker-fields">
              <label>起点<n-input v-model:value="markStartText" :disabled="locked" autocomplete="off" @blur="commitMarker('start')" @keyup.enter="commitMarker('start')" :input-props="{'aria-label':'新删除片段起点'}" /></label>
              <n-button size="small" :disabled="locked" @click="setMarkerValue('start',current)">当前位置设为起点</n-button>
              <label>终点<n-input v-model:value="markEndText" :disabled="locked" autocomplete="off" placeholder="例如 00:31:44.000" @blur="commitMarker('end')" @keyup.enter="commitMarker('end')" :input-props="{'aria-label':'新删除片段终点'}" /></label>
              <n-button size="small" :disabled="locked" @click="setMarkerValue('end',current)">当前位置设为终点</n-button>
            </div>
            <p v-if="hasPendingMarker" class="marker-pending-note" role="status">{{ pendingMarkerDescription }}</p>
            <n-alert v-if="markerTextError || markerActionError" type="warning" :show-icon="false" role="status">{{ markerActionError || markerTextError }}</n-alert>
            <div class="marker-actions"><n-input v-model:value="markReason" :disabled="locked" maxlength="500" placeholder="删除原因（可选，例如广告片段）" :input-props="{'aria-label':'新删除片段原因'}" /><n-button type="primary" secondary size="small" :disabled="locked || pendingMarkerBlocked || validation.length>0 || !!(targetPreviewUrl && !artifactMappingReady)" @click="previewDraft">{{ hasPendingMarker ? '加入并预览' : '预览当前清单' }}</n-button><n-button size="small" :disabled="locked || markerValidation.length>0 || ranges.length>=200" @click="addRange">添加删除片段</n-button><n-button v-if="hasPendingMarker" size="small" quaternary :disabled="locked" @click="clearMarker">清除标记</n-button></div>
            <small class="muted">起止点可输入秒数或“时:分:秒”，支持毫秒；按 Enter 或离开输入框确认。也可逐帧微调后取当前位置。预览或保存会自动收录完整标记，无需先点“添加删除片段”。</small>
          </div>
          <div class="ranges-heading"><strong>删除/恢复清单 <small>{{ ranges.length }} 段{{ restoredRanges.length ? ` · 已恢复 ${restoredRanges.length} 段` : '' }}</small></strong><span class="muted">按原片时间记录；禁用的片段会在虚拟试听中从所选来源恢复</span></div>
          <p v-if="!ranges.length" class="range-empty">尚未添加片段。播放原视频，标记起点与终点后添加。</p>
          <div v-for="(range,index) in ranges" :key="`${rangeEditorGeneration}:${range.id}`" class="range-row" :class="{inactive:!range.enabled}">
            <div class="range-heading"><n-checkbox v-model:checked="range.enabled" :disabled="locked">片段 {{ index+1 }}</n-checkbox><n-tag size="tiny" :type="range.enabled ? 'error' : 'success'">{{ range.enabled ? '删除中' : '已恢复' }}</n-tag><span class="range-duration">{{ range.start!==null && range.end!==null && range.end>range.start ? formatCutTime(range.end-range.start) : '时间待完善' }}</span><span v-if="range.enabled && mappedOutputTime(range.start)!==null" class="range-mapping" title="该删除范围在成片时间轴中收敛为一个定位点">成片定位 {{ formatCutTime(mappedOutputTime(range.start)) }}</span><n-button v-if="range.enabled" size="tiny" quaternary :disabled="locked" @click="disableRange(range)">恢复此段</n-button><n-button v-else size="tiny" quaternary :disabled="locked" @click="restoreRange(range)">重新删除</n-button><n-button size="tiny" quaternary :disabled="locked" :aria-label="`移除片段${index+1}`" @click="removeRange(range.id)">移除</n-button></div>
            <div class="range-fields"><label>起点<TimecodeInput :value="range.start" :max="duration ?? undefined" :disabled="locked" :aria-label="`片段${index+1}起点`" @update:value="updateTime(range,'start',$event)" @invalid-change="setRangeInputInvalid(range.id,'start',$event)" /></label><span class="range-arrow">→</span><label>终点<TimecodeInput :value="range.end" :max="duration ?? undefined" :disabled="locked" :aria-label="`片段${index+1}终点`" @update:value="updateTime(range,'end',$event)" @invalid-change="setRangeInputInvalid(range.id,'end',$event)" /></label><n-button size="tiny" :disabled="range.start===null || invalidRangeInputs[`${range.id}:start`]" @click="seek(range.start || 0)">定位</n-button><n-input v-model:value="range.reason" :disabled="locked" maxlength="500" placeholder="删除原因（可选）" :input-props="{'aria-label':`片段${index+1}原因`}" /></div>
          </div>
          <n-alert v-if="validation.length" type="warning" :show-icon="false"><div v-for="issue in validation" :key="issue">{{ issue }}</div></n-alert>
          <div class="cuts-total"><span>原片 <strong>{{ formatCutTime(duration) }}</strong></span><span class="removed-total">删除 <strong>{{ formatCutTime(removedDuration) }}</strong></span><span>成片预计 <strong>{{ formatCutTime(outputDuration) }}</strong></span></div>
        </template>
        <p v-else class="muted">请先获取原视频。时长就绪后即可标记删除片段。</p>
        <section v-if="currentTimelinePlan?.encoding" class="timeline-encoding-plan" aria-label="剪辑生成方式预检">
          <header><strong>本次剪辑的生成方式</strong><n-tag size="small" :type="currentTimelinePlan.encoding.method === 'smart_cut' ? 'success' : 'info'" :bordered="false">{{ currentTimelinePlan.encoding.method === 'smart_cut' ? '接缝编码，复用主体' : '完整重新编码' }}</n-tag></header>
          <div class="timeline-encoding-metrics"><span>预计视频重编码 <strong>{{ encodingSeconds(currentTimelinePlan.encoding.encoded_seconds) }}</strong></span><span>直接复用 <strong>{{ encodingSeconds(currentTimelinePlan.encoding.copied_seconds) }}</strong></span></div>
          <p>{{ currentTimelinePlan.encoding.audio_mode === 'none' ? '此版本无音轨。' : '音频精确裁剪后编码一次。' }}<span v-if="currentTimelinePlan.encoding.method === 'smart_cut'"> 复用能力取决于来源格式及关键帧，生成时还会核对接缝。</span></p>
          <n-alert v-if="currentTimelinePlan.encoding.fallback_reason" type="info" :show-icon="false">需整片编码：{{ currentTimelinePlan.encoding.fallback_reason }}</n-alert>
          <n-alert v-for="warning in currentTimelinePlan.warnings || []" :key="warning" type="warning" :show-icon="false">{{ warning }}</n-alert>
        </section>
        <div v-if="targetPreviewUrl && versionId" class="cut-apply-action"><div><strong>将当前清单应用到这份成片</strong><small>保留成片已有字幕、配音和评论，仅删除或恢复清单中的片段。新建独立剪辑版，旧版保留。</small></div><n-button type="primary" size="small" :loading="creatingTimeline" :disabled="locked || !!running || !!applyDisabled || unavailable || pendingMarkerBlocked || validation.length>0 || !artifactMappingReady || (!hasCutChanges && !pendingMarkerReady)" @click="createTimelineVersion">基于当前成片生成剪辑版</n-button></div>
        <div class="cuts-footer"><n-button secondary size="small" :loading="saving" :disabled="locked || unavailable || pendingMarkerBlocked || validation.length>0 || (!dirty && !saved.source_changed)" @click="save">{{ hasPendingMarker ? '加入片段并保存清单' : '仅保存清单，稍后生成' }}</n-button><n-button size="small" :disabled="locked || !dirty" @click="restore">放弃调整</n-button><n-button size="small" quaternary :disabled="locked" @click="reload">读取已保存</n-button></div>
        <small class="cuts-note">上方“生成剪辑版”直接按此清单处理现有成片；“仅保存清单”供之后的完整制作使用。预览与保存清单均不会改写已有视频文件。</small>
      </template>
    </div>
  </details>
</template>

<style scoped>
.marker-pending-note,.preview-pending-marker{margin:0;font-size:12px;line-height:1.7;color:var(--df-text);overflow-wrap:anywhere}
.preview-pending-marker{padding:9px 12px;background:var(--df-panel-raised);border-radius:6px}
.preview-draft-state{font-size:11px;color:var(--df-muted)}
.output-cuts{border:1px solid var(--df-border);border-radius:10px;margin:0 0 18px;overflow:hidden;min-width:0}.output-cuts>summary{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;list-style:none;background:var(--df-panel-raised)}.output-cuts>summary::-webkit-details-marker{display:none}.output-cuts>summary:before{content:'›';font-size:20px;color:var(--df-muted);transition:transform .15s}.output-cuts[open]>summary:before{transform:rotate(90deg)}.cuts-heading{display:flex;align-items:baseline;flex:1;gap:12px;flex-wrap:wrap;min-width:0}.cuts-heading strong{font-size:14px}.cuts-heading>span,.scope-label{font-size:12px;color:var(--df-muted);font-variant-numeric:tabular-nums}.scope-label{white-space:nowrap}.cuts-body{padding:16px;display:grid;gap:14px;min-width:0}.cuts-description,.muted,.cuts-note,.timeline-endpoints,.range-empty{font-size:12px;color:var(--df-muted);line-height:1.7}.cuts-description{margin:0}.cuts-toolbar,.enable-control,.cuts-timeline-heading,.cuts-timeline-heading>span,.marker-title,.marker-actions,.ranges-heading,.range-heading,.cuts-total,.cuts-footer{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.cuts-toolbar{justify-content:space-between}.enable-control{font-size:13px}.cuts-baseline{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,360px) auto;gap:10px;align-items:center;padding:11px 12px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-panel-raised)}.cuts-baseline strong,.cuts-baseline small{display:block}.cuts-baseline strong{font-size:12px}.cuts-baseline small{margin-top:3px;color:var(--df-muted);font-size:11px;line-height:1.5}.cuts-player{position:relative;width:100%;max-width:960px;justify-self:center;min-width:0}.cuts-player video{display:block;width:100%;max-height:420px;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px}.cuts-timeline-heading{justify-content:space-between;font-size:12px}.preview-mode-summary{display:inline-flex;align-items:center;gap:10px;min-width:0}.preview-mode-summary>span:first-child{display:inline-flex;align-items:center;gap:4px}.preview-mode-note{margin:-6px 0 0;font-size:11px;line-height:1.5}.preview-draft-note{margin:-4px 0 0}.cuts-timeline-heading i{width:9px;height:9px;display:inline-block;border-radius:2px}.legend-keep{background:#4d9687}.legend-cut{background:#c15b62;margin-left:9px}.cuts-track{position:relative;height:18px;border-radius:4px;background:color-mix(in srgb,#4d9687 30%,var(--df-border));cursor:crosshair;outline-offset:3px;overflow:hidden}.cut-region{position:absolute;top:0;height:100%;background:color-mix(in srgb,#d65f68 75%,var(--df-border));pointer-events:none}.playhead{position:absolute;top:0;bottom:0;width:2px;background:var(--df-text);box-shadow:0 0 0 1px #0004;pointer-events:none}.timeline-endpoints{display:flex;justify-content:space-between;margin-top:-10px;font-size:11px;font-variant-numeric:tabular-nums}.marker-panel{padding:14px;border:1px solid var(--df-border);border-radius:8px;display:grid;gap:12px}.marker-title{justify-content:space-between;font-size:12px}.marker-fields{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr) auto;gap:10px;align-items:end}.marker-fields label,.range-fields label{display:grid;gap:5px;font-size:11px;color:var(--df-muted);min-width:0}.marker-actions :deep(.n-input){flex:1;min-width:180px}.ranges-heading{justify-content:space-between;font-size:13px;margin-top:2px}.ranges-heading small{font-size:11px;color:var(--df-muted);font-weight:400;margin-left:5px}.range-empty{margin:0;padding:18px;text-align:center;border:1px dashed var(--df-border);border-radius:8px}.range-row{border:1px solid var(--df-border);border-radius:8px;padding:12px;min-width:0}.range-row.inactive{background:var(--df-panel-raised)}.range-row.inactive .range-fields{opacity:.65}.range-heading{margin-bottom:10px}.range-heading>:last-child{margin-left:auto}.range-duration{font-size:11px;color:var(--df-muted);font-variant-numeric:tabular-nums}.range-mapping{font-size:11px;color:var(--df-accent,#e3b76c);font-variant-numeric:tabular-nums}.range-fields{display:grid;grid-template-columns:minmax(120px,1fr) auto minmax(120px,1fr) auto minmax(140px,1.3fr);gap:8px;align-items:end}.range-arrow{padding-bottom:6px;color:var(--df-muted)}.range-fields>:last-child{align-self:end}.cuts-total{background:var(--df-panel-raised);border-radius:6px;padding:11px 13px;font-size:12px;gap:18px;font-variant-numeric:tabular-nums}.cuts-total strong{font-weight:500;margin-left:5px}.removed-total{color:#c66d73}.cuts-footer{border-top:1px solid var(--df-border);padding-top:14px}.cuts-note{display:block;font-size:11px;margin-top:-5px}.media-error{font-size:12px;color:#c66d73;margin:0}
@media(max-width:1000px){.marker-fields{grid-template-columns:minmax(0,1fr) auto}.range-fields{grid-template-columns:minmax(0,1fr) auto minmax(0,1fr) auto}.range-fields>:last-child{grid-column:1/-1}.cuts-baseline{grid-template-columns:minmax(0,1fr) minmax(180px,1fr)}.cuts-baseline>.n-button{grid-column:2;justify-self:end}.scope-label{display:none}}
@media(max-width:540px){.cuts-heading{display:grid;gap:4px}.cuts-body{padding:12px}.output-cuts>summary{padding:12px}.marker-fields{grid-template-columns:minmax(0,1fr)}.marker-fields>.n-button{justify-self:start}.range-fields{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.range-arrow{display:none}.range-fields>.n-button{grid-column:1/-1;justify-self:start}.cuts-baseline{grid-template-columns:1fr}.cuts-baseline>.n-button{grid-column:auto;justify-self:start}.cuts-total{gap:8px 16px}.cuts-timeline-heading{align-items:flex-start}}
.cuts-preview-heading{position:absolute;z-index:2;top:10px;left:12px;right:12px;display:flex;align-items:baseline;justify-content:space-between;gap:10px;pointer-events:none;color:#fff;text-shadow:0 1px 3px #000;font-size:12px}.cuts-preview-heading small{color:#d8e0ea;font-size:11px}.cuts-player{isolation:isolate}.cuts-player video{position:relative;z-index:1}.cuts-preview-heading{z-index:2}
.baseline-note{margin-top:-4px}
.baseline-reference{border:1px solid var(--df-border);border-radius:8px;padding:10px 12px;background:var(--df-panel-raised)}.baseline-reference summary{font-size:12px}.baseline-reference summary small{margin-left:8px;color:var(--df-muted);font-size:11px}.baseline-reference :deep(.video-preview){margin-top:10px}.baseline-reference>.muted{display:block;margin-top:8px}
.target-reference{border:1px solid color-mix(in srgb,var(--df-accent,#e3b76c) 42%,var(--df-border));border-radius:10px;padding:12px;background:color-mix(in srgb,var(--df-accent,#e3b76c) 4%,var(--df-panel-raised));display:grid;gap:10px}.target-reference-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.target-reference-heading strong,.target-reference-heading small{display:block}.target-reference-heading strong{font-size:13px}.target-reference-heading small{margin-top:4px;color:var(--df-muted);font-size:11px;line-height:1.5}.target-reference-badges{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end}.target-reference-toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11px}.target-reference-toolbar label{display:inline-flex;align-items:center;gap:7px;font-weight:500}.target-reference-toolbar .audio-source-control :deep(.n-base-selection){min-width:132px}.target-reference-toolbar .muted{flex:1 1 240px;font-size:11px}.target-reference-player{position:relative;max-width:960px;width:100%;justify-self:center}.target-reference-player video{display:block;width:100%;max-height:420px;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px}.target-reference-player :deep(.video-controls-layer){z-index:4}
.virtual-preview-progress-note{display:block;color:var(--df-muted);font-size:11px;line-height:1.5;margin-top:-4px}
.preview-build-toolbar{display:grid;grid-template-columns:minmax(160px,1fr) 160px auto;gap:10px;align-items:center;padding:12px 0;border-top:1px solid var(--df-border);border-bottom:1px solid var(--df-border)}
.preview-build-toolbar strong,.preview-build-toolbar small{display:block}.preview-build-toolbar strong{font-size:12px}.preview-build-toolbar small{font-size:11px;color:var(--df-muted);line-height:1.6;margin-top:3px}.preview-build-toolbar .preview-window-label{grid-column:1/-1;margin:0;font-variant-numeric:tabular-nums}
.restore-source{display:flex;align-items:center;gap:10px;font-size:12px}.restore-source .n-select{flex:1;max-width:340px}
.preview-clock-row{display:flex;align-items:center;gap:8px 18px;flex-wrap:wrap;font-size:11px;color:var(--df-muted);font-variant-numeric:tabular-nums}.preview-clock-row strong{color:var(--df-text);font-weight:500}.seam-actions{display:flex;gap:4px;margin-left:auto}
.cut-seek-freeze{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none;background:#000;z-index:2;border-radius:8px}.cut-seek-status{position:absolute;top:12px;left:12px;z-index:3;padding:5px 9px;background:#161c24e6;color:#f3f5f8;border-radius:4px;font-size:11px;pointer-events:none}
.cuts-range-source>summary{font-size:12px;color:var(--df-muted);cursor:pointer}.cuts-range-source .cuts-baseline{margin-top:10px}
@media(max-width:720px){.preview-build-toolbar{grid-template-columns:1fr auto}.preview-build-toolbar>div{grid-column:1/-1}.preview-build-toolbar .preview-window-label{grid-column:1/-1}.preview-clock-row{gap:6px 12px}.target-reference-heading{flex-wrap:wrap}.target-reference-badges{justify-content:flex-start}}
</style>

<style scoped>
.timeline-encoding-plan{display:grid;gap:10px;padding:14px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-panel-raised)}.timeline-encoding-plan header,.timeline-encoding-metrics{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.timeline-encoding-plan header>strong{font-size:13px}.timeline-encoding-metrics{gap:18px;font-size:12px;color:var(--df-muted)}.timeline-encoding-metrics strong{margin-left:5px;color:var(--df-text);font-variant-numeric:tabular-nums}.timeline-encoding-plan p{margin:0;font-size:11px;line-height:1.6;color:var(--df-muted)}.cut-snapshot-summary{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--df-muted);padding:8px 10px;border-radius:6px;background:var(--df-panel-raised)}.cut-snapshot-summary strong{color:var(--df-text);font-variant-numeric:tabular-nums}.cut-apply-action{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:14px;border:1px solid color-mix(in srgb,var(--df-accent,#e3b76c) 40%,var(--df-border));border-radius:8px;background:var(--df-panel-raised)}.cut-apply-action strong,.cut-apply-action small{display:block}.cut-apply-action strong{font-size:13px}.cut-apply-action small{margin-top:5px;font-size:11px;line-height:1.5;color:var(--df-muted)}
</style>
