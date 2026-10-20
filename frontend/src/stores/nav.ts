import { defineStore } from 'pinia'
import { effectScope, watch } from 'vue'
import { setNavigationExecution } from '../utils/configurationEditability'
import { createLatestRefresh } from '../utils/preview'
import { WORKFLOW_BUSY, workflowPublication } from '../utils/workflow'
import { latestJobActivity, stepActivity, workflowComments, type ProgressActivity } from '../utils/progressActivity'
import { executionForWorkflow, workflowCurrentStep } from '../utils/executionProgress'
import { parseChain, type ChainInfo } from '../utils/chainStatus'
import { isNetworkError } from '../utils/http'
import { readCommentProgress } from '../utils/commentProgressSnapshot'
import { commentStageDisplay, WORKFLOW_STAGES } from '../utils/workflowStages'
import { createExecutionProjection, hasActiveExecution } from '../utils/executionSnapshot'

// 流程导航共享状态: MainLayout (左侧步骤导航) 与 StepView/PipelineView 共用一份轮询,
// 避免多处各自 fetch 造成状态不同步
export const STEP_ORDER = ['download', 'preprocess', 'transcribe', 'split',
  'translate', 'tts', 'subtitles', 'assemble'] as const
// 真实制作阶段子集；九阶段工作区展示统一使用 WORKFLOW_STAGES。
export const NAV_STEP_ORDER = WORKFLOW_STAGES.filter(stage=>stage.kind==='step').map(stage=>stage.taskKeys[0] as typeof STEP_ORDER[number])

// 首屏不依赖状态接口才知道中文名称；加载时也不暴露内部键名。
export const STEP_LABELS: Record<string, string> = {
  ...Object.fromEntries(WORKFLOW_STAGES.map(stage=>[stage.key,stage.label])),subtitles:'字幕导出',
}

export const STATUS_TEXT: Record<string, string> = {
  pending: '待运行', queued: '⏳ 排队中', running: '运行中', done: '✓ 完成',
  failed: '✗ 失败', stale: '⟳ 已过期', interrupted: '已中断', skipped: '已跳过',
  stopped: '已停止', pausing: '正在暂停', paused: '已暂停',
  checking: '任务已结束，产物核验中',
}

export const STATUS_HINTS: Record<string, string> = {
  pending: '尚未生成产物；运行后会按当前输入和设置创建。',
  queued: '已加入执行队列；等待前序步骤完成后开始。',
  running: '正在生成或更新产物。',
  done: '上次运行成功；结果是否仍然最新请看旁边的“已是最新/需重跑”标签。',
  failed: '上次运行失败；可查看步骤日志并重新运行。',
  stale: '输入、上游产物或设置已变化；需要重新运行才能得到当前配置的结果。',
  interrupted: '上次运行被中断；重新运行会从安全检查点继续。',
  skipped: '可选步骤未启用；当前链路会跳过它。',
  stopped: '已按你的操作停止；可查看日志，或重新运行以继续处理。',
  checking: '当前任务已结束；正在核对设置、输入及磁盘产物。核验完成前暂不能重跑。',
  pausing: '已收到暂停请求，当前处理完成后暂停。',
  paused: '已停下等待，点击“继续”恢复执行。',
}

export const FRESHNESS_HINTS = {
  fresh: '产物存在，且输入、上游结果和设置自上次成功运行后没有变化。',
  stale: '产物来自旧输入或旧设置；重新运行后会按当前配置重新生成。',
}

export const STATUS_TAG: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  pending: 'default', queued: 'warning', running: 'info', done: 'success',
  failed: 'error', stale: 'warning', interrupted: 'error', skipped: 'default',
  stopped: 'default', pausing: 'warning', paused: 'warning',
  checking: 'default',
}

type Loader = ReturnType<typeof createLatestRefresh>
type PollSource = 'project' | 'steps' | 'publication' | 'workflow' | 'comments' | 'execution'
const SOURCE_LABELS: Record<PollSource, string> = { project: '项目信息', steps: '步骤状态', publication: '发布状态', workflow: '自动流程状态', comments:'评论状态', execution:'实时进度' }
type ReadFailure = { kind: 'network' | 'timeout' | 'response'; message: string }
// 与领域 project_status 保持一致，复用已经取得的步骤状态，不另算一遍全部输入。
function projectStatus(steps: any[]): string {
  if (steps.some(step => ['running','queued'].includes(step.status))) return 'processing'
  if (steps.some(step => step.status === 'checking')) return 'checking'
  const chosen = steps.filter(step => step.selected)
  if (chosen.some(step => step.status === 'failed')) return 'failed'
  return chosen.length && chosen.every(step => step.fresh) ? 'done' : 'created'
}

function hasExecutionOrOwner(nav: any): boolean {
  return WORKFLOW_BUSY.includes(nav.workflow?.run?.status) || nav.chain?.state === 'active'
    || ['running','pausing','stopping'].includes(nav.releaseBatch?.status)
    || ['queued','preparing','uploading','submitting','joining'].includes(nav.publication?.status)
    || nav.steps.some((step: any) => ['running','queued','stopping'].includes(step.status))
}
const pollers = new WeakMap<object, { project: Loader; steps: Loader; publication: Loader; workflow: Loader; comments:Loader; activity: Loader; workflowPlan: Loader; execution:Loader }>()
const executionWatches = new WeakMap<object, ReturnType<typeof effectScope>>()
const executionProjections = new WeakMap<object, ReturnType<typeof createExecutionProjection>>()
const executionVisibility = new WeakMap<object, () => void>()
function projection(store: object) {
  let value = executionProjections.get(store)
  if (!value) { value = createExecutionProjection(); executionProjections.set(store, value) }
  return value
}
function visible() { return typeof document === 'undefined' || !document.hidden }
function wantsExecution(store: any) {
  return store.uid && !store.suspended && !store.archived && !store.executionBlocked && !store.executionUnavailable && (hasActiveExecution(store.steps)
    || WORKFLOW_BUSY.includes(store.workflow?.run?.status) || store.chain?.state === 'active')
}
function executionTimer(store: any) {
  if (!wantsExecution(store) || !visible()) {
    if (store.executionTimer) { clearInterval(store.executionTimer); store.executionTimer = 0 }
    pollers.get(store)?.execution.cancel()
    return
  }
  if (!store.executionTimer) store.executionTimer = setInterval(() => store.pollExecution(), 1500)
}
function cancelPollers(store: object) {
  const readers = pollers.get(store)
  if (readers) for (const reader of Object.values(readers)) reader.cancel()
}
function getPollers(store: any) {
  let readers = pollers.get(store)
  if (!readers) {
    const reader = (kind: Exclude<PollSource, 'execution'>) => createLatestRefresh(async signal => {
      const uid = store.uid
      const selectionVersion = store.autoStepsVersion
      const executionRevision = projection(store).revision
      if(kind==='comments') {
        const response=await readCommentProgress(uid,signal)
        if(!response.ok) throw Object.assign(new Error(`评论状态读取失败（HTTP ${response.status}）`),{readFailure:true})
        if(!response.value || typeof response.value!=='object' || Array.isArray(response.value))
          throw Object.assign(new Error('评论状态数据格式不正确'),{readFailure:true})
        return {uid,value:response.value,selectionVersion,executionRevision}
      }
      const url = kind === 'publication' ? `/api/publishing/projects/${uid}/status` : `/api/projects/${uid}${kind === 'steps' ? '/steps' : kind === 'workflow' ? '/workflow/status' : '/navigation'}`
      // /steps 会在长片生成期间核对逐句产物，后端现在对同一项目做
      // single-flight；给这一路径更宽的读取窗口，避免把“后端仍在核对”
      // 误报成断线。其余导航状态仍保持较短超时，及时暴露连接问题。
      const timeout = kind === 'steps' ? 30000 : 15000
      const response = await fetch(url, {
        cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(timeout)]),
      })
      let value: any
      try { value = await response.json() }
      catch { throw Object.assign(new Error(response.ok ? '服务返回了无法解析的状态数据' : `状态接口返回 HTTP ${response.status}`), { readFailure: true }) }
      if (!response.ok) throw Object.assign(new Error(`${value?.message || '状态接口请求失败'}（HTTP ${response.status}）`), { readFailure: true })
      const object = (item: any) => item && typeof item === 'object' && !Array.isArray(item)
      if (kind === 'steps' ? !Array.isArray(value) || !value.every(object) : !object(value)) {
        throw Object.assign(new Error('服务返回的状态数据格式不正确'), { readFailure: true })
      }
      return { uid, value, selectionVersion, executionRevision }
    }, ({ uid, value, selectionVersion, executionRevision }) => {
      if (uid !== store.uid || store.executionBlocked) return
      if (kind === 'steps') {
        value = projection(store).mergeFull(value, executionRevision ?? projection(store).revision)
        if (store.autoStepsSaving || selectionVersion !== store.autoStepsVersion) {
          value = value.map((step: any) => ({ ...step, selected: store.autoSteps ? store.autoSteps.includes(step.key) : true }))
        }
        const previous = store.steps.filter((s: any) => s.status === 'running').map((s: any) => `${s.key}:${s.job_id}`).join(',')
        store.steps = value; store.stepsLoaded = true; store.stepsError = store.executionBlocked ? store.executionError : ''; store.status = projectStatus(value)
        const active = value.filter((s: any) => s.status === 'running' && s.job_id)
        store.activities = Object.fromEntries(Object.entries(store.activities).filter(([key, entry]: any) => active.some((s: any) => s.key === key && s.job_id === entry.jobId)))
        if (active.length) void getPollers(store).activity.refresh(previous !== active.map((s: any) => `${s.key}:${s.job_id}`).join(','))
        else getPollers(store).activity.cancel()
      }
      else if (kind === 'publication') { store.publication = value; store.publicationError = '' }
      else if (kind === 'comments') { store.commentState=value;store.commentsError='' }
      else if (kind === 'workflow') {
        const changed = store.workflow?.run?.id !== value.run?.id
        store.workflow = value; store.workflowError = ''
        if (changed) { store.workflowPlan = null; getPollers(store).workflowPlan.cancel() }
        if (value.run?.id && store.workflowPlan?.id !== value.run.id) void getPollers(store).workflowPlan.refresh(false)
      }
      else {
        store.title = value.title; store.status = store.stepsLoaded ? projectStatus(store.steps) : value.status
        store.dirMissing = value.dir_exists === false
        store.archived = value.archived === true
        store.archivedAt = value.archived_at || null
        if (!store.autoStepsSaving && selectionVersion === store.autoStepsVersion) {
          store.autoSteps = Array.isArray(value.auto_steps) ? value.auto_steps : null
        }
        store.chain = parseChain(value.chain)
        store.releaseBatch = value.release_batch || null
        store.projectError = ''
      }
      store.statusCheckedAt[kind] = new Date().toISOString()
      delete store.readFailures[kind]
      store.connectionLost = Object.values(store.readFailures).some((failure: any) => failure.kind === 'network')
    }, error => {
      if (store.executionBlocked) return
      const failure: ReadFailure = isNetworkError(error) ? { kind: 'network', message: '连接失败' }
        : error instanceof Error && error.name === 'TimeoutError' ? { kind: 'timeout', message: '请求超时' }
          : { kind: 'response', message: error instanceof Error ? error.message : '状态读取失败' }
      store.readFailures[kind] = failure
      store.connectionLost = Object.values(store.readFailures).some((item: any) => item.kind === 'network')
      const checked = store.statusCheckedAt[kind]
      const previous = checked ? `保留上次核对结果（${new Date(checked).toLocaleTimeString('zh-CN', { hour12: false })}）。` : '尚未取得该状态。'
      store[kind === 'steps' ? 'stepsError' : kind === 'publication' ? 'publicationError' : kind === 'workflow' ? 'workflowError' : kind==='comments'?'commentsError':'projectError'] = `${SOURCE_LABELS[kind]}未更新：${failure.message}。${previous}请重试。`
    })
    const activity = createLatestRefresh(async signal => {
      const uid = store.uid
      const jobs = store.steps.filter((s: any) => s.status === 'running' && s.job_id).map((s: any) => ({ key: s.key, jobId: s.job_id }))
      const entries = await Promise.all(jobs.map(async (job: any) => {
        try {
          const response = await fetch(`/api/projects/${uid}/steps/${job.key}/log?tail=24`, { cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]) })
          if (!response.ok) throw new Error('动态读取失败')
          const data = await response.json()
          return { ...job, activity: latestJobActivity(data.lines, job.jobId), unavailable: false }
        } catch { return { ...job, activity: null, unavailable: true } }
      }))
      return { uid, entries }
    }, ({ uid, entries }) => {
      if (uid !== store.uid) return
      for (const entry of entries) {
        const step = store.stepMap[entry.key]
        if (step?.status === 'running' && step.job_id === entry.jobId) store.activities[entry.key] = entry
      }
    }, () => {})
    // 冻结计划每个运行只读取一次；不把完整配置/成品扫描放进高频状态轮询。
    const workflowPlan = createLatestRefresh(async signal => {
      const uid = store.uid, id = store.workflow?.run?.id
      const response = await fetch(`/api/projects/${uid}/workflow`, { cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]) })
      if (!response.ok) throw new Error('读取运行计划失败')
      const data = await response.json()
      return { uid, id, run: data.runs?.find((r: any) => r.id === id) }
    }, ({ uid, id, run }) => {
      if (uid === store.uid && id === store.workflow?.run?.id && run) store.workflowPlan = { id, ...run.plan, recipe: run.recipe }
    }, () => {})
    const execution = createLatestRefresh(async signal => {
      const uid = store.uid
      const response = await fetch(`/api/projects/execution?uids=${encodeURIComponent(uid)}`, {
        cache:'no-store', signal:AbortSignal.any([signal, AbortSignal.timeout(8000)]),
      })
      // 前端热更新可能先于后端重启；旧服务不支持新接口时继续使用原有完整状态轮询。
      if (response.status === 404) return {uid, row:null, workspace:'', unsupported:true}
      if (!response.ok) throw new Error(`实时进度读取失败（HTTP ${response.status}）`)
      const data = await response.json()
      if (!Array.isArray(data?.projects) || typeof data.workspace_id !== 'string' || !data.workspace_id.trim()) throw new Error('实时进度数据格式不正确')
      const workspaceHeader = response.headers.get('X-DubForge-Workspace')
      if (workspaceHeader && workspaceHeader !== data.workspace_id) throw new Error('实时进度工作目录身份不一致')
      const row = data.projects.find((entry:any) => entry?.uid === uid)
      if (row && !Array.isArray(row.steps)) throw new Error('实时进度数据格式不正确')
      return { uid, row, workspace:data.workspace_id, unsupported:false }
    }, ({uid, row, workspace, unsupported}) => {
      if (uid !== store.uid || store.suspended || !visible()) return
      if (unsupported) {
        store.executionUnavailable = true
        getPollers(store).execution.cancel()
        store.executionError = ''
        delete store.readFailures.execution
        store.connectionLost = Object.values(store.readFailures).some((failure:any) => failure.kind === 'network')
        return
      }
      if (store.executionWorkspace && store.executionWorkspace !== workspace) {
        // 同名项目不能跨工作目录复用实时状态；保留当前页面及编辑，等待用户重新进入。
        store.executionBlocked = true
        projection(store).reset()
        cancelPollers(store)
        store.executionError = '项目工作目录已变化，请重新打开项目。'
        store.stepsError = store.executionError
        store.readFailures.execution = {kind:'response', message:store.executionError}
        store.connectionLost = Object.values(store.readFailures).some((failure:any) => failure.kind === 'network')
        return
      }
      store.executionWorkspace = workspace
      if (row) {
        const previous = store.steps.filter((step:any) => step.status === 'running').map((step:any) => `${step.key}:${step.job_id}`).join(',')
        const result = projection(store).apply(store.steps, row.steps)
        store.steps = result.steps
        store.status = projectStatus(store.steps)
        const active = store.steps.filter((step:any) => step.status === 'running' && step.job_id)
        store.activities = Object.fromEntries(Object.entries(store.activities).filter(([key, entry]:any) => active.some((step:any) => step.key === key && step.job_id === entry.jobId)))
        if (previous !== active.map((step:any) => `${step.key}:${step.job_id}`).join(',')) {
          if (active.length) void getPollers(store).activity.refresh(true)
          else getPollers(store).activity.cancel()
        }
        // 终态与任务替换只补一次完整核对；普通高频 tick 不扫描产物。
        if (result.needsRefresh) void getPollers(store).steps.refresh(false)
      }
      store.executionError = ''
      delete store.readFailures.execution
      store.statusCheckedAt.execution = new Date().toISOString()
      store.connectionLost = Object.values(store.readFailures).some((failure:any) => failure.kind === 'network')
    }, error => {
      const kind:ReadFailure['kind'] = isNetworkError(error) ? 'network' : error instanceof Error && error.name === 'TimeoutError' ? 'timeout' : 'response'
      store.readFailures.execution = {kind, message:kind === 'network' ? '连接失败' : kind === 'timeout' ? '请求超时' : error instanceof Error ? error.message : '读取失败'}
      store.executionError = '实时进度暂未更新，保留上次结果。'
      store.connectionLost = Object.values(store.readFailures).some((failure:any) => failure.kind === 'network')
    })
    readers = { project: reader('project'), steps: reader('steps'), publication: reader('publication'), workflow: reader('workflow'), comments:reader('comments'), activity, workflowPlan, execution }
    pollers.set(store, readers)
  }
  return readers
}

export const useNavStore = defineStore('nav', {
  state: () => ({
    uid: '',
    suspended: false,
    title: '',
    archived: false,
    archivedAt: null as string | null,
    status: '',
    steps: [] as any[],
    stepsLoaded: false,
    stepsError: '',
    projectError: '',
    publication: null as any,
    publicationError: '',
    commentState:null as any,
    commentsError:'',
    executionError:'',
    executionWorkspace:'',
    executionBlocked:false,
    executionUnavailable:false,
    connectionLost: false,             // 仅表示状态来源发生网络异常，不推断后台任务终态
    readFailures: {} as Partial<Record<PollSource, ReadFailure>>,
    statusCheckedAt: {} as Partial<Record<PollSource, string>>,
    workflow: null as any,
    workflowError: '',
    workflowPlan: null as any,
    activities: {} as Record<string, { jobId: number; activity: ProgressActivity | null; unavailable: boolean }>,
    dirMissing: false,
    autoSteps: null as string[] | null,   // 制作勾选集 (null = 全部, 未自定义过)
    autoStepsSaving: false,
    autoStepsVersion: 0,
    chain: null as ChainInfo | null,      // 自动续跑计划 (null = 无链)
    releaseBatch: null as any,            // 顺序发布占用，包括尚未轮到的项目
    timer: 0 as unknown as ReturnType<typeof setInterval> | 0,
    executionTimer: 0 as unknown as ReturnType<typeof setInterval> | 0,
  }),  getters: {
    connectionIssues(): { source: string; label: string; checkedAt: string }[] {
      return (Object.entries(this.readFailures) as [PollSource, ReadFailure][])
        .filter(([, failure]) => failure.kind === 'network')
        .map(([source]) => ({ source, label: SOURCE_LABELS[source], checkedAt: this.statusCheckedAt[source] || '' }))
    },
    stepMap(): Record<string, any> {
      return Object.fromEntries(this.steps.map((x: any) => [x.key, x]))
    },
    busy(): boolean {
      return hasExecutionOrOwner(this) || this.steps.some((step: any) => step.status === 'checking')
    },
    verificationReason(): string {
      return this.steps.some((step: any) => step.status === 'checking') && !hasExecutionOrOwner(this)
        ? '任务已结束，正在核验产物，写操作暂不可用。' : ''
    },
    publicationDisplay(): ReturnType<typeof workflowPublication> {
      return workflowPublication(this.workflow, this.publication, this.publicationError || this.workflowError)
    },
    commentsDisplay(): ReturnType<typeof commentStageDisplay> { return commentStageDisplay(this.commentState,this.commentsError) },
    activityFor() {
      return (key: string): ProgressActivity | null => {
        const step = this.stepMap[key], entry = this.activities[key]
        const current = entry && entry.jobId === step?.job_id ? entry : null
        const value = stepActivity(step, current?.activity)
        return value && current?.unavailable ? { ...value, message: `${value.message} · 暂未取得最新日志`, unavailable: true } : value
      }
    },
    currentActivity(): ProgressActivity | null {
      const step = workflowCurrentStep(this.workflow?.run, this.workflowPlan, this.steps)
      if (step) return this.activityFor(step.key)
      return WORKFLOW_BUSY.includes(this.workflow?.run?.status) && this.workflow.run.phase
        ? { label: '当前任务', message: this.workflow.run.phase } : null
    },
    currentExecution(): ReturnType<typeof executionForWorkflow> {
      return executionForWorkflow(this.workflow?.run, this.workflowPlan, this.steps)
    },
    commentsProgress(): ReturnType<typeof workflowComments> { return workflowComments(this.workflow?.run, this.workflowPlan) },
    doneCount(): number {
      return this.steps.filter((x: any) => x.status === 'done').length
    },
    requiredDoneCount(): number {
      return this.steps.filter((x: any) => x.selected && x.status === 'done' && x.fresh).length
    },
    requiredStepCount(): number {
      return this.steps.filter((x: any) => x.selected).length
    },
    /** 该步骤是否为挂起链的链头 (失败/中断所在步): 重试并继续的主入口。 */
    chainHeldAt(): (key: string) => boolean {
      return (key: string): boolean => this.chain?.state === 'held' && this.chain.remaining[0] === key
    },
    chainRunning(): boolean {
      return this.chain?.state === 'active'
    },
  },
  actions: {
    archiveChanged(uid: string, archived: boolean, archivedAt: string | null = null) {
      if (uid !== this.uid) return
      // 归档/恢复完成前的导航响应不能复活旧的编辑状态。
      getPollers(this).project.cancel()
      this.archived = archived
      this.archivedAt = archivedAt
    },
    async refreshWorkflow(replace = true) {
      if (this.uid && !this.suspended && !this.executionBlocked) await getPollers(this).workflow.refresh(replace)
    },
    workflowStarted(uid: string, run: any) {
      if (uid !== this.uid || !run?.id) return
      getPollers(this).workflow.cancel()
      getPollers(this).workflowPlan.cancel()
      this.workflowPlan = run.plan ? { id: run.id, ...run.plan, recipe: run.recipe } : null
      this.workflow = { ...this.workflow, run: { ...run, mode: run.recipe?.mode, total: run.plan?.steps?.length || 0 } }
      this.workflowError = ''
    },
    async refreshSteps(replace = true) {
      if (this.uid && !this.suspended && !this.executionBlocked) await getPollers(this).steps.refresh(replace)
    },
    outputRemoved(uid: string, file: { id: number; name: string }, missing: boolean) {
      if (uid !== this.uid) return
      // 服务端已确认成功才更新列表；作废写操作之前的旧轮询，避免记录短暂“复活”。
      getPollers(this).steps.cancel()
      const step = this.steps.find(s => s.key === 'assemble')
      if (!step?.artifacts?.some((a: any) => a.id === file.id && a.name === file.name)) return
      step.artifacts = missing ? step.artifacts.filter((a: any) => a.name !== file.name)
        : step.artifacts.map((a: any) => a.id === file.id ? { ...a, exists: false, file_revision: '', disk_size: null } : a)
    },
    stepStarted(uid: string, key: string, result: { status?: string; job_id?: number }) {
      if (uid !== this.uid || result.status !== 'started') return
      getPollers(this).steps.cancel()
      getPollers(this).execution.cancel()
      projection(this).reset()
      const step = this.steps.find(s => s.key === key)
      getPollers(this).activity.cancel()
      delete this.activities[key]
      if (step) Object.assign(step, {status:'queued', progress:0, job_id:result.job_id, cancelling:false, stop_reason:null})
    },
    async poll(replace = false) {
      if (!this.uid || this.suspended || this.executionBlocked) return
      // 后台标签页休眠轮询; 回到前台由下一个 tick 自然恢复。
      if (typeof document !== 'undefined' && document.hidden) return
      // 慢请求合并，各类状态独立呈现；发布失败不影响制作状态。
      const readers = getPollers(this)
      await Promise.all([
        readers.project.refresh(replace), readers.steps.refresh(replace),
        readers.publication.refresh(replace), readers.workflow.refresh(replace),
        readers.comments.refresh(replace),
      ])
    },
    async pollExecution() {
      if (wantsExecution(this) && visible()) await getPollers(this).execution.refresh(false)
    },
    enter(uid: string) {
      if (this.suspended) this.executionUnavailable = false
      if (this.suspended && this.executionBlocked) {
        // 从其他页面明确重新进入后，建立新一轮只读身份；不在原页面自动切换工作目录。
        if (this.stepsError === this.executionError) this.stepsError = ''
        this.executionWorkspace = ''; this.executionBlocked = false; this.executionError = ''
        delete this.readFailures.execution
      }
      if (!executionWatches.has(this)) {
        // 共享轮询属于 store，不能随第一次调用 enter 的布局组件一起被热更新卸载。
        const scope = effectScope(true)
        executionWatches.set(this, scope)
        scope.run(() => watch(() => [this.uid, this.busy, this.suspended, this.archived, !!wantsExecution(this), this.verificationReason] as const,
            ([key, busy, suspended, archived, , verificationReason]) => {
              setNavigationExecution(key, busy, suspended, archived, verificationReason)
              executionTimer(this)
            }, { immediate: true, flush: 'sync' }))
      }
      if (this.uid !== uid) {
        cancelPollers(this)
        projection(this).reset()
        this.uid = uid
        this.title = ''
        this.archived = false; this.archivedAt = null
        this.steps = []
        this.stepsLoaded = false; this.stepsError = ''; this.projectError = ''
        this.publication = null; this.publicationError = ''
        this.commentState=null;this.commentsError=''
        this.workflow = null; this.workflowError = ''
        this.workflowPlan = null; this.activities = {}
        this.status = ''; this.autoSteps = null; this.autoStepsSaving = false; this.autoStepsVersion++; this.chain = null; this.releaseBatch = null; this.dirMissing = false
        this.readFailures = {}; this.statusCheckedAt = {}; this.connectionLost = false
        this.executionError = ''
        this.executionWorkspace = ''; this.executionBlocked = false
        this.executionUnavailable = false
      }
      this.suspended = false
      if (typeof document !== 'undefined' && !executionVisibility.has(this)) {
        const listener = () => {
          executionTimer(this)
          if (visible()) { void this.pollExecution(); void this.poll(false) }
        }
        executionVisibility.set(this, listener)
        document.addEventListener('visibilitychange', listener)
      }
      executionTimer(this)
      this.poll()
      // 导航请求会计算步骤新鲜度、产物元数据和运行摘要，不能用动画帧频轮询。
      // 4 秒足够保持状态及时，也避免多个状态接口在长任务期间堆积。
      if (!this.timer) this.timer = setInterval(() => this.poll(false), 4000)
    },
    suspend() {
      // 全局页保留侧栏最近状态；离开项目后不继续读取旧项目或应用迟到响应。
      this.suspended = true
      executionWatches.get(this)?.stop()
      executionWatches.delete(this)
      if (this.timer) { clearInterval(this.timer); this.timer = 0 }
      if (this.executionTimer) { clearInterval(this.executionTimer); this.executionTimer = 0 }
      const listener = executionVisibility.get(this)
      if (listener && typeof document !== 'undefined') document.removeEventListener('visibilitychange', listener)
      executionVisibility.delete(this)
      cancelPollers(this)
    },
    reset() {
      this.suspend()
      this.suspended = false
      this.uid = ''
      this.title = ''
      this.archived = false; this.archivedAt = null
      this.steps = []
      this.stepsLoaded = false; this.stepsError = ''; this.projectError = ''
      this.publication = null; this.publicationError = ''
      this.commentState=null;this.commentsError=''
      this.workflow = null; this.workflowError = ''
      this.workflowPlan = null; this.activities = {}
      this.status = ''; this.autoSteps = null; this.autoStepsSaving = false; this.autoStepsVersion++; this.chain = null; this.releaseBatch = null; this.dirMissing = false
      this.connectionLost = false
      this.readFailures = {}; this.statusCheckedAt = {}
      this.executionError = ''
      this.executionWorkspace = ''; this.executionBlocked = false
      this.executionUnavailable = false
      projection(this).reset()
    },
  },
})

/** 墙钟 ETA: 剩余秒数 → 中文预估文案; 无预估返回空串。 */
export function formatEta(seconds: unknown, scope = 'step'): string {
  const n = Number(seconds)
  if (!Number.isFinite(n) || n <= 0) return ''
  const prefix = scope === 'phase' ? '当前阶段预计剩余约' : '预计剩余约'
  if (n < 60) return `${prefix} ${Math.max(5, Math.ceil(n / 5) * 5)} 秒`
  const minutes = n / 60
  if (minutes < 10) return `${prefix} ${Math.ceil(minutes * 2) / 2} 分钟`
  return `${prefix} ${Math.ceil(minutes)} 分钟`
}

/** 阶段和已用时间适用于真实进度；LLM 另外说明估算来源与超时。 */
export function formatProgressDetail(step: any): string {
  if (!step?.progress_phase) return ''
  const seconds = Math.max(0, Math.floor(Number(step.elapsed_seconds) || 0))
  const elapsed = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  if (!step.progress_estimated) return `${step.progress_phase} · 已用 ${elapsed}`
  const note = step.estimate_retrying ? '重试中' : step.estimate_overdue
    ? '已超出预估，仍在处理' : step.estimate_source
  return `${step.progress_phase} · ${note} · 已用 ${elapsed}`
}
