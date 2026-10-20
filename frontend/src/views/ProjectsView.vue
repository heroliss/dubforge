<script setup lang="ts">
import ProjectTemplateBinding from '../components/ProjectTemplateBinding.vue'
import { friendlyError } from '../utils/http'
import BatchProjectImport from '../components/BatchProjectImport.vue'
import ProjectRunPanel from '../components/ProjectRunPanel.vue'
import BatchReleasePanel from '../components/BatchReleasePanel.vue'
import ProjectBatchPanel from '../components/ProjectBatchPanel.vue'
import FilePathLink from '../components/FilePathLink.vue'
import AutoRunBadge from '../components/AutoRunBadge.vue'
import PublicationProgress from '../components/PublicationProgress.vue'
import ExecutionProgress from '../components/ExecutionProgress.vue'
import { executionForStep } from '../utils/executionProgress'
import FreshnessDetails from '../components/FreshnessDetails.vue'
import { freshnessStatus, needsFreshnessDetails } from '../utils/freshness'
import { isPublicationComplete, publicationStage } from '../utils/publishing'
import { publicationUpdateProgress } from '../utils/publicationUpdate'
// 项目列表: 真实 CRUD + 新建 (yt-dlp 支持的任意站点链接 / 本地导入) + 改名
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NForm, NFormItem, NH1, NInput,
  NInputGroup, NLayoutContent, NModal, NProgress, NRadio, NRadioGroup, NSelect, NDropdown,
  NSpace, NSpin, NTag, NText, NTooltip, useMessage, useDialog,
} from 'naive-ui'
import { useNavStore, STEP_ORDER, STEP_LABELS, STATUS_TEXT, STATUS_HINTS } from '../stores/nav'
import { projectWorkflowStages, projectProductionDisplayStatus } from '../utils/projectWorkflowStages'
import { stagePath, WORKFLOW_STAGES } from '../utils/workflowStages'
import { fmtSize } from '../utils/format'
import { chainKind, chainSequence, holdDescription, parseChain, type ChainInfo } from '../utils/chainStatus'
import AppIcon from '../components/AppIcon.vue'
import { hiddenSelectionCount } from '../utils/actionScope'
import { createLatestRefresh } from '../utils/preview'
import { WORKFLOW_BUSY } from '../utils/workflow'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { useProjectListStore, type ProjectListScope } from '../stores/projectList'
import { projectCurrentDelivery, projectCurrentPublication, projectDeliverySummary } from '../utils/projectCard'
import { createProjectDeliveryReader } from '../utils/projectDeliveryReader'
import { projectRequestStage, projectRequestSteps, type ProjectRunFeedback } from '../utils/projectRunFeedback'
import { createExecutionProjection } from '../utils/executionSnapshot'
import { createProjectVerificationReader, currentGenerationSteps, mergeProjectSummary, mergeVerifiedProjects, projectFreshnessKnown } from '../utils/projectListProjection'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()
const listSnapshots = useProjectListStore()

const projects = ref<any[]>([])
const deliveryRevision = ref(0)
const deliveryReader = createProjectDeliveryReader(async (uid, signal) => {
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/workflow/status`, {
    cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]),
  })
  if (!response.ok) throw new Error('交付状态未能核对')
  return response.json()
}, () => { deliveryRevision.value++ })
function currentProjectDelivery(project: any) {
  void deliveryRevision.value
  return projectCurrentDelivery(project, deliveryReader.get(project))
}
function recheckingProjectDelivery(project: any) {
  void deliveryRevision.value
  return deliveryReader.rechecking(project)
}
function currentProjectProcess(project: any) {
  if (projectStartFeedbackOf(project)?.pending) return []
  const active = (project.steps || []).filter((step: any) => ['running', 'queued'].includes(step.status))
  const step = active.find((row: any) => row.key === project.running_step) || active[0]
  return step ? [{...step, execution: executionForStep(step)}] : []
}
const archiveScope = ref<ProjectListScope>('active')
const viewingArchive = computed(() => archiveScope.value === 'archived')
let listContext = ''
let listDisposed = false
let listVerified = false
const runFeedbacks = ref<Record<string, ProjectRunFeedback>>({})
const feedbackContexts = new Map<string, string>()
const feedbackJobs = new Map<string, Map<string, number>>()
let batchProjectionRevision = 0
const batchProjections = new Map<string, { revision: number; value: any }>()
const executionProjections = new Map<string, ReturnType<typeof createExecutionProjection>>()
let executionUnsupported = false
function executionProjection(uid: string) {
  let projection = executionProjections.get(uid)
  if (!projection) { projection = createExecutionProjection(); executionProjections.set(uid, projection) }
  return projection
}
function resetRunFeedback() {
  runFeedbacks.value = {}; feedbackContexts.clear(); feedbackJobs.clear(); batchProjections.clear()
  executionReader.cancel(); executionProjections.clear()
  deliveryReader.clear()
  executionUnsupported = false
  summarySupported = null; fullAttempted = false; verificationError = ''; verificationTokens.clear()
}
function receiveRunFeedback(feedback: ProjectRunFeedback) {
  if (listDisposed || viewingArchive.value) return
  if (feedback.stage === 'checking') {
    feedbackContexts.set(feedback.id, listContext)
    if (!feedbackJobs.has(feedback.id)) feedbackJobs.set(feedback.id, new Map(projects.value.filter(project => feedback.projectUids.includes(project.uid))
      .map(project => [project.uid, Math.max(0, ...(project.steps || []).map((step: any) => Number(step.job_id || step.execution?.job_id || 0)))])))
  }
  if (!feedbackContexts.has(feedback.id) || feedbackContexts.get(feedback.id) !== listContext) return
  const next = { ...runFeedbacks.value }
  const visible = new Set(projects.value.filter(project => !project.archived).map(project => project.uid))
  for (const uid of feedback.projectUids) {
    if (!visible.has(uid)) continue
    if (feedback.stage !== 'checking' && next[uid] && next[uid].id !== feedback.id) continue
    if (feedback.stage === 'clear') {
      const project = projects.value.find(row => row.uid === uid), current = next[uid]
      const awaitingJob = current?.stage === 'accepted' && current.batchId && project?.project_batch?.group_status === 'running'
        && project.project_batch.item_status === 'running' && !hasNewFeedbackJob(project, current)
      if (next[uid]?.id === feedback.id && !awaitingJob) delete next[uid]
    } else next[uid] = { ...feedback, projectUids: [...feedback.projectUids], groupIds: [...feedback.groupIds] }
  }
  runFeedbacks.value = next
  if (feedback.stage === 'clear' && !Object.values(next).some(value => value.id === feedback.id)) { feedbackContexts.delete(feedback.id); feedbackJobs.delete(feedback.id) }
  if (feedback.stage === 'accepted') void executionReader.refresh()
}
function hasNewFeedbackJob(project: any, feedback: ProjectRunFeedback) {
  return (project.steps || []).some((step: any) => Number(step.job_id || step.execution?.job_id || 0)
    > (feedbackJobs.get(feedback.id)?.get(project.uid) || 0))
}
function applyBatchProgress(batch: any) {
  if (listDisposed || viewingArchive.value || !batch?.id) return
  const allowed = Object.entries(runFeedbacks.value).filter(([, feedback]) => feedback.stage === 'accepted'
    && feedback.batchId === batch.id && feedbackContexts.get(feedback.id) === listContext)
  if (!allowed.length) return
  const members = new Map(allowed)
  const changes = new Map<string, any>(), nextFeedbacks = { ...runFeedbacks.value }
  for (const group of batch.groups || []) {
    for (const [index, item] of (group.items || []).entries()) {
      const uid = item.project_uid, feedback = members.get(uid)
      if (!feedback || !feedback.groupIds.includes(group.id)) continue
      const phase = item.phase || group.phase
      const preparing = group.status === 'running' && item.feedback === 'preparing'
      changes.set(uid, { batch_id: batch.id, group_id: group.id, status: batch.status,
        group_status: group.status, title: group.title, phase, item_phase: item.phase,
        item_status: item.status, feedback: item.feedback || '', position: index + 1, total: group.items.length })
      // 这里只接受本次启动回执后的批次状态，不能凭历史步骤的 running 清掉新请求反馈。
      const project = projects.value.find(row => row.uid === uid)
      const awaitingJob = group.status === 'running' && item.status === 'running' && project && !hasNewFeedbackJob(project, feedback)
      if (!preparing && !awaitingJob && (item.status !== 'queued' || !['running', 'queued'].includes(group.status))) delete nextFeedbacks[uid]
    }
  }
  if (!changes.size) return
  batchProjectionRevision++
  for (const [uid, value] of changes) batchProjections.set(uid, { revision: batchProjectionRevision, value })
  projects.value = projects.value.map(project => changes.has(project.uid) ? { ...project, project_batch: changes.get(project.uid) } : project)
  runFeedbacks.value = nextFeedbacks
  if (listVerified && listContext && listContext === listSnapshots.context) listSnapshots.save(archiveScope.value, projects.value)
  for (const [, feedback] of allowed) {
    if (!Object.values(nextFeedbacks).some(value => value.id === feedback.id)) { feedbackContexts.delete(feedback.id); feedbackJobs.delete(feedback.id) }
  }
}
function projectStartFeedbackOf(project: any): { message: string; pending: boolean; warning: boolean } | null {
  if (viewingArchive.value || project.archived) return null
  const feedback = runFeedbacks.value[project.uid]
  if (feedback && feedbackContexts.get(feedback.id) === listContext) {
    const preparing = feedback.stage === 'accepted' && project.project_batch?.group_status === 'running' && project.project_batch?.feedback === 'preparing'
    const queued = feedback.stage === 'accepted' && project.project_batch?.item_status === 'queued' && !preparing
    const handedOff = feedback.stage === 'accepted' && project.project_batch?.item_status === 'running' && !preparing
    return { message: preparing ? project.project_batch.item_phase || '已受理，正在准备继续' : queued
      ? project.project_batch.item_phase || '已确认继续，等待本组前项' : handedOff
      ? project.project_batch.item_phase || '恢复检查完成，等待当前步骤启动' : feedback.message,
      pending: ['checking', 'submitting', 'accepted'].includes(feedback.stage),
      warning: ['unknown', 'blocked'].includes(feedback.stage) }
  }
  return project.project_batch?.group_status === 'running' && project.project_batch?.feedback === 'preparing'
    ? { message: project.project_batch.item_phase || '已受理，正在准备继续', pending: true, warning: false } : null
}
function projectRequestOf(project: any) {
  const feedback = projectStartFeedbackOf(project)
  return feedback?.pending ? projectRequestStage({ stage: runFeedbacks.value[project.uid]?.stage || 'accepted', message: feedback.message }) : null
}
function invalidateProjectCache(context = listContext, preserveCurrent = false) {
  // 已离开的组件仍可能收到写操作回执。它可以使对应目录的快照失效，
  // 不能把离开前的整份列表重存，更不能改动另一个工作目录的快照。
  if (!context || context !== listSnapshots.context) return
  listSnapshots.changed()
  if (preserveCurrent && !listDisposed && listVerified && context === listContext) listSnapshots.save(archiveScope.value, projects.value)
}
const archiving = ref(false)
const deletingUids = ref(new Set<string>())
const deleteConfirmations = new Set<string>()
const archiveResults = ref<{ uid: string; title: string; ok: boolean; error?: string }[]>([])
const archiveFailures = computed(() => archiveResults.value.filter(result => !result.ok))
const loading = ref(true)
const refreshing = ref(false)
const manualRefreshing = ref(false)
const loadError = ref('')
const showNew = ref(false)
const creating = ref(false)
const form = ref({ source_kind: 'link', source_url: '', title: '' })
// 改名
const renaming = ref<any>(null)
const renameTitle = ref('')
const renameSaving = ref(false)
const { locked: renameProjectLocked, reason: renameLockReason, canEdit: canRename, observing: renameObserving } = useConfigurationEditability(() => renaming.value ? 'project:' + renaming.value.uid : '')
const renameLocked = computed(() => renameSaving.value || renameProjectLocked.value || archiving.value || !!renaming.value &&
  (projectBusy(projects.value.find(p => p.uid === renaming.value.uid) || renaming.value)
    || !!(projects.value.find(p => p.uid === renaming.value.uid) || renaming.value).archived))

// 轻摘要持续心跳，完整核验单独单飞；切换页面取消，隐藏标签页暂停轮询。
let listRevision = 0
const contextReady = ref(false)
let workspaceIdentity = ''
let identityGate: Promise<string | null> | null = null
let forceFresh = false
let summarySupported: boolean | null = null
let summarySerial = 0
let summaryInFlight = false
let fullInFlight = false
let lastFullFinished = 0
let fullAttempted = false
let verificationError = ''
let fullSerial = 0
let fullReadId = 0
let summaryReadId = 0
const verificationTokens = new Map<string, string>()
type ListRead = { scope: ProjectListScope; rows: any[]; verified: boolean; workspace: string; context: string; revision: number; fresh: boolean; batchRevision: number; executionRevisions: Map<string, number>; acceptedDirect: Map<string, string>; summary: boolean; tokens: Map<string, string>; serial: number; fullSerial: number };
async function readList(signal: AbortSignal, summary = false): Promise<ListRead> {
  const scope = archiveScope.value
  const contextCheck = identityGate
  const context = listSnapshots.context, revision = listSnapshots.revision
  const batchRevision = batchProjectionRevision
  const executionRevisions = new Map([...executionProjections].map(([uid, projection]) => [uid, projection.revision]))
  const acceptedDirect = new Map(Object.entries(runFeedbacks.value).filter(([, feedback]) => feedback.stage === 'accepted')
    .map(([uid, feedback]) => [uid, feedback.id]))
  const tokens = new Map(projects.value.map(project => [project.uid, project.summary_revision || '']))
  const serial = summarySerial
  const checkedSerial = fullSerial
  const fresh = forceFresh || listSnapshots.freshNeeded[scope]
  if (!summary) forceFresh = false
  refreshing.value = true
  const query = new URLSearchParams()
  if (scope === 'archived') query.set('archive', 'archived')
  if (summary) query.set('view', 'summary')
  const r = await fetch('/api/projects' + (query.size ? '?' + query : ''), {
    cache: 'no-store', signal, ...(fresh ? { headers: { 'X-DubForge-Refresh': 'fresh' } } : {}),
  })
  if (summary && [404, 422].includes(r.status)) {
    const fallback = await readList(signal)
    summarySupported = false
    return fallback
  }
  if (!r.ok) throw new Error(`读取项目失败（HTTP ${r.status}）`)
  const rows = await r.json()
  const isSummary = r.headers?.get?.('X-DubForge-Projection') === 'summary'
    || Array.isArray(rows) && rows.length > 0 && rows.every(row => row.projection === 'summary')
  const workspace = r.headers?.get?.('X-DubForge-Workspace') || ''
  const expected = contextCheck ? await contextCheck : workspaceIdentity
  if (signal.aborted) throw new DOMException('读取已取消', 'AbortError')
  if (workspace && expected && workspace !== expected) {
    throw Object.assign(new Error('项目工作目录已变化，请刷新列表重新核对。'), { workspaceChanged: true })
  }
  if (summary) summarySupported = isSummary
  return { scope, rows: Array.isArray(rows) ? rows : [], verified: !!workspace && workspace === expected, workspace, context, revision, fresh, batchRevision, executionRevisions, acceptedDirect, summary: isSummary, tokens, serial, fullSerial: checkedSerial }
}
function acceptList(next: ListRead) {
  const scope = next.scope
  if (workspaceIdentity && next.workspace && next.workspace !== workspaceIdentity) return
  // 开始于完整核验之前的摘要不能倒灌旧任务；下一轮轻量心跳再接回。
  if (next.summary && next.fullSerial !== fullSerial) return
  if ((next.verified || next.context && next.context === listSnapshots.context) && next.revision !== listSnapshots.revision) {
    // 写入前启动的列表即使迟到成功也不能覆盖刚修改的卡片。
    refreshing.value = false
    void load(false, true)
    return
  }
  // 完整列表可能仍在核对大文件；期间收到的轻量批次回执优先显示。
  // 下一次在回执之后发起的完整读取会自然接回步骤及产物的真实结果。
  const current = new Map(projects.value.map(project => [project.uid, project]))
  const checkedRows = next.summary ? next.rows.map(row => {
    const previous = current.get(row.uid)
    // 摘要的排队/运行是正面执行事实；终态摘要不充当完整产物核验。
    const runtime = executionProjection(row.uid).apply(previous?.steps || [],
      (row.steps || []).filter((step: any) => ['queued', 'running', 'stopping', 'pausing'].includes(step.status)))
    const projected = previous ? { ...previous, steps: currentGenerationSteps(previous.steps || [], runtime.steps).steps } : previous
    return mergeProjectSummary(row, projected)
  })
    : mergeVerifiedProjects(next.rows, projects.value, next.tokens, summarySerial !== next.serial, Date.now())
  const displayRows = scope === 'active' ? checkedRows.map(original => {
    const merged = currentGenerationSteps(original.steps || [], next.summary ? original.steps || []
      : executionProjection(original.uid).mergeFull(original.steps || [], next.executionRevisions.get(original.uid) || 0))
    const active = merged.steps.find((step: any) => ['running', 'queued', 'stopping', 'pausing'].includes(step.status))
    const project = { ...original, steps: merged.steps, status: active ? 'processing' : original.status,
      ...(active ? { running_step: active.key, running_label: active.label } : {}),
      ...(merged.changed ? { freshness_known: false } : {}) }
    const projection = batchProjections.get(project.uid)
    if (projection && projection.revision > next.batchRevision) return { ...project, project_batch: projection.value }
    batchProjections.delete(project.uid)
    return project
  }) : checkedRows
  if (contextReady.value && listContext === listSnapshots.context) {
    if (next.verified) listSnapshots.save(scope, displayRows)
    else listSnapshots.invalidate(scope)
    if (!next.summary && next.verified && next.fresh && next.context === listSnapshots.context) listSnapshots.acceptFresh(scope, next.revision)
  }
  // 切换范围期间旧请求即使被底层浏览器延迟收尾，也不能覆盖当前列表。
  if (scope !== archiveScope.value) return
  projects.value = displayRows
  if (next.summary) summarySerial++
  else { fullAttempted = true; fullSerial++; lastFullFinished = Date.now() }
  for (const project of displayRows) {
    const feedback = runFeedbacks.value[project.uid]
    const terminalWorkflow = ['failed', 'paused', 'attention', 'ready', 'completed', 'cancelled', 'stopped'].includes(project.workflow_status)
    const terminalBatch = feedback?.batchId && (project.project_batch?.batch_id === feedback.batchId
      ? ['failed', 'paused', 'attention', 'stopped', 'done', 'completed'].includes(project.project_batch.group_status)
      : !project.project_batch && terminalWorkflow)
    if (feedback?.stage === 'accepted' && next.acceptedDirect.get(project.uid) === feedback.id
      && (feedback.batchId ? terminalBatch : feedback.workflowId && terminalWorkflow)) {
      // 仅采用受理之后发起的完整核对；没有新 Job 的复用收尾或快速失败也能正确解除准备态。
      receiveRunFeedback({ ...feedback, stage: 'clear', message: '' })
    }
  }
  listVerified = next.verified
  listRevision++
  if (!next.summary) verificationError = ''
  loadError.value = verificationError
  const existing = new Set(displayRows.map(p => p.uid))
  sel.value = new Set([...sel.value].filter(uid => existing.has(uid)))
  loading.value = false
  refreshing.value = false
  if (!viewingArchive.value) deliveryReader.sync(displayRows, listContext, next.fresh)
  if (next.summary) {
    const changed = verificationTokens.size !== next.rows.length || next.rows.some(row => verificationTokens.get(row.uid) !== row.summary_revision)
    verificationTokens.clear()
    for (const row of next.rows) verificationTokens.set(row.uid, row.summary_revision)
    // 状态代次变化只补一次核验；五秒心跳不会给慢核验不断排下一轮。
    if (!fullAttempted && !fullInFlight || changed) void load()
  }
}
function listReadError(err: unknown, full = false) {
  if ((err as any)?.workspaceChanged) {
    listSnapshots.activate(''); listContext = ''; workspaceIdentity = ''
    resetRunFeedback()
    contextReady.value = false; listVerified = false; projects.value = []
  }
  const detail = friendlyError(err, '无法连接服务，请检查连接后重试')
  loadError.value = projects.value.length ? `项目列表未更新，已保留上次显示的项目。${detail}` : detail
  if (full) verificationError = loadError.value
  loading.value = false
  refreshing.value = false
}
const listReader = createProjectVerificationReader(async signal => {
  const id = ++fullReadId
  fullAttempted = true
  fullInFlight = true
  try { return await readList(signal) } finally {
    if (id === fullReadId) { fullInFlight = false; lastFullFinished = Date.now() }
  }
}, acceptList, error => listReadError(error, true))
const summaryReader = createLatestRefresh(async signal => {
  const id = ++summaryReadId
  summaryInFlight = true
  try { return await readList(signal, true) } finally { if (id === summaryReadId) summaryInFlight = false }
}, acceptList, listReadError)
function readSummary(showSpin = false, initialRead = false) {
  if (listDisposed || archiving.value || deletingUids.value.size || summaryInFlight) return Promise.resolve()
  if (summarySupported === false) return !fullInFlight && (initialRead || !fullAttempted || Date.now() - lastFullFinished >= 60000) ? load(showSpin) : Promise.resolve()
  if (showSpin) loading.value = !projects.value.length
  return summaryReader.refresh()
}
function pollList() {
  if (listDisposed || typeof document !== 'undefined' && document.hidden) return Promise.resolve()
  if (!contextReady.value) return initializeList()
  if (!fullInFlight && fullAttempted && Date.now() - lastFullFinished >= 60000) void load()
  return readSummary()
}
function cancelListReads() { listReader.cancel(); summaryReader.cancel(); fullReadId++; summaryReadId++; summaryInFlight = false; fullInFlight = false }
function load(showSpin = false, fresh = false) {
  if (listDisposed || archiving.value || deletingUids.value.size) return Promise.resolve()
  if (fresh) forceFresh = true
  // 核对状态时保留可见卡片；只有首次进入且没有快照时才遮罩等待。
  if (showSpin) loading.value = !projects.value.length && !(contextReady.value && listContext === listSnapshots.context && listSnapshots.read(archiveScope.value))
  return listReader.refresh()
}
function switchArchiveScope(scope: ProjectListScope) {
  if (archiving.value || deletingUids.value.size || archiveScope.value === scope) return
  // 范围切换先作废旧请求；迟到的当前项目响应不能覆盖归档列表。
  cancelListReads()
  executionReader.cancel(); executionProjections.clear()
  deliveryReader.clear()
  archiveScope.value = scope
  const cached = contextReady.value && listContext === listSnapshots.context ? listSnapshots.read(scope) : null
  projects.value = cached?.rows || []
  sel.value = new Set()
  searchText.value = ''; statusFilter.value = 'all'
  verificationError = ''; loadError.value = ''; archiveResults.value = []
  // 有缓存时保留卡片和计数，只在后台核对；没有缓存才显示空列表占位。
  fullAttempted = false; verificationTokens.clear()
  void readSummary(!cached)
}
let contextController: AbortController | null = null
async function initializeList(fresh = false) {
  contextController?.abort()
  // 重新核对身份时不加入旧目录的在途读取；普通五秒心跳仍沿原节流策略。
  cancelListReads()
  const request = new AbortController()
  contextController = request
  contextReady.value = false
  loadError.value = ''
  let releaseIdentity: (workspace: string | null) => void = () => {}
  identityGate = new Promise(resolve => { releaseIdentity = resolve })
  const revision = listRevision
  // 身份与轻量摘要并行；重文件核验在首屏展示后独立进行。
  const listTask = fresh ? load(true, true) : readSummary(true, true)
  try {
    // 身份接口只读实际数据目录和服务实例，不计算项目产物；返回后即可显示
    // 上次列表，同时在后台核对任务状态，避免等待整个项目摘要查询。
    const response = await fetch('/api/projects/context', {
      cache: 'no-store', signal: AbortSignal.any([request.signal, AbortSignal.timeout(4000)]),
    })
    if (!response.ok) throw new Error(`读取项目空间失败（HTTP ${response.status}）`)
    const context = await response.json()
    if (contextController !== request || request.signal.aborted) return
    if (typeof context.workspace_id !== 'string' || !context.workspace_id.trim()) throw new Error('项目空间身份缺失')
    const origin = typeof window === 'undefined' ? '' : window.location.origin
    const nextContext = `${origin}|${context.workspace_id}`
    if (listContext !== nextContext) {
      resetRunFeedback()
      projects.value = []; sel.value = new Set(); listVerified = false
    }
    listContext = nextContext
    workspaceIdentity = context.workspace_id
    listSnapshots.activate(listContext)
    contextReady.value = true
    if (revision === listRevision) {
      const cached = listSnapshots.read(archiveScope.value)
      if (cached) {
        projects.value = cached.rows; listVerified = true; loading.value = false
        if (!viewingArchive.value) deliveryReader.sync(cached.rows, listContext)
        if (loadError.value) loadError.value = `项目列表未更新，已保留上次显示的项目。${loadError.value}`
      }
    }
    releaseIdentity(workspaceIdentity)
  } catch {
    if (contextController !== request || request.signal.aborted) return
    // 旧后端或身份读取失败仍显示实时列表；未经核对的旧快照不参与展示。
    listSnapshots.activate('')
    listContext = ''
    resetRunFeedback()
    workspaceIdentity = ''
    contextReady.value = true
  } finally {
    releaseIdentity(null)
  }
  await listTask
}
async function refreshList() {
  // 刷新只读取列表；找回目录使用独立的确认入口。
  // 后台轮询保持静默，按钮的加载反馈只属于用户主动点击。
  if (manualRefreshing.value || listDisposed) return
  manualRefreshing.value = true
  try {
    if (!contextReady.value) await initializeList(true)
    else await load(true, true)
  } finally { manualRefreshing.value = false }
}
const recovering = ref(false)
function recoverProjects() {
  if (recovering.value) return
  dialog.info({ title: '扫描工作目录并找回项目？',
    content: '读取当前工作目录内的项目记录，找回从回收站还原或手动复制进来的项目。只补缺失登记，不覆盖已有登记，不移动文件，也不运行任何步骤。',
    positiveText: '扫描并找回', negativeText: '取消',
    onPositiveClick: async () => {
      if (recovering.value) return false
      recovering.value = true
      const context = listContext
      try {
        const response = await fetch('/api/settings/rebuild_db', { method: 'POST' })
        const data = await response.json()
        if (!response.ok) throw new Error(data.message || '找回项目失败')
        message.success(`已找回 ${data.projects ?? 0} 个项目，补充 ${data.steps ?? 0} 条步骤登记`)
        invalidateProjectCache(context)
        await load(true, true)
      } catch (error: any) { message.error(error.message || '找回项目失败') }
      finally { recovering.value = false }
    },
  })
}
// 运行进度独立读取；完整列表仍低频核对产物，长片的小文件检查不再挡住当前句反馈。
const executionReader = createLatestRefresh(async signal => {
  const context = listContext, workspace = workspaceIdentity
  if (listDisposed || executionUnsupported || !contextReady.value || !workspace || viewingArchive.value || typeof document !== 'undefined' && document.hidden || archiving.value || deletingUids.value.size) return null
  const uids = projects.value.filter(project => !project.archived && ((project.steps || []).some((step: any) => ['running', 'queued', 'checking'].includes(step.status))
    || WORKFLOW_BUSY.includes(project.workflow_status) || project.project_batch?.group_status === 'running'
    || runFeedbacks.value[project.uid]?.stage === 'accepted')).map(project => project.uid)
  if (!uids.length) return null
  const snapshots: any[] = []
  for (let offset = 0; offset < uids.length; offset += 100) {
    const response = await fetch(`/api/projects/execution?uids=${encodeURIComponent(uids.slice(offset, offset + 100).join(','))}`, {
      cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(4000)]),
    })
    // 用户可能仍在旧后端上配音；兼容回退，不为界面更新要求中断当前任务。
    if (response.status === 404) { executionUnsupported = true; return null }
    if (!response.ok) throw new Error(`读取运行进度失败（HTTP ${response.status}）`)
    const data = await response.json()
    if (data.workspace_id !== workspace || !Array.isArray(data.projects)) return null
    snapshots.push(...data.projects)
  }
  return { context, snapshots }
}, result => {
  if (!result || listDisposed || result.context !== listContext || viewingArchive.value || archiving.value || deletingUids.value.size) return
  const byUid = new Map(result.snapshots.map(snapshot => [snapshot.uid, snapshot]))
  let needsRefresh = false
  projects.value = projects.value.map(project => {
    const snapshot = byUid.get(project.uid)
    if (!snapshot || project.archived) return project
    const execution = executionProjection(project.uid).apply(project.steps || [], snapshot.steps || [])
    const merged = { ...execution, ...currentGenerationSteps(project.steps || [], execution.steps) }
    const feedback = runFeedbacks.value[project.uid]
    // 直接恢复任务同样等待本轮新 Job 的事实，不能让旧失败或旧 running 吞掉新请求。
    if (feedback?.stage === 'accepted' && (snapshot.steps || []).some((step: any) =>
      Number(step.job_id || step.execution?.job_id || 0) > (feedbackJobs.get(feedback.id)?.get(project.uid) || 0))) {
      const nextFeedback = { ...runFeedbacks.value }; delete nextFeedback[project.uid]; runFeedbacks.value = nextFeedback
      if (!Object.values(nextFeedback).some(row => row.id === feedback.id)) { feedbackContexts.delete(feedback.id); feedbackJobs.delete(feedback.id) }
    }
    needsRefresh ||= merged.needsRefresh
    const active = merged.steps.find((step: any) => ['running', 'queued'].includes(step.status))
    return { ...project, steps: merged.steps, status: active ? 'processing' : project.status,
      ...(merged.changed ? { freshness_known: false } : {}),
      running_step: active?.key || null, running_label: active?.label || null }
  })
  if (needsRefresh) void load()
}, () => { /* 读取失败保留已核验状态，由完整列表的连接提示统一说明。 */ })
function executionVisibilityChanged() {
  if (document.hidden) executionReader.cancel()
  else if (!listDisposed) { void executionReader.refresh(); void readSummary(); void load() }
}
let executionTimer: ReturnType<typeof setInterval> | 0 = 0
let listTimer: ReturnType<typeof setInterval> | 0 = 0
let listAlive = false
onMounted(async () => {
  listAlive = true
  document.addEventListener('visibilitychange', executionVisibilityChanged)
  executionTimer = setInterval(() => { if (!document.hidden) void executionReader.refresh() }, 1500)
  await initializeList()
  // 五秒只核对轻量登记与活动。完整产物核验按完成时间低频调度，
  // 任务换代、终态或明确操作会补读，不把定时器变成连续重扫描。
  if (listAlive) listTimer = setInterval(() => {
    if (!document.hidden && !viewingArchive.value) {
      void pollList()
    }
  }, 5000)
})
function disposeList() {
  listDisposed = true
  listAlive = false
  if (executionTimer) clearInterval(executionTimer)
  executionTimer = 0
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', executionVisibilityChanged)
  executionReader.cancel()
  if (listTimer) clearInterval(listTimer)
  listTimer = 0
  contextController?.abort()
  contextController = null
  cancelListReads()
  manualRefreshing.value = false
  resetRunFeedback()
}
onUnmounted(disposeList)

// —— 勾选批量运行 / 单项目运行 / 资源管理器 ——
const sel = ref<Set<string>>(new Set())
function toggleSel(uid: string, v: boolean) {
  if (archiving.value) return
  const s = new Set(sel.value)
  if (v) s.add(uid)
  else s.delete(uid)
  sel.value = s
}
// 单项目和批量共用一份可取消的运行检查，列表只负责传入明确的项目范围。
const stepRun = ref<InstanceType<typeof ProjectRunPanel> | null>(null)
const projectBatchPanel = ref<InstanceType<typeof ProjectBatchPanel> | null>(null)
function openProjectBatch(targets: any[]) {
  if (viewingArchive.value || archiving.value || deletingUids.value.size) return
  const active = targets.filter(project => !project.archived)
  if (active.length) void projectBatchPanel.value?.open(active.map(project => ({uid:project.uid,title:project.title||project.uid})))
}
function releaseStarted() { sel.value = new Set(); deliveryReader.clear() }
function openStepRun(targets: any[], scopeLabel: string) {
  if (viewingArchive.value || archiving.value) return
  const active = targets.filter(p => !p.archived)
  if (active.length) void stepRun.value?.open(active.map(p => ({ uid: p.uid, title: p.title || p.uid })), scopeLabel)
}
function runSelected() {
  if (selectedVerificationPending.value) return
  const selected = projects.value.filter(p => sel.value.has(p.uid) && !p.archived)
  if (selected.length === 1) openStepRun(selected, recoveryScope.value)
  else openProjectBatch(selected)
}
function runAllPending() {
  if (pendingInView.value.length === 1) openStepRun(pendingInView.value, `当前列表待制作 · 1 个项目`)
  else openProjectBatch(pendingInView.value)
}
const projectBusy = (p: any) => !!p.running_step || (p.steps || []).some((step: any) => ['running', 'queued', 'stopping', 'checking'].includes(step.status))
  || p.projection === 'summary' && p.status === 'processing'
  || !!projectStartFeedbackOf(p)?.pending || runFeedbacks.value[p.uid]?.stage === 'unknown'
  || parseChain(p.chain ?? p.params?._chain)?.state === 'active' || WORKFLOW_BUSY.includes(p.workflow_status)
  || ['running','pausing','stopping','attention'].includes(p.release_batch?.status)
  || ['queued','running','pausing','stopping','attention'].includes(p.project_batch?.group_status)
  || ['queued', 'preparing', 'uploading', 'submitting', 'joining'].includes(p.publication?.status)
  || !!publicationUpdateProgress(p.publication)?.active
const verifyingProjects = computed(() => projects.value.filter(p => (p.steps || []).some((step: any) => step.status === 'checking')))
const unknownPlanProjects = computed(() => projects.value.filter(p => !projectFreshnessKnown(p)
  && !projectBusy(p)))
const selectedVerificationPending = computed(() => verifyingProjects.value.some(p => sel.value.has(p.uid)))
const selectedBindingBusy = computed(() => projects.value.some(p => sel.value.has(p.uid) && projectBusy(p)))
const pendingInView = computed(() => viewingArchive.value ? [] : sortedProjects.value
  .filter((p: any) => !p.archived && projectFreshnessKnown(p) && p.status !== 'done' && !projectBusy(p) && p.dir_exists !== false))
function runProject(p: any) { if (!p.archived && !projectBusy(p) && p.dir_exists !== false) openStepRun([p], p.title) }
function showProjectProgress(p: any) {
  if (p.project_batch?.batch_id) {
    void router.push({path:'/',hash:`#project-batch-${p.project_batch.batch_id}`});return
  }
  if (p.release_batch?.id && ['running','pausing','stopping','attention','paused','stopped'].includes(p.release_batch.status)) {
    void router.push({path:'/',hash:`#release-batch-${p.release_batch.id}`});return
  }
  void router.push(p.running_step ? `/projects/${p.uid}/steps/${p.running_step}` : `/projects/${p.uid}`)
}
function projectRunHint(p: any) {
  if (p.dir_exists === false) return '项目目录缺失，请先还原目录。'
  if (parseChain(p.chain ?? p.params?._chain)?.state === 'held' || ['paused','failed','attention'].includes(p.workflow_status))
    return '根据各项目的目标成片和交付设置运行；有中断任务时可继续原计划，再核对实际起点、复用结果与执行终点。'
  return '按合成成片页的目标版本和发布页的交付设置检查。确认后执行，已有有效产物会复用。'
}
function productionStarted() { sel.value = new Set(); projectStateChanged() }
function projectStateChanged() {
  deliveryReader.clear()
  projects.value = projects.value.map(project => ({ ...project, freshness_known: false,
    status: projectBusy(project) ? 'processing' : 'checking' }))
  invalidateProjectCache(listContext, true)
  void load(false, true)
}

// —— 自动续跑链 (ADR-0072): 批量卡片显示计划状态, 挂起可一键继续 ——
const chainMap = computed(() => new Map(sortedProjects.value
  .map((p: any) => [p.uid, parseChain(p.chain ?? p.params?._chain)])))
function chainOf(p: any): ChainInfo | null { return chainMap.value.get(p.uid) ?? null }
function chainSummary(p: any): string {
  const chain = chainOf(p)
  if (!chain) return ''
  if (chain.state === 'held') {
    const head = STEP_LABELS[chain.hold?.step || chain.remaining[0]] || '链头'
    return `⏸ 挂起于「${head}」 · 剩余 ${chain.remaining.length} 步: ${chainSequence(chain, STEP_LABELS)}`
  }
  return `⛓ 续跑中 · 剩余 ${chain.remaining.length} 步: ${chainSequence(chain, STEP_LABELS)}`
}
const recoveryProjects = computed(() => (viewingArchive.value ? [] : sel.value.size ? projects.value.filter(project => sel.value.has(project.uid)) : sortedProjects.value)
  .filter(project => !project.archived)
  .map((project: any) => ({ uid: project.uid, title: project.title })))
const recoveryScope = computed(() => sel.value.size
  ? `所选 ${sel.value.size} 个项目${hiddenSelectionCount([...sel.value], sortedProjects.value.map(p => p.uid)) ? '（包含被筛选隐藏的选择）' : ''}`
  : `当前列表 ${sortedProjects.value.length} 个项目`)
async function reveal(p: any) {
  try {
    const r = await fetch(`/api/projects/${p.uid}/reveal`, { method: 'POST' })
    if (!r.ok) message.error('打开文件管理器失败')
  } catch { message.error('打开文件管理器失败 (网络异常)') }
}
const STATUS_TAG: Record<string, string> = {
  done: 'success', failed: 'error', processing: 'info', created: 'default',
}
const PROJECT_STATUS_OPTS = [
  { label: '全部状态', value: 'all' },
  { label: '制作状态待核对', value: 'checking' },
  { label: '待处理', value: 'created' },
  { label: '进行中', value: 'processing' },
  { label: '制作完成', value: 'done' },
  { label: '有失败步骤', value: 'failed' },
  { label: '已停止', value: 'stopped' },
  { label: '需更新', value: 'needs_update' },
  { label: 'B站已发布', value: 'published' },
]
const searchText = ref('')
const statusFilter = ref('all')
// —— 项目排序 ——
const sortBy = ref<'created' | 'title' | 'progress'>('created')
const sortDesc = ref(true)
const SORT_OPTS = [
  { label: '按创建时间', value: 'created' },
  { label: '按标题', value: 'title' },
  { label: '按完成进度', value: 'progress' },
]
// 项目级状态文案 (步骤级状态用 nav store 的 STATUS_TEXT)
const PROJECT_STATUS_TEXT: Record<string, string> = {
  created: '待处理', processing: '制作中', done: '制作完成', failed: '有失败步骤', stopped: '已停止', checking: '制作状态待核对',
}
// 发布状态是交付维度，与 p.status 的本地制作状态分开；发布完成不掩盖
// 本地产物仍需更新或某个步骤失败的事实。
const isBiliPublished = (p: any) => isPublicationComplete(p?.publication?.status)
// 字幕导出是「字幕与配音」编辑页中的独立可选产物。自动投稿/合成流程
// 可以直接使用 split.txt 与 translate.txt，故它不应在主流程进度中制造
// 一个永远空白的段；字幕文件是否已单独导出在进度条下方单独说明。
const requiredSteps = (p: any) => (p.steps ?? []).filter((x: any) => x.selected && x.key !== 'subtitles')
const doneCount = (p: any) =>
  requiredSteps(p).filter((x: any) => x.status === 'done' && x.fresh).length
const orderedProjectSteps = (project: any) => projectRequestSteps(projectWorkflowStages(project), projectStartFeedbackOf(project)?.pending
  ? { stage: runFeedbacks.value[project.uid]?.stage || 'accepted', message: projectStartFeedbackOf(project)!.message } : null)
const completedStages = (p:any) => orderedProjectSteps(p).filter(stage=>stage.complete).length
const subtitleExportStep = (p: any) => (p.steps ?? []).find((x: any) => x.key === 'subtitles')
const subtitleExportPending = (p: any) => {
  const step = subtitleExportStep(p)
  return !!step && !(step.status === 'done' && step.fresh === true)
}
const subtitleExportNotice = (p: any) => {
  const step = subtitleExportStep(p)
  if (!step || !subtitleExportPending(p)) return ''
  if (!projectFreshnessKnown(p) && step.status === 'failed') return '上次字幕文件导出失败，当前产物待核对'
  if (!projectFreshnessKnown(p) && !['running', 'queued'].includes(step.status)) return '字幕文件状态待核对'
  if (step.status === 'pending') return '字幕文件未单独导出（可选）'
  if (step.status === 'stale' || (step.status === 'done' && step.fresh === false)) return '字幕文件需更新（可选）'
  if (step.status === 'failed') return `字幕文件导出失败${step.error ? '，请查看步骤日志' : ''}`
  if (step.status === 'running' || step.status === 'queued') return '字幕文件导出中'
  return '字幕文件状态待核对'
}
const subtitleExportNeedsAttention = (p: any) => projectFreshnessKnown(p) && ['failed', 'interrupted', 'stopped'].includes(subtitleExportStep(p)?.status)
const updateSteps = (p:any) => projectFreshnessKnown(p) ? (p.steps || []).filter(needsFreshnessDetails) : []
const projectStepStatus = (step:any) => freshnessStatus(step) || STATUS_TEXT[step.status] || step.status
// 制作状态和已有结果是否失效是两个维度；失败或运行不能隐藏其它步骤的更新原因。
const projectCanExplain = (p:any) => updateSteps(p).length>0
const updateSummary = (p:any) => updateSteps(p).slice(0,3).map((step:any)=>`${step.label}：${step.freshness_reason || step.freshness_label || projectStepStatus(step)}`).join('；')
  + (updateSteps(p).length>3 ? `；另有${updateSteps(p).length-3}步，点击查看明细` : '；点击查看具体变化')
const stepPath = (p: any, key: string) =>
  stagePath(p.uid,key)
// 统计和筛选跟随同一展示状态，检查/准备不再一边显示继续中、一边仍计入失败。
const projectDisplayStatus = (project: any) => projectRequestOf(project) ? 'processing' : projectProductionDisplayStatus(project)
const pendingRequestCount = computed(() => projects.value.filter(project => projectRequestOf(project)).length)
const filteredProjects = computed(() => {
  const q = searchText.value.trim().toLocaleLowerCase()
  return projects.value.filter((p: any) => {
    if (statusFilter.value === 'published') {
      if (!isBiliPublished(p)) return false
    } else if (statusFilter.value === 'needs_update') {
      if (!projectCanExplain(p)) return false
    } else if (statusFilter.value !== 'all' && projectDisplayStatus(p) !== statusFilter.value) return false
    if (!q) return true
    return [p.title, p.uid, p.source_url]
      .filter(Boolean)
      .some((v: any) => String(v).toLocaleLowerCase().includes(q))
  })
})
const sortedProjects = computed(() => {
  const dir = sortDesc.value ? -1 : 1
  const arr = [...filteredProjects.value]
  arr.sort((a: any, b: any) => {
    if (sortBy.value === 'title') return dir * a.title.localeCompare(b.title, 'zh')
    if (sortBy.value === 'progress') return dir * (completedStages(a) - completedStages(b))
    return dir * ((a.created_at || '').localeCompare(b.created_at || ''))
  })
  return arr
})
const statusCounts = computed(() => projects.value.reduce((acc: Record<string, number>, p: any) => {
  const status = projectDisplayStatus(p)
  acc[status] = (acc[status] || 0) + 1
  return acc
}, {}))
const publishedCount = computed(() => projects.value.filter(isBiliPublished).length)
const hasProjectFilter = computed(() => !!searchText.value.trim() || statusFilter.value !== 'all')
function clearProjectFilter() {
  searchText.value = ''
  statusFilter.value = 'all'
}
const visibleSelected = computed(() => sortedProjects.value.filter(p => sel.value.has(p.uid)).length)
function selectVisible(checked: boolean) {
  if (archiving.value) return
  const next = new Set(sel.value)
  for (const project of sortedProjects.value) checked ? next.add(project.uid) : next.delete(project.uid)
  sel.value = next
}
const projectMenu = (project: any) => [
  { label: '打开项目文件夹', key: 'reveal' },
  ...(!project.archived && !viewingArchive.value ? [
    { label: '重命名', key: 'rename', disabled: projectBusy(project) || archiving.value },
    { label: '归档项目', key: 'archive', disabled: projectBusy(project) || archiving.value },
  ] : [{ label: '恢复到当前项目', key: 'restore', disabled: archiving.value }]),
  { type: 'divider', key: 'divider' },
  { label: '删除项目…', key: 'delete' },
]
function projectAction(action: string, project: any) {
  if (action === 'reveal') void reveal(project)
  else if (action === 'rename') openRename(project)
  else if (action === 'delete') remove(project)
  else if (action === 'archive' || action === 'restore') void changeArchive([project], action === 'archive')
}

async function changeArchive(targets: any[], archived: boolean) {
  if (archiving.value || deletingUids.value.size) return
  const snapshot = targets.map(project => ({ ...project }))
  const context = listContext
  if (!snapshot.length) return
  archiving.value = true
  archiveResults.value = []
  // 成功后立即移出本范围；写操作前的轮询不能把条目再放回来。
  cancelListReads()
  refreshing.value = false
  try {
    for (const project of snapshot) {
      try {
        if (archived && projectBusy(project)) throw new Error('项目有任务正在执行，结束后才能归档')
        const response = await fetch(`/api/projects/${project.uid}/archive`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archived }),
        })
        const data = await response.json().catch(() => ({}))
        if (!response.ok || data.ok !== true || data.archived !== archived) throw new Error(data.message || `${archived ? '归档' : '恢复'}失败（HTTP ${response.status}）`)
        projects.value = projects.value.filter(item => item.uid !== project.uid)
        // 当前范围可以继续即时显示；另一范围含有刚移动的项目，必须失效，
        // 否则用户来回切换时会先看到已过期的卡片。
        invalidateProjectCache(context, true)
        sel.value = new Set([...sel.value].filter(uid => uid !== project.uid))
        nav.archiveChanged(project.uid, data.archived, data.archived_at)
        archiveResults.value.push({ uid: project.uid, title: project.title || project.uid, ok: true })
      } catch (error) {
        archiveResults.value.push({ uid: project.uid, title: project.title || project.uid, ok: false,
          error: friendlyError(error, archived ? '归档失败' : '恢复失败') })
      }
    }
    const count = archiveResults.value.filter(result => result.ok).length
    if (count) message.success(`已${archived ? '归档' : '恢复'} ${count} 个项目`)
  } finally {
    archiving.value = false
    loading.value = false
  }
}
function changeSelectedArchive() {
  return changeArchive(projects.value.filter(project => sel.value.has(project.uid)), !viewingArchive.value)
}
function archiveDate(value: unknown) {
  const date = new Date(String(value || ''))
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString('zh-CN') : ''
}

function openRename(p: any) {
  if (p.archived || viewingArchive.value || archiving.value) { message.info('已归档项目请先恢复后再修改'); return }
  if (projectBusy(p)) { message.warning('项目正在执行或核验，请完成后再改名'); return }
  renaming.value = p
  renameTitle.value = p.title
}
async function applyRename() {
  if (!renaming.value || renameLocked.value) return
  if (!renameTitle.value.trim()) { message.warning('标题不能为空'); return }
  await saveProjectName({ title: renameTitle.value })
}
async function useVideoTitle() {
  await saveProjectName({ title_mode: 'auto' })
}
async function saveProjectName(payload: { title?: string; title_mode?: 'auto' }) {
  if (!renaming.value || renameLocked.value) return
  const target = renaming.value
  const context = listContext
  if (renameObserving.value && !(await canRename()) || renaming.value !== target || renameLocked.value) return
  renameSaving.value = true
  try {
    const r = await fetch(`/api/projects/${target.uid}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const j = await r.json().catch(() => ({}))
    if (r.ok && j.uid) {
      target.title = j.title
      target.title_mode = j.title_mode || payload.title_mode || 'manual'
      invalidateProjectCache(context, true)
      renaming.value = null
      message.success(payload.title_mode === 'auto' ? '已恢复自动命名' : '已改名')
    } else {
      message.error(j.message || `改名失败 (HTTP ${r.status})`)
    }
  } catch { message.error('改名请求失败 (网络异常)') } finally { renameSaving.value = false }
}

function remove(p: any) {
  if (archiving.value || deletingUids.value.has(p.uid) || deleteConfirmations.has(p.uid)) return
  const hasRun = !!p.running_step
  const directoryMissing = p.dir_exists === false
  deleteConfirmations.add(p.uid)
  let accepted = false
  const close = () => deleteConfirmations.delete(p.uid)
  const removal = directoryMissing
    ? '项目目录已不在磁盘上，这次只清理本地登记。文件若在别处，之后仍可放回工作目录并找回。'
    : '整个项目文件夹会移入系统回收站，并清理本地登记；无法回收时会停止删除，不改用永久删除。外部引用的原视频和B站稿件保留。找回时先从回收站还原文件夹，再在项目列表点击「找回项目…」。'
  dialog.warning({
    title: p.archived ? '删除已归档项目？' : '删除项目？',
    content: hasRun
      ? `「${p.title}」正在运行「${p.running_label}」。删除将先取消该任务并等待退出。${removal}`
      : `确定删除「${p.title}」？${removal}`,
    positiveText: hasRun ? '取消任务并删除' : directoryMissing ? '清理登记' : '移入回收站',
    negativeText: '取消',
    onAfterLeave: close, onNegativeClick: close, onClose: close, onMaskClick: close,
    onPositiveClick: async () => {
      if (accepted || archiving.value || deletingUids.value.has(p.uid)) return false
      accepted = true; deletingUids.value.add(p.uid); listReader.cancel(); loading.value = false; refreshing.value = false
      const context = listContext
      let completed = false
      try {
        const url = hasRun ? `/api/projects/${p.uid}?force=true` : `/api/projects/${p.uid}`
        const r = await fetch(url, { method: 'DELETE' })
        const result = await r.json().catch(() => ({}))
        if (!r.ok || result.ok !== true) throw new Error(result.message || '删除未完成，请刷新列表核对')
        completed = true
        projects.value = projects.value.filter(row => row.uid !== p.uid)
        invalidateProjectCache(context, true)
        sel.value = new Set([...sel.value].filter(uid => uid !== p.uid))
        message.success(directoryMissing ? '已清理项目登记' : '项目已移入回收站')
        if (nav.uid === p.uid) nav.reset()
      } catch (error: any) {
        accepted = false; message.error(friendlyError(error, '删除失败，请稍后重试'))
      } finally { deletingUids.value.delete(p.uid) }
      if (completed) {
        close(); await load(false, true)
      }
      return completed
    },
  })
}

// —— 本地导入: 拖入 (副本进项目目录) / 浏览 + 粘贴路径 (零拷贝引用) ——
const showBrowse = ref(false)
const browsePath = ref('')
const browseParent = ref<string | null>(null)
const browseEntries = ref<any[]>([])
const browseSel = ref<any>(null)
// 拖入: 浏览器拿不到真实路径, 先持有 File, 创建项目后再把内容流式写进项目目录 media/
const pendingFile = ref<File | null>(null)
const ingesting = ref(false)
const ingestPct = ref(0)
const ingestErr = ref('')
const abortingIngest = ref(false), ingestAbortError = ref('')
const newUid = ref<string | null>(null)

let browseSeq = 0
async function browseTo(path: string) {
  const seq = ++browseSeq                    // 序号守卫: 快速连点时慢响应不覆盖新路径
  try {
    const r = await fetch(`/api/files/list?path=${encodeURIComponent(path)}`,
      { cache: 'no-store' })
    if (!r.ok) {
      const j = await r.json().catch(() => ({}))
      message.error(j.message || '打开文件夹失败')
      return
    }
    const j = await r.json()
    if (seq !== browseSeq) return            // 已有更新的浏览请求, 丢弃过期结果
    browsePath.value = j.path
    browseParent.value = j.parent
    browseEntries.value = j.entries
    browseSel.value = null
    localStorage.setItem('dubforge.browse', j.path)     // 记住浏览位置
  } catch { message.error('浏览请求失败 (网络异常)') }
}
function openBrowse() {
  showBrowse.value = true
  void browseTo(localStorage.getItem('dubforge.browse') || '')
}
function browseConfirm() {
  if (!browseSel.value) return
  form.value.source_kind = 'local'
  form.value.source_url = browseSel.value.path
  pendingFile.value = null                    // 后选的路径优先于先前拖入的文件
  if (!form.value.title.trim()) {
    form.value.title = browseSel.value.name.replace(/\.[^.]+$/, '')
  }
  showBrowse.value = false
}
function onDrop(e: DragEvent) {
  if (creating.value || ingesting.value || abortingIngest.value || ingestErr.value) return
  const files = e.dataTransfer?.files
  if (!files?.length) { message.warning('未识别到文件'); return }
  if (files.length > 1) message.info('一次只支持一个视频, 已取第一个')
  const f = files[0]
  if (f.size === 0 && f.type === '') {
    message.warning('似乎拖入的是文件夹 —— 请拖入视频文件')
    return
  }
  form.value.source_kind = 'local'
  form.value.source_url = ''                  // 后拖入的文件优先于先前选的路径
  pendingFile.value = f
  if (!form.value.title.trim()) form.value.title = f.name.replace(/\.[^.]+$/, '')
}

async function create() {
  if (creating.value || ingesting.value || abortingIngest.value) return
  if (form.value.source_kind === 'local' && pendingFile.value) {
    await createThenIngest()
    return
  }
  if (!form.value.source_url.trim()) { message.warning('请填写视频链接或本地路径'); return }
  creating.value = true
  const context = listContext
  try {
    const r = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form.value),
    })
    const j = await r.json()
    if (j.uid) {
      invalidateProjectCache(context)
      showNew.value = false
      message.success('项目已创建')
      router.push(`/projects/${j.uid}`)
    } else {
      message.error(j.message || '创建失败')
    }
  } catch { message.error('创建失败，请检查服务连接后重试') } finally {
    creating.value = false
  }
}

// 拖入流程: 先建项目 (项目目录即容器), 再把文件内容流式写进 media/ —— 不经收件箱
async function createThenIngest() {
  creating.value = true
  const context = listContext
  try {
    const r = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form.value, source_url: '' }),
    })
    const j = await r.json()
    if (!j.uid) { message.error(j.message || '创建失败'); return }
    invalidateProjectCache(context)
    newUid.value = j.uid
    await runIngest()
  } catch { message.error('创建失败，请检查服务连接后重试') } finally { creating.value = false }
}

async function runIngest() {
  if (ingesting.value || abortingIngest.value) return
  const f = pendingFile.value
  if (!newUid.value || !f) return
  ingestErr.value = ''
  ingestAbortError.value = ''
  ingesting.value = true
  ingestPct.value = 0
  try {
    await ingest(newUid.value, f)
    showNew.value = false
    message.success('项目已创建, 视频副本已存入项目目录')
    router.push(`/projects/${newUid.value}`)
  } catch (e: any) {
    ingestErr.value = friendlyError(e, '导入失败')
  } finally { ingesting.value = false }
}

// 用 XHR 而非 fetch: 需要 upload.onprogress 显示复制进度
function ingest(uid: string, f: File): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/projects/${uid}/ingest?filename=${encodeURIComponent(f.name)}`)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) ingestPct.value = Math.round((e.loaded / e.total) * 100)
    }
    xhr.onload = () => {
      if (xhr.status < 300) { resolve(); return }
      let msg = `HTTP ${xhr.status}`
      try { msg = JSON.parse(xhr.responseText).message || msg } catch { /* 原样 */ }
      reject(new Error(msg))
    }
    xhr.onerror = () => reject(new Error('网络错误 —— 请确认后端正在运行'))
    xhr.send(f)
  })
}

// 导入失败时项目刚建、什么都没有: 重试, 或删掉空项目重来
async function abortIngest() {
  if (abortingIngest.value || creating.value || ingesting.value) return
  const uid = newUid.value
  const context = listContext
  if (!uid) { resetNewForm(); showNew.value = false; return }
  abortingIngest.value = true; ingestAbortError.value = ''
  try {
    const response = await fetch(`/api/projects/${uid}`, { method: 'DELETE' })
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      throw new Error(data.message || `删除项目失败（HTTP ${response.status}）`)
    }
    // 收到成功回执后再清空重试所需的项目与文件，失败时继续留在当前表单。
    if (newUid.value !== uid) return
    invalidateProjectCache(context)
    resetNewForm(); showNew.value = false
    message.info('已放弃导入，项目已删除')
  } catch (error) { ingestAbortError.value = friendlyError(error, '删除项目失败，请重试；导入文件和项目记录已保留。') }
  finally { abortingIngest.value = false }
}
function resetNewForm() {
  pendingFile.value = null
  newUid.value = null
  ingestErr.value = ''
  ingestAbortError.value = ''
  ingestPct.value = 0
  form.value = { source_kind: 'link', source_url: '', title: '' }
}
function projectsImported(uids?: string[]) {
  invalidateProjectCache()
  if (uids) sel.value = new Set(uids)
  void load(false, true)
}
</script>

<template>
  <div class="workspace-page projects-page">
    <div class="page-head">
      <div>
        <span class="page-kicker">创作 / 管理 / 交付</span>
        <n-h1 style="margin: 0">项目空间</n-h1>
        <n-text depth="3" class="page-subtitle">
          从一段视频开始，让每一种声音被听见。管理译制进度，继续你的创作。
        </n-text>
      </div>
      <n-space><RouterLink to="/publications" class="records-entry">全部发布记录 →</RouterLink><BatchProjectImport v-if="!viewingArchive" @created="projectsImported()" @select-created="projectsImported($event)"/><n-button type="primary" @click="showNew = true"><template #icon><AppIcon name="plus" /></template>新建项目</n-button></n-space>
    </div>
    <div class="archive-toolbar">
      <div class="archive-tabs" role="tablist" aria-label="项目范围">
        <button role="tab" :aria-selected="!viewingArchive" :disabled="archiving" @click="switchArchiveScope('active')">当前项目</button>
        <button role="tab" :aria-selected="viewingArchive" :disabled="archiving" @click="switchArchiveScope('archived')">已归档</button>
      </div>
      <span class="archive-description">{{ viewingArchive ? '保留全部文件与记录。恢复后可继续编辑、制作和发布。' : '暂时不再处理的项目可以归档，文件与记录仍保留。' }}</span>
    </div>
    <div v-if="projects.length" class="summary-strip">
      <button v-for="item in [{label:viewingArchive ? '归档项目' : '当前项目', value:'all', count:projects.length}, {label:'进行中', value:'processing', count:statusCounts.processing || 0}, {label:'制作完成', value:'done', count:statusCounts.done || 0}, {label:'需关注', value:'failed', count:statusCounts.failed || 0}, {label:'B站已发布', value:'published', count:publishedCount}]"
              :key="item.value" class="summary-item" :class="`summary-${item.value}`" :aria-pressed="statusFilter === item.value" @click="statusFilter = item.value">
        <span>{{ item.label }}</span><strong>{{ item.count.toString().padStart(2, '0') }}</strong>
        <span class="summary-caption">{{ item.value === 'all' ? (viewingArchive ? '文件与记录完整保留' : '正在管理的译制项目') : item.value === 'processing' ? '含核对、准备与实际执行' : item.value === 'done' ? '查看已完成制作的项目' : item.value === 'published' ? 'B站交付已完成' : '查看失败步骤与日志' }}</span>
      </button>
    </div>
    <n-text v-if="verifyingProjects.length" depth="3" class="list-verification-note" role="status">{{ verifyingProjects.length }} 个项目的任务已结束，正在核验产物；核验完成前暂不能运行。</n-text>
    <n-text v-if="unknownPlanProjects.length" depth="3" class="list-verification-note" role="status">{{ unknownPlanProjects.length }} 个项目的制作状态待核对；可以先打开项目、选择模板或勾选运行，运行前会检查实际范围。</n-text>
    <div class="toolbar filter-toolbar">
        <n-input v-model:value="searchText" size="small" clearable
                 placeholder="搜索项目名称或视频来源" aria-label="搜索项目" class="project-search"><template #prefix><AppIcon name="search" :size="16" /></template></n-input>
        <n-select v-model:value="statusFilter" :options="PROJECT_STATUS_OPTS" size="small"
                  aria-label="筛选项目状态" style="width: 124px" />
        <n-select v-model:value="sortBy" :options="SORT_OPTS" size="small"
                  aria-label="项目排序方式" style="width: 130px" />
        <n-button size="small" @click="sortDesc = !sortDesc">
          {{ sortDesc ? '↓ 降序' : '↑ 升序' }}
        </n-button>
        <n-button size="small" quaternary title="只重新读取项目状态，不扫描目录或启动任务"
                  :loading="manualRefreshing" :aria-busy="manualRefreshing" :disabled="archiving" @click="refreshList">刷新列表</n-button>
        <n-button size="small" quaternary :loading="recovering" @click="recoverProjects">找回项目…</n-button>
        <n-button v-if="hasProjectFilter" size="small" quaternary @click="clearProjectFilter">清除筛选</n-button>
      </div>
    <div v-if="sortedProjects.length || sel.size" class="selection-bar">
      <n-checkbox :disabled="archiving || !sortedProjects.length" :checked="sortedProjects.length > 0 && visibleSelected === sortedProjects.length" :indeterminate="visibleSelected > 0 && visibleSelected < sortedProjects.length" @update:checked="selectVisible">选择当前列表</n-checkbox>
      <span class="list-count">{{ sel.size ? `已选 ${sel.size} 个项目` : `共 ${sortedProjects.length} 个项目` }}</span>
      <n-text v-if="hiddenSelectionCount([...sel], sortedProjects.map(p => p.uid))" type="warning" style="font-size:12px">含 {{ hiddenSelectionCount([...sel], sortedProjects.map(p => p.uid)) }} 个被筛选隐藏的项目</n-text>
      <n-button v-if="sel.size" size="small" quaternary :disabled="archiving" @click="sel = new Set()">取消选择</n-button>
      <n-button v-if="sel.size" size="small" secondary :loading="archiving" :disabled="archiving" @click="changeSelectedArchive">{{ viewingArchive ? '恢复所选' : '归档所选' }}（{{ sel.size }}）</n-button>
      <n-button v-if="!viewingArchive && sel.size" size="small" type="primary" :disabled="archiving || selectedVerificationPending" @click="runSelected">{{ sel.size > 1 ? '批量运行' : '运行所选项目' }}（{{ sel.size }}）…</n-button>
      <n-tooltip v-else-if="pendingInView.length" trigger="hover" :delay="300"><template #trigger><n-button size="small" secondary @click="runAllPending">运行待制作项目（{{ pendingInView.length }}）…</n-button></template>按各项目的目标成片和交付设置检查；中断任务可继续原计划。</n-tooltip>
    </div>
    <ProjectBatchPanel v-if="!viewingArchive" ref="projectBatchPanel" hidden-trigger @started="releaseStarted" @changed="projectStateChanged" @run-feedback="receiveRunFeedback" @batch-progress="applyBatchProgress" />
    <BatchReleasePanel v-if="!viewingArchive" :exclude-ids="projectBatchPanel?.managedReleaseIds || []" @started="releaseStarted" @changed="projectStateChanged" />
    <p v-if="statusFilter==='failed' && pendingRequestCount" class="pending-filter-note" role="status">{{ pendingRequestCount }} 个项目正在核对或准备继续，可在「进行中」查看。<n-button text size="tiny" @click="statusFilter='processing'">查看进行中</n-button></p>
    <n-alert v-if="loadError" type="error" class="load-error">{{ loadError }}<template #action><n-button size="small" @click="refreshList">重试</n-button></template></n-alert>
    <n-alert v-if="archiveResults.length" :type="archiveFailures.length ? 'warning' : 'success'" class="archive-result" :closable="!archiving" @close="archiveResults = []">
      {{ viewingArchive ? '恢复' : '归档' }}结果：{{ archiveResults.filter(result => result.ok).length }} 个成功<span v-if="archiveFailures.length">，{{ archiveFailures.length }} 个未完成；失败项保留在列表中，可稍后重试。</span>
      <ul v-if="archiveFailures.length"><li v-for="result in archiveFailures" :key="result.uid">{{ result.title }}：{{ result.error }}</li></ul>
    </n-alert>
    <ProjectTemplateBinding v-if="!viewingArchive && !archiving && sel.size > 1" :uids="[...sel]" :disabled="selectedBindingBusy" @changed="projectStateChanged" />
    <n-spin :show="loading">
      <n-empty v-if="!loading && !loadError && !projects.length"
               :description="viewingArchive ? '还没有归档项目' : '还没有当前项目 —— 新建后可粘贴视频链接或导入本地视频'">
        <template #extra>
          <n-button v-if="!viewingArchive" type="primary" size="small" @click="showNew = true">＋ 新建项目</n-button>
        </template>
      </n-empty>
      <n-empty v-else-if="!loading && !loadError && !sortedProjects.length"
               description="没有符合当前筛选条件的项目">
        <template #extra>
          <n-button size="small" @click="clearProjectFilter">清除筛选</n-button>
        </template>
      </n-empty>
      <div v-else class="project-list">
        <n-card v-for="p in sortedProjects" :key="p.uid" size="small"
                class="project-card" :class="{ selected: sel.has(p.uid) }">
          <div class="project-head">
            <div class="project-main">
              <n-checkbox size="small" :checked="sel.has(p.uid)"
                          :disabled="archiving"
                          :aria-label="`选择项目 ${p.title}`"
                          @update:checked="(v: boolean) => toggleSel(p.uid, v)" />
              <div class="project-title-block">
                <div class="title-line">
                  <router-link :to="`/projects/${p.uid}`" class="project-title">{{ p.title }}</router-link>
                  <n-tag v-if="p.archived || viewingArchive" size="small" :bordered="false">已归档</n-tag>
                  <n-tag size="small" :type="projectRequestOf(p) ? 'info' : (STATUS_TAG as any)[projectDisplayStatus(p)] || 'default'">
                    {{ projectRequestOf(p)?.text || PROJECT_STATUS_TEXT[projectDisplayStatus(p)] || projectDisplayStatus(p) }}
                  </n-tag>
                  <FreshnessDetails v-if="projectCanExplain(p)" :uid="p.uid" :label="p.title"
                                    :status-text="`需更新 · ${updateSteps(p).length}步`" :summary="updateSummary(p)" />
                  <n-tag v-if="!projectRequestOf(p) && recheckingProjectDelivery(p)" size="small" :bordered="false" data-delivery-rechecking title="正在重新核对当前交付；较早流程记录完整保留。">最近交付待核对</n-tag>
                  <n-tag v-else-if="!projectRequestOf(p) && currentProjectDelivery(p)" size="small" :type="currentProjectDelivery(p)!.tone" :bordered="false" data-current-delivery
                    :title="`${currentProjectDelivery(p)!.note}；较早流程记录仍保留在项目总览。`">{{ currentProjectDelivery(p)!.text }}</n-tag>
                  <AutoRunBadge v-else-if="!projectRequestOf(p)" :workflow-status="p.workflow_status" :chain="chainOf(p)" :running-label="p.running_label" />
                  <router-link class="publication-link" :to="`/projects/${p.uid}/publish`"><n-tag size="small" :type="publicationStage(p.publication?.status).tone">B站 · {{ publicationStage(p.publication?.status).text }}</n-tag></router-link>
                  <n-tooltip v-if="p.dir_exists === false" trigger="hover" :delay="0"
                             placement="top">
                    <template #trigger>
                      <n-tag size="small" type="error" :bordered="false">⚠ 目录缺失</n-tag>
                    </template>
                    项目目录不在磁盘上 —— 可能被移动或删除。从回收站还原后此标记会自动消失;
                    确认不再需要可点「删除」清理登记。
                  </n-tooltip>
                </div>
                <n-text depth="3" class="project-source" :title="p.source_kind === 'local' ? '本地导入' : p.source_url">
                  {{ p.source_kind === 'local' ? '本地导入' : p.source_url }} · {{ p.uid }}<template v-if="p.archived_at"> · {{ archiveDate(p.archived_at) }} 归档</template>
                </n-text>
              </div>
            </div>
            <div class="project-actions">
              <n-button size="small" secondary type="primary" @click.stop="router.push(`/projects/${p.uid}`)">打开项目 →</n-button>
              <n-button v-if="p.archived || viewingArchive" size="small" secondary :loading="archiving" :disabled="archiving || deletingUids.has(p.uid)" @click.stop="changeArchive([p], false)">恢复项目</n-button>
              <n-button v-else-if="projectBusy(p)" size="small" @click.stop="showProjectProgress(p)">查看进度 →</n-button>
              <n-tooltip v-else trigger="hover" :delay="300"><template #trigger><n-button size="small" :disabled="archiving || p.dir_exists === false" @click.stop="runProject(p)">运行项目…</n-button></template>{{ projectRunHint(p) }}</n-tooltip>
              <n-dropdown :options="projectMenu(p)" trigger="click" @select="(action: string) => projectAction(action, p)"><n-button size="small" quaternary :loading="deletingUids.has(p.uid)" :disabled="archiving || deletingUids.has(p.uid)" :aria-label="`${p.title}的更多操作`">{{ deletingUids.has(p.uid) ? '正在删除…' : '更多 ···' }}</n-button></n-dropdown>
            </div>
          </div>
          <ProjectTemplateBinding v-if="!p.archived && !viewingArchive" :uids="[p.uid]" compact :summary="p.project_settings"
            :disabled="archiving || deletingUids.has(p.uid) || projectBusy(p) || p.dir_exists === false" @changed="projectStateChanged" />
          <div v-if="p.project_settings" class="project-delivery-summary">
            <RouterLink :to="`/projects/${p.uid}/publish#project-rules`">交付：{{ projectDeliverySummary(p.project_settings,p.workflow_status).delivery }}</RouterLink>
            <span v-if="projectDeliverySummary(p.project_settings,p.workflow_status).target">{{ projectDeliverySummary(p.project_settings,p.workflow_status).target }}</span>
          </div>
          <!-- 每段对应共享工作阶段；实际执行范围仍以本次运行预检为准。 -->
          <div class="segwrap">
            <div class="stage-summary"><span>{{ WORKFLOW_STAGES.length }} 个工作阶段</span><span v-if="!projectFreshnessKnown(p)" title="摘要不检查文件与输入指纹；完整核验在后台进行。">产物待核对</span><span v-else title="按最近完整核验统计；未使用的阶段不算失败，也不要求本次全部执行"><template v-if="p.projection === 'summary'">上次核验 · </template>已完成 {{ completedStages(p) }} 个</span></div>
            <div v-if="projectStartFeedbackOf(p)" class="project-start-feedback" :class="{warning:projectStartFeedbackOf(p)?.warning}" role="status" aria-live="polite" aria-atomic="true" :aria-busy="projectStartFeedbackOf(p)?.pending">
              <n-spin v-if="projectStartFeedbackOf(p)?.pending" :size="12" />
              <span>{{ projectStartFeedbackOf(p)?.message }}</span>
            </div>
            <div class="segbar" aria-label="项目工作阶段">
              <n-tooltip v-for="st in orderedProjectSteps(p)" :key="st.key" trigger="hover"
                         :delay="0" :duration="0" placement="top">
                <template #trigger>
                    <button type="button" class="seg" :data-workflow-stage="st.key" :class="'seg-' + st.status" :aria-label="`第${st.number}阶段 ${st.label}：${st.statusText || projectStepStatus(st)}${st.key === 'tts' && subtitleExportNotice(p) ? '；' + subtitleExportNotice(p) : ''}，${st.kind === 'publication' ? '进入发布页' : st.kind === 'comments' ? '进入评论页' : '进入步骤'}`"
                       @click.stop="router.push(stepPath(p, st.key))">
                    <span class="seg-number">{{ st.number }}</span><span class="seg-label">{{ st.label }}</span>
                    <div class="fill" v-if="st.status === 'running' && !st.progress_indeterminate && Number.isFinite(st.progress)"
                         :style="{ width: (st.progress || 0) + '%' }" />
                  </button>
                </template>
                第 {{ st.number }} 阶段 · {{ st.label }} · {{ st.statusText || projectStepStatus(st)
                }}{{ st.status === 'running' ? (st.progress_indeterminate ? (st.progress_phase ? ` · ${st.progress_phase}` : '') : Number.isFinite(st.progress) ? ` ${st.progress_estimated ? '预计 ' : ''}${Math.round(st.progress)}%` : '') : ''
                }}{{ st.detail ? ` — ${st.detail}` : STATUS_HINTS[st.status]
                ? ` — ${STATUS_HINTS[st.status]}` : '' }}{{ st.key === 'tts' && subtitleExportNotice(p)
                ? `；${subtitleExportNotice(p)}` : '' }}
                <n-text depth="3" style="font-size: 11px">(点击进入工作区)</n-text>
              </n-tooltip>
            </div>
            <n-text depth="3" style="font-size: 11px">
              项目运行按目标规划；未使用的阶段无需全部完成
              <span v-if="subtitleExportNotice(p)" class="subtitle-progress-note" :class="{ attention: subtitleExportNeedsAttention(p) }"> · {{ subtitleExportNotice(p) }}</span>
            </n-text>
            <ExecutionProgress v-for="task in currentProjectProcess(p)" :key="task.key" :execution="task.execution" :percentage="task.progress" :estimated="task.progress_estimated" :indeterminate="task.progress_indeterminate" compact />
            <PublicationProgress :publication="projectCurrentPublication(p)" compact />
            <div v-if="p.project_batch" class="chain-line release-batch-progress" :class="{'release-batch-attention': !projectRequestOf(p) && p.project_batch.group_status === 'attention'}">
              <span>批量运行 · {{ projectRequestOf(p)?.message || p.project_batch.phase || p.project_batch.title }}<span v-if="p.project_batch.position"> · 第 {{ p.project_batch.position }}/{{ p.project_batch.total }} 项</span></span>
              <router-link :to="`/#project-batch-${p.project_batch.batch_id}`">查看批次 →</router-link>
            </div>
            <div v-else-if="p.release_batch" class="chain-line release-batch-progress" :class="{'release-batch-attention': p.release_batch.status === 'attention' || p.release_batch.item_status === 'attention'}">
              <span>顺序发布 · 第 {{ p.release_batch.position }}/{{ p.release_batch.total }} 项<span v-if="p.release_batch.phase"> · {{ p.release_batch.phase }}</span><template v-if="p.release_batch.item_status === 'attention' || p.release_batch.status === 'attention'"> · 需要处理</template></span>
              <small v-if="p.release_batch.error">{{ p.release_batch.error }}</small>
              <router-link :to="`/#release-batch-${p.release_batch.id}`">查看批次 →</router-link>
            </div>
            <div v-if="chainOf(p) && !projectRequestOf(p)" class="chain-line">
              <span :class="chainOf(p)!.state === 'held' ? 'chain-held' : 'chain-active'"
                    :title="chainOf(p)!.state === 'held' ? holdDescription(chainOf(p)!, STEP_LABELS) : '本步完成后自动继续剩余计划'">{{ chainSummary(p) }}</span>
            </div>
          </div>
        </n-card>
      </div>
    </n-spin>
    <ProjectRunPanel ref="stepRun" hidden-trigger allow-workflow @started="productionStarted" @run-feedback="receiveRunFeedback" />

    <n-modal v-model:show="showNew" preset="card" title="新建项目"
             :closable="!creating && !ingesting && !abortingIngest && !ingestErr" :mask-closable="!creating && !ingesting && !abortingIngest && !ingestErr" :close-on-esc="!creating && !ingesting && !abortingIngest && !ingestErr"
             style="width: min(560px, calc(100vw - 32px)); max-width: calc(100vw - 32px)"
             @after-leave="resetNewForm">
      <div @dragover.prevent @drop.prevent="onDrop">
        <n-form class="new-project-form" label-placement="top" :disabled="creating || ingesting || abortingIngest || !!ingestErr">
          <n-form-item label="来源类型">
            <n-radio-group v-model:value="form.source_kind">
              <n-radio value="link">视频链接 (YouTube / B站 等网站)</n-radio>
              <n-radio value="local">本地视频导入</n-radio>
            </n-radio-group>
          </n-form-item>
          <n-form-item :label="form.source_kind === 'local' ? '本地路径' : '视频链接'">
            <n-input-group>
              <n-input v-model:value="form.source_url"
                       :placeholder="form.source_kind === 'local'
                         ? 'D:\\videos\\my.mp4 · 或点右侧「📂 选择文件」'
                         : '支持 YouTube / Bilibili / Twitter 等数千个站点 (由 yt-dlp 支持)'" />
              <n-button v-if="form.source_kind === 'local'" @click="openBrowse">
                📂 选择文件
              </n-button>
            </n-input-group>
          </n-form-item>
          <n-form-item label="标题 (可选)">
            <n-input v-model:value="form.title"
                     placeholder="留空自动获取: 优先用视频标题, 其次 文件名/站点+ID" />
          </n-form-item>
        </n-form>
        <div v-if="ingesting" style="margin-top: 10px">
          <n-progress type="line" :percentage="ingestPct" indicator-placement="inside" processing />
          <n-text depth="3" style="font-size:12px; display:block; margin-top:4px">
            {{ ingestPct >= 100 ? '视频已传送，正在等待项目文件校验与登记' : '正在把视频副本传送到项目目录' }}<template v-if="pendingFile"> · {{ pendingFile.name }} · {{ fmtSize(pendingFile.size) }}</template>
          </n-text>
        </div>
        <n-text v-else depth="3" style="font-size:12px; display:block; line-height:1.8">
          <template v-if="pendingFile">
            已拖入: {{ pendingFile.name }} ({{ fmtSize(pendingFile.size) }}) —— 创建项目时复制一份进项目目录 media\, 随项目存亡
          </template>
          <template v-else>
            提示: 把视频文件<b>拖进本窗口</b> —— 浏览器不提供原文件路径, 内容会作为副本存进项目目录 (随项目存亡)。<br />
            点「📂 选择文件」或<b>直接粘贴路径</b> —— 零拷贝引用: 只登记原位置, 不复制、不改不动原文件, 大文件推荐这种方式。
          </template>
        </n-text>
        <n-alert v-if="ingestErr" type="error" :closable="!abortingIngest && !ingesting" style="margin-top: 8px"
                 @close="ingestErr = ''">
          视频导入失败: {{ ingestErr }}
        </n-alert>
        <n-alert v-if="ingestAbortError" type="warning" :show-icon="false" style="margin-top: 8px" role="status">未能放弃导入：{{ ingestAbortError }} 项目和导入文件已保留，可重试删除或继续导入。</n-alert>
      </div>
      <template #footer>
        <n-space v-if="ingestErr" justify="end">
          <n-button :loading="abortingIngest" :disabled="ingesting || abortingIngest" @click="abortIngest">{{ ingestAbortError ? '重试删除项目' : '放弃并删除项目' }}</n-button>
          <n-button type="primary" :loading="ingesting" :disabled="abortingIngest" @click="runIngest">重试导入</n-button>
        </n-space>
        <n-space v-else justify="end">
          <n-button :disabled="creating || ingesting || abortingIngest" @click="showNew = false">取消</n-button>
          <n-button type="primary" :loading="creating || ingesting" :disabled="abortingIngest || !pendingFile && !form.source_url.trim()" @click="create">创建项目</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="showBrowse" preset="card" title="选择视频文件"
             style="width: min(680px, calc(100vw - 32px)); max-width: calc(100vw - 32px)">
      <div class="browse-toolbar" style="display:flex;gap:8px;margin-bottom:8px">
        <n-input v-model:value="browsePath" size="small" placeholder="输入文件夹路径后回车"
                 @keyup.enter="browseTo(browsePath)" />
        <n-button size="small" @click="browseTo(browsePath)">跳转</n-button>
        <FilePathLink v-if="browsePath" :path="browsePath" label="资源管理器" />
        <n-button size="small" :disabled="!browseParent" @click="browseTo(browseParent || '')">
          上级
        </n-button>
      </div>
      <div class="browse-list">
        <div v-for="e in browseEntries" :key="e.path" class="browse-item"
             :class="{ sel: browseSel?.path === e.path }"
             @click="e.is_dir ? browseTo(e.path) : (browseSel = e)"
             @dblclick="!e.is_dir && browseConfirm()">
          <span>{{ e.is_dir ? '📁' : (e.video ? '🎬' : '📄') }}</span>
          <span style="flex:1;margin-left:8px;overflow:hidden;text-overflow:ellipsis">
            {{ e.name }}
          </span>
          <n-text v-if="!e.is_dir" depth="3" style="font-size:11px">
            {{ fmtSize(e.size) }}
          </n-text>
          <FilePathLink :path="e.path" label="定位" />
        </div>
        <n-empty v-if="!browseEntries.length" size="small" description="(空文件夹)" />
      </div>
      <template #footer>
        <n-space justify="space-between" align="center">
          <n-text depth="3" style="font-size:12px">
            {{ browseSel ? `已选: ${browseSel.name}` : '单击进入文件夹 / 选中文件, 双击文件直接确定' }}
          </n-text>
          <n-space>
            <n-button @click="showBrowse = false">取消</n-button>
            <n-button type="primary" :disabled="!browseSel" @click="browseConfirm">确定</n-button>
          </n-space>
        </n-space>
      </template>
    </n-modal>

    <n-modal :show="!!renaming" preset="card" title="重命名项目"
             :mask-closable="!renameSaving" :closable="!renameSaving" :close-on-esc="!renameSaving"
             style="width: min(460px, calc(100vw - 32px)); max-width: calc(100vw - 32px)"
             @update:show="v => !v && (renaming = null)">
      <n-input v-model:value="renameTitle" maxlength="200" show-count
               :disabled="renameLocked"
               placeholder="输入新的标题" @keyup.enter="applyRename" />
      <p class="rename-hint">手动保存的名称会保留；使用视频标题后，获取资料时会自动更新，优先使用中文标题。</p>
      <n-alert v-if="renameLocked && !renameSaving" type="info" :show-icon="false">{{ renameLockReason || '项目正在运行，请完成后再改名' }}</n-alert>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="renameSaving" @click="renaming = null">取消</n-button>
          <n-button :disabled="renameLocked" @click="useVideoTitle">使用视频标题</n-button>
          <n-button type="primary" :loading="renameSaving" :disabled="renameLocked" @click="applyRename">应用</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.rename-hint { margin: 10px 0; color: var(--df-muted); font-size: 12px; line-height: 1.7; }
.archive-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; margin: 20px 0 16px; }
.archive-tabs { display: inline-flex; padding: 4px; gap: 4px; border: 1px solid var(--df-border); border-radius: 10px; background: var(--df-panel); }
.archive-tabs button { padding: 7px 16px; border: 0; border-radius: 7px; color: var(--df-muted); background: transparent; cursor: pointer; font: inherit; font-size: 13px; }
.archive-tabs button[aria-selected=true] { color: #c4ddff; background: #25374d; }
.archive-tabs button:disabled { opacity: .6; cursor: wait; }
.archive-description { font-size: 12px; color: var(--df-muted); }
.archive-result { margin: 12px 0 16px; }
.archive-result ul { margin: 8px 0 0; padding-left: 20px; }
.load-error { margin-bottom: 16px; }
.selection-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 18px 0 14px; }
.selection-bar > .n-button:last-child { margin-left: auto; }
.records-entry { display: inline-flex; align-items: center; min-height: 34px; padding: 0 8px; color: var(--df-muted); font-size: 12px; text-decoration: none; }
.records-entry:hover { color: var(--df-accent); }
.list-count { font-size: 12px; color: var(--df-muted); }
.filter-toolbar { padding: 14px; background: var(--df-panel); border: 1px solid var(--df-border); border-radius: 12px; justify-content: flex-start; }
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 14px;
}
.page-subtitle { display: block; margin-top: 8px; font-size: 13px; line-height: 1.8; }
.toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 8px;
}
.project-search { width: 280px; flex: 1; min-width: 180px; }
.summary-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 14px;
  margin: 26px 0 24px;
}
.summary-item { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; border: 1px solid var(--df-border); border-radius: 12px; background: var(--df-panel); padding: 18px 22px; text-align: left; color: var(--df-muted); cursor: pointer; }
.summary-item:hover, .summary-item[aria-pressed=true] { border-color: #597ca8; background: #1d2837; }
.summary-item strong { font-size: 30px; font-weight: 600; line-height: 1.2; color: var(--df-text); font-variant-numeric: tabular-nums; }
.summary-done strong { color: #78c9a5; }
.summary-processing strong { color: var(--df-accent); }
.summary-failed strong { color: #efbd70; }
.summary-published strong { color: #78c9a5; }
.summary-caption { font-size: 11px; color: #8392a6; }
.pending-filter-note { display:flex; align-items:center; flex-wrap:wrap; gap:6px 12px; margin:12px 0; color:var(--df-muted); font-size:12px; line-height:1.7; }
.info-number { color: #63b3ed; }
.success-number { color: #63d391; }
.error-number { color: #f08080; }
.result-count { margin-left: auto; font-size: 12px; }
.project-list { display: flex; flex-direction: column; gap: 12px; }
.project-card { transition: border-color .18s ease, background .18s ease; }
.project-card:hover { border-color: #4c5c71; }
.project-card.selected { border-color: #83b5ff; background: #1b2736; }
.project-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.project-main { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.project-title-block { min-width: 0; }
.title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.project-title { max-width: min(50vw, 640px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; font-weight: 600; text-decoration: none; color: var(--df-text); }
.project-title:hover { color: var(--df-accent); }
.publication-link { text-decoration:none; }
.project-source {
  display: block;
  max-width: min(58vw, 760px);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.project-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.browse-list {
  height: 340px;
  overflow-y: auto;
  border: 1px solid #2a343f;
  border-radius: 6px;
}
.browse-item {
  display: flex;
  align-items: center;
  padding: 6px 10px;
  cursor: pointer;
  gap: 4px;
}
.browse-item:hover {
  background: rgba(255, 255, 255, 0.06);
}
.browse-item.sel {
  background: rgba(24, 160, 88, 0.25);
}
.segwrap { margin-top: 18px; }
.stage-summary { display:flex;justify-content:space-between;gap:12px;margin-bottom:8px;color:var(--df-muted);font-size:12px; }
.project-start-feedback { display:flex;align-items:center;gap:8px;margin:8px 0;color:var(--df-accent);font-size:12px;line-height:1.8;overflow-wrap:anywhere; }
.project-start-feedback.warning { color:var(--df-warning,#dfb66f); }
.project-start-feedback>span { min-width:0; }
.project-start-feedback :deep(.n-spin-body) { position:static;transform:none;flex:none; }
.chain-line { display: flex; align-items: center; gap: 10px; margin-top: 6px; font-size: 11px; flex-wrap: wrap; }
.chain-line .chain-held { color: #efbd70; }
.chain-line .chain-active { color: var(--df-accent, #6aa2ff); }
.release-batch-progress small { color: var(--df-muted); overflow-wrap: anywhere; }
.release-batch-progress.release-batch-attention > span { color: var(--df-danger, #d97777); }
.segbar { display: flex; gap: 3px; }
.subtitle-progress-note { color: var(--df-muted); }
.subtitle-progress-note.attention { color: var(--df-danger, #d97777); }
.seg {
  display:flex;align-items:center;justify-content:center;gap:5px;flex:1;min-width:0;height:32px;border-radius:5px;border:1px solid transparent;padding:0 3px;color:var(--df-text);
  background: rgba(128, 128, 128, 0.25);
  overflow: hidden; cursor: pointer; position: relative;
}
.seg-number,.seg-label{position:relative;z-index:1;font-size:11px;line-height:1.4;white-space:nowrap}.seg-number{font-variant-numeric:tabular-nums}.seg-label{overflow:hidden;text-overflow:ellipsis}
.seg:hover { outline: 1px solid rgba(255, 255, 255, 0.4); }
.seg:focus-visible { outline:2px solid var(--df-accent);outline-offset:2px; }
.seg-done { background:rgba(46,164,79,.18);border-color:rgba(46,164,79,.45); }
.project-delivery-summary{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;margin:8px 0 0;font-size:12px;line-height:1.7;color:var(--df-muted)}.project-delivery-summary a{color:var(--df-text);text-decoration:none}.project-delivery-summary a:hover{color:var(--df-accent);text-decoration:underline}
.seg-failed, .seg-interrupted { background:rgba(229,83,75,.18);border-color:rgba(229,83,75,.5); }
.seg-stale,.seg-attention { background:rgba(210,153,34,.18);border-color:rgba(210,153,34,.5); }
.seg-skipped { background: #667085; }
.seg-running { background: rgba(77, 163, 255, 0.25); }
.seg-unknown { border-style:dashed;border-color:var(--df-border); }
.seg-running .fill { position:absolute;inset:0 auto 0 0;background:rgba(77,163,255,.35); }
@media(max-width:1100px){.seg-label{display:none}}
@media (max-width: 900px) {
  .page-head { flex-direction: row; flex-wrap: wrap; }
  .toolbar { justify-content: flex-start; width: 100%; }
  .project-search { width: min(100%, 280px); }
  .summary-strip { flex-wrap: wrap; gap: 12px 20px; }
  .result-count { width: 100%; margin-left: 0; }
  .project-head { flex-direction: column; }
  .project-actions { justify-content: flex-start; padding-left: 28px; }
  .project-title, .project-source { max-width: 100%; }
  .project-main { width: 100%; }
  .summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .summary-strip { align-items: flex-start; }
  .summary-item { padding: 14px; }
  .summary-caption { display: none; }
  .selection-bar > .n-button:last-child { margin-left: 0; }
  .browse-toolbar { flex-wrap: wrap; }
  .browse-toolbar :deep(.n-input) { flex: 1 1 100%; }
  :deep(.new-project-form .n-form-item) { display: block; }
  :deep(.new-project-form .n-form-item-label) { width: 100% !important; padding-bottom: 4px; }
  :deep(.new-project-form .n-form-item-blank) { width: 100%; }
}
</style>
