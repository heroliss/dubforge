<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCheckbox, NInput, NSelect, NTag } from 'naive-ui'
import { partialRanges, partialSummary, partialTime, type PartialRangeDraft } from '../utils/assemblyPartial'
import { parseCutTime } from '../utils/outputCuts'
import { readAssemblySentenceDraft } from '../utils/assemblySentenceLink'

type Candidate = { id: number; fields?: string[]; current?: { source_text?: string; text?: string }; reason?: string }
const props = defineProps<{ uid: string; disabled?: boolean; preferredVersion?: string; requestToken?: unknown;
  embedded?: boolean; candidates?: Candidate[]; versionLimit?: boolean }>()
const emit = defineEmits<{ created: [version: any]; 'busy-change': [busy: boolean]; 'baseline-change': [id: string] }>()
const expanded = ref(!!props.embedded), busy = ref(false), error = ref(''), context = ref<any>(null)
const baseline = ref<string | null>(null), lineIds = ref<number[]>([]), ranges = ref<PartialRangeDraft[]>([])
const replaceAudio = ref(false), name = ref(''), plan = ref<any>(null)
const requestNotice = ref(''), query = ref(''), candidateOnly = ref(!!props.candidates?.length), visibleLimit = ref(80)
const incomingRequest = ref(false), requestBaseline = ref('')
let appliedRequest: unknown = null, openingRequest = false
let controller: AbortController | null = null, generation = 0, disposed = false
const locked = computed(() => !!props.disabled || busy.value)
const options = computed(() => (context.value?.baselines || []).map((row: any) => ({ label: row.name, value: row.version_id })))
const candidateMap = computed(() => new Map((props.candidates || []).map(row => [row.id, row])))
const sentenceRows = computed(() => (context.value?.lines || []).map((row: any) => {
  const candidate = candidateMap.value.get(row.id)
  return { ...row, candidate, sourceText: candidate?.current?.source_text || '', text: candidate?.current?.text || row.text || '' }
}))
const filteredRows = computed(() => {
  const search = query.value.trim().toLocaleLowerCase()
  // 搜索始终覆盖全部句子，避免编辑记录筛选藏住明确搜索的句子。
  return sentenceRows.value.filter((row: any) => search
    ? `${row.id} ${row.text} ${row.sourceText}`.toLocaleLowerCase().includes(search)
    : !candidateOnly.value || !!row.candidate)
})
const displayedRows = computed(() => filteredRows.value.slice(0, visibleLimit.value))
const selectedHidden = computed(() => lineIds.value.filter(id => !filteredRows.value.some((row: any) => row.id === id)).length)
const rangeIssues = computed(() => ranges.value.map((row, index) => {
  try { partialRanges([row], context.value?.duration); return '' }
  catch (issue: any) { return String(issue.message || '请填写有效的时间范围。').replace('第 1 段', `第 ${index + 1} 段`) }
}))
const hasRangeIssue = computed(() => rangeIssues.value.some(Boolean))
const canCheck = computed(() => !!baseline.value && !hasRangeIssue.value && (lineIds.value.length > 0 || ranges.value.length > 0))
const signature = computed(() => JSON.stringify([baseline.value, lineIds.value, ranges.value, replaceAudio.value, name.value]))
watch(signature, () => { plan.value = null; error.value = '' }, { flush: 'sync' })
watch([query, candidateOnly], () => { visibleLimit.value = 80 })
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
watch(baseline, value => {
  if (requestBaseline.value && value !== requestBaseline.value) {
    requestBaseline.value = ''; lineIds.value = []; ranges.value = []
    requestNotice.value = '已切换基准成片，请重新选择修订句子与时间范围，避免沿用其他成片的旧位置。'
  }
}, { flush: 'sync' })
function syncPreferred(value?: string) {
  // 嵌入时父级是唯一基准来源，旧上下文列表不能拒绝刚生成的新版本。
  if (!props.embedded || !value?.trim() || value === baseline.value) return
  requestBaseline.value = ''; baseline.value = value; lineIds.value = []; ranges.value = []; plan.value = null
  name.value = ''; requestNotice.value = ''; error.value = ''; incomingRequest.value = false
}
watch(() => props.preferredVersion, syncPreferred, { flush: 'sync' })
async function request(path: string, body?: any) {
  if (busy.value || disposed) return
  const uid = props.uid, token = generation, read = new AbortController()
  controller = read; busy.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/${path}`, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      signal: AbortSignal.any([read.signal, AbortSignal.timeout(120000)]),
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (response.status === 404) throw new Error('当前后端尚未提供局部修订接口，请重启后端并刷新页面。')
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '局部修订检查失败，请重试。')
    if (!disposed && uid === props.uid && token === generation) return data
  } catch (e: any) {
    if (!disposed && uid === props.uid && token === generation && !read.signal.aborted)
      error.value = e.name === 'TimeoutError' ? '检查耗时较长，请稍后重试；已有成片保持不变。' : e.message
  } finally {
    if (controller === read) { controller = null; busy.value = false }
  }
}
async function load() {
  const data = await request('partial-context')
  if (!data) return
  context.value = data
  if (props.embedded && props.preferredVersion?.trim()) baseline.value = props.preferredVersion
  else if (!options.value.some((row: any) => row.value === baseline.value))
    baseline.value = options.value.some((row: any) => row.value === props.preferredVersion) ? props.preferredVersion! : options.value[0]?.value || null
  return data
}
async function openRequested(storage?: Pick<Storage, 'getItem' | 'setItem'>, replaceDraft = false) {
  if (!props.requestToken || appliedRequest === props.requestToken || openingRequest || props.disabled || disposed) return
  if (!replaceDraft && (lineIds.value.length || ranges.value.length || name.value.trim())) {
    incomingRequest.value = true; expanded.value = true; return
  }
  const token = props.requestToken, uid = props.uid
  let draft
  try { draft = readAssemblySentenceDraft(storage || sessionStorage, uid, token) }
  catch { draft = null }
  expanded.value = true
  if (!draft) { error.value = '句子修订范围已失效，请从句子列表重新打开，或在此手动选择。'; appliedRequest = token; return }
  plan.value = null
  openingRequest = true
  try {
    const loaded = await load()
    if (!loaded || props.uid !== uid || props.requestToken !== token || disposed || !context.value) return
    const base = context.value.baselines.find((row: any) => row.version_id === draft.baseline)
    if (!base || base.artifact_id !== draft.artifactId || base.artifact_hash !== draft.artifactHash) {
      error.value = '基准成片已改变，请回到句子列表刷新对照后重新选择修订范围。'; appliedRequest = token; return
    }
    const existing = new Set(context.value.lines.map((row: any) => row.id))
    const available = draft.lineIds.filter(id => existing.has(id))
    if (draft.lineIds.length && !available.length) { error.value = '这些句子已不存在，请重新选择修订范围。'; appliedRequest = token; return }
    requestBaseline.value = ''; baseline.value = draft.baseline; lineIds.value = available
    // 逐句新旧位置由后端随最终勾选计算；重复带入的合并范围不能越过取消勾选。
    ranges.value = draft.lineIds.length ? [] : draft.ranges.map(row => ({ key: crypto.randomUUID(), start: partialTime(row.start), end: partialTime(row.end) }))
    replaceAudio.value = draft.replaceAudio; appliedRequest = token
    requestBaseline.value = draft.baseline; incomingRequest.value = false
    requestNotice.value = draft.lineIds.length ? `已从句子列表带入 ${available.length} 句。新旧位置由预检随最终勾选计算，请核对后检查修订范围。`
      : `已带入 ${draft.ranges.length} 段自定义时间范围，请核对后检查修订范围。`
    if (props.embedded && props.preferredVersion !== draft.baseline) emit('baseline-change', draft.baseline)
    await nextTick()
    if (typeof document !== 'undefined') document.getElementById('assembly-partial')?.scrollIntoView({ block: 'start' })
  } finally {
    openingRequest = false
    if (!disposed && props.uid === uid && props.requestToken !== token)
      void nextTick(() => { void openRequested(storage) })
  }
}
function keepDraft() { incomingRequest.value = false; appliedRequest = props.requestToken }
function toggle() { expanded.value = !expanded.value; if (expanded.value && !context.value && !locked.value) void load() }
function toggleLine(id: number, checked: boolean) {
  if (!locked.value) lineIds.value = checked ? [...new Set([...lineIds.value, id])] : lineIds.value.filter(value => value !== id)
}
function clearSelection() { if (!locked.value) lineIds.value = [] }
function addRange() { if (!locked.value) ranges.value.push({ key: crypto.randomUUID(), start: '', end: '' }) }
function commitRangeTime(row: PartialRangeDraft, key: 'start' | 'end') {
  if (locked.value) return
  const value = parseCutTime(row[key])
  if (value !== null) row[key] = partialTime(value)
}
function body() {
  return { base_version_id: baseline.value, line_ids: [...lineIds.value],
    ranges: partialRanges(ranges.value, context.value?.duration), replace_audio: replaceAudio.value,
    ...(name.value.trim() ? { name: name.value.trim() } : {}) }
}
async function check() {
  if (locked.value || !canCheck.value) return
  plan.value = null
  try { const data = await request('partial-plan', body()); if (data) plan.value = data }
  catch (e: any) { error.value = e.message }
}
async function create() {
  if (locked.value || props.versionLimit || !plan.value || !canCheck.value) return
  try {
    const data = await request('partial-create', { ...body(), fingerprint: plan.value.fingerprint })
    if (data?.version) { emit('created', { ...data.version, partial_summary: { ranges: data.ranges, replace_audio: replaceAudio.value } }); expanded.value = !!props.embedded; plan.value = null }
    else if (error.value) plan.value = null
  } catch (e: any) { error.value = e.message }
}
function reset() {
  generation++; controller?.abort(); controller = null; busy.value = false
  requestBaseline.value = ''; incomingRequest.value = false
  expanded.value = !!props.embedded; context.value = null; baseline.value = null; lineIds.value = []; ranges.value = []
  replaceAudio.value = false; name.value = ''; plan.value = null; error.value = ''; query.value = ''
  candidateOnly.value = !!props.candidates?.length; visibleLimit.value = 80
  requestNotice.value = ''; appliedRequest = null
}
async function initialize() {
  await openRequested()
  if (props.embedded && !context.value && !busy.value && !props.disabled && !disposed) void load()
}
watch(() => props.uid, () => { reset(); void initialize() })
watch(() => [props.requestToken, props.disabled], () => { if (typeof window !== 'undefined') void initialize() })
onMounted(() => { void initialize() })
onBeforeUnmount(() => { disposed = true; reset(); emit('busy-change', false) })
</script>
<template>
  <section id="assembly-partial" class="partial-editor" :class="{ embedded }" aria-label="按范围修订成片">
    <n-button v-if="!embedded" size="small" secondary :disabled="disabled || busy" :aria-expanded="expanded" @click="toggle">{{ expanded ? '收起范围修订' : '按范围修订成片' }}</n-button>
    <div v-if="embedded || expanded" class="partial-content" :aria-busy="busy">
      <header><h3 v-if="!embedded">选择需要修订的片段</h3><p>{{ embedded ? '所选时间段采用当前字幕、评论和画面设置；其余沿用基片，另存为修订版。' : '所选时间段采用当前全部画面内容，其余沿用基准成片。修订方案保存为独立版本，原成片保留。' }}</p></header>
      <n-alert v-if="error" type="error" :show-icon="false">{{ error }}</n-alert>
      <n-alert v-if="requestNotice" type="info" :show-icon="false">{{ requestNotice }}</n-alert>
      <n-alert v-if="incomingRequest" type="info" :show-icon="false">收到新的句子选择，已保留当前未完成的修订范围。
        <n-button size="tiny" :disabled="locked" @click="openRequested(undefined, true)">载入新选择</n-button>
        <n-button size="tiny" quaternary @click="keepDraft">保留当前范围</n-button>
      </n-alert>
      <p v-if="busy" role="status" class="partial-status"><span class="partial-spinner" />{{ context ? '正在核对修订范围，请稍候…' : '正在读取已有成片…' }}</p>
      <template v-if="context">
        <n-alert v-if="!options.length" type="info" :show-icon="false">先生成一个成片版本，再基于它修订。</n-alert>
        <template v-else>
          <label v-if="!embedded" class="baseline-field">基准成片<n-select v-model:value="baseline" :options="options" :disabled="locked" :title="requestBaseline ? '切换基准会清空带入的句子与时间范围，请重新选择' : undefined" aria-label="基准成片" /></label>
          <div class="sentence-tools"><div class="sentence-filter" role="group" aria-label="修订句子筛选"><button v-if="candidates?.length" type="button" :aria-pressed="candidateOnly" :disabled="locked" @click="candidateOnly=true">有编辑记录 <span>{{ candidates.length }}</span></button><button type="button" :aria-pressed="!candidateOnly" :disabled="locked" @click="candidateOnly=false">全部句子 <span>{{ sentenceRows.length }}</span></button></div><n-input v-model:value="query" clearable :disabled="locked" placeholder="搜索句号或字幕文字" :input-props="{'aria-label':'搜索修订句子'}" /></div>
          <p v-if="candidateOnly && !query" class="partial-hint">编辑记录仅供定位，不代表这些修改尚未应用到旧成片。</p>
          <div class="selection-summary"><strong aria-live="polite">已选 {{ lineIds.length }} 句<span v-if="selectedHidden">（{{ selectedHidden }} 句在当前筛选外）</span></strong><n-button size="tiny" text :disabled="locked || !lineIds.length" @click="clearSelection">清空选择</n-button></div>
          <div class="sentence-list" aria-label="修订句子清单">
            <article v-for="row in displayedRows" :key="row.id" class="sentence-row" :data-selected="lineIds.includes(row.id)"><div class="sentence-heading"><n-checkbox :checked="lineIds.includes(row.id)" :disabled="locked" :aria-label="`修订第 ${row.id} 句`" @update:checked="toggleLine(row.id,$event)"><strong>第 {{ row.id }} 句</strong></n-checkbox><span>{{ partialTime(row.start) }}–{{ partialTime(row.end) }}</span><RouterLink :to="`/projects/${encodeURIComponent(uid)}/steps/tts?line=${row.id}`">编辑此句</RouterLink></div><div class="sentence-text"><p v-if="row.sourceText">原文：{{ row.sourceText }}</p><p>{{ row.sourceText ? '译文：' : '' }}{{ row.text || '此句没有字幕文字' }}</p></div></article>
            <p v-if="!filteredRows.length" class="sentence-empty">{{ query ? '没有找到匹配的句子，请尝试其他句号或文字。' : '此筛选下没有句子，可切换到全部句子。' }}</p>
            <div v-if="filteredRows.length>displayedRows.length" class="more-sentences"><small>已显示 {{ displayedRows.length }}/{{ filteredRows.length }} 句，可搜索缩小范围</small><n-button size="tiny" :disabled="locked" @click="visibleLimit+=80">显示更多句子</n-button></div>
          </div>
          <div class="audio-choice"><n-checkbox v-model:checked="replaceAudio" :disabled="locked">同时更新所选范围的声音</n-checkbox><p class="partial-hint">只修订画面时保留原音轨；勾选后采用当前声音，并重新编码完整音轨。</p></div>
          <details class="advanced-options"><summary>补充时间段与版本名称 <span v-if="ranges.length">· {{ ranges.length }} 段</span><span v-if="hasRangeIssue" class="range-warning"> · 时间待完善</span></summary><div class="advanced-content"><label class="name-field">修订版本名称<n-input v-model:value="name" :disabled="locked" maxlength="80" placeholder="留空自动命名" :input-props="{'aria-label':'局部修订版本名称'}" /></label><div class="partial-range-heading"><strong>补充时间范围</strong><n-button size="tiny" :disabled="locked || ranges.length>=200" @click="addRange">添加时间段</n-button></div><p class="partial-hint">按原视频时间填写，统一显示为时:分:秒.毫秒，也可输入秒数或分:秒。按 Enter 或离开输入框确认；重叠范围自动合并，最多 200 段。有历史记录时包含句子新旧位置；旧成片缺少记录时，请补充原字幕所在范围。</p><div v-for="(row, index) in ranges" :key="row.key" class="partial-range"><span>第 {{ index + 1 }} 段</span><n-input v-model:value="row.start" placeholder="开始，如 00:05:25.000" :disabled="locked" :status="rangeIssues[index] ? 'error' : undefined" @blur="commitRangeTime(row,'start')" @keyup.enter="commitRangeTime(row,'start')" :input-props="{'aria-label':`第${index+1}段开始时间`, 'aria-invalid':!!rangeIssues[index], 'aria-describedby':rangeIssues[index] ? `partial-range-${index}-error` : undefined}" /><span>至</span><n-input v-model:value="row.end" placeholder="结束，如 00:06:00.000" :disabled="locked" :status="rangeIssues[index] ? 'error' : undefined" @blur="commitRangeTime(row,'end')" @keyup.enter="commitRangeTime(row,'end')" :input-props="{'aria-label':`第${index+1}段结束时间`, 'aria-invalid':!!rangeIssues[index], 'aria-describedby':rangeIssues[index] ? `partial-range-${index}-error` : undefined}" /><n-button size="tiny" quaternary :disabled="locked" @click="ranges.splice(index,1)">移除</n-button><p v-if="rangeIssues[index]" :id="`partial-range-${index}-error`" class="range-issue" role="status">{{ rangeIssues[index] }}</p></div></div></details>
          <div v-if="plan" class="partial-result" role="status">
            <div><n-tag type="success" size="small">范围已检查</n-tag><strong>{{ partialSummary(plan) }}</strong></div>
            <p>为保证衔接，视频编码范围扩至关键帧。所选时间段内的字幕、评论和画面设置均采用当前内容；范围外保持原样。</p>
            <ul><li v-for="(row, index) in plan.ranges || plan.requested_ranges || []" :key="index">{{ partialTime(row.start) }} – {{ partialTime(row.end) }}</li></ul>
          </div>
          <p v-if="versionLimit" class="limit-notice" role="status">已达到 32 个版本。可继续选择和检查范围；请先移除不再使用的版本定义，再添加修订方案。</p>
          <footer><n-button v-if="!plan" type="primary" :disabled="locked || !canCheck" :loading="busy" @click="check">检查修订范围<span v-if="lineIds.length">（{{ lineIds.length }} 句）</span></n-button><template v-else><n-button type="primary" :disabled="locked || versionLimit" :loading="busy" @click="create">生成片段修订版</n-button><n-button :disabled="locked" @click="plan=null">返回调整</n-button></template><small>{{ plan ? '保存为独立版本并开始生成；基准成片保留。' : '检查只核对范围，不立即生成或投稿。' }}</small></footer>
        </template>
      </template>
      <n-button v-else-if="!busy" size="small" @click="load">重新读取成片</n-button>
    </div>
  </section>
</template>
<style scoped>
.partial-editor{margin-top:16px;min-width:0}.partial-content{margin-top:16px;padding:20px;background:var(--df-surface-2,var(--df-bg));border:1px solid var(--df-border);border-radius:12px;min-width:0}
.embedded{margin-top:0}.embedded .partial-content{margin-top:0;padding:0;border:0;border-radius:0;background:transparent}
.embedded .partial-content header p{max-width:none;margin-bottom:14px}
.partial-content h3{margin:0 0 8px;font-size:15px}.partial-content header p,.partial-hint,.partial-result p,footer small{color:var(--df-muted);font-size:12px;line-height:1.7}.partial-content header p{margin:0 0 20px;max-width:75ch}
.baseline-field,.name-field{display:flex;flex-direction:column;gap:8px;font-size:12px;max-width:420px}.baseline-field{margin-bottom:16px}
.sentence-tools{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.sentence-tools>.n-input{flex:1;min-width:200px}.sentence-filter{display:flex;gap:4px}
.sentence-filter button{display:flex;gap:8px;align-items:center;border:1px solid transparent;border-radius:6px;background:transparent;font:inherit;font-size:12px;padding:7px 10px;color:var(--df-muted);cursor:pointer}
.sentence-filter button[aria-pressed=true]{color:var(--df-text);background:var(--df-accent-soft);border-color:var(--df-border)}.sentence-filter button:not(:disabled):hover{color:var(--df-text);background:var(--df-surface-2,var(--df-bg))}.sentence-filter button:disabled{cursor:default;opacity:.5}
.sentence-filter button:focus-visible,.advanced-options summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}.sentence-filter span{font-variant-numeric:tabular-nums}
.selection-summary{display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:12px;margin:14px 0 8px}.selection-summary strong{font-weight:600}.selection-summary strong>span{font-weight:400;color:var(--df-muted)}
.sentence-list{max-height:360px;overflow:auto;scrollbar-width:thin;scrollbar-color:var(--df-border) transparent;scrollbar-gutter:stable;border-top:1px solid var(--df-border);border-bottom:1px solid var(--df-border)}
.sentence-row{padding:12px;border-bottom:1px solid var(--df-border);font-size:12px}.sentence-row:last-child{border-bottom:0}.sentence-row[data-selected=true]{background:var(--df-accent-soft)}
.sentence-heading{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}.sentence-heading>span{font-size:11px;font-variant-numeric:tabular-nums;color:var(--df-muted)}.sentence-heading>a{margin-left:auto;color:var(--df-accent);font-size:11px;text-underline-offset:3px}
.sentence-text{padding-left:25px;max-width:90ch}.sentence-text p{margin:6px 0 0;line-height:1.7;overflow-wrap:anywhere;white-space:pre-wrap}
.sentence-empty{padding:16px 12px;color:var(--df-muted);font-size:12px;line-height:1.7}.more-sentences{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px}.more-sentences small{color:var(--df-muted)}
.audio-choice{margin-top:18px;font-size:12px}.partial-hint{margin:8px 0 14px}
.advanced-options{margin-top:18px;border-top:1px solid var(--df-border);padding-top:14px;font-size:12px}.advanced-options summary{color:var(--df-muted);cursor:pointer;line-height:1.7}.advanced-options summary:hover{color:var(--df-text)}.advanced-content{padding-top:16px}
.partial-range-heading{display:flex;align-items:center;gap:16px;margin-top:20px;font-size:12px}.partial-range{display:grid;grid-template-columns:48px minmax(90px,1fr) auto minmax(90px,1fr) auto;gap:10px;align-items:center;font-size:12px;margin-bottom:10px}
.range-issue{grid-column:2/-1;margin:0 0 4px;color:var(--df-error,#c66d73);font-size:11px;line-height:1.6}.range-warning{color:var(--df-warning)}
.partial-result{padding-top:16px;margin-top:20px;border-top:1px solid var(--df-border)}.partial-result>div{display:flex;align-items:center;flex-wrap:wrap;gap:12px}.partial-result strong{font-size:13px}.partial-result ul{padding-left:20px;font-size:12px;line-height:1.8;font-variant-numeric:tabular-nums;max-height:160px;overflow:auto}
.partial-content footer{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin-top:20px}.limit-notice{color:var(--df-warning);font-size:12px;line-height:1.7;margin:14px 0}
.partial-status{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--df-muted)}.partial-spinner{width:14px;height:14px;border:2px solid var(--df-border);border-top-color:var(--df-accent);border-radius:50%;animation:partial-spin .8s linear infinite}
.partial-content :deep(input){caret-color:var(--df-accent)}.partial-content ::selection{background:var(--df-accent-soft);color:var(--df-text)}
@keyframes partial-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.partial-spinner{animation:none}}
@media(max-width:600px){.partial-content{padding:14px}.embedded .partial-content{padding:0}.sentence-tools{gap:10px}.sentence-tools>.n-input{flex-basis:100%}.sentence-heading{gap:6px 12px}.sentence-heading>a{margin-left:0}.partial-range{grid-template-columns:1fr auto 1fr auto;gap:6px}.partial-range>span:first-child{grid-column:1/-1}.partial-content footer{align-items:flex-start}.partial-content footer small{width:100%}}
</style>
