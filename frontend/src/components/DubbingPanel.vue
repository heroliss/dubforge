<script setup lang="ts">
import { computed, h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NInput, NCollapse, NCollapseItem,
  NDrawer, NDrawerContent, NRadioButton, NRadioGroup, NSelect, NSlider, NSpace, NText, useMessage, useThemeVars, useDialog,
} from 'naive-ui'
import { createLatestRefresh, vReleaseMedia } from '../utils/preview'
import { timingWarnings, voiceFor } from '../utils/dubbingTimeline'
import { playbackTime } from '../utils/dubbingPlaybackTime'
import { dubbingTimingView, timeRange } from '../utils/dubbingTimingPresentation'
import { dubbingStatusLabel, hasPendingDubbingChanges, pendingDubbingChangeReason, sentenceFocus } from '../utils/dubbingLinePresentation'
import { selectRange } from '../utils/sentenceSelection'
import { hiddenSelectionCount } from '../utils/actionScope'
import { createListScrollAnchor, listScrollPage } from '../utils/listScrollAnchor'
import { analyzeDubbingSynthesis, dubbingSlots, dubbingReservationSlots, mergeDubbingReservations, dubbingSynthesisExpected, dubbingSynthesisSignature, sentenceRanges, type DubbingSynthesisTarget } from '../utils/dubbingSynthesis'
import { useSmoothProgressMap } from '../utils/smoothProgress'
import ProjectParameterForm from './ProjectParameterForm.vue'
import TtsServiceControl from './TtsServiceControl.vue'
import SubtitleLineEditor from './SubtitleLineEditor.vue'
import SubtitleWorkspace from './SubtitleWorkspace.vue'
import DependencyNotice from './DependencyNotice.vue'
import VideoControls from './VideoControls.vue'
import DubbingDurationReference from './DubbingDurationReference.vue'
import DubbingAdjustmentBadges from './DubbingAdjustmentBadges.vue'
import DubbingSynthesisConfirm from './DubbingSynthesisConfirm.vue'
import DubbingAssemblyLink from './DubbingAssemblyLink.vue'
import { assemblyComparisonKnown, assemblySentenceNotices, assemblySentenceRevision, type AssemblySentenceComparison } from '../utils/assemblySentenceLink'
import SentenceAudioGain from './SentenceAudioGain.vue'
import { useDubbingPreview } from '../composables/useDubbingPreview'
import { useDubbingSelection, type DubbingSelection } from '../composables/useDubbingSelection'
import { useDubbingMix } from '../composables/useDubbingMix'
import { useDubbingFollow } from '../composables/useDubbingFollow'
import { useSynthesisPreview } from '../composables/useSynthesisPreview'
import { useSubtitlePreview } from '../composables/useSubtitlePreview'
import { useNavStore } from '../stores/nav'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { SOURCE_STYLES } from '../utils/configurationSource'
import { INDEX_TTS_PRONUNCIATION_PATH, presetValueLabel } from '../utils/dubbingPreset'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { currentDubbingTask, normalizeDubbingWorkbench } from '../utils/dubbingTaskState'
import { friendlyError } from '../utils/http'

const props = defineProps<{ uid: string; suspended?: boolean }>()
const { locked: projectLocked, reason: projectLockReason, canEdit, canEditPaths, isPathLocked,
  checking: checkingEditability, observing } = useConfigurationEditability(() => 'project:' + props.uid)
const emit = defineEmits<{ (e: 'busy-change', value: boolean): void; (e: 'dirty-change', value: boolean): void;
  (e: 'content-loaded', value: { uid: string; lines: number }): void }>()
const message = useMessage(), nav = useNavStore()
const route = useRoute(), router = useRouter()
const activePane = ref(route.query.panel === 'subtitles' ? 'subtitles' : 'editor')
const subtitlesVisited = ref(activePane.value === 'subtitles')
const dialog = useDialog()
const theme = useThemeVars()
const panelStyle = computed(() => ({
  '--dub-primary': theme.value.primaryColor, '--dub-warning': theme.value.warningColor,
  '--dub-error': theme.value.errorColor, '--dub-info': theme.value.infoColor,
  '--dub-playing': theme.value.successColor,
  '--dub-muted': theme.value.textColor3, '--dub-border': theme.value.borderColor,
  '--dub-text': theme.value.textColor1,
  '--dub-parameter': SOURCE_STYLES.line.color,
  '--dub-surface': theme.value.cardColor,
}))
const serverState = ref<any>(null), loadError = ref(''), sel = ref<number | null>(null)
const refreshingPlacement = ref(false)
const selectionEdits = useDubbingSelection({
  save: async (id, selection) => {
    if (projectLocked.value || observing.value && !(await canEdit())) throw new Error(projectLockReason.value || '项目正在运行，请完成后再选择声源')
    return request(`workbench/lines/${id}/selection`, 'PUT', selection)
  },
  saved: () => { invalidateAssemblyComparison(); void load(); void nav.poll(false) },
  error: (error: any) => message.error(`${error.message || '声源保存失败'}，已恢复之前的选择`),
})
const expectedTaskJobId = computed(() => nav.uid === props.uid
  ? nav.stepMap.tts?.job_id ?? nav.stepMap.tts?.execution?.job_id : undefined)
const state = computed(() => {
  const value = selectionEdits.apply(serverState.value)
  if (!value || expectedTaskJobId.value == null) return value
  const task = currentDubbingTask(value.synthesis_task, expectedTaskJobId.value)
  const staleTask = value.synthesis_task && !task
  return { ...value, synthesis_task: task,
    ...(staleTask ? { active_line: null, active_progress: null } : {}),
    line_config_execution: currentDubbingTask(value.line_config_execution, expectedTaskJobId.value),
    lines: value.lines.map((line: any) => line.config_execution && !currentDubbingTask(line.config_execution, expectedTaskJobId.value)
      ? { ...line, config_execution: null } : line) }
})
const filter = ref(''), stateFilter = ref('all'), checked = ref<number[]>([])
const assemblyComparison = ref<AssemblySentenceComparison | null>(null)
const assemblyReading = ref<'loading' | 'ready' | 'error'>('ready'), assemblySavedRevision = ref(0)
const assemblyNotices = computed(() => assemblySentenceNotices(assemblyComparison.value))
const assemblyFilterLabel = computed(() => assemblyReading.value === 'loading' ? '成片核对中'
  : assemblyReading.value === 'error' || !assemblyComparisonKnown(assemblyComparison.value) ? '成片待核对' : '成片待更新')
function invalidateAssemblyComparison() { assemblyReading.value = 'loading'; assemblySavedRevision.value++ }
const assemblyRevision = computed(() => assemblySentenceRevision(state.value?.lines || []) + JSON.stringify([
  assemblySavedRevision.value, nav.stepMap.assemble?.status, nav.stepMap.assemble?.cfg, nav.stepMap.assemble?.artifacts,
]))
const parametersOpen = ref<string[]>([]), paramsDirty = ref(false), lineDirty = ref(false)
watch(() => [props.uid, route.hash], ([, hash]) => {
  if (hash !== '#dubbing-parameters') return
  activePane.value = 'editor'
  parametersOpen.value = ['parameters']
  void nextTick(() => { if (typeof document !== 'undefined') document.getElementById('dubbing-parameters')?.scrollIntoView({ block: 'start' }) })
}, { immediate: true })
const settingsLine = ref<number | null>(null), serviceRevision = ref(0)
const settingsFooter = ref<HTMLDivElement>()
const submitting = ref(false)
const submissionUncertain = ref(false)
type PendingSynthesisReceipt = { requestId: string; lines: { id: number; take: number; status: string }[] }
const acceptedSubmission = ref<{ uid: string; jobId: number; requests: PendingSynthesisReceipt[] } | null>(null)
const task = computed(() => state.value?.synthesis_task)
const taskRows = computed<Record<number, any>>(() => Object.fromEntries((task.value?.lines ?? []).map((line: any) => [line.id, line])))
const errors = computed<Record<number, string>>(() => Object.fromEntries((task.value?.lines ?? [])
  .filter((line: any) => line.status === 'failed').map((line: any) => [line.id, line.error || '合成失败'])))
const editing = ref(false), gainDirty = ref(false)
const textEditLine = ref<any>(null), textDirty = ref(false), subtitleDirty = ref(false)
const subtitleNames: Record<string, string> = { 'source.srt': '原文', 'trans.srt': '译文', 'bilingual.srt': '双语' }
const exportsReady = computed(() => !!state.value?.subtitles?.fresh && !['running', 'queued'].includes(state.value?.subtitles?.status))
const { mode: subtitleMode, url: subtitlePreview, error: subtitleError, hasTranslation, exported: previewExported } = useSubtitlePreview({
  lines: () => state.value?.lines ?? [], files: () => state.value?.subtitles?.files ?? [],
  exportsReady: () => exportsReady.value,
  sourceFirst: () => !!nav.stepMap.subtitles?.cfg?.subtitles?.source_first,
})
const subtitleOptions = computed(() => [
  { label: '关闭字幕', value: 'off' },
  { type: 'group', label: '当前文字', key: 'current', children: [
    { label: '原文', value: 'source' },
    { label: '译文', value: 'target', disabled: !hasTranslation.value },
    { label: '双语', value: 'bilingual', disabled: !hasTranslation.value },
  ] },
  ...(state.value?.subtitles?.files?.length ? [{ type: 'group', label: '导出文件', key: 'export',
    children: state.value.subtitles.files.map((file: any) => ({ label: `导出 · ${subtitleNames[file.name] || file.name}`,
      value: `export:${file.name}`, disabled: !exportsReady.value })),
  }] : []),
])
let anchor: number | null = null
const choices = {}
const videoEl = ref<HTMLVideoElement>()
const videoDuration = ref(0)
const previewChapters = ref<any[]>([])
let chapterSignature = ''
const chapterReader = createLatestRefresh(async signal => {
  const r = await fetch(`/api/projects/${props.uid}/steps/download/video-info`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取视频章节失败')
  const value = await r.json()
  const rows = value?.info?.content?.chapters
  return Array.isArray(rows) ? rows : []
}, value => { previewChapters.value = value }, () => { previewChapters.value = [] })
function updateVideoDuration(event: Event) {
  const value = (event.target as HTMLVideoElement).duration
  videoDuration.value = Number.isFinite(value) && value > 0 ? value : 0
}
watch(() => state.value?.video?.url, () => { videoDuration.value = 0 })
watch(() => state.value?.video?.url || '', (url) => {
  if (!url) { chapterSignature = ''; chapterReader.cancel(); previewChapters.value = []; return }
  if (url !== chapterSignature) { chapterSignature = url; void chapterReader.refresh(true) }
}, { immediate: true })
const sentenceList = ref<HTMLDivElement>()
const activeSynthesisLine = computed<number | null>(() => state.value?.active_line ?? null)
const synthesisProgressKey = computed(() => {
  const row = activeSynthesisLine.value === null ? undefined : taskRows.value[activeSynthesisLine.value]
  return `${task.value?.job_id}:${activeSynthesisLine.value}:${state.value?.active_attempt ?? 0}:${row?.timing_phase ?? ''}:${row?.duration_factor ?? state.value?.active_factor ?? ''}`
})
const smoothLineProgress = useSmoothProgressMap(() => activeSynthesisLine.value === null ? [] : [{
  key: synthesisProgressKey.value,
  progress: (state.value?.active_progress ?? 0) * 100, status: 'running',
}])
const timing = computed(() => timingWarnings(state.value?.lines ?? [], choices, state.value?.placement, videoDuration.value))
let pollTimer: number | undefined, disposed = false
const selLine = computed(() => state.value?.lines.find((l: any) => l.id === sel.value))
const settingsAudioLine = computed(() => state.value?.lines.find((l: any) => l.id === settingsLine.value))
const filters = computed(() => {
  const lines: any[] = state.value?.lines ?? []
  return [
    { value: 'all', label: '全部', count: lines.length },
    { value: 'pending', label: '待合成', count: lines.filter(l => !l.wav.exists).length },
    { value: 'settings', label: '配音待更新', count: lines.filter(hasPendingDubbingChanges).length },
    ...(assemblyComparison.value?.baseline ? [{ value: 'assembly', label: assemblyFilterLabel.value, count: lines.filter(line => assemblyNotices.value.has(line.id)).length }] : []),
    { value: 'confirmed', label: '已满意', count: lines.filter(l => l.confirmed).length },
    { value: 'silent', label: '无声', count: lines.filter(isSilent).length },
  ]
})
const shownLines = computed<any[]>(() => (state.value?.lines ?? []).filter((l: any) => {
  const q = filter.value.trim().toLowerCase()
  return (!q || `${l.text} ${l.source_text}`.toLowerCase().includes(q) || String(l.id) === q)
    && (stateFilter.value === 'all' || (stateFilter.value === 'confirmed' ? l.confirmed
      : stateFilter.value === 'silent' ? isSilent(l)
      : stateFilter.value === 'assembly' ? assemblyNotices.value.has(l.id)
      : stateFilter.value === 'settings' ? hasPendingDubbingChanges(l) : !l.wav.exists))
}))
const listReading = createListScrollAnchor({
  list: () => sentenceList.value,
  allIds: () => (state.value?.lines ?? []).map((line: any) => line.id),
  page: () => listScrollPage(sentenceList.value),
  scrollPage: top => { listScrollPage(sentenceList.value).scrollTo(top) },
  afterRender: () => nextTick(),
})
const filterReadingKey = (query: string, status: string) => JSON.stringify([query.trim().toLowerCase(), status])
watch([filter, stateFilter], ([query, status], [previousQuery, previousStatus]) => {
  void listReading.transition(filterReadingKey(previousQuery, previousStatus), filterReadingKey(query, status))
})
watch(() => props.uid, () => listReading.reset())
function changeStateFilter(value: string) { stateFilter.value = value }
function cancelListRestore() { listReading.cancel() }
function applyAssemblyComparison(value: AssemblySentenceComparison | null) {
  if (!value) { assemblyComparison.value = null; return }
  void listReading.update(filterReadingKey(filter.value, stateFilter.value), () => { assemblyComparison.value = value })
}
const allChecked = computed(() => shownLines.value.length > 0 && shownLines.value.every(l => checked.value.includes(l.id)))
const checkedBlocked = computed(() => state.value?.lines.some((l: any) => checked.value.includes(l.id) && (!l.translation_available || l.edit_stale || l.unready_inputs?.length)))
const allBlocked = computed(() => state.value?.lines.some((l: any) => !l.translation_available || l.edit_stale || l.unready_inputs?.length))
const someChecked = computed(() => shownLines.value.some(l => checked.value.includes(l.id)))
const dirty = computed(() => paramsDirty.value || lineDirty.value || gainDirty.value || textDirty.value || subtitleDirty.value || editing.value)
// 排队中的 TTS 尚未冻结配置；只把真正进入 running 的任务作为参数编辑忙碌态。
const busy = computed(() => submitting.value || !!task.value?.running || state.value?.active_line != null)
const actionsDisabled = computed(() => projectLocked.value || busy.value || nav.busy || dirty.value || props.suspended)
const runDisabled = computed(() => actionsDisabled.value || submissionUncertain.value || !!acceptedSubmission.value || task.value?.active || task.value?.queued
  || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value)
// 正在合成时只放开追加范围；全部重做、切换声源和满意标记仍使用原有运行锁。
const canAppendSynthesis = computed(() => !!state.value?.line_config_execution?.active
  && state.value?.line_config_execution?.generation_accepting === true
  && !!(task.value?.active || task.value?.running))
const synthesisDisabled = computed(() => projectLocked.value || submitting.value || dirty.value || props.suspended
  || submissionUncertain.value || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value
  || (!canAppendSynthesis.value && !!(nav.busy || task.value?.active || task.value?.queued || busy.value || acceptedSubmission.value)))
const queuedSynthesisIds = computed(() => new Set<number>([
  ...(task.value?.active || task.value?.queued ? (task.value?.lines ?? [])
    .filter((line: any) => ['queued', 'running'].includes(line.status)).map((line: any) => line.id) : []),
  ...(activeSynthesisLine.value === null ? [] : [activeSynthesisLine.value]),
]))
const checkedSynthesisIds = computed(() => checked.value.filter(id => !queuedSynthesisIds.value.has(id)))
const checkedSynthesisBlocked = computed(() => (state.value?.lines ?? []).some((line: any) => checkedSynthesisIds.value.includes(line.id)
  && (!line.translation_available || line.edit_stale || line.unready_inputs?.length)))
const selectionDisabled = computed(() => submitting.value || busy.value && !canAppendSynthesis.value)
function synthesisButtonLabel(id: number) {
  if (isSynthesizing({ id })) return '正在合成本句'
  if (queuedSynthesisIds.value.has(id)) return '本句已在待配音列表'
  return canAppendSynthesis.value ? '加入待配音…' : '生成本句配音…'
}
const canRefreshPlacement = computed(() => !!state.value?.lines.length && !refreshingPlacement.value && !dirty.value
  && !selectionEdits.saving.value && !mixEdits.pending.value && !mixEdits.saving.value && !props.suspended)
const canRegenerateAll = computed(() => !!state.value?.lines.length && !runDisabled.value && !allBlocked.value)
const conflictIds = computed<number[]>(() => {
  const unresolved = new Set(state.value?.timing?.unresolved_ids ?? [])
  return (state.value?.lines ?? []).filter((line: any) => unresolved.has(line.id)
    && voiceFor(line, choices) === 'dub').map((line: any) => line.id)
})
const canRegenerateConflicts = computed(() => !runDisabled.value && !task.value?.active && !task.value?.queued && !!conflictIds.value.length
  && !(state.value?.lines ?? []).some((line: any) => conflictIds.value.includes(line.id)
    && (!line.translation_available || line.edit_stale || line.unready_inputs?.length)))
// 后台合成不占用预览；只有上游媒体重建才需要释放播放器。
const playbackDisabled = computed(() => !!props.suspended)
const { preview, playAll, resume, pause, seek, stopAll, volDub, volBg, solo, monitorAudio, setMonitorAudio, playing, time, cursor, playbackError, finished, interrupted } = useDubbingPreview({
  state: () => state.value, video: videoEl, choices: () => choices,
})
const previewLine = computed<number | null>(() => cursor.value?.lineId ?? (state.value?.lines ?? [])
  .reduce((current: any, line: any) => {
    const start = playbackTime(line).start
    return start <= time.value && (!current || start >= playbackTime(current).start) ? line : current
  }, null)?.id ?? null)
const { mode: followMode, locate } = useDubbingFollow({
  lines: () => state.value?.lines ?? [], selected: sel,
  previewLine: () => previewLine.value,
  seek, reveal: id => { void revealLine(id) },
})
const synthesisPreview = useSynthesisPreview({
  enabled: () => followMode.value === 'synthesis' && activePane.value === 'editor' && !playbackDisabled.value,
  task: () => task.value, lines: () => state.value?.lines ?? [],
  finished: () => finished.value, interrupted: () => interrupted.value,
  error: () => playbackError.value || loadError.value,
  previewLine: () => previewLine.value,
  playing: () => playing.value,
  preservePlaybackOnDisable: () => followMode.value === 'preview',
  select: id => locate(id, false), play: preview, resume, pause,
})
const followPaused = computed(() => ['paused', 'error'].includes(synthesisPreview.status.value))
const followHint = computed(() => {
  const current = synthesisPreview.currentLine.value, pending = synthesisPreview.pendingLine.value
  switch (synthesisPreview.status.value) {
    case 'playing': return !playing.value ? `正在准备第 ${current} 句试听…`
      : `正在试听第 ${current} 句${pending === null ? ' · 播完自动等待' : ` · 随后试听最新第 ${pending} 句`}`
    case 'paused': return '自动跟随已暂停，可手动试听；点击「继续跟随」恢复'
    case 'error': return '自动跟随已停止，请解决播放问题后点击「重试跟随」'
    case 'complete': return `本次合成试听结束${task.value?.failed ? ` · ${task.value.failed} 句合成失败` : ''} · 等待新的合成任务`
    default: return pending !== null ? `等待第 ${pending} 句音频就绪…`
      : activeSynthesisLine.value !== null ? `等待第 ${activeSynthesisLine.value} 句合成完成…`
      : (task.value?.active || task.value?.queued) ? '等待下一句合成完成…' : '等待新的合成任务…'
  }
})
const transportLabel = computed(() => playing.value ? 'Ⅱ 暂停' : followMode.value !== 'synthesis' ? '▶ 连续播放'
  : synthesisPreview.status.value === 'error' ? '↻ 重试跟随' : followPaused.value ? '▶ 继续跟随' : 'Ⅱ 暂停跟随')
function transportAction() {
  if (playbackDisabled.value) return
  if (followMode.value === 'synthesis') {
    if (followPaused.value && !playing.value) synthesisPreview.resume()
    else { synthesisPreview.suspend(false); pause() }
  } else if (playing.value) pause()
  else void playAll()
}
function manualPlayAll() {
  if (playbackDisabled.value) return
  synthesisPreview.suspend(false)
  void playAll()
}
function locateManually(id: number | null) {
  if (playbackDisabled.value) return
  synthesisPreview.suspend(false)
  locate(id)
}
const mixProjectUid = props.uid
const mixPaths = ['assemble.voice_volume', 'assemble.bgm_volume'] as const
const mixPathsLocked = computed(() => mixPaths.some(isPathLocked))
const mixControlsLocked = computed(() => projectLocked.value || mixPathsLocked.value || editing.value || props.suspended)
const mixLockReason = computed(() => projectLockReason.value || (mixPathsLocked.value
  ? '合成成片正在使用这些音量，完成后可保存修改。' : props.suspended ? '正在更新预览媒体，请稍后保存音量。' : ''))
const mixEdits = useDubbingMix({
  voice: volDub, background: volBg,
  save: async mix => {
    if (props.suspended || editing.value || !(await canEditPaths(mixPaths)))
      throw new Error(mixLockReason.value || '暂不能保存音量，请重新核对运行状态；当前试听值已保留。')
    return request('step_params', 'PUT', { updates: {
      'assemble.voice_volume': mix.voice, 'assemble.bgm_volume': mix.background,
    } }, mixProjectUid)
  },
  saved: () => { invalidateAssemblyComparison(); void load(); void nav.poll(false) },
})
watch(playbackDisabled, value => { if (value) stopAll() })
function changePane(pane: string) {
  if (pane === 'subtitles') pause()
  activePane.value = pane
  if (pane === 'subtitles') subtitlesVisited.value = true
  void router.replace({ query: { ...route.query, panel: pane === 'subtitles' ? 'subtitles' : undefined } })
}
watch(() => route.query.panel, value => {
  activePane.value = value === 'subtitles' ? 'subtitles' : 'editor'
  if (activePane.value === 'subtitles') { subtitlesVisited.value = true; pause() }
})
function previewExport(name: string) { subtitleMode.value = `export:${name}`; changePane('editor') }
let leaveConfirmation: Promise<boolean> | null = null
function confirmLeave(): boolean | Promise<boolean> {
  if (!dirty.value) return true
  if (leaveConfirmation) return leaveConfirmation
  leaveConfirmation = new Promise(resolve => {
    const settle = (leave: boolean) => { leaveConfirmation = null; resolve(leave) }
    dialog.warning({
      title: '字幕与配音中有未保存的修改',
      content: '离开页面会丢失草稿。可以留在此页保存，或放弃草稿后离开。',
      positiveText: '放弃草稿并离开', negativeText: '留在此页',
      onPositiveClick: () => settle(true), onNegativeClick: () => settle(false),
      onClose: () => settle(false), onMaskClick: () => settle(false),
    })
  })
  return leaveConfirmation
}
onBeforeRouteLeave(confirmLeave)
// 同一动态路由切换项目/步骤会重建页面，也必须保护草稿；页内标签只改 query。
onBeforeRouteUpdate((to, from) => to.path === from.path || confirmLeave())
function warnUnload(event: BeforeUnloadEvent) {
  if (dirty.value) { event.preventDefault(); event.returnValue = '' }
}
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
watch(() => dirty.value || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value,
  value => emit('dirty-change', value), { flush: 'sync' })

async function request(path: string, method: string, body: any, uid = props.uid) {
  const response = await fetch(`/api/projects/${uid}/${path}`, { method,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.message || '保存失败，请重试'), { status: response.status, code: data.code })
  return data
}
function chooseVoice(line: any, source: DubbingSelection['source'], take = line.selection.take) {
  if (actionsDisabled.value) return
  void selectionEdits.choose(line.id, { source, take: source === 'original' && !line.wav.exists ? null : take })
}
function chooseTake(line: any, event: Event) {
  const value = (event.target as HTMLSelectElement).value
  void chooseVoice(line, 'dub', value === '' ? null : Number(value))
}
function mediaSignature(data: any) {
  return JSON.stringify([data?.video, data?.bg, data?.vocal, data?.original,
    data?.lines?.map((l: any) => [l.id, l.start, l.end, l.text])])
}
const loader = createLatestRefresh(async signal => {
  const targetUid = props.uid
  const readRevision = selectionEdits.revision()
  const mixRevision = mixEdits.revision()
  const r = await fetch(`/api/projects/${targetUid}/workbench?probe=false`, { cache: 'no-store', signal })
  const value = await r.json()
  if (!r.ok) throw Object.assign(new Error(value.message || '读取配音内容失败'), { status: r.status, code: value.code })
  return { targetUid, value: normalizeDubbingWorkbench(value), readRevision, mixRevision }
}, ({ targetUid, value, readRevision, mixRevision }) => {
  if (targetUid !== props.uid) return
  if (acceptedSubmission.value?.uid === targetUid) {
    const jobId = Number(value.synthesis_task?.job_id)
    const receipt = acceptedSubmission.value
    if (jobId > receipt.jobId) acceptedSubmission.value = null
    else if (jobId === receipt.jobId) {
      const accepted = new Set(value.synthesis_task?.accepted_request_ids ?? [])
      receipt.requests = receipt.requests.filter(item => !accepted.has(item.requestId))
      if (!receipt.requests.length) acceptedSubmission.value = null
      else {
        const rows = new Map((value.synthesis_task?.lines ?? []).map((line: any) => [line.id, line]))
        for (const request of receipt.requests) for (const line of request.lines) rows.set(line.id, line)
        value = { ...value,
          synthesis_slots: dubbingSlots(value.lines ?? [], [...(value.synthesis_slots ?? []), ...receipt.requests.flatMap(item => item.lines.map(line => line.take))]),
          synthesis_reserved_takes: mergeDubbingReservations(value.synthesis_reserved_takes, receipt.requests.flatMap(item => item.lines)),
          synthesis_task: { ...value.synthesis_task, lines: [...rows.values()] } }
      }
    } else value = { ...value, synthesis_task: serverState.value?.synthesis_task,
      synthesis_slots: serverState.value?.synthesis_slots,
      synthesis_reserved_takes: serverState.value?.synthesis_reserved_takes }
  }
  if (mediaSignature(state.value) !== mediaSignature(value)) stopAll()
  if ((value.synthesis_task?.active || value.synthesis_task?.queued) && value.synthesis_task.job_id !== serverState.value?.synthesis_task?.job_id) {
    checked.value = value.synthesis_task.lines.map((line: any) => line.id)
  }
  serverState.value = value
  emit('content-loaded', { uid: targetUid, lines: value.lines.length })
  selectionEdits.reconcile(readRevision)
  loadError.value = ''
  if (!value.lines.some((l: any) => l.id === sel.value)) sel.value = value.lines[0]?.id ?? null
  checked.value = checked.value.filter(id => value.lines.some((l: any) => l.id === id))
  mixEdits.reconcile(value.mix, mixRevision)
  focusRequestedLine()
  submissionUncertain.value = false
}, failure => { loadError.value = friendlyError(failure, '读取配音内容失败，请重试'); stopAll() })
let workbenchLoads = 0, lastWorkbenchRefresh = 0
async function load(replace = true) {
  if (disposed) return
  workbenchLoads++
  try { await loader.refresh(replace) }
  finally { workbenchLoads--; lastWorkbenchRefresh = Date.now() }
}
function pollWorkbench(immediate = false) {
  if (disposed || props.suspended || document.hidden || workbenchLoads) return
  const interval = task.value?.active || task.value?.queued || state.value?.active_line != null
    || nav.uid === props.uid && ['running', 'queued'].includes(nav.stepMap.tts?.status) ? 1500 : 4500
  if (immediate || Date.now() - lastWorkbenchRefresh >= interval) void load(false)
}
function resumeWorkbench() { pollWorkbench(true) }
watch(() => props.suspended, value => { if (!value && pollTimer != null) pollWorkbench(true) })
function placementSnapshot(value: any) {
  return JSON.stringify({ enabled: value?.timing?.enabled, manual: value?.timing?.manual,
    unresolved: value?.timing?.unresolved_ids, missing: value?.timing?.missing_ids,
    lines: value?.lines?.map((line: any) => [line.id, playbackTime(line).start, playbackTime(line).end]) })
}
async function refreshPlacement() {
  if (!canRefreshPlacement.value) return
  const targetUid = props.uid, previous = serverState.value, snapshot = placementSnapshot(previous)
  refreshingPlacement.value = true
  try {
    // GET 已按当前音频与已保存设置派生布局；不登记任务，不写回自动位置。
    await load()
    if (disposed || props.uid !== targetUid) return
    if (loadError.value || serverState.value === previous) {
      message.error(loadError.value || '未能获取新的排布结果，请重试')
      return
    }
    const result = serverState.value?.timing
    if (!result) { message.warning('尚无可核对的排布结果，请先准备原文与译文'); return }
    const unchanged = snapshot === placementSnapshot(serverState.value)
    const counts = `当前移动 ${result.shifted ?? 0} 句 · 仍有 ${result.unresolved ?? 0} 句需处理 · 缺少 ${result.missing ?? 0} 句有效音频`
    message.info(`${result.enabled ? unchanged ? '排布无变化' : '已重新核对排布' : '自动排布已关闭，仅重新检查'} · ${counts}`)
  } catch (error: any) {
    if (!disposed && props.uid === targetUid) message.error(error.message || '重新核对排布失败，请重试')
  } finally { refreshingPlacement.value = false }
}
onMounted(async () => {
  window.addEventListener('beforeunload', warnUnload)
  for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown'])
    window.addEventListener(event, cancelListRestore, { passive: true, capture: true })
  document.addEventListener('visibilitychange', resumeWorkbench)
  await load()
  if (!disposed) pollTimer = window.setInterval(pollWorkbench, 1500)
})
onUnmounted(() => {
  window.removeEventListener('beforeunload', warnUnload)
  listReading.cancel()
  for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown'])
    window.removeEventListener(event, cancelListRestore, true)
  document.removeEventListener('visibilitychange', resumeWorkbench)
  disposed = true
  clearInterval(pollTimer); loader.cancel(); chapterReader.cancel(); selectionEdits.dispose(); void mixEdits.dispose(); stopAll()
})
async function parametersSaved() {
  invalidateAssemblyComparison()
  serviceRevision.value++
  await Promise.all([load(), nav.poll()])
}
function fmt(t: number) { return `${String(Math.floor(t / 60)).padStart(2, '0')}:${(t % 60).toFixed(1).padStart(4, '0')}` }
function select(line: any) { locateManually(line.id) }
function listen(line: any) {
  if (!playbackDisabled.value) { synthesisPreview.suspend(false); sel.value = line.id; void preview(line) }
}
function check(id: number, event: Event) {
  checked.value = selectRange(checked.value, shownLines.value.map(l => l.id), id,
    (event.target as HTMLInputElement).checked, anchor, (event as MouseEvent).shiftKey)
  anchor = id
}
function checkShown(value: boolean) {
  const ids = shownLines.value.map(l => l.id)
  checked.value = value ? [...new Set([...checked.value, ...ids])] : checked.value.filter(id => !ids.includes(id))
}
function invertShown() {
  const before = new Set(checked.value)
  shownLines.value.forEach(l => { checked.value = selectRange(checked.value, [], l.id, !before.has(l.id), null, false) })
}
function listKey(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && !selectionDisabled.value) {
    event.preventDefault(); checkShown(true)
  }
}
function pronunciationText(line: any): string {
  return String(line.custom_params?.find((parameter: any) => parameter.path === INDEX_TTS_PRONUNCIATION_PATH)?.value || '')
}
function paramSummary(line: any) { return (line.custom_params ?? []).filter((p: any) => p.path !== INDEX_TTS_PRONUNCIATION_PATH)
  .map((p: any) => `${p.label}：${presetValueLabel(p.path, p.value)}`).join(' · ') }
function lineSettingsSummary(line: any) { return [pronunciationText(line) ? '已保存自定义朗读' : '', paramSummary(line)].filter(Boolean).join(' · ') }
function emptySlot(line: any) { return line.selection.source === 'dub' && line.selection.take === null }
function isSilent(line: any) { return voiceFor(line, choices) === 'silent' }
function isSynthesizing(line: any) { return state.value?.active_line === line.id }
function editLine(line: any) {
  if (actionsDisabled.value || selectionEdits.saving.value || state.value?.edit_blockers?.length) return
  textEditLine.value = JSON.parse(JSON.stringify(line))
}
function closeTextEditor() { textEditLine.value = null; textDirty.value = false }
async function textSaved() { invalidateAssemblyComparison(); await Promise.all([load(), nav.poll(false)]) }
async function subtitlesRefreshed() { invalidateAssemblyComparison(); await load() }
async function discardOrphans() {
  if (actionsDisabled.value) return
  editing.value = true
  try { await request('workbench/discard_orphaned_edits', 'POST', {}); await textSaved() }
  catch (e: any) { message.error(e.message || '清理失败') }
  finally { editing.value = false }
}
async function revealLine(id: number) {
  listReading.cancel()
  if (!shownLines.value.some(line => line.id === id)) {
    filter.value = ''; stateFilter.value = 'all'
  }
  await nextTick()
  if (disposed || sel.value !== id) return
  listReading.cancel()
  const list = sentenceList.value, row = list?.querySelector<HTMLElement>(`[data-id="${id}"]`)
  if (list && row) {
    const bounds = list.getBoundingClientRect(), target = row.getBoundingClientRect()
    // 列表下半部可能在浏览器窗口外，定位到屏幕内可见区域，避免看起来没有跟随。
    const top = Math.max(0, bounds.top), bottom = Math.min(window.innerHeight, bounds.bottom)
    const visibleTop = bottom > top ? top : bounds.top
    const visibleHeight = bottom > top ? bottom - top : list.clientHeight
    if (target.top < visibleTop + 2 || target.bottom > visibleTop + visibleHeight - 2)
      list.scrollTop += target.top - visibleTop - Math.max(2, (visibleHeight - row.offsetHeight) / 2)
  }
}
function timingHint(line: any) {
  const warning = timing.value[line.id]
  if (!warning) return ''
  const adjustment = line.wav.duration_retry?.retries || line.wav.duration_retry?.used_factor <= .5
    ? '缩短译文或调整句子时间'
    : state.value?.line_settings_supported ? '缩短译文或减小本句「时长因子」' : '缩短译文或在服务端调整语速'
  if (warning.kind === 'video_overrun') return `本句人声超出视频结尾 ${warning.excess.toFixed(2)} 秒。正式合成将停止，以免裁掉尾音；可${adjustment}后重新合成，或选择原声/空位。`
  if (warning.kind === 'overlap') return `本句与第 ${warning.nextId} 句人声仍重叠 ${warning.excess.toFixed(2)} 秒。附近空位与当前移动范围不足以容纳完整声音；可检查排布范围，或${adjustment}后重新合成。`
  const limit = warning.nextId ? `距第 ${warning.nextId} 句字幕开始仅` : '本句字幕区间为'
  return `配音 ${line.wav.seconds?.toFixed(2)} 秒，${limit} ${warning.available.toFixed(2)} 秒，超出 ${warning.excess.toFixed(2)} 秒。当前没有已确认的人声重叠；可${adjustment}后重新合成。`
}
function placementHint(line: any) {
  const view = dubbingTimingView(line), time = view.effective
  if (Math.abs(time.start - line.start) < .005 && Math.abs(time.end - line.end) < .005) return ''
  const source = view.mode === 'manual' ? '人工调整' : view.mode === 'source' ? '源稿时间' : '自动排布'
  return `位置来源：${source} · 原文、译文与配音同步`
}
function placementWindowHint(line: any) {
  const row = line.timing
  if (!Number.isInteger(row?.window_lines) || row.window_lines < 1) return ''
  const range = typeof row.window_start === 'number' && Number.isFinite(row.window_start)
    && typeof row.window_end === 'number' && Number.isFinite(row.window_end)
    ? ` · 窗口 ${fmt(row.window_start)} — ${fmt(row.window_end)}` : ''
  return `邻句联动 ${row.window_lines} 句${range}；可在「文字与时间」查看或调整本句位置。`
}
function lineTimeTitle(line: any) {
  const view = dubbingTimingView(line)
  return `原始 ${timeRange(view.source)}\n自动 ${timeRange(view.automatic)}\n最终 ${timeRange(view.effective)}${view.label ? ` · ${view.label}` : ''}\n原文、译文与配音同步；点击编辑可查看和调整`
}
function editLineParameters(id: number) { settingsLine.value = id }
let focusedQueryLine = ''
function focusRequestedLine() {
  const key = String(route.query.line || '')
  if (!key || key === focusedQueryLine || !/^\d+$/.test(key)) return
  const id = Number(key)
  if (!state.value?.lines.some((line: any) => line.id === id)) return
  focusedQueryLine = key
  void nextTick(() => { if (!disposed) locateManually(id) })
}
watch(() => route.query.line, focusRequestedLine)
const lineSettingsLocked = computed(() => projectLocked.value || !!props.suspended || submitting.value
  || (busy.value || nav.busy) && !state.value?.line_config_execution?.editable)
function lineSettingsDisabled(line: any) {
  return projectLocked.value || !!props.suspended || dirty.value || !line.translation_available || line.edit_stale
    || (state.value?.line_settings_supported ? lineSettingsLocked.value : isPathLocked('assemble.audio_line_gains'))
}
function configUpdateLabel(line: any) {
  const value = line.config_execution
  if (!value) return ''
  // 主状态已展示显式入队进度，避免另贴一个“参数更新”标签造成误解或重复。
  if (value.kind === 'generation') return ''
  if (value.pending_revision) return '参数更新待处理'
  return ({ queued: '参数更新待处理', running: '正在更新配音', failed: '参数更新失败', interrupted: '参数更新待恢复' } as Record<string, string>)[value.state] || ''
}
function timingLabel(line: any) {
  const warning = timing.value[line.id]
  return warning ? `${warning.kind === 'video_overrun' ? '超出视频' : warning.kind === 'overlap' ? '人声重叠' : '超出字幕'} ${warning.excess.toFixed(1)} 秒` : ''
}
function statusTone(line: any) {
  return isSynthesizing(line) ? 'working' : isQueued(line) ? 'queued' : errors.value[line.id] ? 'failed'
    : isSilent(line) || line.wav.stale ? 'warning' : line.confirmed ? 'confirmed' : line.wav.exists ? 'ready' : 'neutral'
}
function silenceHint(line: any) {
  return emptySlot(line) ? '当前选中空位；试听和成片均不加入本句人声，保留背景与画面。'
    : '选中的配音已失效或缺失，预览时没有本句人声；成片前请重新合成、选择有效版本或切回原声。'
}
function lineProgress(id: number): number | null {
  const value = state.value?.active_progress
  return state.value?.active_line === id && typeof value === 'number' && Number.isFinite(value)
    ? Math.round((smoothLineProgress.value[synthesisProgressKey.value] ?? 0) * 10) / 10 : null
}
function isQueued(line: any) { return (task.value?.active || task.value?.queued) && taskRows.value[line.id]?.status === 'queued' }
function synthesisStatus(id: number) {
  const row = taskRows.value[id]
  const shortening = row?.timing_phase === 'shorten'
  const stage = shortening ? '排布后缩短重试'
    : state.value?.active_attempt ? `缩时重试 ${state.value.active_attempt}` : '合成中'
  const factor = row?.duration_factor ?? state.value?.active_factor
  const detail = (shortening || state.value?.active_attempt) && typeof factor === 'number' && Number.isFinite(factor)
    ? `${stage} · 因子 ${factor}` : stage
  const progress = lineProgress(id)
  return progress === null ? detail : `${detail} · ${progress}%`
}
function label(line: any) {
  if (isSynthesizing(line)) return synthesisStatus(line.id)
  if (isQueued(line)) {
    const take = taskRows.value[line.id]?.take
    return Number.isInteger(take) && take > 0 && take !== line.selection.take ? `等待合成 · 目标槽 ${take}` : '等待合成'
  }
  return dubbingStatusLabel(line, { error: errors.value[line.id], silent: isSilent(line), emptySlot: emptySlot(line) })
}
async function synthesize(ids: number[], context?: { title: string; scopeLabel: string; note: string; target?: DubbingSynthesisTarget }) {
  if (!ids.length || synthesisDisabled.value) return
  if (ids.some(id => queuedSynthesisIds.value.has(id))) { message.warning('所选句子已在合成或等待中，请选择其他句子'); return }
  const appendJobId = canAppendSynthesis.value ? task.value.job_id : null
  const appending = appendJobId != null
  // 弹窗中的目标选择是本次运行草稿，不提前修改试听和成片使用的槽。
  const lines = JSON.parse(JSON.stringify(state.value?.lines ?? []))
  const projectSlots = [...(state.value?.synthesis_slots ?? [])]
  const reservedSlots = dubbingReservationSlots(state.value?.synthesis_reserved_takes, ids)
  const target = ref<DubbingSynthesisTarget>(context?.target ?? { mode: 'current' })
  const plan = computed(() => analyzeDubbingSynthesis(lines, ids, target.value, reservedSlots))
  if (plan.value.ids.length !== new Set(ids).size) { message.warning('句子列表已变化，请刷新后重新选择范围'); return }
  if (plan.value.blockedIds.length) { message.warning(`第 ${sentenceRanges(plan.value.blockedIds)} 句的译文或上游内容未就绪，请先处理后再合成`); return }
  const signature = dubbingSynthesisSignature(lines, plan.value.ids, projectSlots, !appending)
  const expected = dubbingSynthesisExpected(lines, plan.value.ids, state.value?.synthesis_slots)
  const selected = new Set(plan.value.ids)
  const slots = dubbingSlots(lines, projectSlots).map(value => ({ value,
    count: lines.filter((line: any) => selected.has(line.id) && line.versions?.some((v: any) => v.id === value)).length }))
  submitting.value = true
  const sending = ref(false), submissionError = ref('')
  let needsReconfirmation = false
  let completed = false
  const buttonText = () => appending ? `${plan.value.overwrite ? '确认覆盖并加入' : '加入待配音'} ${plan.value.total} 句`
    : plan.value.overwrite ? `确认覆盖并生成 ${plan.value.total} 句` : `开始生成 ${plan.value.total} 句`
  const syncButtons = () => {
    confirmation.positiveText = sending.value ? needsReconfirmation ? '正在刷新…' : '正在提交…'
      : needsReconfirmation ? '刷新并重新确认' : buttonText()
    confirmation.positiveButtonProps = { loading: sending.value, disabled: sending.value || !needsReconfirmation && !!plan.value.invalidReason }
    confirmation.negativeButtonProps = { disabled: sending.value }
    confirmation.closable = !sending.value
  }
  const execute = async () => {
    if (sending.value || completed || submissionUncertain.value) return false
    const confirmedPlan = plan.value
    if (!needsReconfirmation && confirmedPlan.invalidReason) { message.warning(confirmedPlan.invalidReason); return false }
    let retainDialog = false, replacedDialog = false
    sending.value = true
    submissionError.value = ''
    syncButtons()
    try {
      if (needsReconfirmation) {
        await load()
        if (loadError.value) { submissionError.value = loadError.value; retainDialog = true; return false }
        replacedDialog = true
        confirmation.destroy()
        submitting.value = false
        if (!disposed) await synthesize(ids, context ? { ...context, target: target.value } : {
          title: confirmedPlan.total === 1 ? '生成本句配音' : confirmedPlan.all ? '生成全部配音' : '生成勾选配音',
          scopeLabel: confirmedPlan.all ? '全部' : '勾选', note: '', target: target.value,
        })
        return
      }
      // 本地已知变化立即拦截；跨窗口校验由 POST 在后端启动锁中一次完成。
      const validExecution = appending ? canAppendSynthesis.value && task.value?.job_id === appendJobId
        : !(nav.busy || task.value?.active || task.value?.queued || state.value?.active_line != null)
      if (disposed || projectLocked.value || !validExecution
        || dirty.value || props.suspended || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value) {
        if (!disposed) submissionError.value = '项目运行状态已变化，当前无法提交；请刷新或保存修改后重新确认'
        needsReconfirmation = true; retainDialog = true; return false
      }
      const currentLines = state.value?.lines ?? []
      if (dubbingSynthesisSignature(currentLines, confirmedPlan.ids, state.value?.synthesis_slots, !appending) !== signature) {
        submissionError.value = '句子内容、配音参数或配音槽已变化，请刷新并重新确认'
        needsReconfirmation = true; retainDialog = true; return false
      }
      if (!expected) {
        submissionError.value = '配音确认信息尚未更新，请刷新并重新确认'
        needsReconfirmation = true; retainDialog = true; return false
      }
      const result = await request('workbench/synthesize', 'POST', {
        line_ids: confirmedPlan.ids, takes: confirmedPlan.takes, force: true, expected,
        ...(appending ? { append_to_job_id: appendJobId } : {}),
      })
      if (!['started', 'queued'].includes(result.status) || !Number.isInteger(result.job_id) || result.job_id < 1
        || result.status === 'queued' && !(result.request_id || result.queue_id)
        || appending && (result.status !== 'queued' || result.job_id !== appendJobId)) throw new Error('任务回执不完整')
      completed = true
      if (!disposed) {
        // 回执已确认入队，立刻显示轻量队列；详细排布和导航更新不阻塞弹窗关闭。
        const previous = result.status === 'queued' && task.value?.job_id === result.job_id ? task.value : null
        const rows = (result.lines ?? confirmedPlan.ids.map(id => ({ id, take: confirmedPlan.takes[id], status: 'queued' })))
          .map((line: any) => ({ ...line, status: line.status || 'queued' }))
        const requestId = String(result.request_id || result.queue_id || '')
        const requests = acceptedSubmission.value?.jobId === result.job_id ? [...acceptedSubmission.value.requests] : []
        if (requestId) requests.push({ requestId, lines: rows })
        acceptedSubmission.value = { uid: props.uid, jobId: result.job_id, requests }
        const queuedRows = new Map((previous?.lines ?? []).map((line: any) => [line.id, line]))
        for (const line of rows) queuedRows.set(line.id, line)
        serverState.value = { ...serverState.value,
          synthesis_slots: dubbingSlots(currentLines, [...(state.value?.synthesis_slots ?? []), ...rows.map((line: any) => line.take)]),
          synthesis_reserved_takes: mergeDubbingReservations(state.value?.synthesis_reserved_takes, rows),
          synthesis_task: previous ? { ...previous, lines: [...queuedRows.values()] } : {
            job_id: result.job_id, active: true, queued: true, running: false, lines: rows,
          } }
        if (result.status === 'started') nav.stepStarted(props.uid, 'tts', result)
        const added = rows.filter((line: any) => !line.already_queued).length
        message.success(result.status === 'queued'
          ? added ? `已加入 ${added} 句，当前请求完成后优先合成；生成到${confirmedPlan.destination}` : '这些句子已在待配音列表，无需重复添加'
          : `已提交 ${confirmedPlan.total} 句，生成到${confirmedPlan.destination}；成功后自动选用，可在顶部暂停或停止`)
        void Promise.allSettled([load(), nav.poll()])
      }
    } catch (error: any) {
      if (!disposed) {
        if (error.status >= 400 && error.status < 500) {
          submissionError.value = error.message || '任务未提交，请处理后重试'
          needsReconfirmation = ['TTS_CONFIRMATION_CHANGED', 'TTS_QUEUE_TARGET_CONFLICT', 'TTS_QUEUE_CLOSED', 'LINE_UPDATE_QUEUE_CLOSED'].includes(error.code)
          retainDialog = true
          return false
        } else {
          submissionUncertain.value = true
          message.warning('尚未确认任务是否已被接收；正在重新读取状态，请勿重复提交')
        }
        void load()
      }
    } finally { sending.value = false; if (!replacedDialog) { submitting.value = retainDialog; syncButtons() } }
  }
  const openConfirmation = plan.value.overwrite ? dialog.warning : dialog.info
  const confirmation = openConfirmation({ title: context?.title ?? (appending ? '加入待配音列表' : plan.value.all ? '生成全部配音' : plan.value.total === 1 ? '生成本句配音' : '生成勾选配音'),
    type: plan.value.overwrite ? 'warning' : 'info',
    style: 'width: 620px; max-width: calc(100vw - 32px)',
    content: () => h(DubbingSynthesisConfirm, { plan: plan.value, slots,
      scopeLabel: context?.scopeLabel, note: context?.note,
      sentenceText: plan.value.total === 1 ? lines.find((line: any) => line.id === plan.value.ids[0])?.text : undefined,
      submitting: sending.value, submissionError: submissionError.value, appending,
      'onUpdate:target': (value: DubbingSynthesisTarget) => {
        if (sending.value) return
        target.value = value
        confirmation.type = plan.value.overwrite ? 'warning' : 'info'
        syncButtons()
      } }),
    maskClosable: false, closeOnEsc: false,
    positiveButtonProps: { disabled: !!plan.value.invalidReason },
    positiveText: buttonText(), negativeText: '取消', onPositiveClick: execute,
    onNegativeClick: () => { if (sending.value) return false; submitting.value = false },
    onClose: () => { if (sending.value) return false; submitting.value = false },
  })
}
function regenerateAll() { if (canRegenerateAll.value) return synthesize((state.value?.lines ?? []).map((line: any) => line.id)) }
function regenerateConflicts() {
  if (!canRegenerateConflicts.value) return
  const retryNote = state.value?.line_settings_supported
    ? '生成后先重新排布，必要时再按已保存的超长重试因子缩时。'
    : '生成后重新排布；当前服务的语速由服务端管理，仍放不下时会保留音频并提示调整。'
  return synthesize([...conflictIds.value], { title: '为冲突句生成配音', scopeLabel: '冲突句',
    target: { mode: 'current' }, note: `仅处理当前排布仍放不下的配音句，保留逐句精调参数；默认覆盖各句当前槽，也可改选新槽保留旧声音。${retryNote}` })
}
defineExpose({ regenerateAll, canRegenerateAll })
async function markSatisfied(ids: number[], confirmed: boolean) {
  if (runDisabled.value) return
  if (observing.value && !(await canEdit()) || runDisabled.value) return
  editing.value = true
  try {
    const data = await request('workbench/confirm', 'POST', {line_ids: ids, confirmed})
    message.success(`已${confirmed ? '标记' : '取消'} ${data.updated} 个版本${data.skipped ? `，跳过 ${data.skipped} 个无有效配音的句子` : ''}`)
    await load()
  } catch (error: any) { message.error(error.message || '满意标记保存失败') }
  finally { editing.value = false }
}
function closeSettings() {
  if (lineDirty.value || gainDirty.value) { message.warning('请先保存本句修改，或点击「放弃修改」'); return }
  settingsLine.value = null
}
function discardSettings() { lineDirty.value = false; gainDirty.value = false; settingsLine.value = null }
</script>

<template>
  <section class="dubbing-panel" :style="panelStyle">
    <n-alert v-if="projectLocked" type="info" :show-icon="false" class="section">{{ projectLockReason }}；播放、定位和查看字幕仍可用。</n-alert>
    <nav class="workspace-tabs" aria-label="字幕与配音工作区">
      <n-button :type="activePane === 'editor' ? 'primary' : 'default'" :secondary="activePane === 'editor'" :aria-pressed="activePane === 'editor'" @click="changePane('editor')">校对与配音</n-button>
      <n-button :type="activePane === 'subtitles' ? 'primary' : 'default'" :secondary="activePane === 'subtitles'" :aria-pressed="activePane === 'subtitles'" @click="changePane('subtitles')">字幕工作区<span v-if="subtitleDirty"> · 未保存</span></n-button>
      <n-text depth="3" class="hint">字幕样式、效果预览与文件导出集中在字幕工作区</n-text>
    </nav>
    <n-alert v-if="loadError" type="error" class="section">{{ loadError }} <n-button size="tiny" @click="load()">重试</n-button></n-alert>
    <n-alert v-if="submissionUncertain" type="warning" class="section" :show-icon="false">提交结果尚未确认，正在重新读取任务状态；请勿重复生成。<n-button size="tiny" @click="load()">重新核对</n-button></n-alert>
    <n-alert v-if="state?.task_error" type="error" class="section">{{ state.task_error }}</n-alert>
    <DependencyNotice :uid="uid" :items="state?.edit_blockers ?? []" />
    <n-alert v-if="state?.orphaned_edits?.length" type="warning" class="section">上游已删除这些句子，旧修改未应用：{{ state.orphaned_edits.join('、') }}。<n-button size="tiny" :disabled="actionsDisabled" @click="discardOrphans">清除这些失效记录</n-button></n-alert>
    <div v-show="activePane === 'editor'">
    <n-card size="small" class="section">
      <TtsServiceControl :uid="uid" :disabled="paramsDirty || lineDirty" :revision="serviceRevision" />
      <n-collapse id="dubbing-parameters" v-model:expanded-names="parametersOpen">
        <n-collapse-item name="parameters" title="配音参数 · 步骤设置" display-directive="show">
          <template #header-extra><n-text v-if="paramsDirty" type="warning">有未保存的修改</n-text></template>
          <ProjectParameterForm :uid="uid" step-key="tts" :disabled="projectLocked || busy || settingsLine !== null"
            @dirty-change="paramsDirty = $event" @saved="parametersSaved" />
        </n-collapse-item>
      </n-collapse>
    </n-card>
    <n-alert v-if="paramsDirty || lineDirty" type="warning" class="section" :show-icon="false">请保存参数后再合成。</n-alert>
    <n-alert v-if="state?.placement_pending" type="info" class="section" :show-icon="false">正在按新声源与配音槽更新邻句排布，播放时间将在保存完成后更新。</n-alert>
    <n-alert v-else-if="state?.timing" :type="state.timing.unresolved && !task?.running ? 'warning' : 'info'" class="section" :show-icon="false" aria-label="配音时间排布状态">
      <template v-if="state.timing.enabled">{{ task?.running ? '预览排布 · 本批处理中' : '自适应排布' }} · 已移动 {{ state.timing.shifted ?? 0 }} 句<template v-if="state.timing.manual"> · 人工调整 {{ state.timing.manual }} 句</template><template v-if="state.timing.unresolved"> · {{ state.timing.unresolved }} 句暂未容纳<template v-if="task?.running">，本批完成前还会重新核对。</template><template v-else>，请检查排布设置或重新合成。</template></template><template v-else-if="state.timing.missing"> · 还有 {{ state.timing.missing }} 句等待有效音频。</template><template v-else> · 当前声音可完整容纳。</template></template>
      <template v-else-if="state.timing.manual">自动排布已关闭 · 保留 {{ state.timing.manual }} 句人工位置，其他句子按源时间播放。<template v-if="state.timing.unresolved">仍有 {{ state.timing.unresolved }} 句放不下，请检查人工位置或重新合成。</template><template v-else>重叠检查仍会执行。</template></template>
      <template v-else>自动排布已关闭，按源时间播放。重叠提示仍会保留。</template>
      <n-space align="center" size="small" class="placement-actions">
        <n-button size="small" type="primary" secondary :loading="refreshingPlacement" :disabled="!canRefreshPlacement" title="按已保存设置重新核对全片局部排布；不合成、不新建槽、不清除人工位置" @click="refreshPlacement">重新排布（不改声音）</n-button>
        <n-button v-if="conflictIds.length" size="small" quaternary :disabled="!canRegenerateConflicts" title="重新生成冲突句的声音；先选择范围与目标槽，确认后才调用配音服务" @click="regenerateConflicts">重新生成冲突句…</n-button>
        <n-button size="small" quaternary :disabled="projectLocked || busy" @click="parametersOpen = ['parameters']">调整排布设置</n-button>
      </n-space>
      <n-text v-if="task?.running && state.timing.enabled" depth="3" class="hint note">生成期间根据已有声音预览位置，不据此提前缩时。本批常规合成结束后先检查完整原速排布，仍放不下才按配置因子缩时；每次返回后重新排布。</n-text>
      <n-text v-if="state.timing.enabled" depth="3" class="hint note">重新排布先在每句最大位移内协调完整原速声音，不跨人工位置、原声、静音或缺音频障碍；仍不足时分析局部窗口。原文、译文与配音同步，选槽和满意标记保留。同输入得到相同排布是正常的。</n-text>
      <n-text v-if="state.timing.unresolved" depth="3" class="hint note">重排不改变声音长度。仍放不下时，可在「文字与时间」调整位置，或重新生成冲突句；重新生成会单独确认范围与目标槽。</n-text>
    </n-alert>
    <n-card v-if="state && !state.lines.length" size="small" class="section"><n-empty description="语义整理完成后即可校对原文与时间；译文就绪后可编辑双语并按需配音" /></n-card>
    <div v-else-if="state" class="workbench-grid section">
      <div class="preview-column">
        <n-card size="small" title="快速校对试听">
          <div class="subtitle-controls">
            <span>字幕</span>
            <n-select v-model:value="subtitleMode" :options="subtitleOptions" size="small" aria-label="字幕预览类型" style="width:150px" />
            <n-button size="small" secondary @click="changePane('subtitles')">调整字幕样式与导出</n-button>
            <span v-if="subtitleMode !== 'off'" class="hint" :title="previewExported ? '预览导出文件的文字、时间与双语顺序' : '原文和译文共用当前配音排布时间；源稿时间保留，无需先导出字幕'">{{ previewExported ? '导出文件' : '当前文字 · 无需导出' }}</span>
          </div>
          <n-text v-if="subtitleError" type="error" class="hint note">{{ subtitleError }}</n-text>
          <n-alert v-if="playbackError" type="warning" class="playback-error" title="试听未能播放">
            {{ playbackError }}
            <n-text depth="3" class="hint note">重试仅加载和播放媒体，不会重新生成配音，也不会更改声源与音量。</n-text>
          </n-alert>
          <div v-if="state.video.found && !playbackDisabled" class="video-shell">
            <video v-release-media :key="state.video.url" ref="videoEl"
              preload="metadata" :src="state.video.url" class="video"
              @loadedmetadata="updateVideoDuration" @durationchange="updateVideoDuration">
              <track v-if="subtitlePreview" :key="subtitlePreview" :src="subtitlePreview" kind="subtitles" label="字幕预览" srclang="und" default @load="($event.target as HTMLTrackElement).track.mode = 'showing'" />
            </video>
            <VideoControls :media="videoEl" :chapters="previewChapters" :duration="videoDuration" :audio-control="monitorAudio" @audio-change="setMonitorAudio" />
          </div>
          <div v-else class="no-video">{{ playbackDisabled ? '正在更新预览媒体，完成后自动恢复' : '视频暂不可用，仍可试听音频' }}</div>
          <div class="transport">
            <n-button size="small" secondary type="primary" :disabled="playbackDisabled" @click="transportAction">{{ transportLabel }}</n-button>
            <n-button v-if="followMode === 'synthesis'" size="small" quaternary :disabled="playbackDisabled" title="暂停自动跟随，改为手动连续播放" @click="manualPlayAll">连续播放</n-button>
            <span class="time-display">{{ fmt(time) }}</span>
            <n-checkbox v-model:checked="solo" size="small" title="只改变当前试听；人声与背景的成片比例仍由下方设置决定。">仅听人声</n-checkbox>
          </div>
          <div v-if="followMode === 'synthesis'" class="follow-state" role="status" aria-live="polite"><span :title="followHint">{{ followHint }}</span></div>
          <div class="mix-controls">
            <label>人声<n-slider :value="volDub" :min="0" :max="1" :step="0.05" :disabled="mixControlsLocked" :title="mixLockReason || undefined" aria-label="人声音量"
              @update:value="mixEdits.change('voice', $event)" @dragend="mixEdits.save" @keyup="mixEdits.save" /><output>{{ Math.round(volDub * 100) }}%</output></label>
            <label>背景<n-slider :value="volBg" :min="0" :max="1" :step="0.05" :disabled="mixControlsLocked" :title="mixLockReason || undefined" aria-label="背景音量"
              @update:value="mixEdits.change('background', $event)" @dragend="mixEdits.save" @keyup="mixEdits.save" /><output>{{ Math.round(volBg * 100) }}%</output></label>
          </div>
          <div class="mix-save-state" aria-live="polite">
            <template v-if="mixEdits.error.value">
              <n-text type="warning" class="hint" :title="mixEdits.error.value">音量未保存，试听已生效：{{ mixEdits.error.value }}</n-text>
              <n-button size="tiny" quaternary :loading="mixEdits.saving.value || checkingEditability" :disabled="mixEdits.saving.value || checkingEditability || (mixPathsLocked && !projectLocked) || nav.archived || editing || suspended" title="先重新核对这两个成片音量是否可修改，再保存当前试听值。" @click="mixEdits.save">重试保存</n-button>
            </template>
            <n-text v-else depth="3" class="hint" :title="state.vocal?.found ? '音频跟随视频时间轴。' : '试听原声暂用完整音轨；成片使用分离人声，缺失时会提示。'">{{ mixEdits.pending.value || mixEdits.saving.value ? '正在保存音量，试听已生效…' : '音量已自动保存到项目；用于后续配音成片，版本单独设置优先。' }}</n-text>
          </div>
          <n-text depth="3" class="hint note">播放器音量和“仅听人声”只影响本机试听。此处不模拟归一化与背景避让；最终声音请在合成成片中生成试片核对。</n-text>
          <div v-if="selLine" class="line-detail">
            <div class="line-heading"><n-text depth="3" :title="lineTimeTitle(selLine)">第 {{ selLine.id }} 句 · 当前播放 {{ fmt(playbackTime(selLine).start) }} — {{ fmt(playbackTime(selLine).end) }}</n-text>
              <span class="status" :class="statusTone(selLine)" :title="isSilent(selLine) ? silenceHint(selLine) : errors[selLine.id]">
                <svg v-if="isSilent(selLine) && !isSynthesizing(selLine)" class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                {{ label(selLine) }}
              </span></div>
            <div class="transcript-pair">
              <div v-if="selLine.source_text" class="transcript-row"><span class="transcript-label">原文</span><p class="source-text">{{ selLine.source_text }}</p></div>
              <div class="transcript-row"><span class="transcript-label">译文</span><p class="line-text">{{ selLine.text || '尚无译文，可先校对原文和时间' }}</p></div>
            </div>
            <div v-if="isSilent(selLine) && !hasPendingDubbingChanges(selLine)" class="silence-detail">
              <svg class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
              <span>{{ silenceHint(selLine) }}</span>
            </div>
            <div class="line-playback-summary">
              <n-text depth="3" class="hint">源稿区间 {{ selLine.duration }} 秒<template v-if="selLine.translation_available"> · {{ selLine.wav.exists ? '实际' : '预计' }}配音 {{ selLine.wav.seconds ?? selLine.est_dur }} 秒</template></n-text>
              <DubbingAdjustmentBadges :line="selLine" />
              <n-text v-if="placementHint(selLine)" depth="3" class="hint placement-summary">{{ placementHint(selLine) }}</n-text>
              <n-text v-if="placementWindowHint(selLine)" depth="3" class="hint placement-summary">{{ placementWindowHint(selLine) }}</n-text>
            </div>
            <p v-if="timing[selLine.id]" class="timing-detail">{{ timingHint(selLine) }}</p>
            <n-alert v-if="selLine.unready_inputs?.length" type="warning" :show-icon="false" class="section">{{ selLine.unready_inputs.map((i: any) => `${i.name}：${i.reason}`).join('；') }}</n-alert>
            <div v-if="hasPendingDubbingChanges(selLine)" class="voice-settings-notice" role="status" aria-label="当前修改尚未应用到配音">
              <svg class="settings-notice-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10" cy="10" r="8"/><path d="M10 5.5v5m0 3v.5" stroke-linecap="round"/></svg>
              <div>
                <strong>当前修改尚未应用到配音</strong>
                <small>{{ pendingDubbingChangeReason(selLine) }}</small>
                <p v-if="selLine.wav.stale">旧版本与当前内容或时间不一致，暂不参与试听和成片；{{ isSynthesizing(selLine) || isQueued(selLine)?'本句正在合成或等待中，完成后应用当前修改。':'请重新生成本句配音。' }}</p>
                <p v-else-if="isSynthesizing(selLine) || isQueued(selLine)">本句正在合成或等待中，更新完成前仍使用现有声音。</p>
                <p v-else>当前试听和成片仍使用配音槽 {{ selLine.selection.take }} 中的已有声音。重新生成本句配音后，才会应用当前修改。</p>
              </div>
            </div>
            <p v-if="selLine.wav.reading_caption_independent" class="hint note">本句使用自定义朗读文本；单独修改字幕不会改变朗读内容。如需同步修改声音，请调整朗读文本后重新生成。</p>
            <p v-if="assemblyReading === 'ready' && assemblyNotices.has(selLine.id)" class="assembly-change-note">{{ assemblyNotices.get(selLine.id)?.label }} · 对照「{{ assemblyComparison?.baseline?.name }}」：{{ assemblyNotices.get(selLine.id)?.reason }}</p>
            <n-space class="detail-actions" size="small">
              <n-button size="small" :disabled="actionsDisabled || selectionEdits.saving.value || !!state.edit_blockers?.length" @click="editLine(selLine)">编辑文字与时间</n-button>
              <n-button size="small" type="primary" :disabled="synthesisDisabled || queuedSynthesisIds.has(selLine.id) || !selLine.translation_available || selLine.edit_stale || !!selLine.unready_inputs?.length" :title="canAppendSynthesis ? '确认目标槽后加入当前任务，不中断正在合成的句子' : state.line_config_execution?.active ? state.line_config_execution.generation_reason : '默认修正当前槽；需要对比不同声音时可选择新槽'" @click="synthesize([selLine.id])">{{ synthesisButtonLabel(selLine.id) }}</n-button>
              <n-button size="small" secondary :disabled="runDisabled || !selLine.wav.exists" @click="markSatisfied([selLine.id], !selLine.confirmed)">{{ selLine.confirmed ? '取消满意' : '标记满意' }}</n-button>
            </n-space>
            <n-text v-if="selLine.wav.line_settings_mode === 'project'" depth="3" class="hint note">此槽按统一项目参数生成；本句精调设置仍保留。</n-text>
          </div>
        </n-card>
      </div>
      <n-card size="small" class="sentence-card">
        <div class="list-heading">
          <div class="list-title"><strong>句子列表</strong><span class="total-count">{{ state.counts.total }} 句</span></div>
          <div class="status-counts">
            <span class="status ready" title="已有有效配音，包含已满意的句子">已合成 {{ state.counts.synthesized }}</span>
            <span class="status confirmed" title="已试听并标记满意的当前配音版本">已满意 {{ state.counts.confirmed }}</span>
          </div>
        </div>
        <div class="follow-controls">
          <span>跟随模式</span>
          <n-radio-group v-model:value="followMode" size="small" aria-label="跟随模式">
            <n-radio-button value="off" title="手动选择句子，不自动跟随">关闭</n-radio-button>
            <n-radio-button value="synthesis" title="试听当前任务最新成功生成的句子，播完自动暂停，等待下一句完成">跟随合成</n-radio-button>
            <n-radio-button value="preview" title="跟随预览播放的句子，选中并滚动列表，不改变视频位置">跟随预览</n-radio-button>
          </n-radio-group>
        </div>
        <div v-if="activeSynthesisLine !== null" class="synthesis-current" role="status">
          <span>第 {{ activeSynthesisLine }} 句 · {{ synthesisStatus(activeSynthesisLine) }}</span>
          <n-button size="tiny" quaternary title="暂停自动跟随，选中正在合成的句子并定位视频；保持播放或暂停状态" @click="locateManually(activeSynthesisLine)">定位到本句</n-button>
        </div>
        <DubbingAssemblyLink :uid="uid" :revision="assemblyRevision" :selected="checked" :disabled="dirty || props.suspended || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value"
          @comparison="applyAssemblyComparison" @reading-change="assemblyReading = $event" @filter="changeStateFilter('assembly')" />
        <n-input v-model:value="filter" size="small" placeholder="搜索原文、译文或编号" clearable>
          <template #prefix><svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4" stroke-linecap="round"/></svg></template>
        </n-input>
        <div class="list-filters">
          <n-checkbox :checked="allChecked" :indeterminate="someChecked && !allChecked" :disabled="selectionDisabled" @update:checked="checkShown">{{ filter || stateFilter !== 'all' ? '全选当前结果' : '全选' }}</n-checkbox>
          <div class="filter-options" aria-label="句子筛选">
            <button v-for="item in filters" :key="item.value" type="button" class="filter-chip"
              :class="{ 'silent-filter': item.value === 'silent', 'settings-filter': item.value === 'settings' }" :aria-pressed="stateFilter === item.value" @click="changeStateFilter(item.value)">
              {{ item.label }}<span>{{ item.count }}</span>
            </button>
          </div>
        </div>
        <div class="batch-actions">
          <n-button size="small" type="primary" :disabled="synthesisDisabled || !checkedSynthesisIds.length || checkedSynthesisBlocked" :title="canAppendSynthesis ? '加入尚未排队的勾选句；已在合成或等待中的句子会跳过，不中断当前句' : '使用已保存参数生成勾选句；确认目标槽后执行，可覆盖修正或新建用于对比。'" @click="synthesize([...checkedSynthesisIds])">{{ canAppendSynthesis ? '加入待配音' : '生成勾选配音' }}（{{ checkedSynthesisIds.length }} 句）…</n-button>
          <n-button size="small" :disabled="runDisabled || !checked.length" title="标记所选句子的当前配音版本；不控制是否进入成片" @click="markSatisfied([...checked], true)">标记满意</n-button>
          <n-button size="tiny" quaternary :disabled="runDisabled || !checked.length" @click="markSatisfied([...checked], false)">取消满意</n-button>
          <div class="selection-tools">
            <span class="selection-count">已勾选 {{ checked.length }} 句<span v-if="hiddenSelectionCount(checked, shownLines.map(line => line.id))">（含隐藏 {{ hiddenSelectionCount(checked, shownLines.map(line => line.id)) }} 句）</span></span>
            <n-button size="tiny" quaternary :disabled="selectionDisabled" @click="invertShown">反选当前列表</n-button>
            <n-button v-if="checked.length && !selectionDisabled" size="tiny" quaternary @click="checked = []">清除全部勾选</n-button>
          </div>
        </div>
        <div ref="sentenceList" class="sentence-list" tabindex="0" aria-label="配音句子列表" @keydown="listKey">
          <div v-for="line in shownLines" :key="line.id" :data-id="line.id" class="sentence"
            :aria-current="line.id === sel ? 'true' : undefined"
            :class="{selected: line.id === sel, checked: checked.includes(line.id), active: cursor?.lineId === line.id, waiting: cursor?.lineId === line.id && cursor.phase !== 'playing', generating: isSynthesizing(line), silent: isSilent(line)}">
            <input type="checkbox" :checked="checked.includes(line.id)" :disabled="selectionDisabled" :aria-label="`选择第 ${line.id} 句`" @click="check(line.id, $event)" />
            <n-button size="tiny" quaternary class="sentence-play" :disabled="playbackDisabled || (!line.wav.exists && !state.video.found && !state.vocal?.found && !state.original?.found)"
              :aria-label="`试听第 ${line.id} 句`" :title="voiceFor(line, choices) === 'original' ? '试听本句原声（含前后缓冲）' : voiceFor(line, choices) === 'silent' ? '试听本句（人声静音）' : '试听本句完整配音，结束后留一点缓冲'" @click="listen(line)">▶</n-button>
            <button type="button" class="sentence-content" :disabled="playbackDisabled" :aria-label="`定位第 ${line.id} 句`" @click="select(line)">
              <span class="sentence-meta">
                <span class="sentence-meta-info">
                  <span class="sentence-number">#{{ line.id }}</span><span class="sentence-time" :title="lineTimeTitle(line)">{{ fmt(playbackTime(line).start) }} – {{ fmt(playbackTime(line).end) }}</span>
                  <span v-if="line.edited" class="edit-label" :class="{stale: line.edit_stale}">{{ line.edit_stale ? '修改待核对' : '已校对' }}</span>
                  <span class="status" :class="statusTone(line)" :title="isSilent(line) ? silenceHint(line) : errors[line.id]">
                    <svg v-if="isSilent(line) && !isSynthesizing(line)" class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    {{ label(line) }}
                  </span>
                  <span v-if="hasPendingDubbingChanges(line)" class="status warning settings-unapplied" :title="pendingDubbingChangeReason(line)">配音待更新</span>
                  <span v-if="assemblyReading === 'ready' && assemblyNotices.has(line.id)" class="assembly-pending-label" :title="assemblyNotices.get(line.id)?.reason + '；对照：' + assemblyComparison?.baseline?.name">{{ assemblyNotices.get(line.id)?.label }}</span>
                  <DubbingAdjustmentBadges :line="line" />
                  <span v-if="pronunciationText(line)" class="pronunciation-label" :title="`已保存自定义朗读，再次合成时使用，字幕不变：${pronunciationText(line)}`">自定义朗读</span>
                  <span v-if="configUpdateLabel(line)" class="config-update-label" :class="{failed:line.config_execution?.state==='failed'}" :title="line.config_execution?.error || line.config_execution?.message">{{ configUpdateLabel(line) }}</span>
                  <span v-if="line.audio_gain_state?.active" class="audio-gain-label" title="本句后期音量；不改变原始配音文件">音量 {{ line.audio_gain_db > 0 ? '+' : '' }}{{ line.audio_gain_db }} dB</span>
                </span>
                <span class="sentence-focus" :class="sentenceFocus(line.id, sel, cursor).tone"
                  :aria-hidden="!sentenceFocus(line.id, sel, cursor).label" :data-visible="!!sentenceFocus(line.id, sel, cursor).label">{{ sentenceFocus(line.id, sel, cursor).label }}</span>
              </span>
              <span class="sentence-transcripts">
                <span v-if="line.source_text" class="sentence-text original-text" :class="{primary: voiceFor(line, choices) === 'original'}" :title="`原文：${line.source_text}`">{{ line.source_text }}</span>
                <span class="sentence-text translated-text" :class="{primary: voiceFor(line, choices) === 'dub'}" :title="`译文：${line.text}`">{{ line.text || '尚无译文' }}</span>
              </span>
              <span v-if="lineProgress(line.id) !== null" class="synthesis-progress" role="progressbar"
                :aria-label="`第 ${line.id} 句合成进度`" :aria-valuenow="lineProgress(line.id)!" :aria-valuemin="0" :aria-valuemax="100"
                title="配音服务返回的阶段进度，可能分段更新">
                <span :style="{width: `${lineProgress(line.id)}%`}"></span>
              </span>
              <span v-if="timing[line.id]" class="timing-warning" :title="timingHint(line)">{{ timingLabel(line) }}</span>
              <span v-else-if="voiceFor(line, choices) === 'dub' && line.wav.seconds == null" class="timing-warning"
                title="音频时长尚无法确认，可先试听；正式合成会重新检查，无法读取时长时会保留已有成片并提示修复。">时长未知</span>
              <span v-if="errors[line.id]" class="error-text">{{ errors[line.id] }}</span>
            </button>
            <div class="voice-controls">
              <div class="voice-actions">
                <button type="button" class="voice-toggle" :class="{ 'silent-voice': isSilent(line) }" role="switch" :aria-checked="line.selection.source === 'dub'"
                  :aria-label="`第 ${line.id} 句使用配音`" :aria-busy="selectionEdits.isSaving(line.id)"
                  :disabled="actionsDisabled || selectionEdits.isSaving(line.id) || (line.selection.source === 'original' && !line.wav.exists)"
                  :title="selectionEdits.isSaving(line.id) ? '正在保存本句声源' : line.selection.source === 'dub' ? '点击切换为原声' : line.wav.exists ? '点击切换为配音' : '请先选择或生成配音'"
                  @click="chooseVoice(line, line.selection.source === 'dub' ? 'original' : 'dub')">
                  <span class="toggle-thumb" aria-hidden="true"></span><span>{{ line.selection.source === 'dub' ? '配音' : '原声' }}</span>
                </button>
                <n-button size="tiny" quaternary class="settings-button" :class="{customized: line.customized}"
                  :disabled="lineSettingsDisabled(line)" :aria-label="`第 ${line.id} 句参数`"
                  :title="!state.line_settings_supported ? '当前服务的音色参数由服务端管理；可调整本句后期音量' : line.customized ? `本句设置：${lineSettingsSummary(line)}` : '设置本句读音与声音；未覆盖的参数跟随项目设置，运行时可保存并更新当前槽'" @click="settingsLine = line.id">
                  <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14"/><circle cx="7" cy="5" r="2"/><circle cx="13" cy="10" r="2"/><circle cx="8" cy="15" r="2"/></svg>
                </n-button>
                <n-button size="tiny" quaternary class="settings-button" :disabled="actionsDisabled || selectionEdits.saving.value || !!state.edit_blockers?.length" :aria-label="`编辑第 ${line.id} 句文字与时间`" title="查看原始、自动与人工时间；调整播放排布或校对源稿" @click="editLine(line)">
                  <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m4 12-1 5 5-1L17 7l-4-4-9 9Zm7-7 4 4M3 17h14" stroke-linejoin="round"/></svg>
                </n-button>
              </div>
              <select class="take-select" :class="{ 'silent-voice': isSilent(line) }" :title="isSilent(line) ? silenceHint(line) : '选择试听和成片使用的配音槽；生成目标在生成窗口中选择'" :value="line.selection.take ?? (line.selection.source === 'original' ? 'none' : '')" :disabled="actionsDisabled || selectionEdits.isSaving(line.id) || !line.translation_available || line.edit_stale" :aria-label="`第 ${line.id} 句当前配音槽`" @change="chooseTake(line, $event)">
                <option v-if="line.selection.take === null && line.selection.source === 'original'" value="none" disabled>选择配音</option>
                <option v-for="take in line.versions" :key="take.id" :value="take.id" :disabled="!take.exists">槽 {{ take.id }}{{ take.confirmed ? ' ✓' : take.stale ? '（过期）' : '' }}</option>
                <option value="">本句人声静音</option>
              </select>
            </div>
            <div v-if="paramSummary(line) || isSilent(line)" class="sentence-settings">
              <span v-if="paramSummary(line)" class="parameter-summary" :title="paramSummary(line)"><ConfigurationSourceBadge :source="{kind:'line'}"/><span>{{ paramSummary(line) }}</span></span>
              <span v-if="isSilent(line)" class="empty-notice" :title="silenceHint(line)">{{ emptySlot(line) ? '空位 · 保留背景与画面' : '配音不可用 · 成片前需处理' }}</span>
            </div>
          </div>
          <n-empty v-if="!shownLines.length" :description="!filter && stateFilter === 'silent' ? '没有无声句子' : !filter && stateFilter === 'settings' ? '没有需要更新的配音' : !filter && stateFilter === 'assembly' ? '没有已识别的成片变化' : '没有匹配的句子'" class="list-empty" />
        </div>
        <details class="list-help"><summary>操作说明 <span>Shift 连选 · 点击文本定位 · ▶ 试听</span></summary>
          <p>勾选仅决定批量处理的句子，不决定成片是否包含它们；点击行只定位当前句，不等于勾选。筛选不会清除隐藏句子的勾选。</p>
          <p>单句、勾选、全部生成和冲突修复都先选择目标配音槽，默认覆盖当前槽。需要对比不同声音时可选择新槽，成功后自动选用，并可切回旧槽。覆盖的目标槽需重新确认满意。</p>
          <p>勾选决定配音合成范围；排布始终统一核对全片局部窗口，附近句子可能一起移动，但不会因此重做声音。重新排布保留人工位置，不改配音槽或满意标记。</p>
          <p>列表的槽选择决定试听和成片使用的音频，不是生成目标。「本句人声静音」只保留背景与画面。满意标记记录各槽质量，不决定是否进入成片。</p>
        </details>
      </n-card>
    </div>
    </div>
    <n-alert v-if="activePane === 'subtitles' && (paramsDirty || lineDirty || textDirty || editing)" type="warning" class="section" :show-icon="false">
      校对与配音中还有未保存的修改，请先返回保存或放弃，再编辑字幕设置。
    </n-alert>
    <SubtitleWorkspace v-if="subtitlesVisited" v-show="activePane === 'subtitles'" :uid="uid" :position="time" :duration="videoDuration"
      :active="activePane === 'subtitles'" :lines="state?.lines ?? []" :subtitle-state="state?.subtitles"
      :return-to-assembly="route.query.from === 'assemble'" :no-video="!state?.video?.found || playbackDisabled"
      :disabled="projectLocked || paramsDirty || lineDirty || textDirty || editing || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value"
      @dirty-change="subtitleDirty = $event" @refresh="subtitlesRefreshed" @preview="previewExport"
      @return-assembly="router.push(`/projects/${uid}/steps/assemble`)" />
    <SubtitleLineEditor v-if="textEditLine" :uid="uid" :line="textEditLine" :line-settings-supported="state?.line_settings_supported" :disabled="projectLocked || busy || nav.busy"
      @close="closeTextEditor" @saved="textSaved" @dirty-change="textDirty = $event" @edit-parameters="editLineParameters" />
    <n-drawer :show="settingsLine !== null" :width="680" style="max-width: 100vw" :mask-closable="!lineDirty && !gainDirty" @update:show="!$event && closeSettings()">
      <n-drawer-content :title="`第 ${settingsLine} 句 · 声音设置`" closable>
        <n-alert v-if="settingsAudioLine && !settingsAudioLine.audio_gain_state" type="info" :show-icon="false" class="section">当前后端尚未支持逐句调音与运行中更新。待本轮制作结束后重启后端，即可使用这些功能。</n-alert>
        <SentenceAudioGain v-if="settingsAudioLine?.audio_gain_state" :uid="uid" :line="settingsAudioLine" :disabled="projectLocked || isPathLocked('assemble.audio_line_gains') || !!suspended" @dirty-change="gainDirty=$event" @saved="parametersSaved" />
        <n-text v-if="!state?.line_settings_supported" depth="3" class="hint note">当前服务的音色参数在服务端管理；本句后期音量仍可单独调整。</n-text>
        <ProjectParameterForm v-if="settingsLine !== null && state?.line_settings_supported" :key="settingsLine" :uid="uid" step-key="tts" :line-id="settingsLine" :execution-state="settingsAudioLine?.config_execution" :disabled="lineSettingsLocked"
          @dirty-change="lineDirty = $event" @saved="parametersSaved">
          <template #field-reference="{ field, value }">
            <DubbingDurationReference v-if="field.path === 'tts.indextts25.duration_factor'" :line="settingsAudioLine" :configured-factor="value" />
          </template>
          <template #actions="{ save, saveLabel, updatesRunningVoice, saving, loading, locked, dirty, hasOverrides, resetAll }">
            <Teleport :to="settingsFooter || 'body'" :disabled="!settingsFooter">
              <n-space align="center" justify="end">
                <n-button v-if="hasOverrides" quaternary :disabled="locked || loading" @click="resetAll">填入步骤设置（待保存）</n-button>
                <n-text v-if="dirty" type="warning" class="hint">有未保存的修改</n-text>
                <n-button v-if="dirty || gainDirty" quaternary :disabled="saving" @click="discardSettings">放弃修改</n-button>
                <n-button v-if="updatesRunningVoice" quaternary :disabled="!dirty || locked || loading" @click="save('save')">仅保存设置</n-button>
                <n-button type="primary" :loading="saving" :disabled="!dirty || locked || loading" @click="save()">{{ saveLabel }}</n-button>
              </n-space>
            </Teleport>
          </template>
        </ProjectParameterForm>
        <template #footer><div ref="settingsFooter" class="line-settings-footer" /></template>
      </n-drawer-content>
    </n-drawer>
  </section>
</template>

<style scoped>
.dubbing-panel :deep(.n-card) { border-radius: 12px; }
.workspace-tabs { display:flex; align-items:center; flex-wrap:wrap; gap:10px; padding:14px 0 4px; }
.section { margin-top: 16px; }
.placement-actions { margin-top:10px; }
.playback-error { margin-bottom:12px; }
.workbench-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(430px, 1.2fr); align-items: start; gap: 20px; }
.preview-column, .sentence-card { min-width: 0; }
.video { display: block; width: 100%; max-height: 38vh; background: #000; border-radius: 8px; }
.video-shell { position: relative; min-width: 0; }
.no-video { padding: 32px 16px; text-align: center; color: var(--dub-muted); background: rgba(128,128,128,.06); border: 1px dashed var(--dub-border); border-radius: 8px; font-size: 12px; }
.transport, .mix-controls, .list-heading, .list-filters, .batch-actions, .line-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.transport { margin-top: 12px; }
.transport :deep(.n-checkbox) { margin-left: auto; font-size: 12px; }
.time-display { padding: 2px 7px; border-radius: 4px; color: var(--dub-muted); background: rgba(128,128,128,.07); font: 12px ui-monospace, Consolas, monospace; font-variant-numeric: tabular-nums; }
.mix-controls { gap: 16px; margin: 16px 0 8px; }
.mix-controls label { display: flex; align-items: center; gap: 8px; font-size: 12px; flex: 1; min-width: 160px; }
.mix-controls :deep(.n-slider) { flex: 1; min-width: 60px; }
.mix-controls output { min-width: 34px; text-align: right; color: var(--dub-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.mix-save-state { display: flex; align-items: center; gap: 8px; min-height: 24px; margin-top: 8px; }
.hint { font-size: 12px; }
.config-update-label,.audio-gain-label { border-radius:4px;padding:1px 5px;font-size:10px;line-height:1.7;white-space:nowrap; }
.config-update-label { color:#e5b970;background:rgba(229,185,112,.12); }
.config-update-label.failed { color:#f29a98;background:rgba(242,154,152,.12); }
.audio-gain-label { color:#9cc8ec;background:rgba(156,200,236,.12); }
.note { display: block; margin-top: 10px; line-height: 1.6; }
.line-detail { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--dub-border); }
.line-heading { justify-content: space-between; font-size: 12px; }
.line-heading > .n-text { font-variant-numeric: tabular-nums; }
.line-playback-summary { display: grid; justify-items: start; gap: 7px; }
.placement-summary { line-height: 1.6; }
.transcript-pair { display: grid; gap: 10px; margin: 16px 0 12px; }
.transcript-row { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: baseline; gap: 10px; }
.transcript-label { color: var(--dub-muted); font-size: 11px; }
.source-text { color: var(--dub-muted); font-size: 12px; margin: 0; line-height: 1.6; overflow-wrap: anywhere; }
.line-text { font-size: 15px; line-height: 1.7; margin: 0; overflow-wrap: anywhere; }
.silence-detail { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; margin: 12px 0; color: var(--dub-warning); background: rgba(218,166,77,.09); border: 1px solid rgba(218,166,77,.22); border-radius: 7px; font-size: 12px; line-height: 1.7; }
.silence-detail .mute-icon { margin-top: 3px; }
.detail-actions { margin-top: 14px; }
.voice-settings-notice { display: flex; align-items: flex-start; gap: 9px; padding: 11px 12px; margin-top: 14px; border: 1px solid color-mix(in srgb, var(--dub-warning) 45%, transparent); border-radius: 7px; background: color-mix(in srgb, var(--dub-warning) 9%, var(--dub-surface)); color: var(--dub-warning); }
.settings-notice-icon { flex: 0 0 18px; width: 18px; height: 18px; margin-top: 1px; }
.voice-settings-notice strong { display: block; font-size: 13px; line-height: 1.6; font-weight: 600; }
.voice-settings-notice small { display: block; margin-top: 3px; font-size: 12px; line-height: 1.65; }
.voice-settings-notice p { margin: 4px 0 0; color: var(--dub-text); font-size: 12px; line-height: 1.65; }
.list-heading { justify-content: space-between; margin-bottom: 12px; }
.status-counts { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.status-counts .status { font-size: 11px; }
.list-title { display: flex; align-items: center; gap: 8px; }
.total-count { padding: 1px 7px; border: 1px solid var(--dub-border); border-radius: 5px; color: var(--dub-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.list-filters { justify-content: space-between; margin: 12px 0; font-size: 12px; gap: 8px 12px; }
.filter-options { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.filter-chip { display: inline-flex; align-items: center; gap: 5px; padding: 4px 7px; border: 1px solid transparent; border-radius: 5px; color: var(--dub-muted); background: transparent; font: inherit; font-size: 11px; cursor: pointer; }
.filter-chip > span { font-size: 10px; font-variant-numeric: tabular-nums; opacity: .85; }
.filter-chip:hover { background: rgba(128,128,128,.1); }
.filter-chip[aria-pressed=true] { color: var(--dub-primary); background: var(--df-accent-soft); border-color: #5278a5; }
.filter-chip.silent-filter, .filter-chip.settings-filter { color: var(--dub-warning); }
.filter-chip.silent-filter[aria-pressed=true], .filter-chip.settings-filter[aria-pressed=true] { color: var(--dub-warning); background: rgba(218,166,77,.12); border-color: rgba(218,166,77,.28); }
.filter-chip:focus-visible { outline: 2px solid var(--dub-info); outline-offset: 2px; }
.batch-actions { gap: 6px; margin-bottom: 12px; padding: 8px; border-radius: 7px; background: rgba(128,128,128,.05); border: 1px solid var(--dub-border); }
.selection-tools { display: flex; align-items: center; gap: 4px; margin-left: auto; }
.selection-count { color: var(--dub-muted); font-size: 11px; padding: 0 4px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.sentence-list { max-height: 66vh; overflow-y: auto; scrollbar-gutter: stable; scrollbar-width: thin; scrollbar-color: rgba(128,128,128,.45) transparent; padding: 3px; }
.sentence { display: grid; grid-template-columns: 16px 24px minmax(0, 1fr) 128px; gap: 5px 8px; align-items: center; padding: 10px 8px; margin-bottom: 6px; border: 1px solid transparent; border-left: 4px solid transparent; border-radius: 8px; background: rgba(128,128,128,.035); transition: background-color .15s, border-color .15s, box-shadow .15s; }
.sentence input[type=checkbox] { margin: 0; width: 14px; height: 14px; accent-color: var(--dub-primary); cursor: pointer; }
.sentence:hover { background: rgba(128,128,128,.085); }
.sentence.checked { border-color: color-mix(in srgb, var(--dub-primary) 38%, var(--dub-border)); }
.sentence.generating { border-left-color: var(--dub-info); background: color-mix(in srgb, var(--dub-info) 8%, var(--dub-surface)); }
.sentence.silent { background: rgba(218,166,77,.085); border-color: rgba(218,166,77,.2); border-left-color: var(--dub-warning); }
.sentence.silent:hover { background: rgba(218,166,77,.13); }
/* 当前句始终突出，不能被合成、静音或批量勾选的底色盖住。 */
.sentence.selected, .sentence.selected:hover { background: color-mix(in srgb, var(--dub-primary) 17%, var(--dub-surface)); border-color: color-mix(in srgb, var(--dub-primary) 60%, var(--dub-border)); border-left-color: var(--dub-primary); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--dub-primary) 16%, transparent); }
.sentence.active:not(.waiting), .sentence.active:not(.waiting):hover { background: color-mix(in srgb, var(--dub-playing) 15%, var(--dub-surface)); border-color: color-mix(in srgb, var(--dub-playing) 58%, var(--dub-border)); border-left-color: var(--dub-playing); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--dub-playing) 15%, transparent); }
.sentence.active.waiting:not(.selected) { border-left-color: var(--dub-muted); }
.sentence-play { border-radius: 5px; }
.sentence.active .sentence-play { color: var(--dub-playing); background: color-mix(in srgb, var(--dub-playing) 14%, transparent); }
.sentence.active.waiting .sentence-play { color: var(--dub-muted); background: rgba(128,128,128,.08); }
.sentence-settings { grid-column: 3 / -1; display: flex; align-items: center; gap: 7px; min-width: 0; font-size: 11px; }
.parameter-summary { display:flex; align-items:center; gap:6px; min-width:0; color:var(--dub-parameter); }
.parameter-summary>span:last-child { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.empty-notice { color: var(--dub-warning); margin-left: auto; flex-shrink: 0; }
.voice-controls { display: grid; gap: 5px; }
.voice-actions { display: flex; align-items: center; justify-content: space-between; gap: 3px; }
.voice-toggle { display: flex; align-items: center; justify-content: space-between; width: 68px; height: 24px; padding: 3px 8px 3px 4px; border: 1px solid rgba(128,128,128,.25); border-radius: 14px; color: inherit; font: inherit; font-size: 11px; background: rgba(128,128,128,.1); cursor: pointer; }
.toggle-thumb { width: 16px; height: 16px; border-radius: 50%; background: rgba(128,128,128,.65); flex-shrink: 0; }
.voice-toggle[aria-checked=true] { flex-direction: row-reverse; padding: 3px 4px 3px 8px; color: var(--dub-primary); border-color: rgba(54,173,106,.4); background: rgba(54,173,106,.12); }
.voice-toggle[aria-checked=true] .toggle-thumb { background: var(--dub-primary); }
.voice-toggle.silent-voice { color: var(--dub-warning); border-color: rgba(218,166,77,.35); background: rgba(218,166,77,.1); }
.voice-toggle.silent-voice .toggle-thumb { background: var(--dub-warning); }
.voice-toggle:focus-visible { outline: 2px solid var(--dub-info); outline-offset: 2px; }
.voice-toggle:disabled { cursor: default; opacity: .45; }
.voice-toggle[aria-busy=true] { opacity: .8; }
.voice-toggle[aria-busy=true] .toggle-thumb { border: 2px solid currentColor; border-top-color: transparent; background: transparent; box-sizing: border-box; animation: saving-spin .8s linear infinite; }
.settings-button { flex-shrink: 0; padding: 0 3px; }
@keyframes saving-spin { to { transform: rotate(360deg); } }
.take-select { width: 100%; font: inherit; font-size: 11px; padding: 4px 3px; color: inherit; border: 1px solid rgba(128,128,128,.2); border-radius: 5px; background: rgba(128,128,128,.04); }
.take-select option { color: inherit; background: var(--dub-surface); }
.take-select.silent-voice { color: var(--dub-warning); border-color: rgba(218,166,77,.35); }
.take-select:focus-visible { outline: 2px solid var(--dub-info); outline-offset: 2px; }
.sentence-content { min-width: 0; padding: 0; text-align: left; color: inherit; font: inherit; background: transparent; border: 0; cursor: pointer; }
.sentence-content:focus-visible { outline: 2px solid var(--dub-info); outline-offset: 2px; }
.sentence-meta { display: grid; grid-template-columns: minmax(0, 1fr) 58px; align-items: start; gap: 6px; min-height: 21px; font-size: 11px; margin-bottom: 5px; }
.sentence-meta-info { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; min-width: 0; }
.sentence-number { color: var(--dub-muted); font-weight: 600; }
.sentence.selected .sentence-number { color: var(--dub-primary); }
.sentence.active:not(.waiting) .sentence-number { color: var(--dub-playing); }
.edit-label { color: var(--dub-info); font-size:10px; }
.edit-label.stale { color: var(--dub-warning); }
.pronunciation-label { padding:1px 6px; border-radius:4px; font-size:10px; line-height:1.65; color:var(--dub-parameter); background:color-mix(in srgb, var(--dub-parameter) 12%, transparent); white-space:nowrap; }
.assembly-pending-label { padding:1px 6px; border-radius:4px; font-size:10px; line-height:1.65; color:var(--dub-info); background:color-mix(in srgb, var(--dub-info) 12%, transparent); white-space:nowrap; }
.assembly-change-note { color:var(--dub-info); font-size:12px; line-height:1.7; margin:12px 0; }
.sentence-time { color: var(--dub-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.sentence-focus { display: inline-flex; align-items: center; justify-content: center; width: 58px; height: 21px; border-radius: 5px; color: var(--dub-primary); background: color-mix(in srgb, var(--dub-primary) 16%, transparent); font-size: 10px; font-weight: 600; white-space: nowrap; }
.sentence-focus[data-visible=false] { visibility: hidden; }
.sentence-focus::before { content: ''; width: 4px; height: 4px; margin-right: 4px; border-radius: 50%; background: currentColor; }
.sentence-focus.playing { color: var(--dub-playing); background: color-mix(in srgb, var(--dub-playing) 18%, transparent); }
.sentence-focus.waiting { color: var(--dub-muted); background: color-mix(in srgb, var(--dub-muted) 12%, transparent); }
.synthesis-current { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; padding: 6px 8px; border-radius: 6px; background: rgba(99,179,237,.08); color: var(--dub-info); font-size: 12px; }
.synthesis-current { flex-wrap: wrap; }
.follow-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.follow-controls > span { color: var(--dub-muted); font-size: 12px; }
.follow-state { display: flex; align-items: center; min-height: 18px; margin-top: 8px; color: var(--dub-muted); font-size: 12px; }
.follow-state > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.subtitle-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; font-size: 12px; }
.timing-warning { display: block; margin-top: 4px; color: var(--dub-warning); font-size: 10px; }
.timing-detail { color: var(--dub-warning); font-size: 12px; line-height: 1.7; margin: 8px 0 0; }
.sentence-transcripts { display: grid; gap: 3px; }
.sentence-text { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; color: var(--dub-muted); font-size: 11px; line-height: 1.5; }
.sentence-text:not(.primary) { opacity: .85; }
.sentence-text.primary { color: var(--dub-text); font-size: 13px; font-weight: 500; }
.status { display: inline-flex; align-items: center; gap: 4px; padding: 1px 6px; border-radius: 4px; white-space: nowrap; font-size: 10px; line-height: 1.65; font-weight: 500; font-variant-numeric: tabular-nums; }
.status.neutral { color: var(--dub-muted); background: rgba(128,128,128,.08); }
.status.queued { color: var(--dub-info); background: rgba(99,179,237,.05); }
.status.ready { color: var(--dub-info); background: color-mix(in srgb, var(--dub-info) 10%, transparent); }
.status.ready::before { content: '○'; }
.status.confirmed { color: var(--dub-primary); background: color-mix(in srgb, var(--dub-primary) 14%, transparent); font-weight: 600; }
.status.confirmed::before { content: '✓'; }
.status.warning { color: var(--dub-warning); background: rgba(218,166,77,.14); }
.status.failed { color: var(--dub-error); background: rgba(232,128,128,.12); }
.status.failed::before { content: '!'; font-weight: 700; }
.status.working { color: var(--dub-info); background: rgba(99,179,237,.12); }
.status.working::before { content: ''; width: 8px; height: 8px; border: 1px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: saving-spin 1s linear infinite; }
.mute-icon { width: 14px; height: 14px; flex-shrink: 0; }
.synthesis-progress { display: block; height: 3px; margin-top: 5px; overflow: hidden; border-radius: 2px; background: rgba(128,128,128,.18); }
.synthesis-progress > span { display: block; height: 100%; background: var(--dub-info); }
.error-text { color: var(--dub-error); }
.error-text { display: block; font-size: 11px; margin-top: 4px; }
.customized { color: var(--dub-parameter); }
.line-settings-footer { width: 100%; }
.list-empty { padding: 28px 0; }
.list-help { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--dub-border); color: var(--dub-muted); font-size: 11px; line-height: 1.7; }
.list-help summary { cursor: pointer; }
.list-help summary span { margin-left: 6px; }
.list-help p { margin: 8px 0 0; }
@media (prefers-reduced-motion: reduce) {
  .synthesis-progress > span, .sentence { transition: none; }
  .status.working::before, .voice-toggle[aria-busy=true] .toggle-thumb { animation: none; }
}
@media (max-width: 1150px) { .workbench-grid { grid-template-columns: minmax(0, 1fr); } .sentence-list { max-height: 440px; } .video { max-height: 45vh; } }
@media (max-width: 600px) {
  .sentence { grid-template-columns: 16px 24px minmax(0, 1fr); gap: 8px 6px; padding: 12px 6px; }
  .voice-controls { grid-column: 3; }
  .sentence-settings { grid-column: 1 / -1; flex-wrap: wrap; }
  .empty-notice { margin-left: 0; }
  .sentence-text { -webkit-line-clamp: 3; font-size: 12px; }
  .selection-tools { margin-left: 0; flex-wrap: wrap; }
}
</style>
