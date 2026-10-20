<script setup lang="ts">
// 评论缓存、准备草稿和已确认方案分层；仅明确手选和编辑才固定保留。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute } from 'vue-router'
import { NAlert, NButton, NCheckbox, NCollapse, NCollapseItem, NInput, NInputNumber, NPagination, NSelect, NSlider, NSwitch, NTag, NTooltip, useDialog, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import CommentPreview from './CommentPreview.vue'
import TextColorPresets from './TextColorPresets.vue'
import ConfigurationForm from './ConfigurationForm.vue'
import CommentProcessPanel from './CommentProcessPanel.vue'
import { useCommentProgress } from '../composables/useCommentProgress'
import { commentProgressJob, type CommentCollection } from '../utils/commentProcess'
import { chronologicalComments, commentPublishedAt } from '../utils/commentOrder'
import { textColorPatch } from '../utils/textColorPresets'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import ParameterResetButton from './ParameterResetButton.vue'
import AppIcon from './AppIcon.vue'
import type { ConfigurationSource } from '../utils/configurationSource'
import { commentCandidateReadiness, commentTextBlockingIssues, commentTextIssues, commentTextLength, defaultCommentTextLimits } from '../utils/commentOverlay'
import { textStyleFieldGroups } from '../utils/textStyleFields'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { commentIssueCauses, commentIssueContext, commentIssueCounts, commentIssueLabel, commentJobFeedback, hasCommentTranslation, type CommentAction } from '../utils/commentWorkflow'

const props = defineProps<{ uid: string; version?: string; blocked?: boolean; videoDuration?: number; automation?: { state: string; message: string } | null; collection?: CommentCollection | null }>()
const { locked: projectLocked, reason: projectLockReason, canEdit, observing } = useConfigurationEditability(() => 'project:' + props.uid)
const emit = defineEmits<{ 'busy-change': [value: string]; 'dirty-change': [value: boolean]; 'saving-change': [value: boolean]; 'collection-limit': [value: number]; 'show-diagnostics': [action?: CommentAction] }>()
const dialog = useDialog(), message = useMessage()
const route = useRoute(), rootEl = ref<HTMLElement | null>(null)
const state = ref<any>(null), error = ref(''), pending = ref(''), saving = ref(false)
const compare = ref(false), page = ref(1), onlySuggested = ref(false)
const prompt = ref(''), expandedRows = ref<string[]>([])
const limit = ref<number | null>(10)
const items = ref<any[]>([])
type SelectionOrigin = 'manual' | 'automatic'
const releasedSelections = ref<{ id: string; source_hash: string; origin: SelectionOrigin }[]>([])
const orderMode = ref<'chronological' | 'manual'>('chronological')
const excluded = ref<{ id: string; source_hash: string }[]>([])
const preparation = ref<any>(null)
const translatingId = ref('')
const previewElement = ref<HTMLElement | null>(null)
const previewFocusId = ref(''), focusRequest = ref(0), temporaryPreviewId = ref(''), highlightedId = ref('')
const planExpanded = ref<string[]>(['plan'])
const rowElements = new Map<string, HTMLElement>()
const initialSettings = { start: 5, interval: 15, duration: 6, position: 'top_left', font_size: 36, min_font_size: 18, margin: 60, width_percent: 0, height_percent: 30, show_author: false,
  language_mode: 'translated', source_first: false, color: '#FFFFFF', outline_color: '#000000', outline: 2, background_color: '#000000', background_opacity: 50,
  font: 'Microsoft YaHei', source_style_enabled: false, source_font: 'Microsoft YaHei', source_font_size: 30, source_min_font_size: 18, source_color: '#FFFFFF', source_outline_color: '#000000', source_outline: 2 }
const settings = ref<any>({ ...initialSettings })
const revision = ref(''), baseline = ref('')
const rulesDirty=ref(false),rulesSaving=ref(false),rulesSection=ref<HTMLDetailsElement|null>(null),styleEdited=ref(new Set<string>()),styleReset=ref(new Set<string>())
const styleFields=computed(()=>(state.value?.fields||[]).filter((f:any)=>Object.hasOwn(initialSettings,f.path.slice(9))))
const styleGroups=computed(()=>textStyleFieldGroups(styleFields.value,'comments.',Object.fromEntries(Object.entries(settings.value).map(([k,v])=>['comments.'+k,v])),'整体布局')
  .map(group=>({...group,fields:group.fields.filter(field=>!['comments.min_font_size','comments.source_min_font_size'].includes(field.path))})))
function fontRangeFields(field:any):any[]{
  const minimum=field.path==='comments.font_size'?'comments.min_font_size':field.path==='comments.source_font_size'?'comments.source_min_font_size':''
  const lower=styleFields.value.find((item:any)=>item.path===minimum)
  return lower?[lower,field]:[]
}
function fontRangeLabel(field:any){return field.path==='comments.source_font_size'?'原文字号范围':'译文字号范围（通用）'}
function styleSource(key:string):ConfigurationSource|undefined{return styleReset.value.has(key)?state.value?.configuration?.parent_sources?.['comments.'+key]:styleEdited.value.has(key)?{kind:'project'}:state.value?.configuration?.sources?.['comments.'+key]}
function setStyle(key:string,value:any){if(editLocked.value)return;settings.value[key]=value;styleEdited.value.add(key);styleReset.value.delete(key);preparation.value=null}
function canInheritStyle(key:string){return ownsStyle(key)&&Object.hasOwn(state.value?.configuration?.defaults||{},'comments.'+key)}
function inheritStyle(key:string){if(editLocked.value||!canInheritStyle(key))return;settings.value[key]=state.value.configuration.defaults['comments.'+key];styleReset.value.add(key);styleEdited.value.delete(key);preparation.value=null}
function ownsStyle(key:string){return !styleReset.value.has(key)&&(styleEdited.value.has(key)||Object.hasOwn(state.value?.configuration?.overrides||{},'comments.'+key))}
let disposed = false, epoch = 0, readSequence = 0, fullRead: Promise<void> | undefined, fullController: AbortController | undefined
let readAgain = false, resetAfterRead = false, clearAfterRead = false
const progressFeed = useCommentProgress({
  uid: () => props.uid,
  active: () => !!pending.value || state.value?.job?.status === 'running' || state.value?.prepare_job?.status === 'running' || props.automation?.state === 'running' || !!props.collection?.busy,
  onRevision: value => { if (!state.value || state.value.data_revision !== value) void load() },
  fallback: () => load(),
})
const rows = computed<any[]>(() => state.value?.rows || [])
const rowsById = computed(() => new Map(rows.value.map(row => [row.id, row])))
const draftsById = computed(() => new Map(items.value.map(item => [item.id, item])))
const job = computed(() => commentProgressJob(progressFeed.supported.value ? progressFeed.snapshot.value : null, state.value, pending.value))
const prepareJob = computed(() => progressFeed.snapshot.value?.prepare_job || state.value?.prepare_job)
const processShowsError = computed(() => error.value && [job.value?.error, prepareJob.value?.error].includes(error.value))
const ratingSaved = computed(()=>Number(state.value?.rating_progress?.saved)||0)
const ratingRemaining = computed(()=>state.value?.rating_progress?.remaining??rows.value.length)
const ratingAction = computed(()=>ratingSaved.value&&ratingRemaining.value?`继续评分（${ratingRemaining.value} 条）`:ratingSaved.value?'更新候选（复用评分）':assessments.value.size?'补充 / 更新评分':'为全部评论评分')
const working = computed(() => pending.value || (prepareJob.value?.status === 'running' ? 'prepare' : job.value?.status === 'running' ? job.value.action : ''))
const candidateProcessing = computed(() => !!working.value || props.automation?.state === 'running'
  || !!props.collection?.busy || !!progressFeed.snapshot.value?.collection?.busy)
const locked = computed(() => projectLocked.value || !!working.value || !!props.blocked || props.automation?.state === 'running')
const editLocked = computed(() => locked.value || saving.value || rulesSaving.value)
const untranslated = computed(() => rows.value.filter(row => !hasCommentTranslation(row)).length)
const translationPending = computed(() => rows.value.filter(row => !hasCommentTranslation(row)
  && (!row.ai_issues?.translate || row.ai_issues.translate.status === 'deferred')).length)
const translationAction = computed(() => !untranslated.value ? '已全部有译文'
  : translationPending.value ? `补译未翻译评论（${translationPending.value} 条）` : '暂无可自动补译项')
const assessments = computed(() => new Map<string, any>((state.value?.available_assessments ?? (state.value?.filter_stale ? [] : state.value?.filter?.assessments || [])).map((item: any) => [item.id, item])))
const ranked = computed<any[]>(() => [...assessments.value.values()].sort((a, b) => Number(b.eligible) - Number(a.eligible) || b.score - a.score))
const textLimits = computed(() => state.value?.text_limits || defaultCommentTextLimits)
const textIssues = (item: any) => commentTextIssues(item, settings.value.language_mode, textLimits.value)
const blockingTextIssues = (item: any) => commentTextBlockingIssues(item, settings.value.language_mode, textLimits.value)
const hasLengthWarning = (item: any) => selectionOrigin(item) === 'manual' && textIssues(item).length > blockingTextIssues(item).length
function candidateDraft(row: any) {
  const draft = draftsById.value.get(row.id)
  return draft && draft.source_hash === row.source_hash ? draft : { ...editableRow(row), selection_origin: 'automatic' }
}
const candidateIssues = (row: any) => textIssues(candidateDraft(row))
const eligibleCandidates = computed<any[]>(() => ranked.value.length
  ? ranked.value.filter(row => row.eligible) : state.value?.filter_stale ? [] : (state.value?.filter?.items || []))
const eligibleCandidateIds = computed(() => new Set(eligibleCandidates.value.map(candidate => candidate.id)))
function candidateStatus(row: any) {
  const assessment = assessments.value.get(row.id)
  const eligible = assessment ? !!assessment.eligible : eligibleCandidateIds.value.has(row.id) ? true : null
  return commentCandidateReadiness(candidateDraft(row), settings.value.language_mode, textLimits.value, eligible)
}
const usableCandidates = computed(() => eligibleCandidates.value.filter(candidate => {
  const row = rowsById.value.get(candidate.id)
  return row && !row.review_blocked && !candidateIssues(row).length
}))
const candidateTarget = computed(() => Math.min(100, Math.max(0, Math.floor(Number(limit.value) || 0))))
// Naive UI 设置 precision 会等失焦才更新值；用整数校验保留逐键更新。
const validCandidateTarget = (value: number) => Number.isInteger(value)
const suggestions = computed(() => usableCandidates.value.slice(0, candidateTarget.value))
const hasIssues = (action:string) => rows.value.some(row=>row.ai_issues?.[action])
const issueCounts = computed(() => ({ filter: commentIssueCounts(rows.value, 'filter'), translate: commentIssueCounts(rows.value, 'translate') }))
function retryIds(action: CommentAction) { return rows.value.filter(row => ['failed','deferred'].includes(row.ai_issues?.[action]?.status)).map(row => row.id) }
function retry(action: CommentAction) { const ids = retryIds(action); if (ids.length) return process(action, false, { retry_failed: true, ids }) }
const reasons = computed(() => new Map(suggestions.value.map(item => [item.id, item.reason])))
const sortByScore = ref(false)
const readingOrder = ref<string[]>([])
const pageSize = 20
const visibleRows = computed(() => {
  const displayed = onlySuggested.value ? rows.value.filter(row => reasons.value.has(row.id)) : [...rows.value]
  if (working.value && readingOrder.value.length || sortByScore.value && ranked.value.length) {
    const order = new Map((working.value && readingOrder.value.length ? readingOrder.value : ranked.value.map(row => row.id)).map((id, index) => [id, index]))
    displayed.sort((a, b) => (order.get(a.id) ?? 9999) - (order.get(b.id) ?? 9999))
    return displayed
  }
  return chronologicalComments(displayed, rows.value)
})
const pageRows = computed(() => visibleRows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const snapshot = computed(() => JSON.stringify({ items: items.value, settings: settings.value, excluded_ids: excluded.value, target: candidateTarget.value, order_mode: orderMode.value }))
const isTimeOrdered = computed(() => chronologicalComments(items.value, rows.value).every((item, index) => item.id === items.value[index]?.id))
const dirty = computed(() => !!baseline.value && (baseline.value !== snapshot.value||styleEdited.value.size>0||styleReset.value.size>0))
const manualItems = computed(() => items.value.filter(item => item.selection_origin === 'manual'))
const automaticItems = computed(() => items.value.filter(item => item.selection_origin === 'automatic'))
function selectionOrigin(item: any): SelectionOrigin {
  return item?.selection_origin === 'manual' ? 'manual' : 'automatic'
}
function selectionLabel(item: any) {
  return selectionOrigin(item) === 'manual' ? '手动保留' : '自动选择'
}
function selectionTagType(item: any) {
  return selectionOrigin(item) === 'manual' ? 'success' : 'info'
}
const currentExclusions = computed(() => excluded.value.filter(item => rowsById.value.get(item.id)?.source_hash === item.source_hash))
const preparationIssues = computed(() => (preparation.value?.invalid_manual || []).filter((issue: any) => draftsById.value.has(issue.id)))
const modeOptions = [{ label: '中文', value: 'translated' }, { label: '原文', value: 'original' }, { label: '双语', value: 'bilingual' }]
const positionOptions = [{label: '左上角', value: 'top_left'}, {label: '顶部居中', value: 'top'}, {label: '右上角', value: 'top_right'}, {label: '左下角', value: 'bottom_left'}, {label: '底部居中', value: 'bottom'}, {label: '右下角', value: 'bottom_right'}]
const showChinese = computed(() => settings.value.language_mode !== 'original')
const showSource = computed(() => settings.value.language_mode !== 'translated')
const plannedItems = computed(() => items.value.filter(item => !blockingTextIssues(item).length))
const previewCandidate = computed(() => {
  const row = rowsById.value.get(temporaryPreviewId.value) || draftsById.value.get(temporaryPreviewId.value)
  return row ? candidateDraft(row) : null
})
const previewItems = computed(() => {
  const candidate = previewCandidate.value
  if (!candidate || isPlanned(candidate)) return plannedItems.value
  // 待选项也在同一完整时间轴试放，不把现有方案替换成单条预览。
  const trial = [...plannedItems.value.filter(item => item.id !== candidate.id), candidate]
  return orderMode.value === 'chronological' ? chronologicalComments(trial, rows.value) : trial
})
const previewSourceOnly = computed(() => !!temporaryPreviewId.value && settings.value.language_mode !== 'original'
  && !!previewCandidate.value && !previewCandidate.value.display_text?.trim())
const previewSettings = computed(() => previewSourceOnly.value ? { ...settings.value, language_mode: 'original' } : settings.value)
const previewInvalid = computed(() => !!settingsError.value || previewItems.value.some(item => commentTextBlockingIssues(item, previewSettings.value.language_mode, textLimits.value).length))
const excludedItems = computed(() => items.value.filter(item => blockingTextIssues(item).length))
const settingsError = computed(() => Object.values(settings.value).some(value => value === null) ? '请补齐排版参数。'
  : settings.value.duration > settings.value.interval ? `每条停留 ${settings.value.duration} 秒，超过出现间隔 ${settings.value.interval} 秒；请缩短停留或增大间隔。`
  : settings.value.min_font_size > settings.value.font_size ? '译文最小字号不能大于最大字号。'
  : settings.value.source_style_enabled && settings.value.source_min_font_size > settings.value.source_font_size ? '原文最小字号不能大于最大字号。' : '')
const timelineCapacity = computed<number | null>(() => Number(props.videoDuration) > 0 && Number(settings.value.interval) > 0
  ? Math.max(0, Math.floor((Number(props.videoDuration) - Number(settings.value.start) - Number(settings.value.duration)) / Number(settings.value.interval)) + 1)
  : preparation.value?.summary?.capacity ?? null)
const capacityExceeded = computed(() => timelineCapacity.value !== null && items.value.length > timelineCapacity.value)
const invalid = computed(() => !candidateTarget.value || !!settingsError.value || !!preparationIssues.value.length || !!excludedItems.value.length || capacityExceeded.value)
function plannedPosition(item: any) {
  const index = plannedItems.value.findIndex(row => row.id === item.id)
  return index < 0 ? '需调整文字 · 暂不能确认' : `第 ${index + 1} 条 · ${settings.value.start + index * settings.value.interval} 秒`
}
const planChanged = computed(() => !!revision.value && state.value?.overlay_revision !== revision.value)
function applyTextColors(id: string, source = false) {
  if (editLocked.value) return
  const patch = textColorPatch(id)
  if (patch) Object.entries(patch).forEach(([key,value])=>setStyle((source?'source_':'')+key,value))
}

watch(working, value => emit('busy-change', value), { immediate: true })
watch(() => dirty.value || rulesDirty.value, value => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(() => saving.value || rulesSaving.value, value => emit('saving-change', value), { immediate: true, flush: 'sync' })
watch(() => state.value?.configuration?.values?.['comments.limit'], value => emit('collection-limit', Number.isInteger(value) && value >= 1 && value <= 500 ? value : 50), { immediate: true, flush: 'sync' })
watch(onlySuggested, () => { page.value = 1 })
watch(() => !!previewCandidate.value && isPlanned(previewCandidate.value), value => {
  if (value) temporaryPreviewId.value = ''
})
watch(() => visibleRows.value.length, length => { page.value = Math.min(page.value, Math.max(1, Math.ceil(length / pageSize))) })
watch(working, (value, previous) => {
  if (value && !previous) readingOrder.value = visibleRows.value.map(row => row.id)
  else if (!value) readingOrder.value = []
}, { immediate: true })
function hydrate(value: any) {
  styleEdited.value.clear();styleReset.value.clear()
  releasedSelections.value = []
  items.value = structuredClone(value.overlay?.items || []).map((row: any) => ({ ...row, source_text: row.source_text ?? row.text, selection_origin: selectionOrigin(row) }))
  orderMode.value = value.overlay?.order_mode === 'manual' ? 'manual' : 'chronological'
  excluded.value = structuredClone(value.overlay?.excluded_ids || [])
  preparation.value = null
  limit.value = value.configuration?.values?.['comments.count'] ?? limit.value
  settings.value = { ...initialSettings, ...value.defaults, ...(value.overlay?.settings || {}) }
  revision.value = value.overlay_revision
  baseline.value = snapshot.value
}
function load(reset = false, clearError = true): Promise<void> {
  if (disposed || typeof document !== 'undefined' && document.hidden && !reset) return Promise.resolve()
  if (fullRead) {
    readAgain = true; resetAfterRead ||= reset; clearAfterRead ||= clearError
    return fullRead
  }
  const ticket = epoch, sequence = ++readSequence, uid = props.uid, controller = new AbortController()
  fullController = controller
  // 先登记读取 Promise，再执行请求；同步连接异常也必须能释放读取状态。
  const read = Promise.resolve().then(async () => { try {
    if (disposed || ticket !== epoch || controller.signal.aborted) return
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/comments`, { cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '评论读取失败')
    if (disposed || ticket !== epoch || sequence !== readSequence) return
    const keepDraft = dirty.value || pending.value === 'prepare'
    prompt.value = value.configuration?.values?.['comments.prompt'] || value.default_filter_prompt || ''
    if (!state.value) limit.value=value.configuration?.values?.['comments.count']||10
    state.value = value
    if (clearError) error.value = ''
    if (reset || !keepDraft) hydrate(value)
  } catch (e: any) { if (!disposed && ticket === epoch && sequence === readSequence && e.name !== 'AbortError') error.value = e.name === 'TimeoutError' ? '评论读取超时，已显示内容保留，请稍后重新读取' : e.message || '评论读取失败' }
  finally {
    if (!disposed && ticket === epoch && fullRead === read) {
      fullRead = undefined
      fullController = undefined
      if (readAgain) {
        const nextReset = resetAfterRead, nextClear = clearAfterRead
        readAgain = resetAfterRead = clearAfterRead = false
        void load(nextReset, nextClear)
      }
    }
  } })
  fullRead = read
  return read
}
watch(() => [props.uid, props.version], (_value, old) => {
  if (!old || props.uid !== old[0]) {
    epoch++; fullController?.abort(); fullRead = undefined; readAgain = resetAfterRead = clearAfterRead = false; state.value = null; items.value = []; orderMode.value = 'chronological'; excluded.value = []; preparation.value = null; translatingId.value = ''; temporaryPreviewId.value = ''; previewFocusId.value = ''; highlightedId.value = ''; rowElements.clear(); baseline.value = ''; revision.value = ''; pending.value = ''; error.value = ''; expandedRows.value = []; prompt.value = ''; readingOrder.value = []
  }
  void load()
}, { immediate: true })
// 自动流程采集不由本页按钮触发；阶段变化时也要更新，并保留人工未保存的草稿。
watch(() => props.automation?.state, (value, previous) => {
  if (value === 'running' || value === 'done' || previous === 'running') void load()
})

async function process(action: 'translate' | 'filter', force = false, options:{ retry_failed?: boolean; only_cached?: boolean; ids?: string[] } = {}) {
  if (editLocked.value || !rows.value.length || (action === 'filter' && (rulesDirty.value || !prompt.value.trim() || !limit.value))) return
  if (observing.value && !(await canEdit()) || editLocked.value) { error.value = projectLockReason.value || '项目正在运行，请完成后再处理评论'; return }
  const ticket = epoch, uid = props.uid
  pending.value = action; error.value = ''
  void progressFeed.refresh()
  try {
    const response = await fetch(`/api/projects/${uid}/comments/${action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...(action === 'filter' ? { prompt: prompt.value, limit: limit.value, ...(force ? { force: true } : {}) } : {}), ...options }),
    })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '评论处理失败，已完成的翻译和评分仍保留')
    if (disposed || ticket !== epoch) return
    readSequence++; state.value = value
    if (action === 'translate') {
      for (const item of items.value) {
        const row = value.rows?.find((row: any) => row.id === item.id && row.source_hash === item.source_hash)
        if (!item.display_text?.trim() && hasCommentTranslation(row)) item.display_text = row.text_zh
      }
      preparation.value = null
    }
    const result = commentJobFeedback(value.job, action)
    message[result.type](result.text)
  } catch (e: any) { if (!disposed && ticket === epoch) error.value = e.message || '请求中断，请重新读取确认进度' }
  finally { if (!disposed && ticket === epoch) { pending.value = ''; await load(false, false); void progressFeed.refresh() } }
}
async function stop() {
  const ticket = epoch
  const id = prepareJob.value?.status === 'running' ? prepareJob.value.id : job.value?.id
  if (!id) return
  try {
    const response = await fetch(`/api/projects/${props.uid}/comments/cancel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: id }),
    })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '停止失败')
    if (!disposed && ticket === epoch) await load()
  } catch (e: any) { if (!disposed && ticket === epoch) error.value = e.message }
}
function selected(id: string) { return items.value.some(row => row.id === id) }
function editableRow(row: any) {
  return { id: row.id, source_hash: row.source_hash, text: row.text, author: row.author, url: row.url, timestamp: row.timestamp, display_text: row.text_zh || '', source_text: row.text, selection_origin: 'manual' }
}
function toggle(row: any, checked: boolean) {
  if (editLocked.value) return
  releasedSelections.value = []
  if (!checked) {
    items.value = items.value.filter(item => item.id !== row.id)
    excluded.value = [...excluded.value.filter(item => item.id !== row.id), { id: row.id, source_hash: row.source_hash }]
    preparation.value = null
    return
  }
  if (selected(row.id)) return
  if (items.value.length >= 100) { message.warning('最多选择 100 条上屏评论'); return }
  items.value.push(editableRow(row))
  if (orderMode.value === 'chronological') items.value = chronologicalComments(items.value, rows.value)
  excluded.value = excluded.value.filter(item => item.id !== row.id)
  preparation.value = null
}
function isExcluded(row: any) { return excluded.value.some(item => item.id === row.id && item.source_hash === row.source_hash) }
function restoreCandidate(row: any) {
  if (!editLocked.value) { excluded.value = excluded.value.filter(item => item.id !== row.id); preparation.value = null }
}
function setSelectionOrigin(item: any, origin: 'manual' | 'automatic') {
  if (!editLocked.value && item) { releasedSelections.value = []; item.selection_origin = origin; preparation.value = null }
}
function releaseManualSelections() {
  if (editLocked.value) return
  const released = manualItems.value
  if (!released.length) return
  releasedSelections.value = released.map(item => ({ id: item.id, source_hash: item.source_hash, origin: selectionOrigin(item) }))
  released.forEach(item => { item.selection_origin = 'automatic' })
  preparation.value = null
}
function undoSelectionRelease() {
  if (editLocked.value) return
  for (const previous of releasedSelections.value) {
    const item = items.value.find(row => row.id === previous.id && row.source_hash === previous.source_hash)
    if (item?.selection_origin === 'automatic') item.selection_origin = previous.origin
  }
  releasedSelections.value = []; preparation.value = null
}
function editText(item: any, field: string, value: string) {
  if (editLocked.value || item[field] === value) return
  releasedSelections.value = []
  item[field] = value; item.selection_origin = 'manual'; preparation.value = null
}
function sortByTime() {
  if (editLocked.value) return
  items.value = chronologicalComments(items.value, rows.value)
  orderMode.value = 'chronological'; preparation.value = null
}
function setRowElement(id: string, element: any) {
  if (element) rowElements.set(id, element)
  else rowElements.delete(id)
}
function isPlanned(row: any) { return plannedItems.value.some(item => item.id === row?.id && item.source_hash === row.source_hash) }
async function previewComment(row: any) {
  if (!row?.id) return
  if (!isPlanned(row) && plannedItems.value.length >= 100) {
    message.info('当前已选满 100 条评论，请先移除一条后再试放未选评论')
    return
  }
  temporaryPreviewId.value = isPlanned(row) ? '' : row.id
  previewFocusId.value = row.id; focusRequest.value++; highlightedId.value = row.id
  if (!planExpanded.value.includes('plan')) planExpanded.value = [...planExpanded.value, 'plan']
  const ticket = epoch
  await nextTick()
  if (!disposed && ticket === epoch) previewElement.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
async function locateComment(id: string) {
  if (!rowsById.value.has(id)) { message.info('这条评论已不在当前来源中，请在待上屏草稿中查看'); return }
  const ticket = epoch
  if (!visibleRows.value.some(row => row.id === id)) {
    onlySuggested.value = false
    await nextTick()
    if (disposed || ticket !== epoch) return
  }
  const index = visibleRows.value.findIndex(row => row.id === id)
  page.value = Math.floor(index / pageSize) + 1
  highlightedId.value = id
  if (!expandedRows.value.includes(id)) expandedRows.value = [...expandedRows.value, id]
  await nextTick()
  if (!disposed && ticket === epoch) {
    const element = rowElements.get(id)
    element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    element?.focus({ preventScroll: true })
  }
}
function returnToPlanPreview() {
  temporaryPreviewId.value = ''
  // 停止试放只恢复实际方案，保留播放头在视频时间轴上的位置。
  previewFocusId.value = ''
}
async function translateRow(row: any) {
  if (editLocked.value || hasCommentTranslation(row) || row.ai_issues?.translate?.status === 'blocked') return
  const ticket = epoch
  translatingId.value = row.id
  try { await process('translate', false, { ids: [row.id], retry_failed: true }) }
  finally { if (ticket === epoch) translatingId.value = '' }
}
function expand(id: string) { expandedRows.value = expandedRows.value.includes(id) ? expandedRows.value.filter(key => key !== id) : [...expandedRows.value, id] }
function assessmentLabel(id: string) {
  const item = assessments.value.get(id)
  return item ? `${item.score} 分` : reasons.value.has(id) ? 'AI 建议' : '未入选'
}
function assessmentReason(id: string) {
  const item = assessments.value.get(id)
  const row = rowsById.value.get(id)
  const status = row ? candidateStatus(row).status : 'pending-score'
  return item ? `${item.eligible ? status === 'pending-translation' ? '评分符合条件，待翻译后检查上屏条件。'
    : status === 'needs-text' ? '评分符合条件，上屏文字需要调整。'
    : reasons.value.has(id) ? candidateProcessing.value ? '暂列可用候选。' : '当前可用候选。'
    : '评分符合条件，暂不在当前前 N 条中。' : 'AI 判断不符合评分标准，仍可手动选择。'}${item.reason}`
    : reasons.value.get(id) || '尚无有效评分，评分完成后可查看判断理由。'
}
async function prepareTarget() {
  if (editLocked.value || !rows.value.length || rulesDirty.value || !prompt.value.trim() || !candidateTarget.value || settingsError.value) return
  if (observing.value && !(await canEdit()) || editLocked.value) { error.value = projectLockReason.value || '项目正在运行，请完成后再准备评论'; return }
  releasedSelections.value = []
  const ticket = epoch, uid = props.uid
  const body = { ...JSON.parse(snapshot.value), prompt: prompt.value, revision: revision.value, source_version: state.value?.source_version }
  pending.value = 'prepare'; error.value = ''
  void progressFeed.refresh()
  try {
    const response = await fetch(`/api/projects/${uid}/comments/prepare`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '评论准备失败；已完成的评分和译文保留，当前草稿未修改')
    if (disposed || ticket !== epoch) return
    if (!Array.isArray(value.preparation?.draft?.items)) throw new Error('未收到评论草稿，请重新读取进度后重试')
    readSequence++; state.value = value
    preparation.value = value.preparation
    items.value = structuredClone(value.preparation.draft.items)
    orderMode.value = value.preparation.draft.order_mode === 'manual' ? 'manual' : 'chronological'
    excluded.value = structuredClone(value.preparation.draft.excluded_ids || [])
    message[value.preparation.warnings?.length ? 'warning' : 'success'](`已准备 ${value.preparation.summary?.ready ?? plannedItems.value.length} 条可用评论，请检查后确认保存`)
  } catch (e: any) { if (!disposed && ticket === epoch) error.value = e.message || '请求中断，当前草稿保留；请重新读取进度后继续准备' }
  finally { if (!disposed && ticket === epoch) { pending.value = ''; await load(false, false); void progressFeed.refresh() } }
}
function move(index: number, direction: number) {
  if (editLocked.value) return
  const other = index + direction
  if (other < 0 || other >= items.value.length) return
  ;[items.value[index], items.value[other]] = [items.value[other], items.value[index]]
  orderMode.value = 'manual'
  preparation.value = null
}
function confirmPlan() {
  if (invalid.value || editLocked.value || !state.value) return
  const body = { ...JSON.parse(snapshot.value), items: JSON.parse(JSON.stringify(plannedItems.value)), revision: revision.value, configuration_fingerprint:state.value.configuration?.fingerprint, settings_explicit:[...styleEdited.value],settings_reset:[...styleReset.value] }, uid = props.uid, ticket = epoch
  const excludedNotice = excludedItems.value.length ? ` 已排除 ${excludedItems.value.length} 条文字不符合要求的草稿，不会保存这些草稿的编辑；完整原文和已完成译文仍保留。` : ''
  saving.value = true
  const close = () => { if (ticket === epoch) saving.value = false }
  dialog.info({
    title: plannedItems.value.length ? `确认 ${plannedItems.value.length} 条上屏评论` : state.value.overlay?.items?.length ? '清空已确认方案' : '保存评论设置',
    content: plannedItems.value.length ? `从第 ${settings.value.start} 秒开始，每 ${settings.value.interval} 秒一条，停留 ${settings.value.duration} 秒。仅保存，不会自动合成。${excludedNotice}` : state.value.overlay?.items?.length ? '清空后，新合成无法叠加评论。已有视频不受影响。' : '保存样式与时间设置；尚未选择上屏评论。',
    positiveText: '确认保存', negativeText: '取消', maskClosable: false,
    onNegativeClick: close, onClose: close,
    onPositiveClick: async () => {
      try {
        if (disposed || ticket !== epoch || props.uid !== uid || locked.value || observing.value && !(await canEdit())) {
          error.value = projectLockReason.value || '项目正在运行，请完成后再确认上屏方案'
          return false
        }
        if (disposed || ticket !== epoch || props.uid !== uid || locked.value) return false
        const response = await fetch(`/api/projects/${uid}/comments/overlay`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        const value = await response.json()
        if (!response.ok) throw new Error(value.message || '方案保存失败')
        if (disposed || ticket !== epoch) return
        readSequence++; state.value = value; hydrate(value); message.success('上屏方案已确认，尚未开始合成')
      } catch (e: any) { if (!disposed && ticket === epoch) error.value = e.message || '方案保存失败' }
      finally { close() }
    },
  })
}
let leavePrompt: Promise<boolean> | null = null
function beforeLeave() {
  if (pending.value === 'prepare') { message.warning('正在准备上屏草稿，请等待结果返回或停止本次准备后再离开'); return false }
  if (saving.value || rulesSaving.value) { message.warning('请先完成评论设置保存或上屏方案确认'); return false }
  if (!dirty.value&&!rulesDirty.value) return true
  if (leavePrompt) return leavePrompt
  leavePrompt = new Promise<boolean>(resolve => {
    const finish = (yes: boolean) => { leavePrompt = null; resolve(yes) }
    dialog.warning({ title: '评论上屏草稿尚未确认', content: '离开会丢失本页未保存的勾选、文字和排版设置。已完成的翻译仍保留。', positiveText: '放弃草稿并离开', negativeText: '继续编辑', onPositiveClick: () => finish(true), onNegativeClick: () => finish(false), onClose: () => finish(false), onMaskClick: () => finish(false) })
  })
  return leavePrompt
}
onBeforeRouteLeave(beforeLeave)
onBeforeRouteUpdate((to, from) => to.params.uid !== from.params.uid || to.params.key !== from.params.key ? beforeLeave() : true)
function unload(event: BeforeUnloadEvent) { if (pending.value==='prepare'||dirty.value||rulesDirty.value||saving.value||rulesSaving.value) { event.preventDefault(); event.returnValue = '' } }
function visibilityResume() { if (!document.hidden) void load() }
function showSettings() { if (rulesSection.value) { rulesSection.value.open = true; rulesSection.value.scrollIntoView({ behavior: 'smooth', block: 'start' }) } }
function locate() {
  if (route.hash === '#comments-studio') rootEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  if (route.hash === '#comment-settings') showSettings()
}
defineExpose({ showSettings })
watch(() => route.hash, locate, { flush: 'post' })
onMounted(() => { window.addEventListener('beforeunload', unload); document.addEventListener('visibilitychange', visibilityResume); locate() })
onBeforeUnmount(() => { disposed = true; epoch++; fullController?.abort(); emit('busy-change', ''); emit('dirty-change', false); emit('saving-change', false); if (typeof window !== 'undefined') { window.removeEventListener('beforeunload', unload); document.removeEventListener('visibilitychange', visibilityResume) } })
</script>

<template>
  <div id="comments-studio" ref="rootEl" class="comment-studio">
    <n-alert v-if="projectLocked || blocked" type="info" :show-icon="false" class="notice">{{ projectLockReason || '项目正在运行，请完成后再修改评论方案' }}；原文、评分和日志仍可查看。</n-alert>
    <CommentProcessPanel :progress="progressFeed.snapshot.value" :state="state" :automation="automation" :collection="collection" :pending="pending" @show-diagnostics="emit('show-diagnostics',$event)" @stop="stop" @refresh="progressFeed.refresh()" />
    <n-alert v-if="progressFeed.error.value" type="warning" :show-icon="false" class="notice">{{ progressFeed.error.value }} <n-button text size="tiny" @click="progressFeed.refresh()">重新读取进度</n-button></n-alert>
    <n-alert v-if="error && !processShowsError" type="error" :show-icon="false" class="notice">{{ error }} <n-button size="tiny" @click="load()">重新读取</n-button></n-alert>
    <div v-for="action in (['filter','translate'] as const)" :key="action" class="issue-summary">
      <p v-if="hasIssues(action)"><strong>{{ action==='filter'?'评分':'翻译' }}</strong><span v-if="issueCounts[action].failed"> · 处理失败 {{ issueCounts[action].failed }} 条</span><span v-if="issueCounts[action].deferred"> · 暂缓处理 {{ issueCounts[action].deferred }} 条</span><span v-if="issueCounts[action].blocked"> · 服务商审核拦截 {{ issueCounts[action].blocked }} 条</span>。已完成结果保留，可悬停逐条标签查看原因。</p>
      <p v-if="hasIssues(action)" class="muted">重试只补处理失败与暂缓项；已有审核拦截记录不会自动重发。<n-button text size="tiny" @click="emit('show-diagnostics', action)">查看{{ action==='filter'?'评分':'翻译' }}诊断 →</n-button></p>
    </div>
    <details v-if="issueCounts.filter.blocked || issueCounts.translate.blocked" class="processing-help"><summary>审核拦截如何处理</summary><p class="muted">请先核对原文和服务商返回原因。旧记录可能来自批次请求，不代表每条评论都被独立拒绝；重复点击不能解除拦截。无法处理的评论可先保留原文或暂不选入上屏。若服务配置有误，请先修复正常配置，再重新读取状态并按需补译。</p><div class="tools"><RouterLink :to="{path:'/settings',query:{section:'models'}}">核对模型与服务设置 →</RouterLink><n-button size="tiny" quaternary :disabled="locked" @click="load()">重新读取评论状态</n-button></div></details>
    <details id="comment-settings" ref="rulesSection" class="filter-settings comment-settings"><summary>自动化与评分设置</summary><ConfigurationForm :scope="'project:'+uid" :sections="['comments']" :paths="['comments.mode','comments.limit','comments.prompt','comments.failure_policy']" @dirty-change="rulesDirty=$event" @busy-change="rulesSaving=$event" @saved="load()"/></details>
    <template v-if="rows.length">
      <section class="preparation-card" aria-label="准备上屏评论">
        <div class="tools preparation-heading"><div><h4>准备上屏评论</h4><p>设定数量，自动复用评分和译文，只补缺少的部分。</p></div><n-tag v-if="dirty" type="warning" size="small" :bordered="false">草稿未确认</n-tag></div>
        <div class="tools preparation-controls"><label class="target-control"><span>目标数量</span><n-input-number v-model:value="limit" :disabled="editLocked" :min="1" :max="100" :step="1" :validator="validCandidateTarget" :update-value-on-input="true" style="width:104px" :input-props="{ 'aria-label': '目标上屏评论数量（不调用 AI）' }" /><span>条</span></label>
          <n-tooltip><template #trigger><n-button type="primary" :loading="working==='prepare'" :disabled="editLocked || rulesDirty || !prompt.trim() || !candidateTarget || !!settingsError" @click="prepareTarget">{{ working==='prepare' ? '正在准备…' : '准备目标数量评论' }}</n-button></template>保留手动选择与文字，按评分补齐或减少自动项；仅补缺失评分和译文。完成后核对草稿，再统一确认保存。</n-tooltip>
        </div>
        <div class="candidate-counts" aria-label="候选准备情况" aria-live="polite"><span>目标 <strong>{{ candidateTarget }}</strong> 条</span><span>待上屏 <strong>{{ items.length }}</strong> 条</span><span>手动保留 <strong>{{ manualItems.length }}</strong> 条</span><span>自动选择 <strong>{{ automaticItems.length }}</strong> 条</span><span v-if="currentExclusions.length">手动排除 <strong>{{ currentExclusions.length }}</strong> 条</span></div>
        <div v-if="manualItems.length || releasedSelections.length" class="tools selection-intent-actions" aria-label="管理手动保留"><n-tooltip v-if="manualItems.length"><template #trigger><n-button size="small" quaternary :disabled="editLocked" @click="releaseManualSelections()">取消全部手动保留（{{ manualItems.length }}）</n-button></template>仅解除保留，当前选中评论、文字和顺序不变；下次准备时允许按评分和目标数量替换或移除。确认保存后生效。</n-tooltip><span v-if="releasedSelections.length" class="muted">已解除 {{ releasedSelections.length }} 条的保留，当前评论仍选中。<n-button text size="small" :disabled="editLocked" @click="undoSelectionRelease">撤销解除</n-button></span></div>
        <p class="preparation-hint">按评分选入；{{ orderMode==='manual' ? '保留手动排列' : '上屏按发布时间从早到晚排列' }}。勾选或编辑会手动保留，取消勾选会排除该条。准备后统一确认保存。</p>
        <n-alert v-if="manualItems.length > candidateTarget" type="info" :show-icon="false" class="notice">已手动保留 {{ manualItems.length }} 条，超过目标 {{ candidateTarget }} 条。可以逐条移除，或将它们交给自动选择后再次准备。</n-alert>
        <div v-if="preparation" class="preparation-result" aria-live="polite"><strong>上次准备目标 {{ preparation.summary?.target }} 条 · 可用 {{ preparation.summary?.ready ?? plannedItems.length }} 条</strong><span v-if="preparation.summary?.shortfall"> · 距目标还差 {{ preparation.summary.shortfall }} 条</span><div v-if="preparation.summary?.new_translations != null" class="preparation-totals"><span>新增译文 {{ preparation.summary.new_translations }} 条</span><span>采用已有译文 {{ preparation.summary.reused_translations }} 条</span><span>新增评分 {{ preparation.summary.new_ratings }} 条</span><span>可复用评分 {{ preparation.summary.reused_ratings }} 条</span></div><p v-for="(warning,index) in preparation.warnings || []" :key="index">{{ warning }}</p><small v-if="preparation.summary?.layout_checked === false">当前无可检测的视频画面，合成前还会检查实际画幅与时间容量。</small></div>
      </section>
      <div class="tools collection-summary"><div><h3>评论资料</h3><p class="muted">{{ rows.length }} 条原文 · 有效译文 {{ rows.length - untranslated }} 条<span v-if="ratingSaved"> · 已缓存评分 {{ ratingSaved }} 条</span></p></div><label class="compare"><n-switch v-model:value="compare" size="small" /> 对照原文</label></div>
      <n-collapse class="filter-panel">
        <n-collapse-item title="评分规则与更多处理" name="filter">
          <p class="muted">{{ prompt }} <ConfigurationSourceBadge :source="state?.configuration?.sources?.['comments.prompt']"/></p>
          <div class="tools filter-actions"><n-button size="small" :disabled="locked || rulesDirty || !prompt.trim() || !limit" @click="process('filter')">{{ ratingAction }}</n-button><n-button v-if="assessments.size" size="small" quaternary :disabled="locked||rulesDirty" title="忽略缓存，重新调用 LLM 评价全部评论" @click="process('filter', true)">全部重新评分</n-button><n-button v-if="ratingSaved && ratingRemaining" size="small" quaternary :disabled="locked||rulesDirty" @click="process('filter',false,{only_cached:true})">仅用已完成评分</n-button><n-button v-if="retryIds('filter').length" size="small" :disabled="locked||rulesDirty" @click="retry('filter')">重试未完成评分（{{ retryIds('filter').length }} 条）</n-button></div>
          <div class="tools filter-actions"><n-tooltip><template #trigger><n-button size="small" :disabled="locked || !translationPending" @click="process('translate')">{{ translationAction }}</n-button></template>为阅读补译全部未翻译评论，数量可能超过上屏目标；只补缺项，有效译文复用。</n-tooltip><n-button v-if="retryIds('translate').length" size="small" quaternary :disabled="locked" @click="retry('translate')">重试未完成翻译（{{ retryIds('translate').length }} 条）</n-button></div>
          <p v-if="state?.filter?.prompt && prompt.trim() !== state.filter.prompt" class="muted">标准已修改；更新评分前，列表仍显示上次结果。</p>
        </n-collapse-item>
      </n-collapse>
      <n-alert v-if="state?.filter_stale" type="warning" :show-icon="false" class="notice">评论列表已更新，变化项需重新处理；{{ ratingSaved }} 条有效评分可继续使用。</n-alert>
      <p v-else-if="state?.filter_outdated" class="muted">旧结果没有逐条分数，评分后可自由调整数量。</p>
      <p v-else-if="!candidateProcessing && !ratingRemaining && state?.filter?.generated_at && !eligibleCandidates.length" class="muted">本次评分未找到符合要求的评论，可以调整要求后重试。</p>
      <div class="tools list-controls"><div class="tools"><n-checkbox v-model:checked="onlySuggested">只看当前可用候选（{{ suggestions.length }}）</n-checkbox><n-tooltip v-if="ranked.length"><template #trigger><n-checkbox v-model:checked="sortByScore">按分数浏览</n-checkbox></template>只改变此评论列表的阅读顺序，不改变待上屏排列。默认按原平台发布时间从早到晚浏览。</n-tooltip></div><span class="muted">任意评论均可单独翻译、勾选并编辑上屏文字</span></div>
      <p v-if="candidateProcessing" class="candidate-summary" role="status">评论仍在处理中，候选与上屏检查尚未完成；评分和译文分批保存，待处理统计会随结果更新。</p>
      <p v-if="working && sortByScore && ranked.length" class="muted">处理期间保持当前阅读顺序；任务结束后按最新评分浏览，不影响上屏排列。</p>
      <div class="comment-list" aria-label="评论资料列表">
        <article v-for="(row, rowIndex) in pageRows" :key="row.id" :ref="el => setRowElement(row.id,el)" class="comment-row" :class="{ selected: selected(row.id), 'located-comment': highlightedId===row.id }" tabindex="-1" :aria-label="'评论：' + (row.author || '匿名观众')">
          <div class="comment-selection"><span class="comment-number" :title="`当前列表第 ${(page - 1) * pageSize + rowIndex + 1} 条`">{{ String((page - 1) * pageSize + rowIndex + 1).padStart(2, '0') }}</span><n-checkbox :checked="selected(row.id)" :disabled="editLocked" :aria-label="'选择评论：' + (row.author || '匿名观众')" @update:checked="toggle(row, $event)" /></div>
          <div class="comment-body">
            <header class="comment-heading"><div class="comment-identity"><div class="byline"><span class="author">{{ row.author || '匿名观众' }}</span><n-tag v-if="row.is_pinned" size="tiny" :bordered="false">置顶</n-tag><n-tag v-if="row.author_is_uploader" size="tiny" :bordered="false">作者</n-tag></div><div class="comment-facts"><span title="原平台的评论发布时间">{{ commentPublishedAt(row.timestamp) }}</span><span v-if="row.like_count != null" class="like-count" :aria-label="`${row.like_count} 个赞`"><AppIcon name="heart" :size="13"/>{{ row.like_count }}</span></div></div>
              <div class="comment-state-group" role="group" aria-label="评分与选择状态">
              <n-tooltip v-if="assessments.has(row.id)"><template #trigger><n-tag size="tiny" :bordered="false" :type="reasons.has(row.id) ? 'info' : 'default'" tabindex="0">{{ assessmentLabel(row.id) }}</n-tag></template>{{ assessmentReason(row.id) }}</n-tooltip>
              <n-tooltip v-for="(issue,action) in row.ai_issues||{}" :key="action"><template #trigger><n-tag type="warning" size="tiny" :bordered="false" tabindex="0">{{ action==='filter'?'评分':'翻译' }}：{{ commentIssueLabel(issue) }}</n-tag></template>{{ issue.message }} {{ commentIssueContext(issue) }}<p v-for="(cause,index) in commentIssueCauses(issue)" :key="index">{{ cause }}</p></n-tooltip>
              <n-tooltip v-if="row.review_blocked"><template #trigger><n-tag size="tiny" :bordered="false" type="warning" tabindex="0">本地审查命中 · 不上屏</n-tag></template>本地内容审查影响上屏选择，与服务商翻译结果分别记录；原文与已有有效译文保留。</n-tooltip>
              <span v-if="candidateStatus(row).status === 'pending-score'" class="pending-translation">{{ working === 'filter' ? '待评分' : '尚未评分' }}</span>
              <span v-else-if="!hasCommentTranslation(row)" class="pending-translation">仅原文 · 未译</span>
              <n-tag v-if="selected(row.id)" size="tiny" :bordered="false" :type="selectionTagType(draftsById.get(row.id))">{{ selectionLabel(draftsById.get(row.id)) }}</n-tag>
              <n-tag v-else-if="isExcluded(row)" size="tiny" :bordered="false">手动排除</n-tag>
              </div>
            </header>
            <p v-if="candidateStatus(row).status === 'pending-translation'" class="row-pending">待翻译后检查上屏条件</p>
            <p v-else-if="candidateStatus(row).status === 'needs-text'" class="row-issue">上屏文字待调整 · {{ candidateStatus(row).issues.map(issue => issue.message).join('；') }}<small>可勾选后精简上屏文字</small></p>
            <p v-if="selected(row.id) && hasLengthWarning(candidateDraft(row))" class="row-issue">超出建议字数，仍按手动保留上屏</p>
            <p class="comment-copy" :class="{ clipped: !expandedRows.includes(row.id) && (hasCommentTranslation(row) ? row.text_zh : row.text).length > 180 }">{{ hasCommentTranslation(row) ? row.text_zh : row.text }}</p>
            <div v-if="compare && hasCommentTranslation(row)" class="original"><span class="copy-caption">原文</span><p :class="{ clipped: !expandedRows.includes(row.id) && row.text.length > 180 }">{{ row.text }}</p></div>
            <button v-if="(hasCommentTranslation(row) ? row.text_zh : row.text).length > 180 || (compare && row.text.length > 180)" type="button" class="expand-text" @click="expand(row.id)">{{ expandedRows.includes(row.id) ? '收起' : '展开全文' }}</button>
            <div class="row-actions" role="group" :aria-label="`${row.author || '匿名观众'}的评论操作`">
              <n-button class="comment-action preview-action" size="small" secondary type="info" :title="isPlanned(row) ? '将预览时间轴定位到本条的上屏位置' : '在完整时间轴中试放本条，不改变上屏方案'" @click="previewComment(row)"><template #icon><AppIcon name="locate" :size="15"/></template>{{ isPlanned(row) ? '定位预览' : '试放预览' }}</n-button>
              <n-button v-if="!hasCommentTranslation(row)" class="comment-action" size="small" quaternary :loading="translatingId===row.id" :disabled="editLocked || row.ai_issues?.translate?.status==='blocked'" :title="row.ai_issues?.translate?.status==='blocked' ? '已有服务商审核拦截，请先查看处理原因' : '只翻译本条，复用有效结果；不改变是否上屏'" @click="translateRow(row)"><template #icon><AppIcon name="language" :size="15"/></template>{{ row.ai_issues?.translate ? '重试本条翻译' : '翻译本条' }}</n-button>
              <n-button v-if="selected(row.id)" class="comment-action retention-action" size="small" :secondary="draftsById.get(row.id)?.selection_origin==='manual'" :quaternary="draftsById.get(row.id)?.selection_origin!=='manual'" :type="draftsById.get(row.id)?.selection_origin==='manual' ? 'success' : 'default'" :aria-pressed="draftsById.get(row.id)?.selection_origin==='manual'" :disabled="editLocked" :title="draftsById.get(row.id)?.selection_origin==='manual' ? '解除保留，当前仍选中；下次准备时允许自动替换' : '固定保留此条，下次准备时不会被自动替换'" @click="setSelectionOrigin(draftsById.get(row.id), draftsById.get(row.id)?.selection_origin==='manual' ? 'automatic' : 'manual')"><template #icon><AppIcon name="bookmark" :size="15"/></template>{{ draftsById.get(row.id)?.selection_origin==='manual' ? '取消手动保留' : '手动保留' }}</n-button>
              <n-button v-if="!selected(row.id) && isExcluded(row)" class="comment-action" size="small" quaternary :disabled="editLocked" @click="restoreCandidate(row)"><template #icon><AppIcon name="refresh" :size="15"/></template>恢复自动候选</n-button>
              <span v-if="hasCommentTranslation(row)" class="translation-ready"><AppIcon name="check" :size="13"/>已有译文</span>
            </div>
            <details v-if="row.ai_issues?.filter || row.ai_issues?.translate || row.review_blocked" class="row-processing-reasons"><summary>查看处理原因</summary><div v-for="action in (['filter','translate'] as const).filter(action => row.ai_issues?.[action])" :key="action"><strong>{{ action==='filter'?'评分':'翻译' }}：{{ commentIssueLabel(row.ai_issues[action]) }}</strong><p>{{ row.ai_issues[action].message }}</p><p v-for="(cause,index) in commentIssueCauses(row.ai_issues[action])" :key="index">{{ cause }}</p><p>{{ commentIssueContext(row.ai_issues[action]) }}</p></div><div v-if="row.review_blocked"><strong>本地审查命中 · 不上屏</strong><p v-for="(reason,index) in row.review_reasons || []" :key="index">{{ reason }}</p><p>本地审查影响上屏选择，与服务商翻译结果分别记录；不会删除完整原文或已有有效译文。</p></div></details>
          </div>
        </article>
      </div>
      <n-pagination v-if="visibleRows.length > pageSize" v-model:page="page" :item-count="visibleRows.length" :page-size="pageSize" class="pagination" />
    </template>
    <p v-else-if="!automation || ['none','done','stopped'].includes(automation.state)" class="muted">{{ state ? '尚无评论正文，可手动获取热门评论。' : '正在读取评论…' }}</p>

    <n-collapse v-model:expanded-names="planExpanded" class="plan-panel">
      <n-collapse-item :title="`待上屏 · ${items.length} 条${excludedItems.length ? ` · 需调整 ${excludedItems.length} 条` : ''}`" name="plan">
        <template #header-extra><n-tag size="small" :type="dirty ? 'warning' : 'default'" :bordered="false">{{ dirty ? '未确认修改' : items.length ? '已确认方案' : '未选择' }}</n-tag></template>
        <n-alert v-if="planChanged && dirty" type="warning" :show-icon="false" class="notice">已保存方案发生变化，请先保留自己的文字，再通过“重置草稿”重新读取。</n-alert>
        <div class="tools plan-selection-tools"><span class="muted">手动保留 {{ manualItems.length }} 条 · 自动选择 {{ automaticItems.length }} 条<template v-if="currentExclusions.length"> · 已手动排除 {{ currentExclusions.length }} 条</template></span></div>
        <div class="tools order-controls"><span class="muted">上屏顺序：{{ orderMode==='manual' ? '手动排列' : isTimeOrdered ? '发布时间 · 早 → 晚' : '原方案顺序' }}</span><n-tooltip><template #trigger><n-button size="tiny" quaternary :disabled="editLocked || !items.length || orderMode==='chronological' && isTimeOrdered" @click="sortByTime">按发布时间排列</n-button></template>按原平台评论发布时间从早到晚排列；同一时间或缺少时间时保持采集顺序。上下移动可切换为手动排列，独立于是否手动保留某条评论。</n-tooltip></div>
        <n-alert v-if="excludedItems.length || preparationIssues.length" type="warning" :show-icon="false" class="notice">手动选择仍保留，请调整提示的文字或移除该条后再确认；不会自动丢弃这些草稿。<p v-for="issue in preparationIssues" :key="issue.id">{{ draftsById.get(issue.id)?.author || '评论' }}：{{ issue.reasons.join('；') }}</p></n-alert>
        <n-alert v-if="capacityExceeded" type="warning" :show-icon="false" class="notice">当前视频与出现节奏最多容纳 {{ timelineCapacity }} 条，已保留 {{ items.length }} 条。请减少选择，或在右侧缩短间隔、停留时间后再确认。</n-alert>
        <div class="plan-grid">
          <div class="comment-workbench plan-preview-column" role="region" aria-label="上屏预览与评论编辑">
          <div ref="previewElement" class="preview-anchor"><div v-if="temporaryPreviewId" class="tools temporary-preview"><span>完整时间轴试放 · {{ previewCandidate?.author || '匿名观众' }} · 上屏选择保持不变<template v-if="previewSourceOnly"> · 尚无译文，临时显示原文</template></span><n-button size="tiny" quaternary @click="returnToPlanPreview">结束试放</n-button></div><n-alert v-if="temporaryPreviewId && previewInvalid" type="info" :show-icon="false" class="notice">试放评论的文字或排版尚不适合上屏，可返回列表后调整。<n-button text size="tiny" @click="locateComment(temporaryPreviewId)">返回评论列表 ↑</n-button></n-alert><CommentPreview v-if="previewItems.length" :key="uid" :uid="uid" :items="previewItems" :settings="previewSettings" :invalid="previewInvalid" :video-duration="videoDuration" :focus-id="previewFocusId" :focus-request="focusRequest" :temporary="!!temporaryPreviewId" @locate-comment="locateComment" />
          <p v-else class="muted">{{ items.length ? '所选评论暂不可上屏，请翻译、精简文字或改选可用评论。' : '选择评论后预览；也可从上方列表临时预览任意评论。' }}</p></div>
          <p v-if="excludedItems.length" class="exclusion-summary">{{ excludedItems.length }} 条文字待调整，暂不进入预览。草稿保留，全部符合要求后可统一确认。</p>
          <div class="ordered-list" role="region" aria-label="已选评论编辑区">
            <article v-for="(item, index) in items" :key="item.id" class="edit-row" :class="{ excluded: blockingTextIssues(item).length, 'is-focused': previewFocusId===item.id }" :aria-label="`第${index+1}条上屏评论：${item.author || '匿名观众'}`">
              <header class="edit-row-heading">
                <div class="edit-identity"><span class="edit-number">{{ String(index + 1).padStart(2, '0') }}</span><div><h4 class="edit-position">{{ plannedPosition(item) }}</h4><div class="edit-meta"><span class="author">{{ item.author || '匿名观众' }}</span><span title="原平台的评论发布时间">{{ commentPublishedAt(item.timestamp ?? rowsById.get(item.id)?.timestamp) }}</span></div></div></div>
                <n-tag size="small" :bordered="false" :type="selectionTagType(item)">{{ selectionLabel(item) }}</n-tag>
              </header>
              <div class="bilingual-editor" :class="{ 'source-first': settings.source_first }">
                <div v-if="showChinese" class="chinese-editor"><span class="edit-label">中文上屏文字 <small>{{ commentTextLength(item.display_text) }} / {{ textLimits.translated }} 字符</small></span>
                  <n-input :value="item.display_text" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" :disabled="editLocked" :status="textIssues(item).some(issue => issue.field === 'display_text') ? 'warning' : undefined" placeholder="尚无译文，可先翻译本条，或在此填写" :input-props="{ 'aria-label': '第 ' + (index + 1) + ' 条中文上屏文字' }" @update:value="editText(item,'display_text',$event)" />
                </div>
                <div v-if="showSource" class="source-editor"><span class="edit-label">原文上屏文字 <small>{{ commentTextLength(item.source_text) }} / {{ textLimits.original }} 字符</small></span>
                  <n-input :value="item.source_text" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" :disabled="editLocked" :status="textIssues(item).some(issue => issue.field === 'source_text') ? 'warning' : undefined" :input-props="{ 'aria-label': '第 ' + (index + 1) + ' 条原文上屏文字' }" @update:value="editText(item,'source_text',$event)" />
                </div>
              </div>
              <p v-if="textIssues(item).length" class="row-issue">{{ textIssues(item).map(issue => issue.message).join('；') }}<small v-if="hasLengthWarning(item)">超出建议字数，仍按手动保留上屏；字号达到下限后会扩展高度，请留意停留时间。</small></p>
              <div class="edit-actions" role="group" :aria-label="`第${index+1}条上屏评论操作`">
                <n-button class="comment-action preview-action" size="small" secondary type="info" @click="previewComment(item)"><template #icon><AppIcon name="locate" :size="15"/></template>{{ isPlanned(item) ? '定位预览' : '试放预览' }}</n-button>
                <n-button class="comment-action retention-action" size="small" :secondary="item.selection_origin==='manual'" :quaternary="item.selection_origin!=='manual'" :type="item.selection_origin==='manual' ? 'success' : 'default'" :aria-pressed="item.selection_origin==='manual'" :disabled="editLocked" :title="item.selection_origin==='manual' ? '解除保留，当前仍选中；下次准备时允许自动替换' : '固定保留此条，下次准备时不会被自动替换'" @click="setSelectionOrigin(item,item.selection_origin==='manual' ? 'automatic' : 'manual')"><template #icon><AppIcon name="bookmark" :size="15"/></template>{{ item.selection_origin==='manual' ? '取消手动保留' : '手动保留' }}</n-button>
                <n-button v-if="!hasCommentTranslation(rowsById.get(item.id))" class="comment-action" size="small" quaternary :disabled="editLocked || rowsById.get(item.id)?.ai_issues?.translate?.status==='blocked' || !rowsById.has(item.id)" @click="translateRow(rowsById.get(item.id))"><template #icon><AppIcon name="language" :size="15"/></template>翻译本条</n-button>
                <div class="reorder-actions" role="group" :aria-label="`调整第${index+1}条的顺序`"><n-button size="small" quaternary :disabled="editLocked || index === 0" :aria-label="'上移第 ' + (index + 1) + ' 条'" title="上移" @click="move(index, -1)"><template #icon><AppIcon name="arrow-up" :size="15"/></template></n-button><n-button size="small" quaternary :disabled="editLocked || index === items.length - 1" :aria-label="'下移第 ' + (index + 1) + ' 条'" title="下移" @click="move(index, 1)"><template #icon><AppIcon name="arrow-down" :size="15"/></template></n-button></div>
                <n-button class="comment-action remove-action" size="small" quaternary :disabled="editLocked" @click="toggle(item, false)"><template #icon><AppIcon name="trash" :size="15"/></template>移除</n-button>
              </div>
            </article>
          </div>
          </div>
          <div class="layout-controls" role="region" aria-label="评论节奏与排版参数">
            <h4>出现节奏与排版</h4>
            <p class="layout-strategy">先按最大宽度换行，在字号范围内适应目标高度；达到最小字号后仍放不下时，完整扩展高度。</p>
            <n-tooltip><template #trigger><span class="limit-hint" tabindex="0">自动候选建议：中文 {{ textLimits.translated }} · 原文 {{ textLimits.original }} 字符 ⓘ</span></template>自动筛选使用的建议字数，不是 B 站要求。手动保留可以超出，仍完整预览和使用；达到最小字号后扩展高度。只检查当前显示语言，正文为空仍需补齐。</n-tooltip>
            <TextColorPresets kind="评论" :color="settings.color" :outline-color="settings.outline_color" :outline="settings.outline" :source-enabled="settings.source_style_enabled" :source-color="settings.source_color" :source-outline-color="settings.source_outline_color" :source-outline="settings.source_outline" :disabled="editLocked" @select="applyTextColors" @enable-source="setStyle('source_style_enabled',true)" />
            <section v-for="group in styleGroups" :key="group.name" class="language-style-group"><h5>{{ group.name }}</h5>
              <div v-for="field in group.fields" :key="field.path" class="style-field" :class="{'font-range-field':fontRangeFields(field).length}" role="group" :aria-label="fontRangeFields(field).length ? fontRangeLabel(field) : field.label">
                <template v-if="fontRangeFields(field).length">
                  <span :title="field.help">{{ fontRangeLabel(field) }}</span>
                  <div class="font-range-inputs"><div v-for="(bound,index) in fontRangeFields(field)" :key="bound.path" class="font-range-bound" role="group" :aria-label="`${fontRangeLabel(field)}${index===0?'最小值':'最大值'}`"><span>{{ index===0?'最小':'最大' }}<small><ConfigurationSourceBadge :source="styleSource(bound.path.slice(9))" :inherited="!ownsStyle(bound.path.slice(9))"/><ParameterResetButton :label="bound.label" action="恢复项目基础值" help="移除这一项的项目单独设置，恢复创建或上次应用时保留的基础值；确认上屏方案后保存。" :disabled="editLocked || !canInheritStyle(bound.path.slice(9))" @reset="inheritStyle(bound.path.slice(9))"/></small></span><ParamControl :model-value="settings[bound.path.slice(9)]" :field="bound" :disabled="editLocked" @update:model-value="setStyle(bound.path.slice(9),$event)"/></div></div>
                </template>
                <template v-else>
                  <span class="style-source" :title="field.help"><span class="style-name">{{ field.label }}</span><small><ConfigurationSourceBadge :source="styleSource(field.path.slice(9))" :inherited="!ownsStyle(field.path.slice(9))"/><ParameterResetButton :label="field.label" action="恢复项目基础值" help="移除这一项的项目单独设置，恢复创建或上次应用时保留的基础值；确认上屏方案后保存。" :disabled="editLocked || !canInheritStyle(field.path.slice(9))" @reset="inheritStyle(field.path.slice(9))"/></small></span>
                  <div class="style-control"><ParamControl :model-value="settings[field.path.slice(9)]" :field="field" :disabled="editLocked" @update:model-value="setStyle(field.path.slice(9),$event)"/></div>
                </template>
              </div>
            </section>
            <n-alert v-if="settingsError" type="warning" :show-icon="false">{{ settingsError }}</n-alert>
          </div>
        </div>
        <div class="tools save-bar">
          <RouterLink :to="`/projects/${uid}/steps/assemble`">去合成成片 →</RouterLink>
          <div class="tools"><n-button v-if="dirty" size="small" :disabled="editLocked" @click="dialog.warning({ title: '放弃当前草稿？', content: '重新读取已确认方案，已完成的翻译保留。', positiveText: '放弃草稿', negativeText: '继续编辑', onPositiveClick: () => load(true) })">重置草稿</n-button><n-button type="primary" size="small" :disabled="!state || invalid || editLocked || (!dirty && !!baseline && !excludedItems.length)" @click="confirmPlan">确认上屏方案（{{ plannedItems.length }} 条）</n-button></div>
        </div>
      </n-collapse-item>
    </n-collapse>
  </div>
</template>

<style scoped>
.preparation-card{margin:16px 0 20px;padding:18px;border:1px solid color-mix(in srgb,var(--df-accent) 28%,var(--df-border));border-radius:10px;background:color-mix(in srgb,var(--df-accent) 4%,transparent)}
.preparation-heading h4{margin:0;font-size:14px;font-weight:600}.preparation-heading p{margin:5px 0 0;color:var(--df-muted);font-size:12px;line-height:1.7}.preparation-controls{margin-top:16px}.target-control{display:flex;align-items:center;gap:10px;font-size:13px}.preparation-hint{margin:10px 0 0;color:var(--df-muted);font-size:11px;line-height:1.8}.preparation-result{margin-top:14px;padding:12px 14px;border:1px solid var(--df-border);border-radius:7px;background:var(--df-card-bg,rgba(128,128,128,.04));font-size:12px;line-height:1.8}.preparation-result strong{font-weight:550}.preparation-result p{margin:4px 0;color:var(--df-warning,#dca35c)}.preparation-result small{color:var(--df-muted)}
.comment-studio{container-type:inline-size;container-name:comment-workspace}
.collection-summary{margin:24px 0 12px}.collection-summary h3{margin:0;font-size:16px;font-weight:600}.collection-summary p{margin:6px 0 0}.list-controls{margin:14px 0}.row-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--df-border)}.translation-ready{display:inline-flex;align-items:center;gap:5px;margin-left:auto;color:var(--df-success,#83c7b4);font-size:11px}.plan-selection-tools{margin-bottom:12px}.edit-actions,.edit-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.comment-action{font-size:12px}.preview-action{--n-font-weight:500!important}.retention-action[aria-pressed="true"]{font-weight:500}.edit-row-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}.edit-actions{justify-content:flex-start;margin-top:16px;padding-top:12px;border-top:1px solid var(--df-border)}.edit-meta{line-height:1.8}.edit-identity{display:flex;align-items:flex-start;gap:12px;min-width:0}.edit-identity>div{min-width:0}.edit-number{display:flex;align-items:center;justify-content:center;min-width:30px;height:30px;border-radius:8px;background:var(--df-accent-soft);color:var(--df-accent);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}.edit-position{margin:0 0 4px;color:var(--df-text);font-size:13px;font-weight:600;line-height:1.7}.reorder-actions{display:inline-flex;align-items:center;gap:0;margin-left:auto;border:1px solid var(--df-border);border-radius:6px}.reorder-actions .n-button{width:30px;min-width:30px;height:28px;padding:0}.remove-action{color:var(--df-muted)}.remove-action:not(.n-button--disabled):hover{color:var(--df-danger,#ef9c95)}
.selection-intent-actions{justify-content:flex-start;margin:8px 0}.selection-intent-actions .n-button{margin-left:6px}
.preparation-totals{display:flex;gap:5px 16px;flex-wrap:wrap;margin-top:5px;color:var(--df-muted);font-variant-numeric:tabular-nums}
.order-controls{margin:0 0 14px;padding:8px 12px;border-radius:7px;background:rgba(128,128,128,.04);border:1px solid var(--df-border)}
.preview-anchor{scroll-margin-top:24px}.temporary-preview{margin:0 0 12px;padding:10px 12px;border:1px solid color-mix(in srgb,var(--df-accent) 35%,var(--df-border));border-radius:7px;color:var(--df-accent);font-size:12px}.comment-row.located-comment{outline:2px solid var(--df-accent);outline-offset:-2px;background:color-mix(in srgb,var(--df-accent) 9%,transparent)}.comment-row:focus-visible{outline:2px solid var(--df-accent);outline-offset:-2px}
.processing-help,.row-processing-reasons{margin:10px 0;font-size:12px;line-height:1.7;color:var(--df-muted)}.processing-help summary,.row-processing-reasons summary{cursor:pointer}.row-processing-reasons>div{margin:8px 0}.row-processing-reasons p{margin:4px 0}.row-processing-reasons strong{font-weight:500}
.preparation-guide,.issue-summary p { margin:10px 0; font-size:12px; line-height:1.7; color:var(--df-muted); }
.issue-summary strong { color:var(--df-warning,#dca35c); font-weight:500; }
.style-source{display:flex;justify-content:space-between;align-items:center;gap:8px;min-width:0}.style-source .style-name{min-width:0;white-space:nowrap}.style-source small{display:inline-flex;align-items:center;gap:3px;flex-shrink:0;font-size:10px;color:var(--df-muted)}summary{cursor:pointer;font-size:13px}
.comment-settings{margin:14px 0;scroll-margin-top:24px}.comment-settings>summary{padding:8px 0;color:var(--df-muted)}
.tools { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
.muted,.byline,.edit-meta { font-size:12px; color:#99a7b8; } .compare { display:flex; align-items:center; gap:8px; font-size:12px; }
.notice { margin:10px 0; } .filter-panel { margin:16px 0; } .filter-actions,.preview-tools { justify-content:flex-start; margin-top:10px; }
.suggestion-bar,.old-filter { margin:8px 0; } .comment-list { display:flex;flex-direction:column;gap:12px;max-height:520px;overflow:auto;scrollbar-gutter:stable;padding:2px 4px 2px 2px; }
.comment-row { display:flex;align-items:flex-start;gap:14px;min-width:0;padding:18px 20px;border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel); }
.comment-row:hover { border-color:color-mix(in srgb,var(--df-muted) 65%,var(--df-border)); }
.comment-row.selected { border-color:color-mix(in srgb,var(--df-accent) 38%,var(--df-border));background:color-mix(in srgb,var(--df-accent) 4%,var(--df-panel)); }
.comment-selection{display:flex;flex-direction:column;align-items:center;gap:12px;flex:0 0 26px;padding-top:2px}.comment-number{color:var(--df-muted);font-size:12px;line-height:20px;font-variant-numeric:tabular-nums}.selected .comment-number{color:var(--df-accent)}
.comment-body { flex:1;min-width:0; }.comment-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.comment-identity{min-width:0}.byline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;line-height:1.6}.comment-facts{display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;margin-top:4px;color:var(--df-muted);font-size:11px;line-height:1.6}.like-count{display:inline-flex;align-items:center;gap:4px;font-variant-numeric:tabular-nums}.comment-state-group{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:6px;max-width:55%;font-size:11px}.comment-state-group:empty{display:none}
.author{font-size:13px;font-weight:600;color:var(--df-text);overflow-wrap:anywhere}.pending-translation{color:var(--df-muted)}
.comment-body p { margin:4px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.75;font-size:14px; }.comment-copy{max-width:100ch}
.comment-body .row-issue,.row-issue,.exclusion-summary { color:var(--df-warning,#dca35c); font-size:12px; line-height:1.6; margin:6px 0; }
.candidate-summary { padding:9px 12px; margin:10px 0; border:1px solid var(--df-border); border-radius:7px; background:rgba(128,128,128,.05); color:var(--df-muted); font-size:12px; line-height:1.65; }
.candidate-counts { display:flex; align-items:center; flex-wrap:wrap; gap:6px 18px; margin:10px 0; font-size:12px; color:var(--df-muted); }
.candidate-counts strong { color:var(--df-text); font-size:13px; font-variant-numeric:tabular-nums; }
.candidate-counts small { flex-basis:100%; line-height:1.65; }
.comment-body .row-pending { color:var(--df-muted); font-size:11px; line-height:1.6; margin:6px 0; }
.row-issue small { display:block; font-size:11px; color:var(--df-muted); }
.edit-row.excluded { background:color-mix(in srgb,var(--df-warning,#dca35c) 6%,transparent); }
.limit-hint { font-size:11px; color:var(--df-muted); line-height:1.6; cursor:help; }
.comment-body p.clipped { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.original { display:flex;align-items:baseline;gap:10px;max-width:110ch;color:var(--df-muted);margin:10px 0 2px; }.copy-caption{flex-shrink:0;font-size:11px;color:var(--df-muted)}
.original p { min-width:0;font-size:12px; }.expand-text{padding:7px 0 2px;border:0;background:none;color:var(--df-accent);cursor:pointer;font-size:12px}.expand-text:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px;border-radius:2px}.comment-list,.ordered-list{scrollbar-color:var(--df-border) transparent;scrollbar-width:thin}
.pagination { margin-top:14px; justify-content:flex-end; } .plan-panel { margin-top:24px; padding-top:20px; border-top:1px solid var(--df-border); }
.plan-grid { display:grid;grid-template-columns:minmax(0,1fr) 520px;gap:20px;align-items:start; }
.comment-workbench { min-width:0; }.plan-preview-column .preview-anchor{max-width:1280px;margin-inline:auto}.ordered-list { max-height:1280px;overflow:auto;resize:vertical;scrollbar-gutter:stable;margin-top:20px;padding:2px 4px 2px 2px; }.edit-row{min-width:0;margin-bottom:12px;padding:18px 20px;border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel)}.edit-row:last-child{margin-bottom:0}.edit-row.is-focused{border-color:color-mix(in srgb,var(--df-accent) 65%,var(--df-border))}
.bilingual-editor { display:flex;flex-direction:column;gap:14px; }.bilingual-editor.source-first .source-editor{order:-1}
.edit-label { display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 6px;color:var(--df-text);font-size:12px; }.edit-label small{color:var(--df-muted);font-size:10px;font-variant-numeric:tabular-nums}
.layout-controls { display:flex; flex-direction:column; gap:12px;min-width:0;padding:18px;background:rgba(128,128,128,.035);border:1px solid var(--df-border);border-radius:9px;container-type:inline-size;container-name:comment-parameters; }
.layout-controls h4 { margin:0;font-size:14px;font-weight:600; }.layout-controls .style-field { display:grid;grid-template-columns:minmax(0,1fr) 210px;align-items:center;gap:12px;font-size:12px;min-width:0; }.layout-controls .style-field.font-range-field{grid-template-columns:minmax(0,1fr);gap:8px}.style-control{min-width:0}.style-control > :deep(.n-input-number),.style-control > :deep(.n-select){width:100%!important}.style-control :deep(.n-switch){vertical-align:middle}
.layout-strategy{margin:0;font-size:11px;line-height:1.7;color:var(--df-muted)}
.font-range-inputs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.font-range-bound{display:flex;flex-direction:column;gap:6px;min-width:0}.font-range-bound>span{display:flex;align-items:center;justify-content:space-between;gap:5px;color:var(--df-muted);font-size:11px;white-space:nowrap}.font-range-inputs small{display:flex;align-items:center;gap:2px;flex-shrink:0}.font-range-inputs :deep(.n-input-number){width:100%!important}
.style-field :deep(.n-switch) { align-self:flex-start; }
.language-style-group { display:flex; flex-direction:column; gap:12px; padding-top:14px; border-top:1px solid var(--df-border); }
.language-style-group h5 { margin:0 0 2px;font-size:12px;font-weight:550;color:var(--df-text); }
.invalid { color:#e8ad77; } .frame { display:block; max-width:100%; max-height:420px; border-radius:8px; margin-top:14px; } .frame.stale { opacity:.65; }
.save-bar { margin-top:18px; } .save-bar > a { font-size:12px; }
@container comment-workspace (max-width:1140px){.plan-grid{grid-template-columns:minmax(0,1fr)}.plan-preview-column .preview-anchor{max-width:600px}.layout-controls{padding:16px}.edit-actions{width:100%}}
@container comment-workspace (max-width:600px){.comment-heading{flex-direction:column;gap:8px}.comment-state-group{justify-content:flex-start;max-width:100%}.comment-row{padding:16px 14px;gap:10px}.edit-row{padding:16px 14px}.edit-row-heading{flex-wrap:wrap}.original{flex-direction:column;gap:0}}
@container comment-parameters (max-width:360px){.layout-controls .style-field{grid-template-columns:minmax(0,1fr);gap:6px}.style-source .style-name{white-space:normal}.font-range-inputs{gap:10px}.font-range-bound>span{gap:3px}.font-range-inputs :deep(.configuration-source){font-size:10px;padding-inline:4px}}
@container comment-workspace (max-width:420px){.comment-row{padding:14px 10px;gap:8px}.comment-selection{flex-basis:22px}.row-actions{gap:6px}.translation-ready{flex-basis:100%;margin-left:0}.comment-action{font-size:11px}.font-range-inputs{grid-template-columns:minmax(0,1fr)}.edit-actions{row-gap:6px}.reorder-actions{margin-left:0}.layout-controls{padding:14px}}
</style>
