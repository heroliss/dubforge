<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NInput, NCollapse, NCollapseItem,
  NDrawer, NDrawerContent, NRadioButton, NRadioGroup, NSelect, NSlider, NSpace, NText, useMessage, useThemeVars, useDialog,
} from 'naive-ui'
import { createLatestRefresh, vReleaseMedia } from '../utils/preview'
import { timingWarnings, voiceFor } from '../utils/dubbingTimeline'
import { selectRange } from '../utils/sentenceSelection'
import { hiddenSelectionCount } from '../utils/actionScope'
import { useSmoothProgressMap } from '../utils/smoothProgress'
import ProjectParameterForm from './ProjectParameterForm.vue'
import TtsServiceControl from './TtsServiceControl.vue'
import SubtitleLineEditor from './SubtitleLineEditor.vue'
import SubtitleWorkspace from './SubtitleWorkspace.vue'
import DependencyNotice from './DependencyNotice.vue'
import { useDubbingPreview } from '../composables/useDubbingPreview'
import { useDubbingSelection, type DubbingSelection } from '../composables/useDubbingSelection'
import { useDubbingMix } from '../composables/useDubbingMix'
import { useDubbingFollow } from '../composables/useDubbingFollow'
import { useSubtitlePreview } from '../composables/useSubtitlePreview'
import { useNavStore } from '../stores/nav'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { SOURCE_STYLES } from '../utils/configurationSource'

const props = defineProps<{ uid: string; suspended?: boolean }>()
const emit = defineEmits<{ (e: 'busy-change', value: boolean): void; (e: 'dirty-change', value: boolean): void }>()
const message = useMessage(), nav = useNavStore()
const route = useRoute(), router = useRouter()
const activePane = ref(route.query.panel === 'subtitles' ? 'subtitles' : 'editor')
const subtitlesVisited = ref(activePane.value === 'subtitles')
const dialog = useDialog()
const theme = useThemeVars()
const panelStyle = computed(() => ({
  '--dub-primary': theme.value.primaryColor, '--dub-warning': theme.value.warningColor,
  '--dub-error': theme.value.errorColor, '--dub-info': theme.value.infoColor,
  '--dub-muted': theme.value.textColor3, '--dub-border': theme.value.borderColor,
  '--dub-text': theme.value.textColor1,
  '--dub-parameter': SOURCE_STYLES.line.color,
  '--dub-surface': theme.value.cardColor,
}))
const serverState = ref<any>(null), loadError = ref(''), sel = ref<number | null>(null)
const selectionEdits = useDubbingSelection({
  save: (id, selection) => request(`workbench/lines/${id}/selection`, 'PUT', selection),
  saved: () => { void load(); void nav.poll(false) },
  error: (error: any) => message.error(`${error.message || '声源保存失败'}，已恢复之前的选择`),
})
const state = computed(() => selectionEdits.apply(serverState.value))
const filter = ref(''), stateFilter = ref('all'), checked = ref<number[]>([])
const parametersOpen = ref<string[]>([]), paramsDirty = ref(false), lineDirty = ref(false)
const settingsLine = ref<number | null>(null), serviceRevision = ref(0)
const settingsFooter = ref<HTMLDivElement>()
const submitting = ref(false)
const task = computed(() => state.value?.synthesis_task)
const taskRows = computed<Record<number, any>>(() => Object.fromEntries((task.value?.lines ?? []).map((line: any) => [line.id, line])))
const errors = computed<Record<number, string>>(() => Object.fromEntries((task.value?.lines ?? [])
  .filter((line: any) => line.status === 'failed').map((line: any) => [line.id, line.error || '合成失败'])))
const editing = ref(false)
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
function updateVideoDuration(event: Event) {
  const value = (event.target as HTMLVideoElement).duration
  videoDuration.value = Number.isFinite(value) && value > 0 ? value : 0
}
watch(() => state.value?.video?.url, () => { videoDuration.value = 0 })
const sentenceList = ref<HTMLDivElement>()
const activeSynthesisLine = computed<number | null>(() => state.value?.active_line ?? null)
const synthesisProgressKey = computed(() => `${task.value?.job_id}:${activeSynthesisLine.value}:${state.value?.active_attempt ?? 0}`)
const smoothLineProgress = useSmoothProgressMap(() => activeSynthesisLine.value === null ? [] : [{
  key: synthesisProgressKey.value,
  progress: (state.value?.active_progress ?? 0) * 100, status: 'running',
}])
const timing = computed(() => timingWarnings(state.value?.lines ?? [], choices))
let pollTimer: number | undefined, disposed = false
const selLine = computed(() => state.value?.lines.find((l: any) => l.id === sel.value))
const filters = computed(() => {
  const lines: any[] = state.value?.lines ?? []
  return [
    { value: 'all', label: '全部', count: lines.length },
    { value: 'pending', label: '待合成', count: lines.filter(l => !l.wav.exists).length },
    { value: 'confirmed', label: '已满意', count: lines.filter(l => l.confirmed).length },
    { value: 'silent', label: '无声', count: lines.filter(isSilent).length },
  ]
})
const shownLines = computed<any[]>(() => (state.value?.lines ?? []).filter((l: any) => {
  const q = filter.value.trim().toLowerCase()
  return (!q || `${l.text} ${l.source_text}`.toLowerCase().includes(q) || String(l.id) === q)
    && (stateFilter.value === 'all' || (stateFilter.value === 'confirmed' ? l.confirmed
      : stateFilter.value === 'silent' ? isSilent(l) : !l.wav.exists))
}))
const allChecked = computed(() => shownLines.value.length > 0 && shownLines.value.every(l => checked.value.includes(l.id)))
const checkedBlocked = computed(() => state.value?.lines.some((l: any) => checked.value.includes(l.id) && (!l.translation_available || l.edit_stale || l.unready_inputs?.length)))
const someChecked = computed(() => shownLines.value.some(l => checked.value.includes(l.id)))
const dirty = computed(() => paramsDirty.value || lineDirty.value || textDirty.value || subtitleDirty.value || editing.value)
const busy = computed(() => submitting.value || !!task.value?.active || state.value?.active_line != null)
const actionsDisabled = computed(() => busy.value || nav.busy || dirty.value || props.suspended)
const runDisabled = computed(() => actionsDisabled.value || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value)
// 后台合成不占用预览；只有上游媒体重建才需要释放播放器。
const playbackDisabled = computed(() => !!props.suspended)
const { preview, playAll, pause, seek, stopAll, volDub, volBg, solo, playing, time, cursor, playbackError } = useDubbingPreview({
  state: () => state.value, video: videoEl, choices: () => choices,
})
const { mode: followMode, locate } = useDubbingFollow({
  lines: () => state.value?.lines ?? [], selected: sel,
  synthesis: () => ({ job: task.value?.job_id, line: activeSynthesisLine.value }),
  previewLine: () => cursor.value?.lineId ?? null,
  seek, reveal: id => { void revealLine(id) },
})
const mixProjectUid = props.uid
const mixEdits = useDubbingMix({
  voice: volDub, background: volBg,
  save: mix => request('step_params', 'PUT', { updates: {
    'assemble.voice_volume': mix.voice, 'assemble.bgm_volume': mix.background,
  } }, mixProjectUid),
  saved: () => { void load(); void nav.poll(false) },
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
  if (!response.ok) throw new Error(data.message || '保存失败，请重试')
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
  if (!r.ok) throw new Error(value.message || '读取配音内容失败')
  return { targetUid, value, readRevision, mixRevision }
}, ({ targetUid, value, readRevision, mixRevision }) => {
  if (targetUid !== props.uid) return
  if (mediaSignature(state.value) !== mediaSignature(value)) stopAll()
  if (value.synthesis_task?.active && value.synthesis_task.job_id !== serverState.value?.synthesis_task?.job_id) {
    checked.value = value.synthesis_task.lines.map((line: any) => line.id)
  }
  serverState.value = value
  selectionEdits.reconcile(readRevision)
  loadError.value = ''
  if (!value.lines.some((l: any) => l.id === sel.value)) sel.value = value.lines[0]?.id ?? null
  checked.value = checked.value.filter(id => value.lines.some((l: any) => l.id === id))
  mixEdits.reconcile(value.mix, mixRevision)
}, () => { loadError.value = '读取配音内容失败，请重试'; stopAll() })
async function load(replace = true) { if (!disposed) await loader.refresh(replace) }
onMounted(async () => {
  window.addEventListener('beforeunload', warnUnload)
  await load()
  if (!disposed) pollTimer = window.setInterval(() => { void load(false) }, 1500)
})
onUnmounted(() => {
  window.removeEventListener('beforeunload', warnUnload)
  disposed = true
  clearInterval(pollTimer); loader.cancel(); selectionEdits.dispose(); void mixEdits.dispose(); stopAll()
})
async function parametersSaved() {
  serviceRevision.value++
  await Promise.all([load(), nav.poll()])
}
function fmt(t: number) { return `${String(Math.floor(t / 60)).padStart(2, '0')}:${(t % 60).toFixed(1).padStart(4, '0')}` }
function select(line: any) { locate(line.id) }
function listen(line: any) { if (!playbackDisabled.value) { sel.value = line.id; void preview(line) } }
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
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && !busy.value) {
    event.preventDefault(); checkShown(true)
  }
}
function paramSummary(line: any) { return line.custom_params.map((p: any) => `${p.label}：${Array.isArray(p.value) ? p.value.join('、') : p.value}`).join(' · ') }
function emptySlot(line: any) { return line.selection.source === 'dub' && line.selection.take === null }
function isSilent(line: any) { return voiceFor(line, choices) === 'silent' }
function isSynthesizing(line: any) { return state.value?.active_line === line.id }
function editLine(line: any) {
  if (actionsDisabled.value || selectionEdits.saving.value || state.value?.edit_blockers?.length) return
  textEditLine.value = JSON.parse(JSON.stringify(line))
}
function closeTextEditor() { textEditLine.value = null; textDirty.value = false }
async function textSaved() { await Promise.all([load(), nav.poll(false)]) }
async function discardOrphans() {
  if (actionsDisabled.value) return
  editing.value = true
  try { await request('workbench/discard_orphaned_edits', 'POST', {}); await textSaved() }
  catch (e: any) { message.error(e.message || '清理失败') }
  finally { editing.value = false }
}
async function revealLine(id: number) {
  if (!shownLines.value.some(line => line.id === id)) {
    filter.value = ''; stateFilter.value = 'all'
  }
  await nextTick()
  if (disposed || sel.value !== id) return
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
  const adjustment = line.wav.duration_retry?.retries || line.wav.duration_retry?.used_factor <= .5
    ? '缩短译文或调整句子时间'
    : state.value?.line_settings_supported ? '缩短译文或减小本句「时长因子」' : '缩短译文或在服务端调整语速'
  const limit = warning?.nextId ? `距第 ${warning.nextId} 句开始仅` : '本句可用时长为'
  return warning ? `配音 ${line.wav.seconds} 秒，${limit} ${warning.available.toFixed(2)} 秒，超出 ${warning.excess.toFixed(2)} 秒。可${adjustment}后重新合成；音频不会自动截断。` : ''
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
function isQueued(line: any) { return task.value?.active && taskRows.value[line.id]?.status === 'queued' }
function label(line: any) {
  if (isSynthesizing(line)) {
    const progress = lineProgress(line.id)
    const stage = state.value?.active_attempt ? `缩时重试 ${state.value.active_attempt}` : '合成中'
    return progress === null ? stage : `${stage} ${progress}%`
  }
  if (isQueued(line)) return '等待合成'
  return errors.value[line.id] ? '合成失败' : isSilent(line) ? (emptySlot(line) ? '人声静音' : '预览无声')
    : line.confirmed ? '已满意' : line.wav.exists ? '已合成' : line.wav.stale ? '已过期' : '尚无配音'
}
async function synthesize(ids: number[]) {
  if (!ids.length || runDisabled.value) return
  submitting.value = true
  ids = [...ids]
  const targets = state.value.lines.filter((line: any) => ids.includes(line.id))
  const takes = Object.fromEntries(targets
    .map((line: any) => [line.id, line.selection.take]))
  const overwrite = targets.filter((line: any) => line.selection.take != null && line.wav.exists).length
  const execute = async () => {
    try {
      await request('workbench/synthesize', 'POST', { line_ids: ids, takes, force: true })
      if (!disposed) {
        await Promise.all([load(), nav.poll()])
        message.success(`已提交 ${ids.length} 句后台合成，可在顶部暂停或停止`)
      }
    } catch (error: any) {
      if (!disposed) message.error(error.message || '提交合成任务失败')
    } finally { submitting.value = false }
  }
  if (overwrite) {
    dialog.warning({ title: `确认生成 ${ids.length} 句配音`,
      content: `仅处理句号：${ids.join('、')}。其中 ${overwrite} 句将覆盖当前选中的配音版本，覆盖后需重新确认满意；选择空位的句子会新增版本。不会处理其他句子。`,
      maskClosable: false, closeOnEsc: false,
      positiveText: `生成这 ${ids.length} 句`, negativeText: '取消', onPositiveClick: execute,
      onNegativeClick: () => { submitting.value = false }, onClose: () => { submitting.value = false },
    })
  } else await execute()
}
async function markSatisfied(ids: number[], confirmed: boolean) {
  if (runDisabled.value) return
  editing.value = true
  try {
    const data = await request('workbench/confirm', 'POST', {line_ids: ids, confirmed})
    message.success(`已${confirmed ? '标记' : '取消'} ${data.updated} 个版本${data.skipped ? `，跳过 ${data.skipped} 个无有效配音的句子` : ''}`)
    await load()
  } catch (error: any) { message.error(error.message || '满意标记保存失败') }
  finally { editing.value = false }
}
function closeSettings() {
  if (lineDirty.value) { message.warning('请先保存本句参数，或点击「放弃修改」'); return }
  settingsLine.value = null
}
function discardSettings() { lineDirty.value = false; settingsLine.value = null }
</script>

<template>
  <section class="dubbing-panel" :style="panelStyle">
    <nav class="workspace-tabs" aria-label="字幕与配音工作区">
      <n-button :type="activePane === 'editor' ? 'primary' : 'default'" :secondary="activePane === 'editor'" :aria-pressed="activePane === 'editor'" @click="changePane('editor')">校对与配音</n-button>
      <n-button :type="activePane === 'subtitles' ? 'primary' : 'default'" :secondary="activePane === 'subtitles'" :aria-pressed="activePane === 'subtitles'" @click="changePane('subtitles')">字幕工作区<span v-if="subtitleDirty"> · 未保存</span></n-button>
      <n-text depth="3" class="hint">字幕样式、效果预览与文件导出集中在字幕工作区</n-text>
    </nav>
    <n-alert v-if="loadError" type="error" class="section">{{ loadError }} <n-button size="tiny" @click="load()">重试</n-button></n-alert>
    <n-alert v-if="state?.task_error" type="error" class="section">{{ state.task_error }}</n-alert>
    <DependencyNotice :uid="uid" :items="state?.edit_blockers ?? []" />
    <n-alert v-if="state?.orphaned_edits?.length" type="warning" class="section">上游已删除这些句子，旧修改未应用：{{ state.orphaned_edits.join('、') }}。<n-button size="tiny" :disabled="actionsDisabled" @click="discardOrphans">清除这些失效记录</n-button></n-alert>
    <div v-show="activePane === 'editor'">
    <n-card size="small" class="section">
      <TtsServiceControl :uid="uid" :disabled="paramsDirty || lineDirty" :revision="serviceRevision" />
      <n-collapse v-model:expanded-names="parametersOpen">
        <n-collapse-item name="parameters" title="配音参数 · 步骤设置" display-directive="show">
          <template #header-extra><n-text v-if="paramsDirty" type="warning">有未保存的修改</n-text></template>
          <ProjectParameterForm :uid="uid" step-key="tts" :disabled="busy || nav.busy || settingsLine !== null"
            @dirty-change="paramsDirty = $event" @saved="parametersSaved" />
        </n-collapse-item>
      </n-collapse>
    </n-card>
    <n-alert v-if="paramsDirty || lineDirty" type="warning" class="section" :show-icon="false">请保存参数后再合成。</n-alert>
    <n-card v-if="state && !state.lines.length" size="small" class="section"><n-empty description="语义整理完成后即可校对原文与时间；译文就绪后可编辑双语并按需配音" /></n-card>
    <div v-else-if="state" class="workbench-grid section">
      <div class="preview-column">
        <n-card size="small" title="同步预览">
          <div class="subtitle-controls">
            <span>字幕</span>
            <n-select v-model:value="subtitleMode" :options="subtitleOptions" size="small" aria-label="字幕预览类型" style="width:150px" />
            <n-button size="small" secondary @click="changePane('subtitles')">调整字幕样式与导出</n-button>
            <span v-if="subtitleMode !== 'off'" class="hint" :title="previewExported ? '预览导出文件的文字、时间与双语顺序' : '使用已保存的文字与起止时间；双语顺序沿用导出设置，无需先导出字幕'">{{ previewExported ? '导出文件' : '当前文字 · 无需导出' }}</span>
          </div>
          <n-text v-if="subtitleError" type="error" class="hint note">{{ subtitleError }}</n-text>
          <n-alert v-if="playbackError" type="warning" class="playback-error" title="试听未能播放">
            {{ playbackError }}
            <n-text depth="3" class="hint note">重试仅加载和播放媒体，不会重新生成配音，也不会更改声源与音量。</n-text>
          </n-alert>
          <video v-if="state.video.found && !playbackDisabled" v-release-media :key="state.video.url" ref="videoEl"
            controls preload="metadata" :src="state.video.url" class="video"
            @loadedmetadata="updateVideoDuration" @durationchange="updateVideoDuration">
            <track v-if="subtitlePreview" :key="subtitlePreview" :src="subtitlePreview" kind="subtitles" label="字幕预览" srclang="und" default @load="($event.target as HTMLTrackElement).track.mode = 'showing'" />
          </video>
          <div v-else class="no-video">{{ playbackDisabled ? '正在更新预览媒体，完成后自动恢复' : '视频暂不可用，仍可试听音频' }}</div>
          <div class="transport">
            <n-button size="small" secondary type="primary" :disabled="playbackDisabled" @click="playing ? pause() : playAll()">{{ playing ? 'Ⅱ 暂停' : '▶ 连续播放' }}</n-button>
            <span class="time-display">{{ fmt(time) }}</span>
            <n-checkbox v-model:checked="solo" size="small">单句试听仅人声</n-checkbox>
          </div>
          <div class="mix-controls">
            <label>人声<n-slider :value="volDub" :min="0" :max="1" :step="0.05" :disabled="busy || nav.busy || editing" aria-label="人声音量"
              @update:value="mixEdits.change('voice', $event)" @dragend="mixEdits.save" @keyup="mixEdits.save" /><output>{{ Math.round(volDub * 100) }}%</output></label>
            <label>背景<n-slider :value="volBg" :min="0" :max="1" :step="0.05" :disabled="busy || nav.busy || editing" aria-label="背景音量"
              @update:value="mixEdits.change('background', $event)" @dragend="mixEdits.save" @keyup="mixEdits.save" /><output>{{ Math.round(volBg * 100) }}%</output></label>
          </div>
          <div class="mix-save-state" aria-live="polite">
            <template v-if="mixEdits.error.value">
              <n-text type="warning" class="hint" :title="mixEdits.error.value">音量未保存，试听已生效</n-text>
              <n-button size="tiny" quaternary :loading="mixEdits.saving.value" @click="mixEdits.save">重试保存</n-button>
            </template>
            <n-text v-else depth="3" class="hint" :title="state.vocal?.found ? '音频跟随视频时间轴。' : '试听原声暂用完整音轨；成片使用分离人声，缺失时会提示。'">{{ mixEdits.pending.value || mixEdits.saving.value ? '正在保存音量，试听已生效…' : '当前声源、配音版本和音量同步用于成片。' }}</n-text>
          </div>
          <div v-if="selLine" class="line-detail">
            <div class="line-heading"><n-text depth="3">第 {{ selLine.id }} 句 · {{ fmt(selLine.start) }} — {{ fmt(selLine.end) }}</n-text>
              <span class="status" :class="statusTone(selLine)" :title="isSilent(selLine) ? silenceHint(selLine) : errors[selLine.id]">
                <svg v-if="isSilent(selLine) && !isSynthesizing(selLine)" class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                {{ label(selLine) }}
              </span></div>
            <div class="transcript-pair">
              <div v-if="selLine.source_text" class="transcript-row"><span class="transcript-label">原文</span><p class="source-text">{{ selLine.source_text }}</p></div>
              <div class="transcript-row"><span class="transcript-label">译文</span><p class="line-text">{{ selLine.text || '尚无译文，可先校对原文和时间' }}</p></div>
            </div>
            <div v-if="isSilent(selLine)" class="silence-detail">
              <svg class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
              <span>{{ silenceHint(selLine) }}</span>
            </div>
            <n-text depth="3" class="hint">本句 {{ selLine.duration }} 秒<template v-if="selLine.translation_available"> · {{ selLine.wav.exists ? '实际' : '预计' }}配音 {{ selLine.wav.seconds ?? selLine.est_dur }} 秒</template></n-text>
            <n-text v-if="!isSynthesizing(selLine) && selLine.wav.exists && selLine.wav.duration_retry?.retries" depth="3" class="hint note">已缩时重试 {{ selLine.wav.duration_retry.retries }} 次 · 此版本时长因子 {{ selLine.wav.duration_retry.used_factor }}<template v-if="selLine.wav.duration_retry.error"> · 重试失败，已保留有效音频</template></n-text>
            <p v-if="timing[selLine.id]" class="timing-detail">{{ timingHint(selLine) }}</p>
            <n-alert v-if="selLine.unready_inputs?.length" type="warning" :show-icon="false" class="section">{{ selLine.unready_inputs.map((i: any) => `${i.name}：${i.reason}`).join('；') }}</n-alert>
            <n-space class="detail-actions" size="small">
              <n-button size="small" :disabled="actionsDisabled || selectionEdits.saving.value || !!state.edit_blockers?.length" @click="editLine(selLine)">编辑文字与时间</n-button>
              <n-button size="small" type="primary" :disabled="runDisabled || !selLine.translation_available || selLine.edit_stale || !!selLine.unready_inputs?.length" @click="synthesize([selLine.id])">{{ selLine.selection.take ? `覆盖配音 ${selLine.selection.take}` : '生成新配音' }}</n-button>
              <n-button size="small" secondary :disabled="runDisabled || !selLine.wav.exists" @click="markSatisfied([selLine.id], !selLine.confirmed)">{{ selLine.confirmed ? '取消满意' : '标记满意' }}</n-button>
            </n-space>
            <n-text v-if="selLine.wav.settings_changed" depth="3" class="hint note">此版本使用之前的参数生成，仍可试听和用于成片；再次合成使用当前参数。</n-text>
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
          <span>自动定位</span>
          <n-radio-group v-model:value="followMode" size="small" aria-label="自动定位模式">
            <n-radio-button value="off" title="手动选择句子，不自动跟随">关闭</n-radio-button>
            <n-radio-button value="synthesis" title="跟随正在合成的句子，选中并滚动列表，同时定位视频；保持播放或暂停状态">跟随合成</n-radio-button>
            <n-radio-button value="preview" title="跟随预览播放的句子，选中并滚动列表，不改变视频位置">跟随预览</n-radio-button>
          </n-radio-group>
        </div>
        <div v-if="activeSynthesisLine !== null" class="synthesis-current" role="status">
          <span>正在合成第 {{ activeSynthesisLine }} 句<template v-if="state.active_attempt"> · 缩时重试 {{ state.active_attempt }} · 因子 {{ state.active_factor }}</template><span v-if="lineProgress(activeSynthesisLine) !== null"> · {{ lineProgress(activeSynthesisLine) }}%</span></span>
          <n-button size="tiny" quaternary title="选中正在合成的句子并定位视频，保持播放或暂停状态" @click="locate(activeSynthesisLine)">定位到本句</n-button>
        </div>
        <n-input v-model:value="filter" size="small" placeholder="搜索原文、译文或编号" clearable>
          <template #prefix><svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4" stroke-linecap="round"/></svg></template>
        </n-input>
        <div class="list-filters">
          <n-checkbox :checked="allChecked" :indeterminate="someChecked && !allChecked" :disabled="busy" @update:checked="checkShown">{{ filter || stateFilter !== 'all' ? '全选当前结果' : '全选' }}</n-checkbox>
          <div class="filter-options" aria-label="句子筛选">
            <button v-for="item in filters" :key="item.value" type="button" class="filter-chip"
              :class="{ 'silent-filter': item.value === 'silent' }" :aria-pressed="stateFilter === item.value" @click="stateFilter = item.value">
              {{ item.label }}<span>{{ item.count }}</span>
            </button>
          </div>
        </div>
        <div class="batch-actions">
          <n-button size="small" type="primary" :disabled="runDisabled || !checked.length || checkedBlocked" title="只处理勾选句；覆盖已有版本前会再次确认，选择空位则新增版本。" @click="synthesize([...checked])">生成勾选句配音（{{ checked.length }} 句）</n-button>
          <n-button size="small" :disabled="runDisabled || !checked.length" title="标记所选句子的当前配音版本；不控制是否进入成片" @click="markSatisfied([...checked], true)">标记满意</n-button>
          <n-button size="tiny" quaternary :disabled="runDisabled || !checked.length" @click="markSatisfied([...checked], false)">取消满意</n-button>
          <div class="selection-tools">
            <span class="selection-count">已勾选 {{ checked.length }} 句<span v-if="hiddenSelectionCount(checked, shownLines.map(line => line.id))">（含隐藏 {{ hiddenSelectionCount(checked, shownLines.map(line => line.id)) }} 句）</span></span>
            <n-button size="tiny" quaternary :disabled="busy" @click="invertShown">反选当前列表</n-button>
            <n-button v-if="checked.length && !busy" size="tiny" quaternary @click="checked = []">清除全部勾选</n-button>
          </div>
        </div>
        <div ref="sentenceList" class="sentence-list" tabindex="0" aria-label="配音句子列表" @keydown="listKey">
          <div v-for="line in shownLines" :key="line.id" :data-id="line.id" class="sentence"
            :class="{selected: line.id === sel, checked: checked.includes(line.id), active: cursor?.lineId === line.id, waiting: cursor?.lineId === line.id && cursor.phase !== 'playing', generating: isSynthesizing(line), silent: isSilent(line)}">
            <input type="checkbox" :checked="checked.includes(line.id)" :disabled="busy" :aria-label="`选择第 ${line.id} 句`" @click="check(line.id, $event)" />
            <n-button size="tiny" quaternary class="sentence-play" :disabled="playbackDisabled || (!line.wav.exists && !state.video.found && !state.vocal?.found && !state.original?.found)"
              :aria-label="`试听第 ${line.id} 句`" :title="`试听本句${voiceFor(line, choices) === 'original' ? '原声' : voiceFor(line, choices) === 'silent' ? '（人声静音）' : '配音'}`" @click="listen(line)">▶</n-button>
            <button type="button" class="sentence-content" :disabled="playbackDisabled" :aria-label="`定位第 ${line.id} 句`" @click="select(line)">
              <span class="sentence-meta">
                <span class="sentence-number">#{{ line.id }}</span><span class="sentence-time" :title="`开始 ${line.start.toFixed(3)} 秒 · 结束 ${line.end.toFixed(3)} 秒`">{{ fmt(line.start) }} – {{ fmt(line.end) }}</span>
                <span v-if="line.edited" class="edit-label" :class="{stale: line.edit_stale}">{{ line.edit_stale ? '修改待核对' : '已校对' }}</span>
                <span v-if="cursor?.lineId === line.id" class="playing-label" :class="{waiting: cursor.phase !== 'playing'}">{{ cursor.phase === 'gap' ? '句间停顿' : cursor.phase === 'silent' ? '当前 · 无声' : '播放中' }}</span>
                <span class="status" :class="statusTone(line)" :title="isSilent(line) ? silenceHint(line) : errors[line.id]">
                  <svg v-if="isSilent(line) && !isSynthesizing(line)" class="mute-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M9 3 5 6H2v8h3l4 3V3ZM13 7l5 6m0-6-5 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
                  {{ label(line) }}
                </span>
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
              <span v-if="timing[line.id]" class="timing-warning" :title="timingHint(line)">超时 {{ timing[line.id].excess.toFixed(1) }} 秒</span>
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
                  :disabled="actionsDisabled || !state.line_settings_supported || !line.translation_available || line.edit_stale" :aria-label="`第 ${line.id} 句参数`"
                  :title="!state.line_settings_supported ? '当前服务的声音设置由服务端管理' : line.customized ? `本句设置：${paramSummary(line)}` : '未覆盖的参数跟随项目的最终设置；点击设置本句参数'" @click="settingsLine = line.id">
                  <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14"/><circle cx="7" cy="5" r="2"/><circle cx="13" cy="10" r="2"/><circle cx="8" cy="15" r="2"/></svg>
                </n-button>
                <n-button size="tiny" quaternary class="settings-button" :disabled="actionsDisabled || selectionEdits.saving.value || !!state.edit_blockers?.length" :aria-label="`编辑第 ${line.id} 句文字与时间`" title="编辑双语文字与起止时间" @click="editLine(line)">
                  <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m4 12-1 5 5-1L17 7l-4-4-9 9Zm7-7 4 4M3 17h14" stroke-linejoin="round"/></svg>
                </n-button>
              </div>
              <select class="take-select" :class="{ 'silent-voice': isSilent(line) }" :title="isSilent(line) ? silenceHint(line) : '试听和成片使用当前选中的配音版本'" :value="line.selection.take ?? (line.selection.source === 'original' ? 'none' : '')" :disabled="actionsDisabled || selectionEdits.isSaving(line.id) || !line.translation_available || line.edit_stale" :aria-label="`第 ${line.id} 句配音版本`" @change="chooseTake(line, $event)">
                <option v-if="line.selection.take === null && line.selection.source === 'original'" value="none" disabled>选择配音</option>
                <option v-for="take in line.versions" :key="take.id" :value="take.id" :disabled="!take.exists">配音 {{ take.id }}{{ take.confirmed ? ' ✓' : take.stale ? '（过期）' : '' }}</option>
                <option value="">＋ 新配音（空位）</option>
              </select>
            </div>
            <div v-if="line.customized || isSilent(line)" class="sentence-settings">
              <span v-if="line.customized" class="parameter-summary" :title="paramSummary(line)"><ConfigurationSourceBadge :source="{kind:'line'}"/><span>{{ paramSummary(line) }}</span></span>
              <span v-if="isSilent(line)" class="empty-notice" :title="silenceHint(line)">{{ emptySlot(line) ? '空位 · 保留背景与画面' : '配音不可用 · 成片前需处理' }}</span>
            </div>
          </div>
          <n-empty v-if="!shownLines.length" :description="stateFilter === 'silent' && !filter ? '没有无声句子' : '没有匹配的句子'" class="list-empty" />
        </div>
        <details class="list-help"><summary>操作说明 <span>Shift 连选 · 点击文本定位 · ▶ 试听</span></summary>
          <p>勾选仅决定批量处理的句子，不决定成片是否包含它们；点击行只定位当前句，不等于勾选。筛选不会清除隐藏句子的勾选。</p>
          <p>满意仅标记当前配音版本，覆盖合成后需重新确认。成片使用各句选定的声源与版本；空位不加入本句人声，保留背景与画面。</p>
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
      :disabled="busy || nav.busy || paramsDirty || lineDirty || textDirty || editing || selectionEdits.saving.value || mixEdits.pending.value || mixEdits.saving.value"
      @dirty-change="subtitleDirty = $event" @refresh="load()" @preview="previewExport"
      @return-assembly="router.push(`/projects/${uid}/steps/assemble`)" />
    <SubtitleLineEditor v-if="textEditLine" :uid="uid" :line="textEditLine" :disabled="busy || nav.busy"
      @close="closeTextEditor" @saved="textSaved" @dirty-change="textDirty = $event" />
    <n-drawer :show="settingsLine !== null" :width="680" style="max-width: 100vw" :mask-closable="!lineDirty" @update:show="!$event && closeSettings()">
      <n-drawer-content :title="`第 ${settingsLine} 句 · 配音参数`" closable>
        <ProjectParameterForm v-if="settingsLine !== null" :key="settingsLine" :uid="uid" step-key="tts" :line-id="settingsLine" :disabled="busy || nav.busy"
          @dirty-change="lineDirty = $event" @saved="parametersSaved">
          <template #actions="{ save, saving, loading, dirty, hasOverrides, resetAll }">
            <Teleport :to="settingsFooter || 'body'" :disabled="!settingsFooter">
              <n-space align="center" justify="end">
                <n-button v-if="hasOverrides" quaternary :disabled="busy || nav.busy || saving || loading" @click="resetAll">填入步骤设置（待保存）</n-button>
                <n-text v-if="dirty" type="warning" class="hint">有未保存的修改</n-text>
                <n-button v-if="dirty" quaternary :disabled="saving" @click="discardSettings">放弃修改</n-button>
                <n-button type="primary" :loading="saving" :disabled="!dirty || busy || nav.busy || loading" @click="save">保存本句参数</n-button>
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
.playback-error { margin-bottom:12px; }
.workbench-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(430px, 1.2fr); align-items: start; gap: 20px; }
.preview-column, .sentence-card { min-width: 0; }
.video { display: block; width: 100%; max-height: 38vh; background: #000; border-radius: 8px; }
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
.note { display: block; margin-top: 10px; line-height: 1.6; }
.line-detail { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--dub-border); }
.line-heading { justify-content: space-between; font-size: 12px; }
.transcript-pair { display: grid; gap: 10px; margin: 16px 0 12px; }
.transcript-row { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: baseline; gap: 10px; }
.transcript-label { color: var(--dub-muted); font-size: 11px; }
.source-text { color: var(--dub-muted); font-size: 12px; margin: 0; line-height: 1.6; overflow-wrap: anywhere; }
.line-text { font-size: 15px; line-height: 1.7; margin: 0; overflow-wrap: anywhere; }
.silence-detail { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; margin: 12px 0; color: var(--dub-warning); background: rgba(218,166,77,.09); border: 1px solid rgba(218,166,77,.22); border-radius: 7px; font-size: 12px; line-height: 1.7; }
.silence-detail .mute-icon { margin-top: 3px; }
.detail-actions { margin-top: 14px; }
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
.filter-chip.silent-filter { color: var(--dub-warning); }
.filter-chip.silent-filter[aria-pressed=true] { color: var(--dub-warning); background: rgba(218,166,77,.12); border-color: rgba(218,166,77,.28); }
.filter-chip:focus-visible { outline: 2px solid var(--dub-info); outline-offset: 2px; }
.batch-actions { gap: 6px; margin-bottom: 12px; padding: 8px; border-radius: 7px; background: rgba(128,128,128,.05); border: 1px solid var(--dub-border); }
.selection-tools { display: flex; align-items: center; gap: 4px; margin-left: auto; }
.selection-count { color: var(--dub-muted); font-size: 11px; padding: 0 4px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.sentence-list { max-height: 66vh; overflow-y: auto; scrollbar-gutter: stable; scrollbar-width: thin; scrollbar-color: rgba(128,128,128,.45) transparent; padding: 3px; }
.sentence { display: grid; grid-template-columns: 16px 24px minmax(0, 1fr) 128px; gap: 5px 8px; align-items: center; padding: 10px 8px; margin-bottom: 6px; border: 1px solid transparent; border-left: 3px solid transparent; border-radius: 7px; background: rgba(128,128,128,.035); transition: background-color .15s, border-color .15s; }
.sentence input[type=checkbox] { margin: 0; width: 14px; height: 14px; accent-color: var(--dub-primary); cursor: pointer; }
.sentence:hover { background: rgba(128,128,128,.085); }
.sentence.checked { border-color: #5278a5; }
.sentence.selected { background: rgba(99,179,237,.07); outline: 1px solid rgba(99,179,237,.35); outline-offset: -1px; }
.sentence.active:not(.silent) { border-left-color: var(--dub-primary); }
.sentence.active.waiting:not(.silent) { border-left-color: var(--dub-muted); }
.sentence.generating { border-left-color: var(--dub-info); background: rgba(99,179,237,.08); }
.sentence.silent { background: rgba(218,166,77,.085); border-color: rgba(218,166,77,.2); border-left-color: var(--dub-warning); }
.sentence.silent:hover { background: rgba(218,166,77,.13); }
.sentence.silent.selected { outline-color: rgba(218,166,77,.6); }
.sentence-play { border-radius: 5px; }
.sentence.active .sentence-play { color: var(--dub-primary); background: rgba(54,173,106,.1); }
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
.sentence-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 11px; margin-bottom: 5px; }
.sentence-number { color: var(--dub-muted); font-weight: 600; }
.edit-label { color: var(--dub-info); font-size:10px; }
.edit-label.stale { color: var(--dub-warning); }
.sentence-time { color: var(--dub-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.playing-label { color: var(--dub-primary); font-size: 10px; }
.playing-label.waiting { color: var(--dub-muted); }
.synthesis-current { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; padding: 6px 8px; border-radius: 6px; background: rgba(99,179,237,.08); color: var(--dub-info); font-size: 12px; }
.synthesis-current { flex-wrap: wrap; }
.follow-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.follow-controls > span { color: var(--dub-muted); font-size: 12px; }
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
