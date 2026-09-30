<script setup lang="ts">
// 评论原文、AI 建议和人工确认方案分层；AI 不自动替用户勾选。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute } from 'vue-router'
import { NAlert, NButton, NCheckbox, NCollapse, NCollapseItem, NInput, NInputNumber, NPagination, NSelect, NSlider, NSwitch, NTag, NTooltip, useDialog, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import CommentPreview from './CommentPreview.vue'
import TextColorPresets from './TextColorPresets.vue'
import ConfigurationForm from './ConfigurationForm.vue'
import { textColorPatch } from '../utils/textColorPresets'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import type { ConfigurationSource } from '../utils/configurationSource'
import { commentTextIssues, commentTextLength, defaultCommentTextLimits } from '../utils/commentOverlay'
import { textStyleFieldGroups } from '../utils/textStyleFields'

const props = defineProps<{ uid: string; version?: string; blocked?: boolean; videoDuration?: number; automation?: { state: string; message: string } | null }>()
const emit = defineEmits<{ 'busy-change': [value: string]; 'show-diagnostics': [] }>()
const dialog = useDialog(), message = useMessage()
const route = useRoute(), rootEl = ref<HTMLElement | null>(null)
const state = ref<any>(null), error = ref(''), pending = ref(''), saving = ref(false)
const compare = ref(false), page = ref(1), onlySuggested = ref(false)
const prompt = ref(''), expandedRows = ref<string[]>([])
const limit = ref<number | null>(10)
const items = ref<any[]>([])
const initialSettings = { start: 5, interval: 15, duration: 6, position: 'top_left', font_size: 36, margin: 60, width_percent: 0, show_author: false,
  language_mode: 'translated', source_first: false, color: '#FFFFFF', outline_color: '#000000', outline: 2, background_color: '#000000', background_opacity: 50,
  font: 'Microsoft YaHei', source_style_enabled: false, source_font: 'Microsoft YaHei', source_font_size: 30, source_color: '#FFFFFF', source_outline_color: '#000000', source_outline: 2 }
const settings = ref<any>({ ...initialSettings })
const revision = ref(''), baseline = ref('')
const rulesDirty=ref(false),styleEdited=ref(new Set<string>()),styleReset=ref(new Set<string>())
const styleFields=computed(()=>(state.value?.fields||[]).filter((f:any)=>Object.hasOwn(initialSettings,f.path.slice(9))))
const styleGroups=computed(()=>textStyleFieldGroups(styleFields.value,'comments.',Object.fromEntries(Object.entries(settings.value).map(([k,v])=>['comments.'+k,v])),'整体布局'))
function styleSource(key:string):ConfigurationSource|undefined{return styleReset.value.has(key)?state.value?.configuration?.parent_sources?.['comments.'+key]:styleEdited.value.has(key)?{kind:'project'}:state.value?.configuration?.sources?.['comments.'+key]}
function setStyle(key:string,value:any){if(saving.value)return;settings.value[key]=value;styleEdited.value.add(key);styleReset.value.delete(key)}
function inheritStyle(key:string){if(saving.value)return;settings.value[key]=state.value.configuration.defaults['comments.'+key];styleReset.value.add(key);styleEdited.value.delete(key)}
function ownsStyle(key:string){return !styleReset.value.has(key)&&(styleEdited.value.has(key)||Object.hasOwn(state.value?.configuration?.overrides||{},'comments.'+key))}
let disposed = false, epoch = 0, readSequence = 0, timer: ReturnType<typeof setTimeout> | undefined
const rows = computed<any[]>(() => state.value?.rows || [])
const rowsById = computed(() => new Map(rows.value.map(row => [row.id, row])))
const draftsById = computed(() => new Map(items.value.map(item => [item.id, item])))
const job = computed(() => state.value?.job)
const ratingSaved = computed(()=>Number(state.value?.rating_progress?.saved)||0)
const ratingRemaining = computed(()=>state.value?.rating_progress?.remaining??rows.value.length)
const ratingAction = computed(()=>ratingSaved.value&&ratingRemaining.value?`继续评分（${ratingRemaining.value} 条）`:ratingSaved.value?'更新候选（复用评分）':assessments.value.size?'补充 / 更新评分':'为全部评论评分')
const working = computed(() => pending.value || (job.value?.status === 'running' ? job.value.action : ''))
const locked = computed(() => !!working.value || !!props.blocked || props.automation?.state === 'running')
const untranslated = computed(() => rows.value.filter(row => !row.text_zh).length)
const assessments = computed(() => new Map<string, any>((state.value?.available_assessments ?? (state.value?.filter_stale ? [] : state.value?.filter?.assessments || [])).map((item: any) => [item.id, item])))
const ranked = computed<any[]>(() => [...assessments.value.values()].sort((a, b) => Number(b.eligible) - Number(a.eligible) || b.score - a.score))
const textLimits = computed(() => state.value?.text_limits || defaultCommentTextLimits)
const textIssues = (item: any) => commentTextIssues(item, settings.value.language_mode, textLimits.value)
function candidateDraft(row: any) {
  const draft = draftsById.value.get(row.id)
  return draft && draft.source_hash === row.source_hash ? draft : editableRow(row)
}
const candidateIssues = (row: any) => textIssues(candidateDraft(row))
const eligibleCandidates = computed<any[]>(() => ranked.value.length
  ? ranked.value.filter(row => row.eligible) : state.value?.filter_stale ? [] : (state.value?.filter?.items || []))
const usableCandidates = computed(() => eligibleCandidates.value.filter(candidate => {
  const row = rowsById.value.get(candidate.id)
  return row && !candidateIssues(row).length
}))
const excludedCandidates = computed(() => eligibleCandidates.value.length - usableCandidates.value.length)
const suggestions = computed(() => usableCandidates.value.slice(0, Math.max(0, Number(limit.value || 0))))
const hasIssues = (action:string) => rows.value.some(row=>row.ai_issues?.[action])
const issueLabel = (issue:any) => issue.status==='blocked'?'审核拦截':issue.status==='deferred'?'待处理':'处理失败'
const reasons = computed(() => new Map(suggestions.value.map(item => [item.id, item.reason])))
const sortByScore = ref(true)
const pageSize = 20
const visibleRows = computed(() => {
  const displayed = onlySuggested.value ? rows.value.filter(row => reasons.value.has(row.id)) : [...rows.value]
  if (sortByScore.value && ranked.value.length) {
    const order = new Map(ranked.value.map((row, index) => [row.id, index]))
    displayed.sort((a, b) => (order.get(a.id) ?? 9999) - (order.get(b.id) ?? 9999))
  }
  return displayed
})
const pageRows = computed(() => visibleRows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const snapshot = computed(() => JSON.stringify({ items: items.value, settings: settings.value }))
const dirty = computed(() => !!baseline.value && (baseline.value !== snapshot.value||styleEdited.value.size>0||styleReset.value.size>0))
const modeOptions = [{ label: '中文', value: 'translated' }, { label: '原文', value: 'original' }, { label: '双语', value: 'bilingual' }]
const positionOptions = [{label: '左上角', value: 'top_left'}, {label: '顶部居中', value: 'top'}, {label: '右上角', value: 'top_right'}, {label: '左下角', value: 'bottom_left'}, {label: '底部居中', value: 'bottom'}, {label: '右下角', value: 'bottom_right'}]
const showChinese = computed(() => settings.value.language_mode !== 'original')
const showSource = computed(() => settings.value.language_mode !== 'translated')
const plannedItems = computed(() => items.value.filter(item => !textIssues(item).length))
const excludedItems = computed(() => items.value.filter(item => textIssues(item).length))
const settingsError = computed(() => Object.values(settings.value).some(value => value === null) ? '请补齐排版参数。'
  : settings.value.duration > settings.value.interval ? `每条停留 ${settings.value.duration} 秒，超过出现间隔 ${settings.value.interval} 秒；请缩短停留或增大间隔。` : '')
const invalid = computed(() => !!settingsError.value || !!items.value.length && !plannedItems.value.length)
function plannedPosition(item: any) {
  const index = plannedItems.value.findIndex(row => row.id === item.id)
  return index < 0 ? '已排除 · 不会上屏' : `第 ${index + 1} 条 · ${settings.value.start + index * settings.value.interval} 秒`
}
const planChanged = computed(() => !!revision.value && state.value?.overlay_revision !== revision.value)
function applyTextColors(id: string, source = false) {
  if (saving.value) return
  const patch = textColorPatch(id)
  if (patch) Object.entries(patch).forEach(([key,value])=>setStyle((source?'source_':'')+key,value))
}

watch(working, value => emit('busy-change', value), { immediate: true })
watch([onlySuggested, () => visibleRows.value.length], () => { page.value = 1 })
function hydrate(value: any) {
  styleEdited.value.clear();styleReset.value.clear()
  items.value = structuredClone(value.overlay?.items || []).map((row: any) => ({ ...row, source_text: row.source_text ?? row.text }))
  settings.value = { ...initialSettings, ...value.defaults, ...(value.overlay?.settings || {}) }
  revision.value = value.overlay_revision
  baseline.value = snapshot.value
}
async function load(reset = false) {
  const ticket = epoch, sequence = ++readSequence
  try {
    const response = await fetch(`/api/projects/${props.uid}/comments`)
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '评论读取失败')
    if (disposed || ticket !== epoch || sequence !== readSequence) return
    const keepDraft = dirty.value
    prompt.value = value.configuration?.values?.['comments.prompt'] || value.default_filter_prompt || ''
    if (!state.value) limit.value=value.configuration?.values?.['comments.count']||10
    state.value = value
    if (reset || !keepDraft) hydrate(value)
  } catch (e: any) { if (!disposed && ticket === epoch && sequence === readSequence) error.value = e.message || '评论读取失败' }
  finally {
    if (!disposed && ticket === epoch && sequence === readSequence) {
      clearTimeout(timer)
      if (working.value || props.automation?.state === 'running') timer = setTimeout(() => void load(), 2000)
    }
  }
}
watch(() => [props.uid, props.version], (_value, old) => {
  if (!old || props.uid !== old[0]) {
    epoch++; clearTimeout(timer); state.value = null; items.value = []; baseline.value = ''; revision.value = ''; pending.value = ''; error.value = ''; expandedRows.value = []; prompt.value = ''
  }
  void load()
}, { immediate: true })
// 自动流程采集不由本页按钮触发；阶段变化时也要更新，并保留人工未保存的草稿。
watch(() => props.automation?.state, (value, previous) => {
  if (value === 'running' || value === 'done' || previous === 'running') void load()
})

async function process(action: 'translate' | 'filter', force = false, options:Record<string,boolean> = {}) {
  if (locked.value || !rows.value.length || (action === 'filter' && (rulesDirty.value || !prompt.value.trim() || !limit.value))) return
  const ticket = epoch, uid = props.uid
  pending.value = action; error.value = ''
  clearTimeout(timer); timer = setTimeout(() => void load(), 1000)
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
        if (!item.display_text?.trim() && row?.text_zh) item.display_text = row.text_zh
      }
    }
    if(value.job?.status==='partial')message.warning(value.job.warning)
    else message.success(action === 'translate' ? '中文翻译已更新' : '逐条评分已保存，可直接调整候选数量，无需再次调用 AI')
  } catch (e: any) { if (!disposed && ticket === epoch) error.value = e.message || '请求中断，请重新读取确认进度' }
  finally { if (!disposed && ticket === epoch) { pending.value = ''; await load() } }
}
async function stop() {
  const ticket = epoch
  const id = job.value?.id
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
  return { id: row.id, source_hash: row.source_hash, text: row.text, author: row.author, url: row.url, display_text: row.text_zh || '', source_text: row.text }
}
function toggle(row: any, checked: boolean) {
  if (saving.value) return
  if (!checked) { items.value = items.value.filter(item => item.id !== row.id); return }
  if (selected(row.id)) return
  if (items.value.length >= 100) { message.warning('最多选择 100 条上屏评论'); return }
  items.value.push(editableRow(row))
}
function expand(id: string) { expandedRows.value = expandedRows.value.includes(id) ? expandedRows.value.filter(key => key !== id) : [...expandedRows.value, id] }
function assessmentLabel(id: string) {
  const item = assessments.value.get(id)
  return item ? `${item.score} 分` : reasons.value.has(id) ? 'AI 建议' : '未入选'
}
function assessmentReason(id: string) {
  const item = assessments.value.get(id)
  const row = rowsById.value.get(id)
  return item ? `${item.eligible ? row && candidateIssues(row).length ? '评分符合条件，但当前文字不满足上屏要求。' : reasons.value.has(id) ? '当前候选。' : '符合条件，但不在当前前 N 条中。' : 'AI 判断不符合评分标准，仍可手动选择。'}${item.reason}`
    : reasons.value.get(id) || '旧版筛选未记录未入选原因；逐条评分后可查看判断理由。'
}
function useCandidates() {
  if (saving.value) return
  const ticket = epoch
  const wanted = suggestions.value.slice(0, 100).map(suggestion => rowsById.value.get(suggestion.id)).filter(Boolean)
  const next = JSON.parse(JSON.stringify(wanted.map(row => items.value.find(item => item.id === row.id && item.source_hash === row.source_hash) || editableRow(row))))
  const removed = items.value.filter(item => !wanted.some(row => row.id === item.id)).length
  const replaced = items.value.filter(item => wanted.some(row => row.id === item.id && row.source_hash !== item.source_hash)).length
  const changes = [removed ? `移出 ${removed} 条不在当前候选中的评论` : '', replaced ? `替换 ${replaced} 条原文已变化的评论及其上屏草稿` : ''].filter(Boolean).join('，')
  if (removed || replaced) dialog.warning({ title: `调整为前 ${next.length} 条候选`, content: `将${changes}；保留其余评论已编辑的文字。不调用 AI，不自动保存或合成。`, positiveText: '调整列表', negativeText: '保留当前选择', onPositiveClick: () => { if (!disposed && ticket === epoch) items.value = next } })
  else items.value = next
}
function move(index: number, direction: number) {
  const other = index + direction
  if (other < 0 || other >= items.value.length) return
  ;[items.value[index], items.value[other]] = [items.value[other], items.value[index]]
}
function confirmPlan() {
  if (invalid.value || saving.value || !state.value) return
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
  if (saving.value) { message.warning('请先完成上屏方案确认'); return false }
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
function unload(event: BeforeUnloadEvent) { if (dirty.value||rulesDirty.value) { event.preventDefault(); event.returnValue = '' } }
function locate() { if (route.hash === '#comments-studio') rootEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
watch(() => route.hash, locate, { flush: 'post' })
onMounted(() => { window.addEventListener('beforeunload', unload); locate() })
onBeforeUnmount(() => { disposed = true; epoch++; clearTimeout(timer); emit('busy-change', ''); if (typeof window !== 'undefined') window.removeEventListener('beforeunload', unload) })
</script>

<template>
  <div id="comments-studio" ref="rootEl" class="comment-studio">
    <n-alert v-if="automation && (automation.state !== 'done' || !rows.length)" :type="automation.state==='stopped'?'warning':'info'" :show-icon="false" class="notice">{{ automation.message }}</n-alert>
    <details class="filter-panel"><summary>评论准备与评分规则</summary><ConfigurationForm :scope="'project:'+uid" :sections="['comments']" :paths="['comments.mode','comments.limit','comments.count','comments.prompt','comments.failure_policy']" @dirty-change="rulesDirty=$event" @saved="load()"/></details>
    <n-alert v-if="error" type="error" :show-icon="false" class="notice">{{ error }} <n-button size="tiny" @click="load()">重新读取</n-button></n-alert>
    <n-alert v-if="working" type="info" :show-icon="false" class="notice">
      {{ working === 'translate' ? '正在翻译' : '正在评分' }} {{ job?.status === 'running' ? `${job.processed} / ${job.total}` : '…' }}
      <n-button v-if="job?.status === 'running'" size="tiny" :disabled="job.cancelling" @click="stop">{{ job.cancelling ? '正在停止…' : '停止' }}</n-button>
    </n-alert>
    <n-alert v-else-if="['failed','interrupted'].includes(job?.status) && !error" type="warning" :show-icon="false" class="notice">{{ job.error }}<p v-if="job.suggestion">{{ job.suggestion }}</p></n-alert>
    <n-alert v-else-if="job?.status==='partial'" type="warning" :show-icon="false" class="notice">{{ job.warning }}</n-alert>
    <n-button v-if="job || working || error || automation?.state==='stopped'" text size="small" @click="emit('show-diagnostics')">查看下方 LLM 对话与评论日志 →</n-button>
    <template v-if="rows.length">
      <div class="tools">
        <span class="muted">{{ rows.length }} 条 · 已翻译 {{ rows.length - untranslated }} 条<span v-if="ratingSaved"> · 已缓存评分 {{ ratingSaved }} 条</span></span>
        <div class="tools">
          <n-tooltip><template #trigger><n-button size="small" :disabled="locked || !untranslated" @click="process('translate')">{{ untranslated ? '全部翻译成中文' : '已全部翻译' }}</n-button></template>调用已配置的 LLM；只补充尚未翻译的评论。原文永久保留，已勾选的上屏文字不会被覆盖。</n-tooltip>
          <n-button v-if="hasIssues('translate')" size="small" quaternary :disabled="locked" @click="process('translate',false,{retry_failed:true})">重试未完成翻译</n-button>
          <label class="compare"><n-switch v-model:value="compare" size="small" /> 对照原文</label>
        </div>
      </div>
      <n-collapse class="filter-panel">
        <n-collapse-item title="AI 评分标准" name="filter">
          <template #header-extra><n-tooltip><template #trigger><n-button size="small" :disabled="locked || rulesDirty || !prompt.trim() || !limit" @click.stop="process('filter')">{{ ratingAction }}</n-button></template>调用 LLM 逐条评价；标准相同的未变评论复用评分，只处理新增或变化部分。</n-tooltip></template>
          <p class="muted">{{ prompt }} <ConfigurationSourceBadge :source="state?.configuration?.sources?.['comments.prompt']"/>（在上方“评论准备与评分规则”中修改）</p>
        <div class="tools filter-actions"><n-button v-if="assessments.size" size="small" quaternary :disabled="locked||rulesDirty" title="忽略缓存，重新调用 LLM 评价全部评论" @click="process('filter', true)">全部重新评分</n-button></div>
          <p v-if="state?.filter?.prompt && prompt.trim() !== state.filter.prompt" class="muted">标准已修改；更新评分前，列表仍显示上次结果。</p>
        </n-collapse-item>
      </n-collapse>
      <div v-if="ratingSaved && ratingRemaining || hasIssues('filter')" class="tools"><n-button v-if="ratingSaved" size="small" :disabled="locked||rulesDirty" title="仅根据已有有效评分更新候选，不调用 AI" @click="process('filter',false,{only_cached:true})">仅用已完成评分</n-button><n-button v-if="hasIssues('filter')" size="small" quaternary :disabled="locked||rulesDirty" @click="process('filter',false,{retry_failed:true})">重试未完成评分</n-button></div>
      <n-alert v-if="state?.filter_stale" type="warning" :show-icon="false" class="notice">评论列表已更新，变化项需重新处理；{{ ratingSaved }} 条有效评分可继续使用。</n-alert>
      <p v-else-if="state?.filter_outdated" class="muted">旧结果没有逐条分数，评分后可自由调整数量。</p>
      <p v-else-if="state?.filter?.generated_at && !eligibleCandidates.length" class="muted">本次评分未找到符合要求的评论，可以调整要求后重试。</p>
      <div v-if="ranked.length || suggestions.length" class="tools suggestion-bar">
        <div class="tools"><span class="muted">候选前</span><n-input-number v-model:value="limit" :min="1" :max="500" :precision="0" style="width:90px" :input-props="{ 'aria-label': '候选评论数量（不调用 AI）' }" /><span class="muted">条</span><n-checkbox v-model:checked="onlySuggested">只看候选（{{ suggestions.length }}）</n-checkbox><n-checkbox v-if="ranked.length" v-model:checked="sortByScore">按分数排序</n-checkbox></div>
        <n-tooltip><template #trigger><n-button size="small" :disabled="saving || !suggestions.length" @click="useCandidates">使用前 {{ Math.min(suggestions.length, 100) }} 条可用评论</n-button></template>按评分选择，自动跳过超长或缺少所选语言文字的评论并递补；保留仍入选的编辑文字。最多上屏 100 条，不调用 AI。</n-tooltip>
      </div>
      <p v-if="excludedCandidates" class="exclusion-summary">{{ excludedCandidates }} 条评分合格的评论因超长或缺少所选语言文字已排除，按分数递补。完整评论仍可阅读、翻译。</p>
      <div class="comment-list" aria-label="紧凑评论列表">
        <article v-for="row in pageRows" :key="row.id" class="comment-row" :class="{ selected: selected(row.id) }">
          <n-checkbox :checked="selected(row.id)" :disabled="saving" :aria-label="'选择评论：' + (row.author || '匿名观众')" @update:checked="toggle(row, $event)" />
          <div class="comment-body">
            <div class="byline"><span class="author">{{ row.author || '匿名观众' }}</span><span v-if="row.like_count != null">♥ {{ row.like_count }}</span><n-tag v-if="row.is_pinned" size="tiny" :bordered="false">置顶</n-tag><n-tag v-if="row.author_is_uploader" size="tiny" :bordered="false">作者</n-tag>
              <n-tooltip v-if="assessments.has(row.id)"><template #trigger><n-tag size="tiny" :bordered="false" :type="reasons.has(row.id) ? 'info' : 'default'" tabindex="0">{{ assessmentLabel(row.id) }}</n-tag></template>{{ assessmentReason(row.id) }}</n-tooltip>
              <n-tooltip v-for="(issue,action) in row.ai_issues||{}" :key="action"><template #trigger><n-tag type="warning" size="tiny" :bordered="false" tabindex="0">{{ action==='filter'?'评分':'翻译' }}：{{ issueLabel(issue) }}</n-tag></template>{{ issue.message }}</n-tooltip>
              <span v-if="!row.text_zh" class="pending-translation">原文</span>
            </div>
            <p v-if="candidateIssues(row).length" class="row-issue">不上屏 · {{ candidateIssues(row).map(issue => issue.message).join('；') }}<small>可勾选后精简上屏文字</small></p>
            <p :class="{ clipped: !expandedRows.includes(row.id) && (row.text_zh || row.text).length > 180 }">{{ row.text_zh || row.text }}</p>
            <div v-if="compare && row.text_zh" class="original"><p :class="{ clipped: !expandedRows.includes(row.id) && row.text.length > 180 }">{{ row.text }}</p></div>
            <button v-if="(row.text_zh || row.text).length > 180 || (compare && row.text.length > 180)" type="button" class="expand-text" @click="expand(row.id)">{{ expandedRows.includes(row.id) ? '收起' : '展开全文' }}</button>
          </div>
        </article>
      </div>
      <n-pagination v-if="visibleRows.length > pageSize" v-model:page="page" :item-count="visibleRows.length" :page-size="pageSize" class="pagination" />
    </template>
    <p v-else-if="!automation || ['none','done','stopped'].includes(automation.state)" class="muted">{{ state ? '尚无评论正文，可手动获取热门评论。' : '正在读取评论…' }}</p>

    <n-collapse class="plan-panel" :default-expanded-names="['plan']">
      <n-collapse-item :title="`待上屏 · ${plannedItems.length} 条${excludedItems.length ? ` · 已排除 ${excludedItems.length} 条` : ''}`" name="plan">
        <template #header-extra><n-tag size="small" :type="dirty ? 'warning' : 'default'" :bordered="false">{{ dirty ? '未确认修改' : items.length ? '已确认方案' : '未选择' }}</n-tag></template>
        <n-alert v-if="planChanged && dirty" type="warning" :show-icon="false" class="notice">已保存方案发生变化，请先保留自己的文字，再通过“重置草稿”重新读取。</n-alert>
        <div class="plan-grid">
          <div class="comment-workbench">
          <CommentPreview v-if="plannedItems.length" :key="uid" :uid="uid" :items="plannedItems" :settings="settings" :invalid="!!settingsError" :video-duration="videoDuration" />
          <p v-else class="muted">{{ items.length ? '所选评论暂不可上屏，请翻译、精简文字或改选可用评论。' : '选择评论后预览；可先设置出现节奏与排版。' }}</p>
          <p v-if="excludedItems.length" class="exclusion-summary">{{ excludedItems.length }} 条已自动排除，不进入预览和保存；下方仍可编辑，符合要求后自动纳入。</p>
          <div class="ordered-list">
            <article v-for="(item, index) in items" :key="item.id" class="edit-row" :class="{ excluded: textIssues(item).length }">
              <div class="tools"><span class="muted">{{ plannedPosition(item) }} · {{ item.author || '匿名观众' }}</span><div>
                <n-button size="tiny" quaternary :disabled="saving || index === 0" :aria-label="'上移第 ' + (index + 1) + ' 条'" @click="move(index, -1)">↑</n-button>
                <n-button size="tiny" quaternary :disabled="saving || index === items.length - 1" :aria-label="'下移第 ' + (index + 1) + ' 条'" @click="move(index, 1)">↓</n-button>
                <n-button size="tiny" quaternary :disabled="saving" @click="toggle(item, false)">移除</n-button>
              </div></div>
              <div class="bilingual-editor" :class="{ 'source-first': settings.source_first }">
                <div v-if="showChinese" class="chinese-editor"><span class="edit-label">中文 <small>{{ commentTextLength(item.display_text) }} / {{ textLimits.translated }} 字符</small></span>
                  <n-input v-model:value="item.display_text" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" :disabled="saving" :status="textIssues(item).some(issue => issue.field === 'display_text') ? 'warning' : undefined" placeholder="尚无译文，可先全部翻译，或在此填写" :input-props="{ 'aria-label': '第 ' + (index + 1) + ' 条中文上屏文字' }" />
                </div>
                <div v-if="showSource" class="source-editor"><span class="edit-label">原文 <small>{{ commentTextLength(item.source_text) }} / {{ textLimits.original }} 字符</small></span>
                  <n-input v-model:value="item.source_text" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }" :disabled="saving" :status="textIssues(item).some(issue => issue.field === 'source_text') ? 'warning' : undefined" :input-props="{ 'aria-label': '第 ' + (index + 1) + ' 条原文上屏文字' }" />
                </div>
              </div>
              <p v-if="textIssues(item).length" class="row-issue">{{ textIssues(item).map(issue => issue.message).join('；') }}</p>
            </article>
          </div>
          </div>
          <div class="layout-controls">
            <h4>出现节奏与排版</h4>
            <n-tooltip><template #trigger><span class="limit-hint" tabindex="0">上屏上限：中文 {{ textLimits.translated }} · 原文 {{ textLimits.original }} 字符 ⓘ</span></template>这是应用内置的排版保护上限，目前不可在设置中修改，不是 B 站要求。仅检查所选显示语言；不截断完整原文或译文。实际画面还受字号和画幅影响。</n-tooltip>
            <TextColorPresets kind="评论" :color="settings.color" :outline-color="settings.outline_color" :outline="settings.outline" :source-enabled="settings.source_style_enabled" :source-color="settings.source_color" :source-outline-color="settings.source_outline_color" :source-outline="settings.source_outline" :disabled="saving" @select="applyTextColors" @enable-source="setStyle('source_style_enabled',true)" />
            <section v-for="group in styleGroups" :key="group.name" class="language-style-group"><h5>{{ group.name }}</h5>
              <div v-for="field in group.fields" :key="field.path" class="style-field" role="group" :aria-label="field.label">
                <span class="style-source" :title="field.help">{{ field.label }}<small><ConfigurationSourceBadge :source="styleSource(field.path.slice(9))" :inherited="!ownsStyle(field.path.slice(9))"/> <n-button v-if="ownsStyle(field.path.slice(9))" text size="tiny" :disabled="saving" :aria-label="`${field.label}恢复继承`" @click="inheritStyle(field.path.slice(9))">↶</n-button></small></span>
                <ParamControl :model-value="settings[field.path.slice(9)]" :field="field" :disabled="saving" @update:model-value="setStyle(field.path.slice(9),$event)"/>
              </div>
            </section>
            <n-alert v-if="settingsError" type="warning" :show-icon="false">{{ settingsError }}</n-alert>
          </div>
        </div>
        <div class="tools save-bar">
          <RouterLink :to="`/projects/${uid}/steps/assemble`">去合成成片 →</RouterLink>
          <div class="tools"><n-button v-if="dirty" size="small" :disabled="saving" @click="dialog.warning({ title: '放弃当前草稿？', content: '重新读取已确认方案，已完成的翻译保留。', positiveText: '放弃草稿', negativeText: '继续编辑', onPositiveClick: () => load(true) })">重置草稿</n-button><n-button type="primary" size="small" :disabled="!state || invalid || saving || (!dirty && !!baseline && !excludedItems.length)" @click="confirmPlan">确认上屏方案（{{ plannedItems.length }} 条）</n-button></div>
        </div>
      </n-collapse-item>
    </n-collapse>
  </div>
</template>

<style scoped>
.style-source{display:flex;justify-content:space-between;align-items:center;gap:8px}.style-source small{font-size:10px;color:var(--df-muted)}summary{cursor:pointer;font-size:13px}
.tools { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
.muted,.byline,.edit-meta { font-size:12px; color:#99a7b8; } .compare { display:flex; align-items:center; gap:8px; font-size:12px; }
.notice { margin:10px 0; } .filter-panel { margin:16px 0; } .filter-actions,.preview-tools { justify-content:flex-start; margin-top:10px; }
.suggestion-bar,.old-filter { margin:8px 0; } .comment-list { border:1px solid var(--df-border); border-radius:8px; max-height:520px; overflow:auto; scrollbar-gutter:stable; }
.comment-row { display:flex; align-items:flex-start; gap:10px; padding:9px 12px; border-bottom:1px solid rgba(128,128,128,.12); }
.comment-row:last-child { border-bottom:0; } .comment-row:hover { background:rgba(128,128,128,.04); }
.comment-row.selected { background:rgba(87,143,208,.07); } .comment-row > .n-checkbox { margin-top:4px; }
.comment-body { flex:1; min-width:0; } .byline { display:flex; gap:8px; align-items:center; flex-wrap:wrap; font-size:11px; }
.author { font-weight:600; color:#b3c0d0; } .pending-translation { margin-left:auto; opacity:.65; }
.comment-body p { margin:4px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.55; font-size:13px; }
.comment-body .row-issue,.row-issue,.exclusion-summary { color:var(--df-warning,#dca35c); font-size:12px; line-height:1.6; margin:6px 0; }
.row-issue small { display:block; font-size:11px; color:var(--df-muted); }
.edit-row.excluded { border-left:2px solid var(--df-warning,#dca35c); padding-left:10px; }
.limit-hint { font-size:11px; color:var(--df-muted); line-height:1.6; cursor:help; }
.comment-body p.clipped { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.original { border-left:2px solid var(--df-border); padding:0 9px; color:#929dab; margin:6px 0 2px; }
.original p { font-size:12px; } .expand-text { padding:2px 0 0; border:0; background:none; color:var(--df-accent); cursor:pointer; font-size:11px; }
.pagination { margin-top:14px; justify-content:flex-end; } .plan-panel { margin-top:24px; padding-top:20px; border-top:1px solid var(--df-border); }
.plan-grid { display:grid; grid-template-columns:minmax(0,1fr) 240px; gap:24px; }
.comment-workbench { min-width:0; } .ordered-list { max-height:380px; overflow:auto; } .edit-row { padding:8px 0 12px; border-bottom:1px solid var(--df-border); }
.bilingual-editor { display:flex; flex-direction:column; gap:8px; } .bilingual-editor.source-first .source-editor { order:-1; }
.edit-label { display:flex; justify-content:space-between; margin:3px 0; color:#98a6b7; font-size:11px; } .edit-label small { font-size:10px; opacity:.75; }
.layout-controls { display:flex; flex-direction:column; gap:14px; padding:16px; background:rgba(128,128,128,.045); border-radius:8px; }
.layout-controls h4 { margin:0; } .layout-controls .style-field { display:flex; flex-direction:column; gap:6px; font-size:12px; }
.style-field :deep(.n-switch) { align-self:flex-start; }
.language-style-group { display:flex; flex-direction:column; gap:14px; padding-top:10px; border-top:1px solid var(--df-border); }
.language-style-group h5 { margin:0; font-size:11px; color:var(--df-muted); }
.invalid { color:#e8ad77; } .frame { display:block; max-width:100%; max-height:420px; border-radius:8px; margin-top:14px; } .frame.stale { opacity:.65; }
.save-bar { margin-top:18px; } .save-bar > a { font-size:12px; }
@media(max-width:760px) { .plan-grid { grid-template-columns:1fr; } .layout-controls { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); } .layout-controls h4 { grid-column:1/-1; } }
@media(max-width:520px) { .layout-controls { grid-template-columns:1fr; } }
</style>
