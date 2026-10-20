<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCheckbox, NSelect, NTag } from 'naive-ui'
import { createLatestRefresh } from '../utils/preview'
import { commentFailure, commentWorkspaceLink } from '../utils/commentWorkflow'
import { partialSummary, partialTime } from '../utils/assemblyPartial'
import AssemblyPartialEditor from './AssemblyPartialEditor.vue'
import { actualChanges, changeBlockedReason, changeComparisonText, changeRangeLabel, linkedChangeIds, reconcileChangeSelection, toggleChangeSelection, updateAction, visibleChangeGroups, type AssemblyChanges, type AssemblyChangeKind, type ChangeSelection } from '../utils/assemblyChanges'

const props = defineProps<{ uid: string; revision?: string; preferredVersion?: string; disabled?: boolean; versionLimit?: boolean; requestToken?: unknown }>()
const emit = defineEmits<{ created: [version: any]; full: [id: string]; 'busy-change': [value: boolean]; 'baseline-change': [id: string] }>()
const data = ref<AssemblyChanges | null>(null), baselineId = ref<string | null>(null)
const reading = ref(false), working = ref(false), error = ref(''), group = ref<AssemblyChangeKind>('subtitle')
const commentProblem = ref(false)
const operationLabel = ref('')
const plan = ref<any>(null), plannedBody = ref<any>(null)
const selection = ref<ChangeSelection | null>(null)
const mode = ref<'changes' | 'ranges'>('changes'), manualVisited = ref(false), manualBusy = ref(false)
let modeChosen = false
let disposed = false, readNumber = 0, contextNumber = 0, followPreferred = true, controller: AbortController | null = null
const locked = computed(() => !!props.disabled || reading.value || working.value || manualBusy.value || !!error.value || !comparisonReady.value)
const modeLocked = computed(() => !!props.disabled || working.value || manualBusy.value)
const chosenBaseline = computed(() => props.preferredVersion || baselineId.value || data.value?.baseline?.version_id)
// When embedded in the version workbench, the version list is the sole source
// of truth for the comparison target. Keeping a second select here allowed the
// two controls to drift (especially while an unsaved version was locked).
const parentManaged = computed(() => !!props.preferredVersion)
// 草稿版本并不是可更新的基准；父级选择变化时，也不能暂借另一版本的比较结果。
function baselineIsCurrent() {
  return !!data.value?.baseline && (!props.preferredVersion || data.value.baseline.version_id === props.preferredVersion)
}
const comparisonReady = computed(baselineIsCurrent)
const baselineEmptyMessage = computed(() => !data.value?.baselines.length
  ? '尚未生成正式成片；生成后可在这里检查改动或按片段更新。'
  : '当前版本尚未生成正式成片；请在下方选择已生成的版本检查改动。')
const managedBaseline = computed(() => {
  const id = props.preferredVersion || data.value?.baseline?.version_id || ''
  return options.value.find(row => row.value === id)
    || (data.value?.baseline?.version_id === id ? { value: id, label: data.value.baseline.name } : null)
})
const action = computed(() => updateAction(data.value))
const options = computed(() => (data.value?.baselines || []).map(row => ({ label: row.name, value: row.version_id })))
const groups = computed(() => visibleChangeGroups(data.value))
const changes = computed(() => actualChanges(data.value))
const selectedChanges = computed(() => selection.value?.selected || [])
const availableChanges = computed(() => changes.value.filter(row => !changeBlockedReason(row, changes.value)))
const remainingCount = computed(() => changes.value.length - selectedChanges.value.length)
const activeGroup = computed(() => groups.value.find(row => row.kind === group.value))
const candidates = computed(() => data.value?.manual_candidates || [])
const unknownGroups = computed(() => (data.value?.groups || []).filter(row => row.status === 'unknown'))
const allChecked = computed(() => !!availableChanges.value.length && availableChanges.value.every(row => selectedChanges.value.includes(row.id)))
const groupAvailable = computed(() => (activeGroup.value?.changes || []).filter(row => !changeBlockedReason(row, changes.value)))
const groupChecked = computed(() => !!groupAvailable.value.length && groupAvailable.value.every(row => selectedChanges.value.includes(row.id)))
const groupSomeChecked = computed(() => groupAvailable.value.some(row => selectedChanges.value.includes(row.id)))
const linkedLabels = computed(() => (plan.value?.linked_changes || []).map((row: any) => row.label || row.reason).filter(Boolean))
function acceptComparison(value: AssemblyChanges) {
  data.value = value
  selection.value = reconcileChangeSelection(value, selection.value)
  if (!activeGroup.value) group.value = groups.value[0]?.kind || 'subtitle'
  if (!modeChosen) mode.value = value.baseline && value.coverage !== 'known' && !changes.value.length ? 'ranges' : 'changes'
  if (comparisonReady.value && mode.value === 'ranges') manualVisited.value = true
}
function chooseMode(value: 'changes' | 'ranges') {
  if (modeLocked.value || !baselineIsCurrent()) return
  modeChosen = true; mode.value = value; clearPlan()
  if (value === 'ranges') manualVisited.value = true
}
const reader = createLatestRefresh(async signal => {
  const uid = props.uid, version = baselineId.value, token = contextNumber
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/changes${version ? `?version_id=${encodeURIComponent(version)}` : ''}`, {
    cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
  })
  if (response.status === 404) throw new Error('成片改动检查需要重启后端后使用。')
  const value = await response.json()
  if (!response.ok) throw Object.assign(new Error(value.message || '读取成片改动失败，请重试。'), { code: value.code })
  return { uid, version, token, value: value as AssemblyChanges }
}, result => {
  if (disposed || props.uid !== result.uid || result.token !== contextNumber || baselineId.value !== result.version) return
  const preferred = props.preferredVersion
  if (followPreferred && preferred && result.value.baselines.some(row => row.version_id === preferred)
      && result.value.baseline?.version_id !== preferred) {
    baselineId.value = preferred; void refresh(); return
  }
  acceptComparison(result.value); error.value = ''; commentProblem.value = false
}, (e: any) => { error.value = e.name === 'TimeoutError' ? '读取成片改动超时，请重试。' : e.message; commentProblem.value = commentFailure(e) })
async function refresh() {
  if (disposed || working.value || manualBusy.value) return
  const token = ++readNumber; reading.value = true; plan.value = null; error.value = ''; commentProblem.value = false
  await reader.refresh(true)
  if (token === readNumber) reading.value = false
}
function chooseBaseline(id: string, follow = false) {
  if (modeLocked.value || id === baselineId.value && id === data.value?.baseline?.version_id && selection.value) return
  followPreferred = follow
  baselineId.value = id; selection.value = null; clearPlan(); void refresh()
}
function requestedBaseline(id: string) {
  // 跨页带入在子编辑器校验基片身份后再统一父级基准。
  const syncParent = !!props.preferredVersion && id !== props.preferredVersion
  if (!syncParent && id === baselineId.value && id === data.value?.baseline?.version_id && selection.value) return
  followPreferred = false; baselineId.value = id; selection.value = null; clearPlan()
  if (syncParent) emit('baseline-change', id)
  // 基准请求来自正在读取的编辑器；在其原子请求完成后再核对差异。
  if (!manualBusy.value) void refresh()
}
function clearPlan() { plan.value = null; plannedBody.value = null }
function toggleChanges(ids: string[], checked: boolean) {
  if (locked.value || !selection.value) return
  selection.value = { ...selection.value, selected: toggleChangeSelection(changes.value, selectedChanges.value, ids, checked), added: [] }
  clearPlan()
}
function linkedCount(id: string) { return linkedChangeIds(changes.value, [id]).length - 1 }
watch(selectedChanges, clearPlan, { deep: true, flush: 'sync' })
watch([working, manualBusy], ([value, manual]) => {
  emit('busy-change', value || manual)
  const preferred = props.preferredVersion
  if (!value && !manual && baselineId.value && (baselineId.value !== data.value?.baseline?.version_id || !selection.value) && !reading.value) {
    void refresh(); return
  }
  if (!value && !manual && followPreferred && preferred && options.value.some(row => row.value === preferred)
      && preferred !== data.value?.baseline?.version_id) chooseBaseline(preferred, true)
}, { flush: 'sync' })
async function request(path: string, body: any) {
  if (locked.value || !baselineIsCurrent() || disposed) return
  const uid = props.uid, token = contextNumber, preferred = props.preferredVersion, read = new AbortController()
  working.value = true; controller = read; error.value = ''; commentProblem.value = false
  operationLabel.value = path.endsWith('-create') ? '正在保存所选改动并准备生成…' : '正在核对所选改动、关联内容与生成范围…'
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/${path}`, {
      method: 'POST', cache: 'no-store', signal: AbortSignal.any([read.signal, AbortSignal.timeout(120000)]),
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    if (response.status === 404 && path.startsWith('update-')) throw new Error('所选改动更新需要重启后端后使用，请重启后重新检查。')
    const result = await response.json()
    if (!response.ok) throw Object.assign(new Error(result.message || '成片更新检查失败。'), { code: result.code })
    if (!disposed && props.uid === uid && token === contextNumber) {
      if (props.preferredVersion !== preferred) { error.value = '所选成片已变化，请重新检查改动。'; return }
      return result
    }
  } catch (e: any) {
    if (!disposed && uid === props.uid && !read.signal.aborted) {
      error.value = e.name === 'TimeoutError' ? '检查耗时较长，请稍后重试。' : e.message
      commentProblem.value = commentFailure(e)
    }
  } finally { if (controller === read) { working.value = false; controller = null } }
}
async function check() {
  if (locked.value || !baselineIsCurrent() || mode.value !== 'changes' || !data.value?.baseline || !selectedChanges.value.length) return
  const body = { base_version_id: data.value.baseline.version_id, change_ids: [...selectedChanges.value] }
  plan.value = null
  const result = await request('update-plan', body)
  if (result) { plan.value = result; plannedBody.value = body }
}
async function create() {
  if (mode.value !== 'changes' || !plan.value || !plannedBody.value || locked.value || !baselineIsCurrent()) return
  const method = plan.value.method
  const result = await request('update-create', { ...plannedBody.value, fingerprint: plan.value.fingerprint })
  if (result?.version) {
    emit('created', { ...result.version, update_summary: { ...result, method },
      ...(method === 'partial' ? { partial_summary: { ...result, replace_audio: result.replace_audio ?? plannedBody.value.replace_audio } } : {}) })
    plan.value = null
  } else if (error.value) plan.value = null
}
function full() {
  if (!props.disabled && !error.value && !locked.value && baselineIsCurrent() && data.value?.baseline && (data.value.coverage !== 'known' || !changes.value.length && action.value === 'full'))
    emit('full', data.value.baseline.version_id)
}
function focusRefresh() { if (typeof document === 'undefined' || !document.hidden) void refresh() }
watch(() => props.revision, () => { void refresh() })
watch(() => props.preferredVersion, id => {
  followPreferred = true; selection.value = null; clearPlan()
  if (id && options.value.some(row => row.value === id)) chooseBaseline(id, true)
}, { flush: 'sync' })
watch(() => props.uid, () => {
  contextNumber++; reader.cancel(); controller?.abort(); controller = null; working.value = false
  followPreferred = true; modeChosen = false; mode.value = 'changes'; manualVisited.value = false; manualBusy.value = false
  selection.value = null; data.value = null; baselineId.value = null; clearPlan(); error.value = ''; void refresh()
})
watch(() => props.requestToken, token => {
  if (token) { modeChosen = true; mode.value = 'ranges'; manualVisited.value = true; clearPlan() }
}, { immediate: true })
onMounted(() => { void refresh(); window.addEventListener('focus', focusRefresh) })
onBeforeUnmount(() => { disposed = true; reader.cancel(); controller?.abort(); emit('busy-change', false); window.removeEventListener('focus', focusRefresh) })
</script>
<template>
  <section class="assembly-changes" aria-label="检查与更新成片" :aria-busy="reading || working || manualBusy">
    <header class="revision-heading"><div><h3>检查与更新成片</h3><p>{{ parentManaged ? '先在下方成片版本清单点击版本名称，选择本次唯一基准，再决定要更新的内容。' : '选择基准成片，再决定本次要更新的内容。' }}</p></div><n-button size="small" :loading="reading" :disabled="modeLocked || reading" @click="refresh">重新检查改动</n-button></header>
    <div v-if="comparisonReady" class="baseline-row">
      <label>{{ parentManaged ? '当前基准' : '基准成片' }}</label>
      <n-select v-if="!parentManaged" :value="chosenBaseline" :options="options" :disabled="modeLocked || reading" aria-label="检查改动的成片版本" @update:value="chooseBaseline" />
      <span v-else class="baseline-summary" role="status" aria-label="当前基准成片"><n-tag type="info" size="small">{{ managedBaseline?.label || '当前成片版本' }}</n-tag><small>由下方版本清单控制</small></span>
      <span>更新成功前，已有成片保持原样。</span>
    </div>
    <div v-if="comparisonReady" class="revision-modes" role="group" aria-label="成片更新方式">
      <button type="button" :aria-pressed="mode === 'changes'" :disabled="modeLocked" @click="chooseMode('changes')">按改动更新</button>
      <button type="button" :aria-pressed="mode === 'ranges'" :disabled="modeLocked" @click="chooseMode('ranges')">按片段重制</button>
    </div>
    <n-alert v-if="error" title="暂时无法检查成片改动" type="error" :show-icon="false">{{ error }}<RouterLink v-if="commentProblem" class="comment-recovery" :to="commentWorkspaceLink(uid)">前往上屏评论检查</RouterLink></n-alert>
    <p v-if="working" class="checking" role="status">{{ operationLabel }}</p>
    <p v-if="reading" class="checking" role="status">正在核对已保存的制作内容…</p>
    <div v-show="mode === 'changes'" class="changes-mode">
      <template v-if="!reading && comparisonReady && data?.baseline && !error">
        <p class="mode-description">只应用勾选的差异；未勾选内容沿用基准成片，继续显示待更新。</p>
        <n-alert v-if="data.coverage !== 'known'" type="info" :show-icon="false" class="coverage-notice"><strong>旧成片的制作记录不完整</strong><p>这里只能选择已确认的差异。{{ unknownGroups.length ? `待核对：${unknownGroups.map(item => item.label).join('、')}。` : '' }}指定句子或时间范围，请切换到“按片段重制”。</p></n-alert>
        <div v-if="changes.length" class="selection-bar">
          <n-checkbox :checked="allChecked" :indeterminate="!!selectedChanges.length && !allChecked" :disabled="locked || !availableChanges.length" @update:checked="toggleChanges(availableChanges.map(item => item.id), $event)">全选改动</n-checkbox>
          <p aria-live="polite"><strong>本次应用 {{ selectedChanges.length }} 项</strong><span> · 保留 {{ remainingCount }} 项待更新</span></p>
          <n-button text size="small" :disabled="locked || !selectedChanges.length" @click="toggleChanges(selectedChanges, false)">清空选择</n-button>
        </div>
        <p v-if="selection?.added.length" class="selection-notice" role="status">发现 {{ selection.added.length }} 项新增改动，尚未勾选。</p>
        <div v-if="groups.length" class="change-tabs" role="group" aria-label="成片改动类型"><button v-for="item in groups" :key="item.kind" type="button" :aria-pressed="item.kind === group" :disabled="locked" @click="group = item.kind">{{ item.label }}<span>{{ item.changes.filter(row => selectedChanges.includes(row.id)).length }}/{{ item.changes.length }}</span></button></div>
        <div v-if="activeGroup" class="group-selection"><n-checkbox :checked="groupChecked" :indeterminate="groupSomeChecked && !groupChecked" :disabled="locked || !groupAvailable.length" @update:checked="toggleChanges(groupAvailable.map(item => item.id), $event)">选择本组{{ activeGroup.label }}改动</n-checkbox><small>关联的时间或呈现改动会一同选择。</small></div>
        <div v-if="activeGroup" class="change-list" :aria-label="`${activeGroup.label}改动清单`">
          <article v-for="item in activeGroup.changes" :key="item.id" class="change-row" :data-selected="selectedChanges.includes(item.id)" :data-blocked="!!changeBlockedReason(item, changes)">
            <div class="change-row-heading"><n-checkbox :checked="selectedChanges.includes(item.id)" :disabled="locked || !!changeBlockedReason(item, changes)" :aria-label="`应用${item.line_id ? `第 ${item.line_id} 句${activeGroup.label}` : item.reason}改动`" @update:checked="toggleChanges([item.id], $event)"><strong>{{ item.line_id ? `第 ${item.line_id} 句` : item.reason }}</strong></n-checkbox><span v-if="changeRangeLabel(item)">{{ changeRangeLabel(item) }}</span><RouterLink v-if="item.line_id" :to="`/projects/${encodeURIComponent(uid)}/steps/tts?line=${item.line_id}`">编辑此句</RouterLink></div>
            <div class="change-row-content"><p v-if="item.line_id" class="change-reason">{{ item.reason }}</p><p v-if="changeBlockedReason(item, changes)" class="blocked-reason"><n-tag type="warning" size="small">需先处理</n-tag>{{ changeBlockedReason(item, changes) }}</p><p v-else-if="linkedCount(item.id)" class="linked-reason">与另外 {{ linkedCount(item.id) }} 项关联改动共同应用或保留。</p><div v-if="changeComparisonText(activeGroup.kind, item, 'before') || changeComparisonText(activeGroup.kind, item, 'current')" class="change-texts"><div><small>成片中 · 取消勾选时保留</small><p>{{ changeComparisonText(activeGroup.kind, item, 'before') || '原来没有此内容' }}</p></div><div><small>当前内容 · 勾选后应用</small><p>{{ changeComparisonText(activeGroup.kind, item, 'current') || '当前已移除' }}</p></div></div></div>
          </article>
        </div>
        <p v-if="!changes.length && data.coverage === 'known'" class="empty-group">{{ data.coverage === 'known' && action === 'unchanged' ? '当前内容已应用，无需更新。' : '暂无可自动比较的差异，可切换到“按片段重制”指定范围。' }}</p>
        <div v-if="changes.length || plan" class="revision-actions">
          <template v-if="!plan"><n-button type="primary" :disabled="locked || !selectedChanges.length" :loading="working" @click="check">检查所选改动（{{ selectedChanges.length }} 项）</n-button><span>检查后确认生成，不会立即修改成片。</span></template>
          <div v-else class="checked-plan" role="status">
            <div class="plan-heading"><strong>本次应用 {{ plan.selection_count ?? selectedChanges.length }} 项，保留 {{ plan.remaining_count ?? remainingCount }} 项待更新</strong><n-tag :type="plan.method === 'full' ? 'warning' : 'success'" size="small">{{ plan.method === 'full' ? '完整生成画面' : '局部替换' }}</n-tag></div>
            <p>{{ plan.method === 'full' ? '所选改动需要完整生成；未勾选内容仍使用基准成片的版本。' : partialSummary(plan) }}</p>
            <ul v-if="linkedLabels.length" class="update-reasons"><li v-for="label in linkedLabels" :key="label">关联应用：{{ label }}</li></ul><ul v-if="plan.warnings?.length" class="update-reasons"><li v-for="warning in plan.warnings" :key="warning">{{ warning }}</li></ul>
            <div v-if="plan.ranges?.length" class="plan-ranges"><span v-for="(range,index) in plan.ranges" :key="index">{{ partialTime(range.start) }}–{{ partialTime(range.end) }}</span></div>
            <div class="plan-actions"><n-button type="primary" :loading="working" :disabled="locked" @click="create">应用所选改动并生成</n-button><n-button :disabled="locked" @click="clearPlan">返回调整</n-button></div><small>更新同一个成片版本；生成失败时保留原文件。</small>
          </div>
        </div>
      </template>
    </div>
    <div v-show="mode === 'ranges'" class="ranges-mode">
      <AssemblyPartialEditor v-if="manualVisited && (comparisonReady || !!requestToken)" :key="uid" :uid="uid" embedded :preferred-version="chosenBaseline" :request-token="requestToken" :candidates="candidates" :version-limit="versionLimit" :disabled="!!disabled || reading || working" @baseline-change="requestedBaseline" @busy-change="manualBusy=$event" @created="emit('created',$event)" />
    </div>
    <details v-if="!reading && !error && comparisonReady && data?.baseline && (data.coverage !== 'known' || !changes.length && action === 'full')" class="full-update-alternative"><summary>整片重制与建立对照</summary><div><n-button size="small" :disabled="locked" @click="full">{{ data.coverage === 'known' ? '重新生成缺失的成片…' : '完整更新并建立对照…' }}</n-button><p>采用全部当前制作内容重新生成，可供之后准确比较差异。</p></div></details>
    <p v-if="!reading && !error && data && !comparisonReady && !requestToken" class="empty-group" role="status">{{ baselineEmptyMessage }}</p>
  </section>
</template>
<style scoped>
.assembly-changes{padding:24px 0;margin-top:16px;border-block:1px solid var(--df-border);min-width:0}
.revision-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}
.revision-heading h3{font-size:17px;margin:0}
.revision-heading p,.mode-description{font-size:13px;line-height:1.7;color:var(--df-muted);margin:6px 0 18px}
.baseline-row{display:flex;align-items:center;flex-wrap:wrap;gap:10px 16px;font-size:12px;margin:4px 0 20px}
.baseline-row>label{font-weight:600}.baseline-row :deep(.n-select){flex:0 1 360px;min-width:180px}.baseline-row>span{color:var(--df-muted)}
.baseline-summary{display:inline-flex;align-items:center;gap:8px;min-width:220px}.baseline-summary small{font-size:11px;color:var(--df-muted)}
.revision-modes{display:flex;gap:24px;border-bottom:1px solid var(--df-border);margin-bottom:18px}
.revision-modes button{font:inherit;color:var(--df-muted);font-size:14px;background:transparent;border:0;border-bottom:2px solid transparent;padding:10px 2px 12px;cursor:pointer}
.revision-modes button[aria-pressed=true]{color:var(--df-text);border-bottom-color:var(--df-accent);font-weight:600}
.revision-modes button:disabled{opacity:.5;cursor:not-allowed}
.revision-modes button:focus-visible,.change-tabs button:focus-visible,.full-update-alternative summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}
.coverage-notice{margin-bottom:18px}.coverage-notice p{margin:5px 0 0;line-height:1.7}
.comment-recovery{display:block;margin-top:8px;font-size:12px}
.selection-bar,.group-selection{display:flex;align-items:center;flex-wrap:wrap;gap:12px 20px;font-size:12px}
.selection-bar{padding:12px 0}.selection-bar p{margin:0;flex:1;font-variant-numeric:tabular-nums}.selection-bar p>span,.group-selection small{color:var(--df-muted)}
.group-selection{padding:4px 0 12px}.group-selection small{font-size:11px}
.change-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 16px}
.change-tabs button{font:inherit;font-size:12px;color:var(--df-muted);background:transparent;border:1px solid var(--df-border);border-radius:6px;padding:6px 10px;display:flex;gap:8px;cursor:pointer}
.change-tabs button[aria-pressed=true]{background:var(--df-accent-soft);border-color:var(--df-accent);color:var(--df-text)}
.change-list{max-height:440px;overflow:auto;scrollbar-width:thin;scrollbar-gutter:stable}
.change-row{padding:16px 0;border-top:1px solid var(--df-border);font-size:12px}.change-row:first-child{border-top:0}
.change-row-heading{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.change-row-heading>span{font-variant-numeric:tabular-nums;color:var(--df-muted);font-size:11px}.change-row-heading>a{margin-left:auto}
.change-row-content{padding-left:26px}.change-reason,.linked-reason{color:var(--df-muted);line-height:1.7;margin:6px 0}
.change-texts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:10px}.change-texts>div{min-width:0;padding:12px;background:var(--df-surface-2,var(--df-bg));border-radius:6px}.change-texts small{color:var(--df-muted);font-size:11px}.change-texts p{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;margin:6px 0 0}
.change-row[data-selected=true] .change-texts>div:last-child{background:var(--df-accent-soft)}
.blocked-reason{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;color:var(--df-warning);line-height:1.7}.change-row[data-blocked=true]{color:var(--df-muted)}
.assembly-changes a{color:var(--df-accent);text-underline-offset:3px}
.selection-notice,.checking,.empty-group{font-size:12px;line-height:1.7;color:var(--df-muted);margin:12px 0}.selection-notice{color:var(--df-warning)}
.revision-actions{border-top:1px solid var(--df-border);padding-top:18px;margin-top:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.revision-actions>span,.checked-plan p,.checked-plan>small{color:var(--df-muted);font-size:12px;line-height:1.7}.checked-plan{width:100%}.plan-heading,.plan-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.plan-heading{font-size:13px}.plan-actions{margin-top:16px}.checked-plan>small{display:block;margin-top:10px}
.update-reasons{font-size:12px;color:var(--df-muted);line-height:1.8;padding-left:18px}.plan-ranges{display:flex;flex-wrap:wrap;gap:8px 16px;max-height:120px;overflow:auto;font-size:12px;font-variant-numeric:tabular-nums}
.full-update-alternative{margin-top:24px;padding-top:16px;border-top:1px solid var(--df-border);font-size:12px}.full-update-alternative summary{cursor:pointer;color:var(--df-muted);width:fit-content}.full-update-alternative>div{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin-top:12px}.full-update-alternative p{margin:0;line-height:1.7;color:var(--df-muted)}
@media(max-width:650px){.revision-heading{flex-wrap:wrap;gap:12px}.baseline-row :deep(.n-select){flex:1 1 180px}.baseline-row>span{width:100%}.change-texts{grid-template-columns:1fr;gap:8px}.change-row-content{padding-left:0}.revision-actions{align-items:flex-start;flex-direction:column}.revision-modes{gap:20px}}
</style>
