<script setup lang="ts">
// 制作入口统一只读预检；执行范围取已展示的步骤，和恢复历史计划明确区分。
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NSpin, NTag } from 'naive-ui'
import { STEP_ORDER, STEP_LABELS, STATUS_TEXT } from '../stores/nav'
import { parseChain } from '../utils/chainStatus'
import { WORKFLOW_BUSY } from '../utils/workflow'

type Target = { uid: string; title: string }
type Item = Target & {
  detail: any; plan: any; steps: string[]; blockers: string[]; notes: string[]
  selected: boolean; allFresh: boolean; submitted: boolean; result: string
  resultKind: '' | 'started' | 'skipped' | 'unknown' | 'error'
}
type IssueGroup = {
  id: string; step: string; label: string; state: string; reason: string
  configBlocked: boolean; count: number; names: string[]
}
const emit = defineEmits<{ started: [] }>()
const show = ref(false), checking = ref(false), busy = ref(false)
const items = ref<Item[]>([]), includeDependencies = ref(false), scopeName = ref('')
const error = ref(''), checkedCount = ref(0), needsRecheck = ref(false)
const submittedCount = ref(0)
const targets = ref<Target[]>([])
const eligible = (item: Item) => !!item.plan && !item.blockers.length && !item.allFresh && !item.submitted
const ready = computed(() => items.value.filter(item => item.selected && eligible(item)))
const canConfirm = computed(() => show.value && !busy.value && !checking.value && !needsRecheck.value
  && inspectedScope === activeScope && !!inspectedScope && ready.value.length > 0)
const confirmLabel = computed(() => busy.value && !checking.value
  ? `正在提交 ${submittedCount.value} 个项目`
  : ready.value.length ? `确认运行 ${ready.value.length} 个项目` : '确认运行')
let disposed = false, generation = 0, activeScope = '', inspectedScope = ''
let readAbort: AbortController | undefined, inspection: Promise<void> | undefined
let resettingScope = false

function requestError(failure: any, fallback = '请求失败，请重新检查。'): string {
  const message = String(failure?.message || '')
  if (failure?.status && message) return message
  if (failure?.name === 'TimeoutError' || /timed?\s*out|timeout/i.test(message)) {
    return '请求超时，请查看后端服务状态后重新检查。'
  }
  if (/failed to fetch|fetch failed|networkerror|network.*(?:error|failed|down)|load failed|econn/i.test(message)) {
    return '无法连接后端服务，请检查服务状态和网络连接。'
  }
  return message || fallback
}
// 配音的逐句文件通常共享同一原因，汇总展示避免几百项淹没运行范围。
function issueGroups(values: any[]): IssueGroup[] {
  const groups = new Map<string, IssueGroup>()
  for (const value of values || []) {
    const step = typeof value?.step === 'string' ? value.step : ''
    const label = value?.label || STEP_LABELS[step] || step
    const state = value?.state || ''
    const reason = typeof value === 'string' ? value : value?.reason || value?.message || '依赖检查未通过'
    const configBlocked = !!value?.config_blocked
    const id = JSON.stringify([step, label, state, reason, configBlocked])
    let group = groups.get(id)
    if (!group) {
      group = { id, step, label, state, reason, configBlocked, count: 0, names: [] }
      groups.set(id, group)
    }
    group.count++
    if (value?.name && group.names.length < 3 && !group.names.includes(value.name)) group.names.push(value.name)
  }
  return [...groups.values()]
}
function issueSummary(group: IssueGroup): string {
  const state = ({ stale: '需更新', missing: '缺失', deleted: '已删除' } as Record<string, string>)[group.state] || '待处理'
  return [group.label, group.count > 1 ? `${group.count}项${state}` : group.names[0], group.reason].filter(Boolean).join('：')
}
const stepLabel = (item: Item, key: string) => item.plan?.labels?.[key]
  || item.detail?.steps?.find((step: any) => step.key === key)?.label || STEP_LABELS[key] || key
const stepReference = (item: Item, key: string) => {
  const step = item.detail?.steps?.find((value: any) => value.key === key)
  if (!step) return '检查时尚未取得步骤状态'
  return step.fresh === true ? '检查时已有最新结果'
    : step.fresh === false ? step.freshness_label || '检查时需生成或更新'
      : STATUS_TEXT[step.status] || '检查时状态待核对'
}
function select(item: Item, selected: boolean) {
  if (!busy.value && !needsRecheck.value && eligible(item)) item.selected = selected
}
async function api(path: string, body?: any, signal?: AbortSignal) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000),
  })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.message || `请求失败（HTTP ${response.status}）`),
    { code: data.code, status: response.status })
  return data
}
async function inspect() {
  if (disposed || !show.value || !targets.value.length || (busy.value && !checking.value)) return
  const ticket = ++generation, signature = activeScope, dependencies = includeDependencies.value
  const batch = targets.value.map(target => ({ ...target }))
  readAbort?.abort(); const controller = new AbortController(); readAbort = controller
  checking.value = true; busy.value = true; error.value = ''; items.value = []; checkedCount.value = 0
  submittedCount.value = 0
  needsRecheck.value = false; inspectedScope = ''
  try {
    // 逐项目读取，单项错误不丢弃其余项目的检查结果。
    for (const target of batch) {
      if (disposed || ticket !== generation) return
      const item: Item = { ...target, detail: null, plan: null, steps: [], blockers: [], notes: [],
        selected: false, allFresh: false, submitted: false, result: '', resultKind: '' }
      try {
        const detail = await api(`/api/projects/${encodeURIComponent(target.uid)}`, undefined, controller.signal)
        if (disposed || ticket !== generation) return
        item.detail = detail
        item.title = detail.title || target.title
        const chosen = new Set((detail.steps || []).filter((step: any) => step.selected).map((step: any) => step.key))
        item.steps = STEP_ORDER.filter(key => chosen.has(key))
        const chain = parseChain(detail.chain ?? detail.params?._chain)
        const workflowStatus = typeof detail.workflow_status === 'string'
          ? detail.workflow_status : detail.workflow_status?.status
        if (detail.dir_exists === false) item.blockers.push('项目目录缺失，请先在项目总览核对文件位置。')
        if (detail.running_step || (detail.steps || []).some((step: any) => ['running', 'queued'].includes(step.status))
            || chain?.state === 'active' || WORKFLOW_BUSY.includes(workflowStatus)) {
          item.blockers.push('项目正在运行、排队或停止中，请先查看当前进度。')
        }
        if (chain?.state === 'held') item.notes.push('本次使用总览勾选的新制作计划，会替换保留的制作续跑计划；需要从断点继续请使用检查恢复方案。')
        if (['paused', 'failed', 'attention'].includes(workflowStatus)) item.notes.push('项目有未完成的自动流程；本次仅运行制作步骤。如需继续原流程，请在总览检查恢复方案。')
        if (!item.steps.length) item.blockers.push('未勾选制作步骤，请在项目总览选择要运行的步骤。')
        if (!item.blockers.length) {
          const plan = await api(`/api/projects/${encodeURIComponent(target.uid)}/run_plan`,
            { steps: [...item.steps], include_dependencies: dependencies }, controller.signal)
          if (disposed || ticket !== generation) return
          item.plan = JSON.parse(JSON.stringify(plan))
          item.blockers.push(...issueGroups(plan.blockers || []).map(issueSummary))
          if (!Array.isArray(plan.steps) || !plan.steps.length
              || plan.steps.some((key: any) => !STEP_ORDER.some(step => step === key))) {
            item.blockers.push('制作计划缺少有效步骤，请重新检查或查看项目总览。')
          }
          const states = new Map((detail.steps || []).map((step: any) => [step.key, step]))
          const needsInputs = (plan.requirements || []).some((input: any) => ['missing', 'deleted', 'stale'].includes(input.state))
          item.allFresh = !item.blockers.length && !(plan.added || []).length && !needsInputs
            && plan.steps.every((key: string) => (states.get(key) as any)?.fresh === true)
          item.selected = eligible(item)
        }
      } catch (failure: any) {
        if (disposed || ticket !== generation || controller.signal.aborted) return
        item.blockers = [requestError(failure, '读取项目制作计划失败，请重新检查。')]
      }
      if (disposed || ticket !== generation) return
      items.value.push(item); checkedCount.value++
    }
    if (!disposed && ticket === generation) inspectedScope = signature
  } finally {
    if (!disposed && ticket === generation) { checking.value = false; busy.value = false }
  }
}
async function open(input: Target[], scopeLabel = '') {
  if (disposed || (busy.value && !checking.value)) return
  const next = [...new Map(input.filter(target => target?.uid).map(target =>
    [target.uid, { uid: target.uid, title: target.title || target.uid }])).values()]
  if (!next.length) return
  const signature = JSON.stringify(next.map(target => target.uid).sort())
  if (show.value && signature === activeScope) { if (checking.value) await inspection; return }
  generation++; readAbort?.abort()
  targets.value = next; activeScope = signature
  scopeName.value = scopeLabel || (next.length === 1 ? next[0].title : `本次 ${next.length} 个项目`)
  resettingScope = true; includeDependencies.value = false; resettingScope = false
  show.value = true; busy.value = false; checking.value = false
  inspection = inspect(); await inspection
}
function close() {
  if (busy.value && !checking.value) return
  generation++; readAbort?.abort(); inspectedScope = ''
  checking.value = false; busy.value = false; show.value = false
}
watch(includeDependencies, () => {
  if (!resettingScope && show.value && !(busy.value && !checking.value)) inspection = inspect()
}, { flush: 'sync' })

async function confirmStart() {
  if (!canConfirm.value) return
  const selected = ready.value.map(item => ({ item, steps: [...item.plan.steps] }))
  const body = { uids: selected.map(({ item }) => item.uid),
    steps_by_uid: Object.fromEntries(selected.map(({ item, steps }) => [item.uid, steps])) }
  busy.value = true; needsRecheck.value = true; error.value = ''
  submittedCount.value = selected.length
  for (const { item } of selected) { item.submitted = true; item.selected = false; item.result = '正在交给后台…' }
  let accepted = 0, refreshNeeded = false
  try {
    const receipt = await api('/api/projects/run_many', body)
    if (disposed) return
    refreshNeeded = Number(receipt.started) > 0
    const results = new Map((Array.isArray(receipt.results) ? receipt.results : [])
      .map((result: any) => [result.uid, result]))
    for (const { item } of selected) {
      const result: any = results.get(item.uid)
      if (result?.skipped) {
        item.resultKind = 'skipped'; item.result = `未启动：${result.skipped}`
      } else if (result?.started && /全部.*最新|跳过/.test(String(result.started))) {
        item.resultKind = 'skipped'; item.result = String(result.started)
      } else if (result?.started) {
        item.resultKind = 'started'; item.result = `已交给后台，从「${result.started}」开始。`; accepted++
      } else if (result?.error) {
        item.resultKind = 'error'; item.result = `未启动：${result.error}`
      } else {
        item.resultKind = 'unknown'; item.result = '未返回此项目的启动结果，请先查看项目状态，再重新检查。'
        refreshNeeded = true
      }
    }
  } catch (failure: any) {
    if (disposed) return
    refreshNeeded = true
    error.value = `启动结果未能确认，该请求可能已被后台处理。请先查看项目状态，再重新检查。${requestError(failure)}`
    for (const { item } of selected) { item.resultKind = 'unknown'; item.result = '启动结果未确认，请查看项目状态；不会自动重复提交。' }
  } finally {
    if (!disposed) busy.value = false
  }
  // 回执与界面刷新分开：父页面可异步读取状态，不阻塞按钮和抽屉。
  if ((accepted || refreshNeeded) && !disposed) emit('started')
}
onUnmounted(() => { disposed = true; generation++; readAbort?.abort() })
defineExpose({ open, close, inspect, confirmStart, show, checking, busy, items,
  includeDependencies, ready, canConfirm })
</script>

<template>
  <n-drawer :show="show" width="min(800px,100vw)" :mask-closable="!busy || checking"
            :close-on-esc="!busy || checking" @update:show="value => { if (!value) close() }">
    <n-drawer-content title="运行制作步骤" :closable="!busy || checking">
      <div class="run-form" data-testid="project-step-run" :aria-busy="checking">
        <div class="run-intro"><strong>{{ scopeName }}</strong>
          <p>按项目总览中已勾选的制作步骤执行，使用各步骤已保存的参数，已有最新结果会复用。</p>
          <p class="scope-limit">本次终点是制作，不上传、不投稿。</p>
        </div>
        <n-checkbox v-model:checked="includeDependencies" :disabled="busy && !checking">
          补齐缺失或需更新的依赖
        </n-checkbox>
        <p class="muted dependency-hint">更改此选项会重新检查，并列出补入的步骤；检查不会启动任务。</p>
        <n-alert v-if="error" type="warning">{{ error }}</n-alert>
        <n-alert v-else-if="needsRecheck" type="info">本次请求已处理。再次运行前，请先查看状态并重新检查。</n-alert>
        <n-spin v-if="checking" size="small" :description="`正在检查 ${checkedCount}/${targets.length} 个项目…`" />
        <article v-for="item in items" :key="item.uid" class="project-plan" :data-project="item.uid">
          <div class="plan-heading">
            <n-checkbox :checked="item.selected" :disabled="busy || needsRecheck || !eligible(item)"
                        :aria-label="`选择运行「${item.title}」`" @update:checked="value => select(item, value)">
              {{ item.title }}
            </n-checkbox>
            <RouterLink :to="`/projects/${item.uid}`">查看项目总览 →</RouterLink>
          </div>
          <n-alert v-for="note in item.notes" :key="note" type="warning">{{ note }}
            <RouterLink :to="`/projects/${item.uid}#automatic-workflow`">查看恢复方案 →</RouterLink>
          </n-alert>
          <p v-if="item.allFresh" class="fresh-result">检查时所选步骤已是最新，无需运行。</p>
          <div v-if="item.plan" class="execution-plan">
            <p class="plan-caption">本次执行顺序 <small>步骤状态仅作检查时参考</small></p>
            <ol aria-label="本次制作步骤">
              <li v-for="key in item.plan.steps" :key="key" :data-step="key">
                <div><RouterLink :to="`/projects/${item.uid}/steps/${key}`">{{ stepLabel(item, key) }}</RouterLink>
                  <n-tag v-if="item.plan.added?.includes(key)" size="small" type="warning">补入依赖</n-tag>
                </div><small>{{ stepReference(item, key) }}</small>
              </li>
            </ol>
            <ul v-if="item.plan.requirements?.length" class="requirements" aria-label="制作依赖">
              <li v-for="group in issueGroups(item.plan.requirements)" :key="group.id" :title="group.names.join('、')">
                <RouterLink v-if="group.step" :to="`/projects/${item.uid}/steps/${group.step}`">{{ issueSummary(group) }}</RouterLink>
                <span v-else>{{ issueSummary(group) }}</span>
              </li>
            </ul>
            <ul v-if="item.plan.effects?.length" class="effects" aria-label="制作范围说明">
              <li v-for="effect in item.plan.effects" :key="effect">{{ effect }}</li>
            </ul>
          </div>
          <n-alert v-if="item.blockers.length" type="warning">
            <ul class="blockers"><li v-for="blocker in item.blockers" :key="blocker">{{ blocker }}</li></ul>
          </n-alert>
          <p v-if="item.result" role="status" class="run-result" :class="`result-${item.resultKind}`">{{ item.result }}
            <RouterLink :to="`/projects/${item.uid}`">查看后台进度 →</RouterLink>
          </p>
        </article>
      </div>
      <template #footer>
        <div class="run-actions">
          <n-button :disabled="busy && !checking" @click="close">{{ checking ? '取消检查' : '关闭' }}</n-button>
          <n-button :disabled="busy" @click="inspect">重新检查</n-button>
          <n-button type="primary" :loading="busy && !checking" :disabled="!canConfirm" @click="confirmStart">{{ confirmLabel }}</n-button>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.run-form { display:grid; gap:16px; padding:18px 0; }
.run-intro { display:grid; gap:8px; }.run-intro p,.project-plan p { margin:0; font-size:12px; line-height:1.7; }
.scope-limit { color:var(--df-muted); }.muted { color:var(--df-muted); font-size:12px; }
.dependency-hint { margin:-8px 0 0; }.project-plan { display:grid; gap:12px; padding:18px 0; border-top:1px solid var(--df-border); }
.plan-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; }.plan-heading>a { flex:none; font-size:12px; }
.project-plan a { color:var(--df-accent); }.project-plan .n-alert a { display:inline-block; margin-top:6px; font-size:12px; }
.execution-plan { display:grid; gap:10px; }.plan-caption { color:var(--df-text); font-weight:600; }.plan-caption small { display:block; font-weight:400; color:var(--df-muted); }
ol { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 28px; margin:0; padding-left:22px; }ol li { font-size:12px; padding-left:4px; }ol li>div { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }ol small { display:block; margin-top:4px; color:var(--df-muted); }
.requirements,.effects,.blockers { margin:0; padding-left:18px; font-size:12px; line-height:1.7; }.requirements,.effects { color:var(--df-muted); }
.fresh-result,.result-started { color:var(--df-success,#6ea58a); }.result-skipped,.result-unknown,.result-error { color:var(--df-warning,#dfb66f); }
.run-result a { display:inline-block; margin-left:6px; }.run-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:12px; }
@media(max-width:560px) { .plan-heading { align-items:flex-start; flex-direction:column; }ol { grid-template-columns:1fr; } }
</style>
