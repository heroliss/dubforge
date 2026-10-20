<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NProgress, NSelect, NTag, NText } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
import { UPDATE_COPY_FIELDS, UPDATE_STATUS, updateDifferences, updateDuration, updateIsActive, updateNeedsReconcile, updateProgress, updateStages } from '../utils/publicationUpdate'
import LlmConversationButton from './LlmConversationButton.vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { executionStatusType } from '../utils/executionProgress'
import { fmtSize } from '../utils/format'
import ChapterAdaptationSummary from './ChapterAdaptationSummary.vue'
import PublishVideoIdentity from './PublishVideoIdentity.vue'
import { videoFileLabel } from '../utils/publishVideo'

const props = defineProps<{ publication: any; disabled?: boolean; disabledReason?: string }>()
const emit = defineEmits<{ 'busy-change': [value: boolean]; 'dirty-change': [value: boolean]; 'active-change': [value: boolean]; changed: []; 'automatic-update': [] }>()
const expanded = ref(false), context = ref<any>(null), task = ref<any>(null)
const loading = ref(false), writing = ref(false), error = ref(''), requestUnknown = ref(false)
const replaceVideos = ref(true), replaceMap = ref<Record<string, string | number | null>>({})
const replacementActive = ref<Record<string, boolean>>({})
const selectedFields = ref<string[]>([]), values = ref<Record<string, any>>({})
const chaptersEnabled = ref(false), translate = ref(true), instruction = ref(''), permanent = ref(true), overwriteExisting = ref(false)
const collectionEnabled = ref(false), seasonId = ref<string | number | null>(null), sectionId = ref<string | number | null>(null)
const edited = ref(false), historyId = ref<string | null>(null)
let generation = 0, disposed = false, mounted = false, timer: ReturnType<typeof setTimeout> | undefined
const sourceKey = computed(() => String(props.publication?.id || ''))
const active = computed(() => updateIsActive(task.value))
const prepared = computed(() => task.value?.status === 'prepared')
const deliveryMode = computed(() => task.value?.delivery_mode || task.value?.snapshot?.delivery_mode || 'publish')
const localDraft = computed(() => prepared.value && deliveryMode.value==='draft')
const uploaded = computed(() => task.value?.status==='uploaded' && deliveryMode.value==='upload')
const needsReconcile = computed(() => requestUnknown.value || updateNeedsReconcile(task.value))
const locked = computed(() => !!props.disabled || loading.value || writing.value || active.value || prepared.value || uploaded.value || needsReconcile.value)
const remoteParts = computed(() => context.value?.remote?.videos || [])
const replacementSupported = computed(() => context.value?.remote?.edit_supported !== false)
const candidates = computed(() => context.value?.candidates || [])
const candidateOptions = computed(() => candidates.value.map((item: any) => ({ value: item.id,
  label: videoFileLabel(item), file: item,
  disabled: item.exists === false || item.review_ready === false })))
function renderCandidateLabel(option: any) { return h(PublishVideoIdentity, { file: option.file }) }
function renderCandidateTag({ option }: any) { return h(PublishVideoIdentity, { file: option.file, compact: true }) }
function replacementFile(cid: number | string) { return candidates.value.find((row: any) => row.id === replaceMap.value[String(cid)]) }
const replacementSelected = (cid: string | number) => replaceVideos.value && replacementActive.value[String(cid)] === true
  && replaceMap.value[String(cid)] !== null && replaceMap.value[String(cid)] !== undefined
const unassignedReplacements = computed(() => replaceVideos.value ? remoteParts.value
  .filter((part: any) => replacementActive.value[String(part.cid)] === true
    && (replaceMap.value[String(part.cid)] === null || replaceMap.value[String(part.cid)] === undefined)).length : 0)
const selectedReplacements = computed(() => replaceVideos.value ? remoteParts.value
  .filter((part: any) => replacementSelected(part.cid))
  .map((part: any) => ({ old_cid: part.cid, artifact_id: replaceMap.value[String(part.cid)] })) : [])
const hasDubbing = computed(() => !!task.value?.preview?.has_dubbing || !!context.value?.has_dubbing
  || selectedReplacements.value.some((part: any) => candidates.value.some((item: any) => item.id === part.artifact_id && item.has_dubbing)))
const seasons = computed(() => context.value?.metadata?.seasons || [])
const seasonOptions = computed(() => [{ label: '移出当前合集', value: 0 }, ...seasons.value.map((item: any) => ({ label: item.title || `合集（ID ${item.id}）`, value: item.id }))])
const sections = computed(() => seasons.value.find((item: any) => String(item.id) === String(seasonId.value))?.sections || [])
const sectionOptions = computed(() => sections.value.map((item: any) => ({ label: item.title || `小节（ID ${item.id}）`, value: item.id })))
const hasScope = computed(() => selectedReplacements.value.length > 0 || selectedFields.value.length > 0 || chaptersEnabled.value || collectionEnabled.value)
const scopeSummary = computed(() => [
  selectedReplacements.value.length ? `${selectedReplacements.value.length} 个分P` : '',
  selectedFields.value.length ? `${selectedFields.value.length} 项文案` : '',
  chaptersEnabled.value ? '视频章节' : '', collectionEnabled.value ? '目标合集' : '',
].filter(Boolean))
const canPrepare = computed(() => !!context.value && !locked.value && hasScope.value
  && unassignedReplacements.value === 0
  && (!collectionEnabled.value || seasonId.value === 0 || !!seasonId.value && !!sectionId.value))
const stages = computed(() => updateStages(task.value))
const progress = computed(() => updateProgress(task.value))
const progressLabel = computed(() => {
  if (!task.value) return ''
  if (uploaded.value) return '替换文件已上传，等待确认提交原稿更新'
  if (localDraft.value) return '本地更新方案已准备，尚未上传或提交'
  if (prepared.value) return '方案已检查，等待确认'
  if (task.value.status === 'completed') return '更新已完成并核验'
  if (task.value.status === 'failed') return '部分阶段未完成'
  if (task.value.status === 'waiting') return '等待B站处理后继续核对'
  return progress.value.active?.label || task.value.phase || UPDATE_STATUS[task.value.status] || '正在处理'
})
const differences = computed(() => updateDifferences(task.value).map(row => row.key !== 'declaration_id' ? row : {
  ...row, before: declarationName(row.before), after: declarationName(row.after),
}))
const previews = computed(() => task.value?.preview?.parts || [])
const chapterDelivered = computed(() => (task.value?.execution?.delivery?.parts || []).some((part: any) => part.items?.length))
const chapterParts = computed(() => chapterDelivered.value ? task.value.execution.delivery.parts : task.value?.preview?.chapters?.parts || [])
const warnings = computed(() => task.value?.preview?.warnings || [])
const history = computed(() => (context.value?.updates || []).map((item: any) => ({ value: item.id,
  label: `${item.created_at ? new Date(item.created_at).toLocaleString() : '更新记录'} · ${UPDATE_STATUS[item.status] || '状态待核对'}` })))
const unsupported = computed(() => {
  const raw = context.value?.capabilities?.field_capabilities || context.value?.capabilities?.studio_capabilities?.field_capabilities || {}
  return Object.entries(raw).filter(([, value]: any) => ['submission_only', 'read_only', 'unsupported'].includes(value?.write))
    .map(([key, value]: any) => ({ key, label: value.label || ({ cover:'封面', selected_comments:'精选评论', visibility:'可见范围', human_type2:'分区', tid:'分区', publish_at:'定时发布', copyright:'稿件类型', source:'原视频来源', close_comments:'关闭评论', close_danmaku:'关闭弹幕', topic:'活动与话题', watermark:'B站水印', subtitles:'平台字幕', dolby:'杜比音效', lossless_music:'无损音质', commercial_promotion:'商业推广' } as Record<string,string>)[key] || '其他平台设置', reason: value.reason || '当前不支持修改，请到B站官网处理。' }))
})
const taskSummary = computed(() => task.value?.status === 'failed' && ['completed', 'success'].includes(task.value?.execution?.stages?.edit?.status)
  ? '视频或资料已提交，后续步骤未完成。继续只补做未完成部分。'
  : task.value?.status === 'waiting' ? '更新已提交，正在等待B站转码或审核；此时尚不能确认全部同步完成。'
  : task.value?.status === 'unknown' || requestUnknown.value ? '请求可能已经到达B站。先核对实际结果，避免重复提交。'
  : task.value?.status === 'completed' ? '本次范围已完成并回读核验，原BV保持不变。'
  : uploaded.value ? '替换视频文件已上传，B站原稿尚未提交更新。确认后才会替换指定分P并同步已选资料。'
  : localDraft.value ? '只准备本地更新方案；需要上传或提交时，在发布步骤修改交付方式，再从总览重新预检。'
  : task.value?.status === 'paused' ? '已保存执行断点，继续时复用已完成的上传和同步结果。' : '')

watch(writing, value => emit('busy-change', value), { immediate: true })
watch(edited, value => emit('dirty-change', value), { immediate: true })
watch(active, () => schedule(), { immediate: true })
watch(() => active.value || prepared.value && !localDraft.value || needsReconcile.value, value => emit('active-change', value), { immediate: true })
watch(sourceKey, reset)
function reset() {
  generation++; clearTimeout(timer); expanded.value = false; context.value = null
  task.value = props.publication?.latest_update || null; error.value = ''; loading.value = false; writing.value = false
  requestUnknown.value = false; edited.value = false; historyId.value = task.value?.id || null; schedule()
}
function changed() { if (!locked.value) { edited.value = true; error.value = '' } }
function toggleReplacement(cid: string | number, value: boolean) {
  if (locked.value) return
  const key = String(cid)
  replacementActive.value = { ...replacementActive.value, [key]: value }
  if (!value) replaceMap.value = { ...replaceMap.value, [key]: null }
  changed()
}
function selectReplacement(cid: string | number, value: string | number | null) {
  if (locked.value) return
  const key = String(cid)
  replaceMap.value = { ...replaceMap.value, [key]: value }
  replacementActive.value = { ...replacementActive.value, [key]: value !== null && value !== undefined }
  changed()
}
function selectField(key: string, checked: boolean) { if (locked.value) return; selectedFields.value = checked ? [...new Set([...selectedFields.value, key])] : selectedFields.value.filter(item => item !== key); changed() }
function setValue(key: string, value: any) { if (locked.value) return; values.value = { ...values.value, [key]: value }; changed() }
function declarationName(value: string) {
  if (value === '（空）') return '未选择'
  return context.value?.metadata?.declarations?.find((item: any) => String(item.value) === value)?.label || `创作声明（ID ${value}）`
}
function adoptProjectCopy() {
  if (locked.value) return
  const saved = context.value?.project_values || {}
  values.value = { ...values.value, ...Object.fromEntries(Object.keys(UPDATE_COPY_FIELDS).filter(key => Object.hasOwn(saved, key)).map(key => [key, saved[key]])) }
  selectedFields.value = Object.keys(UPDATE_COPY_FIELDS).filter(key => Object.hasOwn(saved, key)); changed()
}
function chapterOption(key: string, value: any) {
  if (locked.value) return
  if (key === 'translate') translate.value = value
  if (key === 'permanent') permanent.value = value
  if (key === 'overwrite') overwriteExisting.value = value
  if (key === 'instruction') instruction.value = value
  changed()
}
function changeScope(key: string, value: boolean) {
  if (locked.value) return
  if (key === 'videos' && replacementSupported.value) replaceVideos.value = value
  if (key === 'chapters') chaptersEnabled.value = value
  if (key === 'collection') collectionEnabled.value = value
  changed()
}
function selectSeason(value: string | number | null) {
  if (locked.value) return
  seasonId.value = value; const parts = seasons.value.find((item: any) => String(item.id) === String(value))?.sections || []
  sectionId.value = parts.length === 1 ? parts[0].id : null; changed()
}
function seed(value: any) {
  context.value = value; const map: Record<string, any> = {}
  for (const part of value.remote?.videos || []) map[String(part.cid)] = null
  for (const row of value.suggested_replacements || []) if (Object.hasOwn(map, String(row.old_cid))) map[String(row.old_cid)] = row.artifact_id ?? null
  replaceMap.value = map
  replacementActive.value = Object.fromEntries(Object.entries(map).map(([key, item]) => [key, item !== null && item !== undefined]))
  replaceVideos.value = value.remote?.edit_supported !== false && Object.values(map).some(item => item !== null)
  values.value = JSON.parse(JSON.stringify(value.remote?.settings || {})); selectedFields.value = []
  const chapter = value.chapter_options || {}
  chaptersEnabled.value = chapter.enabled === true; translate.value = chapter.translate !== false
  instruction.value = chapter.instruction || ''; permanent.value = chapter.permanent !== false; overwriteExisting.value = false
  collectionEnabled.value = false; seasonId.value = value.project_values?.season_id || value.remote?.collection?.season_id || null
  sectionId.value = value.project_values?.section_id || value.remote?.collection?.section_id || null
  edited.value = false
  const latest = value.updates?.[0]
  if (latest) { task.value = latest; historyId.value = latest.id }
}
async function openEditor() {
  if (loading.value || writing.value) return
  expanded.value = true
  if (context.value) return
  const ticket = generation; loading.value = true; error.value = ''
  try {
    const value = await publishApi(`/publications/${encodeURIComponent(sourceKey.value)}/updates/context`)
    if (!disposed && ticket === generation) seed(value)
  } catch (e: any) { if (!disposed && ticket === generation) error.value = e.message || '更新资料读取失败，请重试' }
  finally { if (!disposed && ticket === generation) { loading.value = false; schedule() } }
}
async function prepare() {
  if (!canPrepare.value) return
  const ticket = generation; writing.value = true; error.value = ''
  try {
    const value = await publishApi(`/publications/${encodeURIComponent(sourceKey.value)}/updates/plan`, {
      replacements: selectedReplacements.value,
      values: Object.fromEntries(selectedFields.value.map(key => [key, values.value[key] ?? (key === 'tags' ? [] : '')])),
      chapters: { enabled: chaptersEnabled.value, translate: translate.value, instruction: instruction.value,
        permanent: permanent.value, overwrite_existing: overwriteExisting.value },
      collection: { enabled: collectionEnabled.value, season_id: seasonId.value === 0 ? null : seasonId.value, section_id: seasonId.value === 0 ? null : sectionId.value },
    })
    if (!disposed && ticket === generation) {
      task.value = value; historyId.value = value.id; edited.value = false; requestUnknown.value = false
      context.value.updates = [value, ...(context.value.updates || []).filter((item: any) => item.id !== value.id)]
    }
  } catch (e: any) { if (!disposed && ticket === generation) error.value = e.message || '更新方案检查失败，请调整后重试' }
  finally { if (!disposed && ticket === generation) writing.value = false }
}
async function perform(action: 'start' | 'submit' | 'retry' | 'reconcile' | 'pause') {
  if (!task.value?.id || writing.value || loading.value || disposed) return
  if (action === 'start' && (!prepared.value || localDraft.value || props.disabled || needsReconcile.value)) return
  if (action === 'submit' && (!uploaded.value || props.disabled || needsReconcile.value)) return
  if (action === 'retry' && (props.disabled || !['failed', 'paused'].includes(task.value.status) || needsReconcile.value)) return
  if (action === 'reconcile' && !needsReconcile.value && task.value?.status !== 'waiting') return
  if (action === 'pause' && !active.value) return
  const id = task.value.id, ticket = generation; writing.value = true; error.value = ''
  try {
    const value = await publishApi(`/updates/${encodeURIComponent(id)}/actions`, { action, confirmed: true, fingerprint: task.value.fingerprint })
    if (!disposed && ticket === generation && task.value?.id === id) {
      task.value = value; requestUnknown.value = false; emit('changed')
    }
  } catch (e: any) {
    if (!disposed && ticket === generation) {
      error.value = e.message || '操作未完成，请先核对状态'
      // 发送结果不明时不重发写请求；只提供读取与显式核对。
      if (['PUBLISH_REQUEST_TIMEOUT', 'PUBLISH_UPDATE_UNKNOWN', 'PUBLISH_WRITE_UNKNOWN'].includes(e.code) || !e.code) requestUnknown.value = true
    }
  } finally { if (!disposed && ticket === generation) { writing.value = false; schedule() } }
}
async function refreshTask() {
  if (!task.value?.id || writing.value || loading.value) return
  const id = task.value.id, ticket = generation
  try {
    const value = await publishApi(`/updates/${encodeURIComponent(id)}`)
    if (!disposed && ticket === generation && task.value?.id === id) { task.value = value; error.value = ''; emit('changed') }
  } catch (e: any) { if (!disposed && ticket === generation) error.value = e.message || '状态暂未读取成功，可稍后刷新' }
}
async function selectHistory(id: string) {
  if (writing.value || loading.value || edited.value || active.value) return
  const selected = context.value?.updates?.find((item: any) => item.id === id)
  if (!selected) return
  task.value = selected; historyId.value = id; requestUnknown.value = false; await refreshTask()
}
function revise() { if (writing.value || active.value || needsReconcile.value) return; task.value = null; historyId.value = null; edited.value = true }
async function newAttempt() {
  if (writing.value || loading.value || active.value || needsReconcile.value || props.disabled) return
  const ticket = generation; loading.value = true; error.value = ''
  try {
    const value = await publishApi(`/publications/${encodeURIComponent(sourceKey.value)}/updates/context`)
    if (!disposed && ticket === generation) { seed(value); task.value = null; historyId.value = null }
  } catch (e: any) { if (!disposed && ticket === generation) error.value = e.message || '更新资料读取失败，请重试' }
  finally { if (!disposed && ticket === generation) loading.value = false }
}
function discard() { if (!writing.value && !active.value) { edited.value = false; error.value = ''; context.value = null; task.value = null; expanded.value = false } }
function openAutomaticUpdate(){if(!props.disabled && !loading.value && !writing.value && !active.value && !needsReconcile.value && !edited.value)emit('automatic-update')}
function schedule() {
  clearTimeout(timer)
  if (mounted && !disposed && active.value) timer = setTimeout(async () => { await refreshTask(); schedule() }, error.value ? 12000 : 5000)
}
async function copyChapters() {
  const text = chapterParts.value.map((part: any, i: number) => `P${i + 1} · ${part.title || '视频'}\n${(part.items || []).map((item: any) => `${updateDuration(item.start_time)} ${item.title}`).join('\n')}`).join('\n\n')
  try { await navigator.clipboard.writeText(text) } catch { error.value = '未能复制章节，请从下方清单手动选择复制' }
}
onMounted(() => { mounted = true; if (props.publication?.latest_update) { task.value = props.publication.latest_update; historyId.value = task.value.id }; schedule() })
onBeforeUnmount(() => { disposed = true; generation++; clearTimeout(timer); emit('busy-change', false); emit('dirty-change', false); emit('active-change', false) })
</script>

<template>
  <section id="publication-original-update" class="original-update publication-panel" aria-label="更新原稿件（保留原BV）">
    <div class="update-heading"><div><h3>更新原稿件 <span>保留原BV</span></h3><p>选择要替换的成片与要同步的资料，检查后统一确认。</p></div><div class="update-entry-actions"><n-button v-if="!expanded && !task" type="primary" :loading="loading" :disabled="disabled || loading" @click="openEditor">选择现有成片与资料</n-button><n-button secondary :disabled="disabled || loading || writing || active || needsReconcile || edited" @click="openAutomaticUpdate">设置项目交付目标…</n-button></div></div>
    <p v-if="!expanded && !task" class="muted update-entry-note">成片已准备好时直接选择；需要重新制作时，在本页项目发布设置中保存交付目标，再从总览运行项目。</p>
    <p v-if="disabled && (!task || prepared)" class="muted" role="status">{{ disabledReason || '当前有未完成操作或未保存修改，请先处理后再更新原稿件。' }}</p>
    <div v-if="expanded || task" class="update-body">
      <div class="update-target"><n-tag size="small" type="info" :bordered="false">{{ publication.receipt?.bvid }}</n-tag><span>{{ context?.source?.account_name || publication.snapshot?.account_name }} · {{ context?.source?.title || publication.snapshot?.settings?.title }}</span></div>
      <p v-if="loading" role="status" class="muted">正在读取原稿分P、成片和可同步设置…</p>
      <n-alert v-if="error" type="warning" :show-icon="false" class="update-alert">{{ error }}<n-button v-if="!context && !task" size="small" quaternary :disabled="loading" @click="openEditor">重新读取</n-button></n-alert>
      <div v-if="context && !task" class="update-form">
        <section class="scope-block" data-scope="videos" :data-selected="replaceVideos"><div class="scope-label"><n-checkbox :checked="replaceVideos" :disabled="locked || !replacementSupported" @update:checked="changeScope('videos', $event)">替换视频</n-checkbox><p>先勾选要替换的分P，再为它选择新成片；未勾选的分P会保留原视频。</p><small v-if="replaceVideos" class="scope-count">{{ selectedReplacements.length }} / {{ remoteParts.length }} 个分P将替换<span v-if="selectedReplacements.length < remoteParts.length"> · {{ remoteParts.length - selectedReplacements.length }} 个保留原视频</span></small></div><div class="scope-content"><p v-if="!replacementSupported" class="warning">{{ context.remote.edit_limitation || '此稿件暂不支持自动替换，请到B站官网处理。' }}</p>
          <p v-if="unassignedReplacements" class="warning">已有 {{ unassignedReplacements }} 个分P勾选了替换，但还没有选择新成片；请选择后才能检查方案。</p>
          <div v-if="replaceVideos" class="replacement-list"><div v-for="(part, index) in remoteParts" :key="part.cid" class="replacement-row"><div><div class="replacement-part-heading"><n-checkbox class="replacement-toggle" :checked="replacementActive[String(part.cid)] === true" :disabled="locked" @update:checked="toggleReplacement(part.cid, $event)">替换此P</n-checkbox><strong>P{{ index + 1 }} · {{ part.title }}</strong><n-tag size="small" :type="replacementSelected(part.cid) ? 'info' : 'default'" :bordered="false">{{ replacementSelected(part.cid) ? '将替换' : '保留原视频' }}</n-tag></div><small>原视频 {{ updateDuration(part.duration) }}</small></div><div class="replacement-choice"><n-select :value="replaceMap[String(part.cid)]" :options="candidateOptions" :render-label="renderCandidateLabel" :render-tag="renderCandidateTag" :virtual-scroll="false" :consistent-menu-width="false" :menu-props="{style:{minWidth:'min(420px, calc(100vw - 48px))',maxWidth:'min(640px, calc(100vw - 48px))'}}" filterable clearable :disabled="locked || replacementActive[String(part.cid)] !== true" :aria-label="`P${index + 1}替换成片`" placeholder="先勾选“替换此P”" @update:value="selectReplacement(part.cid, $event)" /><PublishVideoIdentity v-if="replacementFile(part.cid)" class="selected-video-info" :file="replacementFile(part.cid)" /></div></div><p v-if="!remoteParts.length">尚未取得原稿分P，暂不能替换视频。</p></div>
        </div></section>
        <section class="scope-block" data-scope="copy" :data-selected="selectedFields.length > 0"><div class="scope-label"><h4>同步文案</h4><p>只更新勾选的字段。初始内容来自B站，也可采用项目已保存文案。</p><small v-if="selectedFields.length" class="scope-count">已选 {{ selectedFields.length }} 项</small></div><div class="scope-content"><div class="copy-toolbar"><n-button size="small" secondary :disabled="locked" @click="adoptProjectCopy">采用已保存项目文案</n-button></div><p v-if="hasDubbing" class="warning">含AI配音成片，请核对B站AI创作声明。同一稿件全部分P共用声明；未勾选“创作声明”时保留B站当前值，需要更改请勾选并选择。</p><div v-for="(label, key) in UPDATE_COPY_FIELDS" :key="key" class="copy-field" :data-selected="selectedFields.includes(String(key))"><n-checkbox :checked="selectedFields.includes(String(key))" :disabled="locked" @update:checked="selectField(String(key), $event)">{{ label }}</n-checkbox><template v-if="selectedFields.includes(String(key))"><n-select v-if="key === 'declaration_id'" :value="values[key]" :options="context.metadata?.declarations || []" :disabled="locked" aria-label="本次更新创作声明" @update:value="setValue(String(key), $event)" /><n-input v-else :value="key === 'tags' ? (values[key] || []).join('，') : values[key] || ''" :type="key === 'description' ? 'textarea' : 'text'" :autosize="key === 'description' ? {minRows:3,maxRows:8} : undefined" :disabled="locked" :aria-label="`本次更新${label}`" @update:value="setValue(String(key), key === 'tags' ? $event.split(/[,，]/).map(item => item.trim()).filter(Boolean) : $event)" /></template><span v-else class="field-retained">保留B站当前内容</span></div></div></section>
        <section class="scope-block" data-scope="chapters" :data-selected="chaptersEnabled"><div class="scope-label"><n-checkbox :checked="chaptersEnabled" :disabled="locked" @update:checked="changeScope('chapters', $event)">同步视频章节</n-checkbox><p>按新成片映射时间轴，待转码或审核就绪后写入。</p></div><div class="scope-content"><div v-if="chaptersEnabled" class="chapter-settings"><ChapterAdaptationSummary /><div class="chapter-options"><n-checkbox :checked="translate" :disabled="locked" @update:checked="chapterOption('translate', $event)">整理时将标题翻译为中文</n-checkbox><n-checkbox :checked="permanent" :disabled="locked" @update:checked="chapterOption('permanent', $event)">B站App默认展示章节</n-checkbox><n-checkbox :checked="overwriteExisting" :disabled="locked" @update:checked="chapterOption('overwrite', $event)">允许覆盖该分P已有章节</n-checkbox></div><n-input :value="instruction" type="textarea" :autosize="{minRows:2,maxRows:4}" :disabled="locked" placeholder="章节标题自定义要求（可选）" aria-label="原稿更新章节标题要求" :maxlength="2000" show-count @update:value="chapterOption('instruction', $event)" /><small>检查时展示映射后的时间点；执行时由LLM整理标题，并校验平台固定限制。</small></div><p v-else class="scope-retained">保留B站已有章节</p></div></section>
        <section class="scope-block" data-scope="collection" :data-selected="collectionEnabled"><div class="scope-label"><n-checkbox :checked="collectionEnabled" :disabled="locked" @update:checked="changeScope('collection', $event)">同步目标合集</n-checkbox><p>调整此稿件所在的合集与小节。</p></div><div class="scope-content"><div v-if="collectionEnabled" class="collection-settings"><n-select :value="seasonId" :options="seasonOptions" :disabled="locked" aria-label="原稿更新目标合集" placeholder="选择目标合集" @update:value="selectSeason" /><n-select :value="sectionId" :options="sectionOptions" :disabled="locked || !seasonId" aria-label="原稿更新目标小节" placeholder="选择合集小节" @update:value="value => { if (!locked) { sectionId=value; changed() } }" /><p v-if="context.metadata_error || context.metadata?.collection_error" class="warning">{{ context.metadata_error || '合集列表暂未读取成功；可先更新视频和文案，或重新打开以读取合集。' }}</p></div><p v-else class="scope-retained">保留B站当前合集</p></div></section>
        <details v-if="unsupported.length" class="capability-notes"><summary>其他B站设置的同步范围</summary><p v-for="item in unsupported" :key="item.key"><strong>{{ item.label }}</strong> · {{ item.reason }}</p><a href="https://member.bilibili.com/platform/home" target="_blank" rel="noopener noreferrer">到B站创作中心处理 ↗</a></details>
        <footer class="update-form-footer"><div class="scope-summary" aria-live="polite"><strong>本次更新范围</strong><span>{{ scopeSummary.length ? scopeSummary.join('、') : '尚未选择，请勾选需要更新的内容' }}</span><small>其余内容保留；下一步核对方案后再确认提交。</small></div><div class="update-actions"><n-button quaternary :disabled="writing" @click="discard">取消本次准备</n-button><n-button type="primary" :loading="writing" :disabled="!canPrepare" @click="prepare">检查更新方案</n-button></div></footer>
      </div>
      <div v-if="task" class="update-task" :data-update-status="task.status" aria-live="polite">
        <div class="task-heading"><n-tag :type="needsReconcile || task.status === 'failed' ? 'warning' : task.status === 'completed' ? 'success' : 'info'" :bordered="false">{{ requestUnknown ? '请求结果待核对' : UPDATE_STATUS[task.status] || '状态待读取' }}</n-tag><n-button size="small" quaternary :disabled="writing || loading" @click="refreshTask">刷新状态</n-button><LlmConversationButton v-if="task.preview?.chapters?.enabled" prominent :project-uid="publication.project_uid" operation="chapters" :owner-id="task.id" /></div>
        <section v-if="!prepared" class="update-progress-summary" aria-label="原稿更新总体进度">
          <div class="update-progress-heading"><div><strong>本次更新进度</strong><span>{{ progressLabel }}</span></div><strong v-if="progress.percentage !== null">{{ Math.round(progress.percentage) }}%</strong><span v-else>等待进度</span></div>
          <n-progress type="line" :percentage="progress.percentage === null ? 0 : progress.percentage" :show-indicator="false" :processing="progress.active?.status === 'running' || progress.active?.status === 'active'" :status="task.status === 'failed' ? 'error' : task.status === 'completed' ? 'success' : 'default'" />
          <p class="update-progress-facts">已完成 {{ progress.completed }} / {{ progress.total }} 个阶段<template v-if="progress.active"> · 当前：{{ progress.active.label }}</template><template v-if="progress.active?.estimated"> · 阶段进度按已完成分P估算</template></p>
        </section>
        <p v-if="taskSummary" class="task-summary">{{ taskSummary }}</p><p v-if="task.phase && !prepared" class="task-summary">{{ task.phase }}<template v-if="task.execution?.total_bytes > 0"> · {{ fmtSize(task.execution.loaded_bytes || 0) }} / {{ fmtSize(task.execution.total_bytes) }}</template><template v-if="task.execution?.current_part"> · 第 {{ task.execution.current_part }} 个替换视频</template></p><ExecutionProgress v-if="task.execution?.delivery?.execution" :execution="task.execution.delivery.execution" compact /><n-alert v-if="task.error" type="warning" :show-icon="false" class="update-alert">{{ task.error }}</n-alert>
        <div v-if="prepared || uploaded" class="update-confirmation"><h4>{{ localDraft?'本地准备范围':uploaded?'确认提交已上传的更新':'确认本次范围' }}</h4><p v-if="hasDubbing" class="warning">本次包含AI配音。创作声明按下方已检查的差异执行；没有声明差异时保留B站现有值，请核对是否符合平台要求。</p><p>原稿 <strong>{{ task.bvid || publication.receipt?.bvid }}</strong> · {{ context?.source?.account_name || publication.snapshot?.account_name }}。{{ previews.filter((part: any) => part.replaced).length }} 个分P替换；其他分P与未选设置保留。</p></div>
        <ol v-if="!prepared" class="update-stages" aria-label="原稿更新执行进度"><li v-for="stage in stages" :key="stage.key" :data-stage-status="stage.status"><span class="stage-dot" aria-hidden="true"></span><strong>{{ stage.label }}</strong><n-text class="update-stage-state" :type="executionStatusType(stage.status)" :depth="executionStatusType(stage.status) === 'default' ? 3 : undefined" :data-status-type="executionStatusType(stage.status)">{{ stage.text }}<template v-if="stage.progress !== null"> · {{ stage.estimated ? '约 ' : '' }}{{ Math.round(stage.progress) }}%</template></n-text><div v-if="stage.progress !== null && stage.status !== 'skipped'" class="stage-progress"><n-progress type="line" :percentage="stage.progress" :show-indicator="false" :processing="stage.status === 'running' || stage.status === 'active'" :status="stage.status === 'failed' ? 'error' : stage.status === 'completed' || stage.status === 'success' ? 'success' : 'default'" /><span>{{ stage.estimated ? '估算' : '阶段' }}</span></div><p v-if="stage.error">{{ stage.error }}</p></li></ol>
        <details v-if="previews.length || differences.length || chapterParts.length || warnings.length" :open="prepared" class="checked-plan"><summary>{{ prepared ? '已检查的更新清单' : '本次已确认的更新清单' }}</summary><div v-for="(part,index) in previews" :key="part.old_cid || index" class="planned-part"><strong>P{{ index + 1 }} · {{ part.title }}</strong><n-tag size="small" :type="part.replaced ? 'info' : 'default'" :bordered="false">{{ part.replaced ? '替换视频' : '保留原视频' }}</n-tag><p v-if="part.replaced">{{ updateDuration(part.old_duration) }} → {{ updateDuration(part.new_duration) }} · {{ part.version_generated_name || part.version_name || part.artifact_name || '所选成片' }}</p><p v-if="part.timeline_note" class="timeline-note">{{ part.timeline_note }}</p></div><div v-for="difference in differences" :key="difference.key" class="copy-difference"><strong>{{ difference.label }}</strong><div><small>当前B站</small><p>{{ difference.before }}</p></div><div><small>更新后</small><p>{{ difference.after }}</p></div></div><div v-if="chapterParts.length" class="planned-chapters"><div class="chapter-heading"><h4>{{ chapterDelivered ? '本次已整理章节' : '按新成片时间轴待整理的章节' }}</h4><n-button size="small" quaternary @click="copyChapters">复制章节清单</n-button></div><details v-for="(part,index) in chapterParts" :key="part.old_cid || part.cid || index"><summary>P{{ index + 1 }} · {{ part.title }} · {{ part.items?.length || 0 }} 章</summary><ChapterAdaptationSummary v-if="part.adaptation" :value="part.adaptation" :local="false" /><p v-if="part.timeline_note || part.timeline?.label">{{ part.timeline_note || part.timeline?.label }}</p><ol><li v-for="(item,itemIndex) in part.items || []" :key="itemIndex"><time>{{ updateDuration(item.start_time) }}</time>{{ item.title }}</li></ol></details></div><p v-if="task.preview?.collection?.enabled" class="planned-collection">目标合集：{{ task.preview.collection.season_id ? task.preview.collection.season_title || task.preview.collection.title || '名称暂未返回' : '移出当前合集' }}<template v-if="task.preview.collection.section_title"> · {{ task.preview.collection.section_title }}</template></p><ul v-if="warnings.length" class="plan-warnings"><li v-for="(warning,index) in warnings" :key="index">{{ typeof warning === 'string' ? warning : warning.message }}</li></ul></details>
        <div class="update-actions">
          <n-button v-if="uploaded && !needsReconcile" type="primary" :loading="writing" :disabled="disabled || writing" @click="perform('submit')">确认提交原稿更新</n-button>
          <n-button v-if="localDraft" secondary :disabled="disabled || writing" @click="openAutomaticUpdate">去发布步骤调整交付方式</n-button>
          <n-button v-if="prepared && !localDraft && !needsReconcile" type="primary" :loading="writing" :disabled="disabled || writing" @click="perform('start')">{{ deliveryMode==='upload'?'确认上传替换文件':'确认更新原稿件' }}</n-button>
          <n-button v-if="prepared && !localDraft && !needsReconcile" secondary :disabled="writing" @click="revise">返回调整</n-button>
          <n-button v-if="needsReconcile || task.status === 'waiting'" type="primary" secondary :loading="writing" :disabled="writing" @click="perform('reconcile')">核对平台结果</n-button>
          <n-button v-if="['failed','paused'].includes(task.status) && !needsReconcile" type="primary" :loading="writing" :disabled="writing || disabled" @click="perform('retry')">{{ task.status === 'paused' ? '继续原稿更新' : '仅重试未完成步骤' }}</n-button>
          <n-button v-if="active" secondary :disabled="writing" @click="perform('pause')">{{ task.status === 'waiting' ? '暂停自动跟进' : '暂停更新' }}</n-button>
          <n-button v-if="['completed','failed','paused'].includes(task.status) && !needsReconcile" quaternary :disabled="writing || loading || disabled" @click="newAttempt">准备另一次更新</n-button>
        </div>
      </div>
      <div v-if="history.length > 1" class="update-history"><label>更新历史</label><n-select :value="historyId" :options="history" :disabled="writing || edited || active" aria-label="原稿更新历史" @update:value="selectHistory" /></div>
    </div>
  </section>
</template>

<style scoped>
.original-update { padding:24px; border:1px solid var(--df-border); border-radius:12px; scroll-margin-top:20px; min-width:0; }
.update-heading,.update-entry-actions,.update-target,.task-heading,.update-actions,.chapter-heading { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
.update-heading { justify-content:space-between; align-items:flex-start; gap:20px; }
.update-heading>div:first-child { flex:1 1 300px; min-width:0; }
.update-heading h3 { margin:0; font-size:17px; font-weight:600; line-height:1.6; }
.update-heading h3 span { display:inline-block; margin-left:8px; padding:2px 7px; border-radius:5px; background:var(--df-accent-soft); color:var(--df-accent); font-size:11px; font-weight:500; vertical-align:middle; }
.update-heading p,.muted,.update-target,.task-summary,.scope-label p,.scope-content>p,.planned-part p,.capability-notes p { color:var(--df-muted); font-size:12px; line-height:1.8; }
.update-heading p { margin:6px 0 0; }
.update-entry-actions { justify-content:flex-end; }
.update-entry-note { margin:14px 0 0; max-width:75ch; }
.update-body { margin-top:22px; padding-top:18px; border-top:1px solid var(--df-border); }
.update-target { margin-bottom:12px; overflow-wrap:anywhere; }
.scope-block { display:grid; grid-template-columns:minmax(155px,.25fr) minmax(0,1fr); gap:28px; padding:24px 0; border-bottom:1px solid var(--df-border); }
.scope-block:first-child { padding-top:14px; }
.scope-label,.scope-content { min-width:0; }
.scope-label :deep(.n-checkbox__label) { font-size:13px; font-weight:600; }
.scope-label h4,.chapter-heading h4,.update-confirmation h4 { margin:0; font-size:13px; font-weight:600; line-height:1.8; }
.scope-label p { margin:7px 0; max-width:28ch; }
.scope-count { color:var(--df-accent); font-size:11px; font-variant-numeric:tabular-nums; }
.scope-content>p { margin:0; }
.scope-content .scope-retained { margin:2px 0 0; }
.replacement-list { display:grid; gap:18px; }
.replacement-row { display:grid; grid-template-columns:minmax(115px,.6fr) minmax(0,1.4fr); gap:18px; align-items:start; }
.replacement-row+.replacement-row { padding-top:18px; border-top:1px solid var(--df-border); }
.replacement-row>div { min-width:0; }
.replacement-part-heading { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.replacement-toggle :deep(.n-checkbox__label) { font-size:11px; color:var(--df-text); }
.replacement-row strong { font-size:12px; overflow-wrap:anywhere; font-weight:550; line-height:1.8; }
.replacement-row small { display:block; color:var(--df-muted); font-size:11px; margin-top:4px; }
.replacement-choice { min-width:0; display:grid; gap:10px; }
.selected-video-info { padding:10px 12px; background:var(--df-surface-2,var(--df-bg)); border-radius:6px; }
.copy-toolbar { display:flex; justify-content:flex-end; margin-bottom:8px; }
.copy-field { display:grid; grid-template-columns:105px minmax(0,1fr); gap:14px; padding:10px 0; align-items:start; }
.copy-field+.copy-field { border-top:1px solid var(--df-border); }
.copy-field :deep(.n-checkbox) { padding-top:4px; }
.copy-field .field-retained { font-size:12px; color:var(--df-muted); padding-top:4px; line-height:1.6; }
.chapter-settings { display:grid; gap:14px; }
.chapter-options { display:flex; flex-wrap:wrap; gap:10px 22px; }
.chapter-settings>small { color:var(--df-muted); font-size:11px; line-height:1.7; }
.collection-settings { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:12px; }
.collection-settings>p { grid-column:1/-1; margin:0; }
.warning,.plan-warnings { color:var(--df-warning,#dba871); font-size:12px; line-height:1.8; }
.update-alert { margin:14px 0; }
.update-form-footer { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:18px; margin-top:22px; padding-top:6px; }
.scope-summary { display:grid; gap:5px; font-size:12px; line-height:1.7; max-width:65ch; }
.scope-summary strong { font-size:13px; font-weight:600; }
.scope-summary small { color:var(--df-muted); font-size:11px; }
.update-actions { margin-top:18px; }
.update-form-footer .update-actions { margin-top:0; }
.capability-notes { font-size:12px; margin-top:16px; }
.capability-notes summary,.checked-plan>summary,.planned-chapters summary { cursor:pointer; line-height:1.8; color:var(--df-muted); }
summary:focus-visible { outline:2px solid var(--df-accent); outline-offset:4px; border-radius:3px; }
.capability-notes p { margin:7px 0; }
.capability-notes strong { font-weight:500; }
.update-task { margin-top:20px; }
.task-heading { padding-bottom:12px; border-bottom:1px solid var(--df-border); }
.task-heading>:nth-child(2) { margin-left:auto; }
.task-summary { margin:12px 0; max-width:75ch; }
.update-progress-summary { margin:16px 0 18px; padding:14px 16px; border:1px solid var(--df-border); border-radius:9px; background:color-mix(in srgb,var(--df-accent) 4%,transparent); }
.update-progress-heading { display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; font-size:12px; }
.update-progress-heading>div { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; min-width:0; }
.update-progress-heading strong { font-size:13px; font-weight:600; }
.update-progress-heading span,.update-progress-facts { color:var(--df-muted); font-size:11px; }
.update-progress-summary :deep(.n-progress) { margin-top:10px; }
.update-progress-facts { margin:7px 0 0; line-height:1.7; }
.update-confirmation { padding:16px 18px; background:var(--df-accent-soft); border-radius:8px; margin:16px 0; }
.update-confirmation p { margin:8px 0 0; font-size:12px; line-height:1.8; }
.checked-plan { border-top:1px solid var(--df-border); padding-top:14px; margin-top:20px; font-size:12px; }
.checked-plan>summary { font-weight:600; color:var(--df-text); }
.planned-part { padding:14px 0; border-bottom:1px solid var(--df-border); }
.planned-part>strong { font-weight:550; margin-right:10px; overflow-wrap:anywhere; }
.planned-part p { margin:6px 0 0; }
.planned-part .timeline-note { color:var(--df-accent); }
.copy-difference { display:grid; grid-template-columns:70px minmax(0,1fr) minmax(0,1fr); gap:16px; padding:16px 0; border-bottom:1px solid var(--df-border); }
.copy-difference strong { font-weight:550; }
.copy-difference small { color:var(--df-muted); font-size:11px; }
.copy-difference p { white-space:pre-wrap; overflow-wrap:anywhere; margin:6px 0; line-height:1.8; max-height:240px; overflow:auto; }
.chapter-heading { justify-content:space-between; }
.planned-chapters { padding:18px 0; }
.planned-chapters details { padding:10px 0; }
.planned-chapters p { color:var(--df-accent); line-height:1.8; }
.planned-chapters ol { list-style:none; margin:8px 0; padding:0; }
.planned-chapters li { display:flex; gap:14px; padding:5px 0; line-height:1.7; }
.planned-chapters time { color:var(--df-muted); font-variant-numeric:tabular-nums; min-width:50px; }
.planned-collection { padding:12px 0; line-height:1.8; }
.plan-warnings { padding-left:20px; }
.update-stages { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 24px; list-style:none; padding:0; margin:18px 0; }
.update-stages li { display:grid; grid-template-columns:8px minmax(0,1fr) auto; gap:10px; align-items:center; padding:13px 0; border-bottom:1px solid var(--df-border); font-size:12px; }
.update-stages strong { font-weight:500; }
.stage-dot { height:6px; width:6px; border-radius:50%; background:var(--df-muted); opacity:.5; }
.update-stages li[data-stage-status=completed] .stage-dot,.update-stages li[data-stage-status=success] .stage-dot { background:var(--df-success,#6bb798); opacity:1; }
.update-stages li[data-stage-status=running] .stage-dot,.update-stages li[data-stage-status=active] .stage-dot { background:var(--df-accent); opacity:1; }
.update-stages li[data-stage-status=failed] .stage-dot,.update-stages li[data-stage-status=unknown] .stage-dot { background:var(--df-warning,#dba871); opacity:1; }
.update-stages li>p { grid-column:2/4; color:var(--df-warning,#dba871); margin:0; line-height:1.7; overflow-wrap:anywhere; }
.stage-progress { grid-column:2/4; display:flex; align-items:center; gap:8px; margin-top:2px; }
.stage-progress :deep(.n-progress) { flex:1; min-width:80px; }
.stage-progress>span { color:var(--df-muted); font-size:10px; white-space:nowrap; }
.update-history { display:flex; align-items:center; gap:12px; padding-top:18px; margin-top:24px; border-top:1px solid var(--df-border); font-size:12px; color:var(--df-muted); }
.update-history>label { flex-shrink:0; }
.update-history :deep(.n-select) { max-width:400px; }
@media(max-width:900px) { .scope-block { grid-template-columns:155px minmax(0,1fr); gap:20px; } .replacement-row { grid-template-columns:1fr; gap:8px; } }
@media(max-width:640px) { .original-update { padding:16px; } .update-heading h3 { font-size:16px; } .update-entry-actions { justify-content:flex-start; } .scope-block,.collection-settings,.update-stages { grid-template-columns:1fr; } .scope-block { gap:14px; padding:20px 0; } .scope-label p { max-width:65ch; } .copy-difference { grid-template-columns:1fr 1fr; } .copy-difference>strong { grid-column:1/-1; } .copy-field { grid-template-columns:1fr; gap:6px; } .copy-field .field-retained { display:none; } .copy-toolbar { justify-content:flex-start; } .update-form-footer .update-actions { width:100%; justify-content:flex-end; } }
</style>
