<script setup lang="ts">
// 批次持久化及分组调度由后端负责，页面关闭后不影响后台执行。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NSpin, NTag } from 'naive-ui'
import { friendlyError } from '../utils/http'
import { workflowModeLabel } from '../utils/workflow'
import { publicationVisibility } from '../utils/publishing'
import { createProjectRunSync, type ProjectRunFeedback, type ProjectRunFeedbackStage } from '../utils/projectRunFeedback'
import { chronologicalProjectItems, moveProjectBatchItem, projectBatchControls, projectBatchDelivery,
  projectBatchGroupTitle, projectBatchNeedsDateCheck, projectBatchOrderedGroups, projectBatchProgress, projectBatchPublishCount,
  projectBatchStage, projectBatchTargets, projectBatchGroupCounts, projectBatchGroupFailure, projectBatchGroupPhase,
  projectBatchItemPhase, projectBatchIssues, projectBatchFooter, projectBatchParentError, projectBatchErrorSummary, PROJECT_BATCH_ACTIVE,
  type ProjectBatchItem, type ProjectBatchGroup, type ProjectBatchTarget } from '../utils/projectBatch'
import ReleaseProjectPlan from './ReleaseProjectPlan.vue'
import ReleaseCurrentProgress from './ReleaseCurrentProgress.vue'
import PublicationRightsHint from './PublicationRightsHint.vue'
import RecoveryPlan from './RecoveryPlan.vue'
import WorkflowUpdateSummary from './WorkflowUpdateSummary.vue'
import DubbingRunOptions from './DubbingRunOptions.vue'
import PublicationTargetSummary from './PublicationTargetSummary.vue'

const props = withDefaults(defineProps<{ projects?: ProjectBatchTarget[]; hiddenTrigger?: boolean; disabled?: boolean }>(),
  { projects: () => [], hiddenTrigger: false, disabled: false })
const emit = defineEmits<{ changed: []; started: []; runFeedback: [feedback: ProjectRunFeedback]; batchProgress: [batch: any] }>()
const route = useRoute()
const batches = ref<any[]>([]), listLoading = ref(false), listError = ref(''), showCompleted = ref(false)
const show = ref(false), loading = ref(false), readingDates = ref(false), writing = ref(false), error = ref('')
const items = ref<ProjectBatchItem[]>([]), preview = ref<any>(null), existing = ref<any>(null)
const selectedGroupIds = ref<string[]>([]), orderConfirmed = ref(false), rightsConfirmed = ref(false)
const controlling = ref<string[]>([])
const pendingCreation = ref<{ requestId: string; body: any; state?: 'creating' | 'unknown' } | null>(null)
const runFeedback = ref<ProjectRunFeedback | null>(null)
let disposed = false, generation = 0, readController: AbortController | undefined
let listController: AbortController | undefined, listing: Promise<void> | undefined, listRevision = 0
let timer: ReturnType<typeof setInterval> | undefined, startRequest: { key: string; id: string } | null = null, positionedHash = ''
let mounted = false
const resumeSync = createProjectRunSync(async () => {
  await load(true)
  return !runFeedback.value?.batchId || !['accepted', 'unknown'].includes(runFeedback.value.stage)
}, () => !disposed && document.visibilityState === 'visible')
const visibleBatches = computed(() => showCompleted.value ? batches.value : batches.value.filter(batch => !['done', 'completed'].includes(batch.status)))
const completedCount = computed(() => batches.value.filter(batch => ['done', 'completed'].includes(batch.status)).length)
const presentedBatches = computed(() => visibleBatches.value.map(batch => {
  const presented = { ...batch, groups: (batch.groups || []).map((group: ProjectBatchGroup) => {
    const feedback = pendingGroupFeedback(batch, group)
    if (!feedback) return group
    return { ...group, status: 'queued', phase: feedback.message, error: '', feedback: 'preparing', current_progress: null,
      items: (group.items || []).map(item => ['done', 'completed'].includes(item.status || '') ? item
        : { ...item, status: 'queued', phase: feedback.message, error: '', feedback: 'preparing' }) }
  }) }
  const displayedGroups = presented.groups.map((group: ProjectBatchGroup) => ({ group,
    failure: projectBatchGroupFailure(group), phase: projectBatchGroupPhase(group) }))
  const pendingCount = presented.groups.filter((group: ProjectBatchGroup) => pendingGroupFeedback(batch, group)).length
  const counts = projectBatchGroupCounts({ ...presented, groups: presented.groups.filter((group: ProjectBatchGroup) => !pendingGroupFeedback(batch, group)) })
  if (pendingCount) counts.unshift({ key: 'checking', label: `${pendingCount} 组正在核对或提交`, count: pendingCount })
  return { batch, counts, issues: projectBatchIssues(presented), groups: displayedGroups,
    parentError: projectBatchParentError(batch),
    footer: pendingBatchFeedback(batch) ? '正在处理本次继续请求，请等待后台回执。' : projectBatchFooter(batch) }
}))
const managedReleaseIds = computed<string[]>(() => [...new Set<string>(batches.value.flatMap(batch => (batch.groups || [])
  .filter((group: ProjectBatchGroup) => group.kind === 'collection').map((group: ProjectBatchGroup) => group.child_id || group.child?.id).filter(Boolean)))])
const groups = computed<ProjectBatchGroup[]>(() => {
  const selected: string[] | null = Array.isArray(preview.value?.group_ids) ? preview.value.group_ids
    : selectedGroupIds.value.length ? selectedGroupIds.value : null
  return (preview.value?.groups || []).filter((group: ProjectBatchGroup) => selected === null || selected.includes(group.id))
})
const orderedGroups = computed(() => projectBatchOrderedGroups(groups.value))
const publishCount = computed(() => projectBatchPublishCount(groups.value))
const locked = computed(() => loading.value || writing.value || !!existing.value || !!pendingCreation.value)
const blockers = computed<string[]>(() => [...new Set<string>([...(preview.value?.blockers || []),
  ...groups.value.flatMap(group => [...(group.blockers || []), ...(group.items || []).flatMap(item => item.blockers || [])])])])
const warnings = computed<string[]>(() => [...new Set<string>(preview.value?.warnings || [])])
const canStart = computed(() => !props.disabled && !loading.value && !writing.value && pendingCreation.value?.state !== 'creating' && !!preview.value?.fingerprint
  && !blockers.value.length && groups.value.length > 0 && (!orderedGroups.value.length || orderConfirmed.value)
  && (!publishCount.value || rightsConfirmed.value))
const confirmLabel = computed(() => {
  if (writing.value) return runFeedback.value?.stage === 'submitting' ? '正在提交请求…' : '正在核对执行方案…'
  const count = groups.value.reduce((total, group) => total + (group.items || []).length, 0)
  const action = existing.value && existing.value.status !== 'ready' ? '确认继续' : '确认运行'
  return `${action} ${count} 个项目${publishCount.value ? `，投稿 ${publishCount.value} 个` : '（不投稿）'}`
})
const controlKey = (batch: any, groupId = '') => `${batch.id}:${groupId}`
const controlBusy = (batch: any, groupId = '') => controlling.value.includes(controlKey(batch, groupId))

async function api(path: string, body?: any, signal?: AbortSignal) {
  const response = await fetch(`/api/project-batches${path}`, { cache: 'no-store',
    ...(body !== undefined ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000) })
  let data: any
  try { data = await response.json() }
  catch { throw new Error('无法读取服务返回的回执。后台任务可能仍在继续，请恢复连接后核对状态。') }
  if (!response.ok) throw Object.assign(new Error(data.message || '无法读取批量任务，请重试。'), { status: response.status, code: data.code })
  return data
}
function upsert(batch: any) {
  if (!batch?.id) return
  batches.value = [batch, ...batches.value.filter(row => row.id !== batch.id)]
}
function resetConfirmation() { orderConfirmed.value = false; rightsConfirmed.value = false }
function setRunFeedback(stage: ProjectRunFeedbackStage, message: string, scope?: Omit<ProjectRunFeedback, 'stage' | 'message'>) {
  const value = scope || runFeedback.value
  if (!value) return
  runFeedback.value = { ...value, stage, message }
  emit('runFeedback', { ...runFeedback.value, groupIds: [...value.groupIds], projectUids: [...value.projectUids] })
}
function pendingGroupFeedback(batch: any, group: ProjectBatchGroup) {
  const feedback = runFeedback.value
  return feedback && feedback.batchId === batch.id && feedback.groupIds.includes(group.id)
    && ['checking', 'submitting'].includes(feedback.stage) ? feedback : null
}
function pendingBatchFeedback(batch: any) {
  return (batch.groups || []).some((group: ProjectBatchGroup) => pendingGroupFeedback(batch, group))
}
function groupFeedbackComplete(group: ProjectBatchGroup) {
  const terminal = ['attention', 'failed', 'paused', 'stopped', 'done', 'completed'].includes(group.status || '')
  return terminal || (PROJECT_BATCH_ACTIVE.includes(group.status || '') && group.feedback !== 'preparing'
    && !(group.items || []).some(item => item.feedback === 'preparing') && !!group.phase
    && !/启动已确认|等待启动|准备继续|准备执行|正在核对/.test(group.phase))
}
function feedbackComplete(batch: any, feedback: ProjectRunFeedback) {
  if (!batch) return false
  const selected = (batch.groups || []).filter((group: ProjectBatchGroup) => feedback.groupIds.includes(group.id))
  const complete = feedback.stage !== 'unknown' && selected.length === feedback.groupIds.length
    && selected.every((group: ProjectBatchGroup) => groupFeedbackComplete(group))
  if (complete) setRunFeedback('clear', '')
  return complete
}
function groupRunFeedback(batch: any, group: ProjectBatchGroup): string {
  const feedback = runFeedback.value
  if (feedback && feedback.batchId === batch.id && feedback.groupIds.includes(group.id) && ['checking', 'submitting', 'accepted', 'unknown'].includes(feedback.stage)) {
    if (feedback.stage === 'accepted' && groupFeedbackComplete(group)) return ''
    return feedback.message
  }
  return group.feedback === 'preparing' ? group.phase || '正在核对已有结果与执行条件…' : ''
}
function requestScope() { return selectedGroupIds.value.length ? { group_ids: [...selectedGroupIds.value] } : {} }
function acceptPlan(plan: any) {
  preview.value = plan
  const enriched = new Map<string, ProjectBatchItem>((plan.groups || []).flatMap((group: ProjectBatchGroup) => group.items || []).map((item: ProjectBatchItem) => [item.project_uid, item]))
  if (!items.value.length) items.value = [...enriched.values()]
  else items.value = items.value.map(item => enriched.get(item.project_uid) || item)
}
async function readPlan(signal: AbortSignal) {
  return existing.value ? api(`/${encodeURIComponent(existing.value.id)}/plan`, requestScope(), signal)
    : api('/plan', { project_uids: items.value.map(item => item.project_uid), order_confirmed: true }, signal)
}
async function readCandidates(uids: string[], signal: AbortSignal, ticket: number) {
  readingDates.value = true
  try {
    try { return await api('/candidates', { project_uids: uids }, signal) }
    catch (failure) {
      if (signal.aborted || disposed || ticket !== generation) throw failure
      // 日期补齐失败时仍可检查已有资料，合集内的缺失日期会在顺序旁提醒。
      try { return await api(`/candidates?uids=${encodeURIComponent(uids.join(','))}`, undefined, signal) }
      catch (cachedFailure) {
        if (signal.aborted || disposed || ticket !== generation) throw cachedFailure
        return null
      }
    }
  } finally { if (!disposed && ticket === generation) readingDates.value = false }
}
async function recoverCreation(signal: AbortSignal, ticket: number) {
  if (!pendingCreation.value) return 'none'
  const data = await api(`?request_id=${encodeURIComponent(pendingCreation.value.requestId)}`, undefined, signal)
  if (disposed || ticket !== generation || signal.aborted) return 'cancelled'
  const batch = (data.batches || [])[0]
  return batch ? acceptCreation(batch) : 'unknown'
}
function acceptCreation(batch: any) {
  if (batch.status === 'creating') {
    if (pendingCreation.value) pendingCreation.value.state = 'creating'
    error.value = '批次正在后台检查并保存，请稍后重新检查找回结果；当前项目和顺序保持锁定。'
    return 'waiting'
  }
  if (batch.status === 'creation_failed') {
    pendingCreation.value = null; existing.value = null; preview.value = null; resetConfirmation()
    error.value = `上次批次未创建：${batch.creation_error?.message || batch.error || '创建检查未通过'}。请重新检查后确认。`
    return 'failed'
  }
  existing.value = batch; pendingCreation.value = null; upsert(batch)
  return 'ready'
}
async function open(input: (ProjectBatchTarget | string)[] = props.projects) {
  if (disposed || props.disabled || writing.value) return
  show.value = true
  if (pendingCreation.value) { await inspect(); return }
  const targets = projectBatchTargets(input)
  if (!targets.length) return
  const ticket = ++generation
  readController?.abort(); const controller = new AbortController(); readController = controller
  loading.value = true; error.value = ''; items.value = []; preview.value = null; existing.value = null
  selectedGroupIds.value = []; resetConfirmation()
  try {
    const data = await readCandidates(targets.map(target => target.uid), controller.signal, ticket)
    if (disposed || ticket !== generation) return
    items.value = chronologicalProjectItems(data?.items || targets.map(target => ({ project_uid: target.uid, title: target.title })))
    const plan = await readPlan(controller.signal)
    if (!disposed && ticket === generation) acceptPlan(plan)
  } catch (failure) {
    if (!disposed && ticket === generation && !controller.signal.aborted) error.value = friendlyError(failure, '项目检查失败，请重新检查。')
  } finally { if (!disposed && ticket === generation) loading.value = false }
}
function close() {
  if (writing.value) return
  generation++; readController?.abort(); show.value = false; loading.value = false; readingDates.value = false
}
async function inspect(refreshDates = true) {
  if (disposed || writing.value || !show.value) return
  const ticket = ++generation
  readController?.abort(); const controller = new AbortController(); readController = controller
  const refreshCandidates = refreshDates && !existing.value && !pendingCreation.value && items.value.length > 0
  loading.value = true; readingDates.value = false; error.value = ''; resetConfirmation()
  try {
    const recovered = await recoverCreation(controller.signal, ticket)
    if (disposed || ticket !== generation) return
    if (recovered === 'waiting') return
    if (refreshCandidates) {
      const data = await readCandidates(items.value.map(item => item.project_uid), controller.signal, ticket)
      if (disposed || ticket !== generation) return
      const refreshed = new Map<string, ProjectBatchItem>((data?.items || []).map((item: ProjectBatchItem) => [item.project_uid, item]))
      // 重新检查只补资料，不能覆盖用户已调整的组内顺序。
      items.value = items.value.map(item => refreshed.get(item.project_uid) || item)
    }
    const plan = await readPlan(controller.signal)
    if (!disposed && ticket === generation) acceptPlan(plan)
  } catch (failure) {
    if (!disposed && ticket === generation && !controller.signal.aborted) {
      preview.value = null; error.value = friendlyError(failure, '方案检查失败，请重新检查。')
    }
  } finally { if (!disposed && ticket === generation) loading.value = false }
}
async function move(group: ProjectBatchGroup, index: number, offset: number) {
  if (locked.value || group.kind !== 'collection') return
  const next = moveProjectBatchItem(items.value, group, index, offset)
  if (next === items.value) return
  items.value = next; preview.value = null; resetConfirmation(); await inspect(false)
}
async function openBatch(batch: any, groupIds: string[] = []) {
  if (disposed || props.disabled || writing.value || pendingCreation.value) return
  const ticket = ++generation
  readController?.abort(); const controller = new AbortController(); readController = controller
  show.value = true; loading.value = true; readingDates.value = false; error.value = ''; preview.value = null; items.value = []
  existing.value = batch; selectedGroupIds.value = [...groupIds]; resetConfirmation()
  try {
    const detail = await api(`/${encodeURIComponent(batch.id)}`, undefined, controller.signal)
    if (disposed || ticket !== generation) return
    existing.value = detail
    const plan = await readPlan(controller.signal)
    if (!disposed && ticket === generation) acceptPlan(plan)
  } catch (failure) {
    if (!disposed && ticket === generation && !controller.signal.aborted) error.value = friendlyError(failure, '批次读取失败，请重新检查。')
  } finally { if (!disposed && ticket === generation) loading.value = false }
}
async function start() {
  if (!canStart.value) return
  writing.value = true; error.value = ''; listRevision++
  const confirmedPlan = preview.value, ticket = generation
  const confirmedGroups = groups.value
  const scope = { id: crypto.randomUUID(), batchId: existing.value?.id,
    groupIds: confirmedGroups.map(group => group.id),
    projectUids: [...new Set(confirmedGroups.flatMap(group => group.items || [])
      .filter(item => !['done', 'completed'].includes(item.status || '')).map(item => item.project_uid))] }
  let submitted = false
  setRunFeedback('checking', existing.value ? '正在核对已有结果与执行条件…' : '正在保存并核对本次执行方案…', scope)
  try {
    if (!existing.value) {
      if (!pendingCreation.value) {
        const requestId = crypto.randomUUID()
        pendingCreation.value = { requestId, body: { project_uids: items.value.map(item => item.project_uid),
          order_confirmed: true, request_id: requestId, confirmed: true, fingerprint: confirmedPlan.fingerprint } }
      }
      const created = await api('', pendingCreation.value.body)
      if (disposed || ticket !== generation) return
      if (acceptCreation(created) !== 'ready') {
        setRunFeedback(created.status === 'creating' ? 'unknown' : 'blocked', error.value)
        return
      }
      scope.batchId = created.id
      setRunFeedback('checking', '正在核对已有结果与执行条件…', scope)
    }
    const checked = await api(`/${encodeURIComponent(existing.value.id)}/plan`, requestScope())
    if (disposed || ticket !== generation) return
    acceptPlan(checked)
    if (checked.fingerprint !== confirmedPlan.fingerprint || blockers.value.length) {
      resetConfirmation(); error.value = '启动前核对发现方案已变化，请查看当前方案并重新确认。'
      setRunFeedback('blocked', error.value); return
    }
    const key = JSON.stringify([existing.value.id, selectedGroupIds.value, checked.fingerprint])
    if (!startRequest || startRequest.key !== key) startRequest = { key, id: crypto.randomUUID() }
    setRunFeedback('submitting', '正在提交请求…')
    submitted = true
    const result = await api(`/${encodeURIComponent(existing.value.id)}/start`, { ...requestScope(), request_id: startRequest.id,
      confirmed: true, rights_confirmed: rightsConfirmed.value, fingerprint: checked.fingerprint })
    if (disposed || ticket !== generation) return
    existing.value = result; upsert(result); show.value = false; preview.value = null
    // 确定受理后该请求已消费；后续同方案再次恢复必须发起新请求。
    startRequest = null
    setRunFeedback('accepted', '请求已接收，正在准备执行…')
    // 启动回执本身就是最新轻量状态，不再多等一轮 GET 才更新项目卡片。
    emit('batchProgress', result)
    if (mounted) resumeSync.start()
    emit('started'); emit('changed')
  } catch (failure) {
    if (!disposed && ticket === generation) {
      error.value = friendlyError(failure, '启动回执未确认，请核对或重试本批次。')
      const status = (failure as { status?: number })?.status || 0
      const definitive = status >= 400 && status < 500
      if (definitive) startRequest = null
      setRunFeedback(!definitive && (submitted || pendingCreation.value) ? 'unknown' : 'blocked',
        !definitive && (submitted || pendingCreation.value) ? '请求回执未确认，请核对原批次状态。' : error.value)
      if (mounted && runFeedback.value?.stage === 'unknown' && runFeedback.value.batchId) resumeSync.start()
    }
  } finally {
    writing.value = false
    if (!disposed) { listRevision++; void load(true) }
  }
}
async function control(batch: any, action: 'pause' | 'stop', groupId = '') {
  const key = controlKey(batch, groupId)
  if (disposed || controlBusy(batch, groupId)) return
  controlling.value = [...controlling.value, key]; listError.value = ''; listRevision++
  try {
    const result = await api(`/${encodeURIComponent(batch.id)}/${action}`, groupId ? { group_id: groupId } : {})
    if (!disposed) { upsert(result); emit('changed') }
  } catch (failure) { if (!disposed) listError.value = friendlyError(failure, '操作未确认，请重新读取批次状态。') }
  finally {
    controlling.value = controlling.value.filter(value => value !== key)
    if (!disposed) { listRevision++; void load(true) }
  }
}
function load(quiet = false): Promise<void> {
  if (disposed) return Promise.resolve()
  if (!quiet) listLoading.value = true
  if (listing) return listing
  const revision = listRevision, controller = new AbortController(); listController = controller
  let again = false
  listing = (async () => {
    try {
      const result = await api('', undefined, controller.signal)
      if (disposed) return
      if (revision !== listRevision) { again = true; return }
      const previous = new Map(batches.value.map(batch => [batch.id, batch]))
      batches.value = (result.batches || []).map((batch: any) => {
        const current = previous.get(batch.id)
        return current?.updated_at && batch.updated_at && current.updated_at > batch.updated_at ? current : batch
      }); listError.value = ''
      const feedback = runFeedback.value
      if (feedback?.batchId && ['accepted', 'unknown'].includes(feedback.stage)) {
        const batch = batches.value.find(row => row.id === feedback.batchId)
        if (batch) emit('batchProgress', batch)
        if (feedbackComplete(batch, feedback)) resumeSync.stop()
      }
      await positionHash()
    } catch (failure) {
      if (!disposed && !controller.signal.aborted && revision === listRevision) listError.value = friendlyError(failure, '批次状态读取失败，请重试。')
    } finally {
      listing = undefined
      if (!disposed) { listLoading.value = false; if (again) void load(true) }
    }
  })()
  return listing
}
async function positionHash() {
  if (!route.hash.startsWith('#project-batch-') || route.hash === positionedHash || typeof document === 'undefined') return
  showCompleted.value = true; await nextTick()
  const element = document.getElementById(route.hash.slice(1))
  if (element) { element.scrollIntoView({ block: 'center' }); positionedHash = route.hash }
}
watch(() => route.hash, () => { positionedHash = ''; void positionHash() })
watch(() => props.disabled, disabled => { if (disabled && !writing.value) close() })
onMounted(() => {
  mounted = true
  void load(true)
  timer = setInterval(() => {
    if (document.visibilityState === 'visible' && batches.value.some(batch => PROJECT_BATCH_ACTIVE.includes(batch.status)
      || (batch.groups || []).some((group: ProjectBatchGroup) => PROJECT_BATCH_ACTIVE.includes(group.status || '')))) void load(true)
  }, 4000)
})
onUnmounted(() => { disposed = true; generation++; resumeSync.stop(); readController?.abort(); listController?.abort(); if (timer) clearInterval(timer) })
defineExpose({ open, openBatch, close, load, managedReleaseIds })
</script>

<template>
  <n-button v-if="!hiddenTrigger" size="small" type="primary" :disabled="disabled || !projects.length" @click="open()">批量运行…</n-button>
  <section v-if="batches.length || listError" class="project-batches" aria-label="批量任务" data-testid="project-batches">
    <header class="batch-heading"><div><h3>批量任务</h3><p>合集组内保序，其他项目独立推进。关闭页面后仍由后台继续。</p></div><n-button size="tiny" :loading="listLoading" @click="load()">刷新状态</n-button></header>
    <n-alert v-if="listError" type="warning">{{ listError }}</n-alert>
    <article v-for="{batch,counts,issues,groups:displayedGroups,parentError,footer} in presentedBatches" :id="`project-batch-${batch.id}`" :key="batch.id" class="batch-record">
      <header class="batch-record-heading"><div class="batch-record-title"><strong>{{ batch.title || '批量运行' }}</strong><small>{{ projectBatchProgress(batch) }}<span v-if="batch.created_at"> · {{ batch.created_at.slice(0,16).replace('T',' ') }}</span></small></div><n-tag size="small" :bordered="false" :type="pendingBatchFeedback(batch) ? 'info' : projectBatchStage(batch.status).tone">{{ pendingBatchFeedback(batch) ? '正在检查继续' : projectBatchStage(batch.status).label }}</n-tag></header>
      <ul v-if="counts.length" class="batch-counts" aria-label="分组状态"><li v-for="count in counts" :key="count.key" :class="{'warning':count.key==='attention'}">{{ count.label }}</li></ul>
      <p v-else-if="batch.phase" class="batch-phase">{{ batch.phase }}</p>
      <aside v-if="issues.length" class="batch-issues" aria-label="上次模型请求中断">
        <strong>上次模型请求中断</strong>
        <ul><li v-for="issue in issues" :key="issue.key">{{ issue.summary }}<span v-if="issue.groupIds.length < displayedGroups.length" class="issue-scope">（记录来自 {{ issue.groupIds.length }} 个组）</span></li></ul>
        <RouterLink :to="{path:'/settings',query:{section:'models'}}">查看模型设置 →</RouterLink>
      </aside>
      <div v-if="parentError" class="batch-parent-issue"><p class="warning">{{ projectBatchErrorSummary(parentError) }}</p><details class="failure-details"><summary>查看批次错误详情</summary><pre>{{ parentError }}</pre></details></div>
      <div v-for="{group,failure,phase} in displayedGroups" :key="group.id" class="group-progress" :data-group="group.id">
        <div class="group-progress-title"><strong>{{ projectBatchGroupTitle(group) }}</strong><n-tag size="small" :bordered="false" :type="projectBatchStage(group.status).tone">{{ pendingGroupFeedback(batch,group) ? (runFeedback?.stage==='submitting' ? '正在提交' : '核对中') : group.feedback==='preparing' ? '准备执行' : projectBatchStage(group.status).label }}</n-tag></div>
        <p v-if="groupRunFeedback(batch,group)" class="run-feedback" role="status" aria-live="polite">{{ groupRunFeedback(batch,group) }}</p>
        <p v-if="failure || !groupRunFeedback(batch,group)" :class="failure?'interrupted-phase':'muted'">{{ phase }}<span v-if="!failure"> · {{ group.kind==='collection' ? '合集内按确认顺序' : '独立执行' }}</span></p>
        <p v-if="failure?.reason" class="warning">{{ failure.reason }}</p>
        <details v-if="failure?.technical" class="failure-details"><summary>查看上次中断详情</summary><pre>{{ failure.technical }}</pre></details>
        <ReleaseCurrentProgress v-if="group.current_progress" :progress="group.current_progress" :total="group.items?.length || 1" />
        <ol class="progress-items" :class="{independent:group.kind==='independent'}"><li v-for="item in group.items || []" :key="item.project_uid"><RouterLink :to="`/projects/${item.project_uid}`">{{ item.title }}</RouterLink><span>{{ projectBatchItemPhase(item,group) }}</span></li></ol>
        <div v-if="!pendingGroupFeedback(batch,group)" class="group-actions">
          <n-button v-if="projectBatchControls(group.status).resume" size="tiny" :disabled="controlBusy(batch,group.id) || writing" @click="openBatch(batch,[group.id])">检查并继续本组…</n-button>
          <n-button v-if="projectBatchControls(group.status).pause" size="tiny" :loading="controlBusy(batch,group.id)" @click="control(batch,'pause',group.id)">暂停本组</n-button>
          <n-button v-if="projectBatchControls(group.status).stop" size="tiny" :disabled="controlBusy(batch,group.id)" @click="control(batch,'stop',group.id)">停止本组</n-button>
        </div>
      </div>
      <footer class="batch-record-actions"><span class="muted">{{ footer }}</span><n-button v-if="projectBatchControls(batch.status).resume" size="small" :disabled="writing" @click="openBatch(batch)">检查待继续的组…</n-button><n-button v-if="projectBatchControls(batch.status).pause" size="small" :loading="controlBusy(batch)" @click="control(batch,'pause')">暂停全部</n-button><n-button v-if="projectBatchControls(batch.status).stop" size="small" :disabled="controlBusy(batch)" @click="control(batch,'stop')">停止全部</n-button></footer>
    </article>
    <n-button v-if="completedCount" text size="small" @click="showCompleted=!showCompleted">{{ showCompleted?'收起已完成任务':`查看 ${completedCount} 个已完成任务` }}</n-button>
  </section>
  <n-drawer :show="show" width="min(860px,100vw)" :mask-closable="!writing" :close-on-esc="!writing" @update:show="value => { if (!value) close() }">
    <n-drawer-content :title="existing ? '继续批量任务' : '批量运行检查'" :closable="!writing">
      <div class="batch-inspection" data-testid="project-batch-inspection">
        <p class="inspection-intro">{{ existing ? '沿用本批次已确认的终点、分P与发布目标，从断点继续。' : '读取各项目已保存的成片与交付设置。同一账号、合集和小节组成保序组，其他项目独立推进。' }}</p>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <div v-if="writing" class="inspection-loading run-feedback" role="status" aria-live="polite"><n-spin size="small" /><span>{{ runFeedback?.message }}</span></div>
        <n-alert v-if="pendingCreation" type="warning">{{ pendingCreation.state==='creating' ? '批次仍在后台检查并保存，请稍后重新检查找回结果。' : '批次创建回执尚未确认。请重新检查找回原批次，或重试同一方案。' }}项目与顺序已锁定，避免重复创建。</n-alert>
        <div v-if="loading" class="inspection-loading" role="status" aria-live="polite"><n-spin size="small" /><span>{{ readingDates ? '正在补齐原视频发布日期…' : '正在读取项目设置并检查执行方案…' }}</span></div>
        <p v-if="preview" class="inspection-summary">{{ groups.reduce((total,group)=>total+group.items.length,0) }} 个项目 · {{ groups.filter(group=>group.kind==='collection').length }} 个合集组 · {{ groups.filter(group=>group.kind==='independent').length }} 个独立项目</p>
        <ul v-if="blockers.length" class="plan-messages warning" aria-label="需要处理"><li v-for="message in blockers" :key="message">{{ message }}</li></ul>
        <ul v-if="warnings.length" class="plan-messages muted" aria-label="检查提醒"><li v-for="message in warnings" :key="message">{{ message }}</li></ul>
        <section v-for="group in groups" :key="group.id" class="inspection-group" :data-group="group.id">
          <header><h3>{{ projectBatchGroupTitle(group) }}</h3><span>{{ group.kind==='collection' ? '组内按顺序提交并核验合集排列' : '独立执行，不等待其他组' }}</span></header>
          <p v-if="projectBatchNeedsDateCheck(group)" class="warning">部分原视频发布日期仍无法获取，请核对下方顺序。</p>
          <div v-for="(item,index) in group.items" :key="item.project_uid" class="inspection-item" :data-project="item.project_uid">
            <div class="inspection-item-heading"><span v-if="group.kind==='collection'" class="order-position">{{ index+1 }}</span><div class="inspection-item-title"><strong>{{ item.title }}</strong><small>{{ item.source_date ? `原视频：${item.source_date}` : '发布日期未获取' }}</small></div><n-tag size="small" :bordered="false">{{ workflowModeLabel(projectBatchDelivery(item),'按已保存目标') }}</n-tag></div>
            <div class="inspection-item-meta"><span v-if="item.recipe?.mode==='update' || item.plan?.recipe?.mode==='update'">更新原BV · {{ item.recipe?.update?.bvid || item.plan?.recipe?.update?.bvid || '沿用已保存原稿' }}</span><span v-else-if="item.recipe?.version_ids?.length">{{ item.recipe.version_ids.length }} 个分P · {{ item.recipe.version_ids.map((id:string)=>item.recipe.part_titles?.[id] || item.plan?.version_specs?.find((version:any)=>version.id===id)?.name || id).join('、') }}</span><span v-else>按项目保存的成片目标执行</span></div>
            <p v-if="projectBatchDelivery(item)!=='produce' && item.target?.kind==='new'" class="muted">{{ item.target_selection?.source==='first_publish' ? '首次发布 · 默认创建新稿' : '已明确选择创建新稿' }}</p>
            <PublicationTargetSummary v-if="group.kind==='independent' && projectBatchDelivery(item)!=='produce' && item.plan?.settings" :plan="{...item.plan,account_name:item.account_name || group.account_name}" />
            <p v-if="projectBatchDelivery(item)!=='produce' && item.plan?.settings" class="muted">{{ publicationVisibility(item.plan.settings.visibility) }}</p>
            <p v-if="item.blockers?.length" class="warning">{{ item.blockers.join('；') }}</p>
            <div class="inspection-item-actions"><RouterLink :to="`/projects/${item.project_uid}`">查看项目设置 →</RouterLink><div v-if="!existing && group.kind==='collection' && group.items.length>1" class="order-actions"><n-button size="tiny" :aria-label="`上移 ${item.title}`" :disabled="locked || index===0" @click="move(group,index,-1)">上移</n-button><n-button size="tiny" :aria-label="`下移 ${item.title}`" :disabled="locked || index===group.items.length-1" @click="move(group,index,1)">下移</n-button></div></div>
            <details v-if="item.plan || item.production" class="item-details"><summary>查看步骤与分P详情</summary>
              <ReleaseProjectPlan v-if="group.kind==='collection'" :item="item" :index="index" />
              <div v-else class="independent-plan">
                <WorkflowUpdateSummary v-if="item.plan?.update_plan" :plan="item.plan.update_plan" :replace-videos="item.recipe?.update?.replace_videos!==false" :sync-fields="item.recipe?.update?.sync_fields" :delivery-mode="item.recipe?.delivery_mode" readonly />
                <p v-for="version in item.plan?.version_specs || []" :key="version.id" class="muted">{{ version.name }}</p>
                <DubbingRunOptions v-if="item.plan?.dubbing" :uid="item.project_uid" :summary="item.plan.dubbing" readonly />
                <RecoveryPlan v-if="item.plan?.recovery?.rows?.length" :recovery="item.plan.recovery" :pending-steps="item.plan.pending_production_steps" />
              </div>
            </details>
          </div>
        </section>
        <p v-if="preview && !groups.length" class="muted">没有可运行的项目。请回到项目设置保存成片与交付目标后重新检查。</p>
      </div>
      <template #footer>
        <div class="inspection-confirmation">
          <n-checkbox v-if="orderedGroups.length" v-model:checked="orderConfirmed" :disabled="writing || loading">已核对上方合集组内顺序（各项目的分P顺序沿用发布设置）</n-checkbox>
          <n-checkbox v-if="publishCount" v-model:checked="rightsConfirmed" :disabled="writing || loading">确认素材可用于发布，并允许按上述目标自动投稿 {{ publishCount }} 个项目</n-checkbox>
          <PublicationRightsHint v-if="publishCount" />
          <div class="inspection-footer-actions"><n-button :disabled="writing" @click="close">{{ loading?'取消读取':'关闭' }}</n-button><n-button :disabled="writing || loading" :loading="loading" @click="inspect()">重新检查</n-button><n-button type="primary" :disabled="!canStart" :loading="writing" @click="start">{{ confirmLabel }}</n-button></div>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.run-feedback{color:var(--df-accent);font-size:12px;line-height:1.8}
.batch-counts{display:flex;flex-wrap:wrap;gap:5px 20px;list-style:none;padding:0;margin:10px 0 0;font-size:12px;line-height:1.8;color:var(--df-muted);font-variant-numeric:tabular-nums}.batch-issues{margin-top:16px;padding:12px 0;border-top:1px solid var(--df-border);font-size:12px;line-height:1.8}.batch-issues>strong{font-size:12px;color:var(--df-warning,#dfb66f);font-weight:600}.batch-issues ul{margin:5px 0 7px;padding-left:18px;color:var(--df-text)}.batch-issues a{color:var(--df-accent)}.issue-scope{color:var(--df-muted)}.interrupted-phase{color:var(--df-warning,#dfb66f)}.failure-details{font-size:12px;line-height:1.8;margin-top:7px}.failure-details>summary{cursor:pointer;color:var(--df-muted);width:fit-content}.failure-details>summary:hover{color:var(--df-accent)}.failure-details pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:var(--df-muted);max-height:240px;overflow:auto;margin:8px 0 0;padding:10px 12px;background:var(--df-bg);border-radius:6px}.failure-details>summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}.batch-parent-issue{margin-top:12px}
.project-batches{display:grid;gap:14px;margin:20px 0}.batch-heading,.batch-record-heading,.group-progress-title,.batch-record-actions,.inspection-item-heading,.inspection-item-actions{display:flex;align-items:center;gap:12px}.batch-heading>div,.batch-record-title,.inspection-item-title{flex:1;min-width:0}.batch-heading h3,.inspection-group h3{font-size:14px;margin:0;font-weight:600}.batch-heading p,.batch-phase,.group-progress p,.batch-record small,.batch-inspection p,.inspection-group header>span{font-size:12px;line-height:1.8;margin:5px 0 0}.batch-heading p,.muted,.batch-record small,.inspection-item small,.inspection-group header>span,.inspection-item-meta{color:var(--df-muted)}.batch-record{border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel);padding:18px;scroll-margin:28px}.batch-record-heading strong{font-size:14px}.batch-record small,.inspection-item small{display:block;overflow-wrap:anywhere}.batch-record-heading strong,.group-progress-title strong,.inspection-item strong{overflow-wrap:anywhere}.group-progress{padding:14px 0;border-top:1px solid var(--df-border);margin-top:14px}.group-progress-title{justify-content:space-between}.group-progress-title strong{font-size:13px}.progress-items{margin:9px 0 10px;padding-left:22px;font-size:12px;line-height:1.9}.progress-items.independent{list-style:none;padding-left:0}.progress-items li{padding-left:2px}.progress-items a{color:var(--df-accent);overflow-wrap:anywhere}.progress-items span{color:var(--df-muted);margin-left:12px;overflow-wrap:anywhere}.group-actions,.order-actions{display:flex;gap:8px;flex-wrap:wrap}.batch-record-actions{border-top:1px solid var(--df-border);padding-top:13px;flex-wrap:wrap;justify-content:flex-end}.batch-record-actions>span{font-size:11px;margin-right:auto;line-height:1.8}.warning{color:var(--df-warning,#dfb66f);font-size:12px;line-height:1.8;overflow-wrap:anywhere}.batch-inspection{padding:18px 0 24px}.inspection-intro{margin:0 0 16px!important;color:var(--df-muted)}.batch-inspection>.n-alert{margin-bottom:14px}.inspection-loading{display:flex;gap:12px;align-items:center;font-size:12px;line-height:1.8;padding:18px 0}.inspection-summary{font-weight:600}.plan-messages{font-size:12px;line-height:1.9;padding-left:20px;margin:12px 0}.inspection-group{margin-top:24px}.inspection-group>header{padding-bottom:10px;border-bottom:1px solid var(--df-border)}.inspection-group header>span{display:block;margin-top:3px}.inspection-item{padding:16px 0;border-bottom:1px solid var(--df-border)}.inspection-item-heading{align-items:flex-start}.inspection-item-heading strong{font-size:13px;line-height:1.8}.inspection-item small{font-size:11px;line-height:1.8}.order-position{flex:none;width:22px;font-variant-numeric:tabular-nums;color:var(--df-muted);font-size:12px;line-height:1.9}.inspection-item-meta{font-size:12px;line-height:1.9;margin-top:7px;overflow-wrap:anywhere}.inspection-item-actions{justify-content:space-between;flex-wrap:wrap;margin-top:9px}.inspection-item-actions>a,.item-details>summary{font-size:12px;color:var(--df-accent)}.item-details{margin-top:11px}.item-details>summary{cursor:pointer;width:fit-content;line-height:1.9}.item-details .release-project-plan{margin-top:12px;background:transparent;border:0;border-radius:0;padding:0}.inspection-confirmation{display:grid;gap:12px}.inspection-confirmation :deep(.n-checkbox__label){font-size:12px;line-height:1.8}.inspection-footer-actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.n-button:focus-visible,.item-details>summary:focus-visible,a:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}@media(max-width:600px){.batch-record{padding:14px}.batch-heading,.batch-record-heading{align-items:flex-start;flex-wrap:wrap}.inspection-item-heading{flex-wrap:wrap;gap:8px}.inspection-item-title{min-width:65%}.inspection-item-heading>.n-tag{margin-left:30px}.inspection-footer-actions>.n-button--primary-type{width:100%}.progress-items span{display:block;margin-left:0}.batch-record-actions{justify-content:flex-start}}
</style>
