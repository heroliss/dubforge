import { defineStore } from 'pinia'
import { watch } from 'vue'
import { setNavigationExecution } from '../utils/configurationEditability'
import { createLatestRefresh } from '../utils/preview'
import { WORKFLOW_BUSY, workflowPublication } from '../utils/workflow'
import { latestJobActivity, stepActivity, workflowComments, type ProgressActivity } from '../utils/progressActivity'
import { parseChain, type ChainInfo } from '../utils/chainStatus'

// 流程导航共享状态: MainLayout (左侧步骤导航) 与 StepView/PipelineView 共用一份轮询,
// 避免多处各自 fetch 造成状态不同步
export const STEP_ORDER = ['download', 'preprocess', 'transcribe', 'split',
  'translate', 'tts', 'subtitles', 'assemble'] as const
export const NAV_STEP_ORDER = STEP_ORDER.filter(key => key !== 'subtitles')

// 首屏不依赖状态接口才知道中文名称；加载时也不暴露内部键名。
export const STEP_LABELS: Record<string, string> = {
  download: '获取视频', preprocess: '音轨分离', transcribe: '语音转文字', split: '语义整理',
  translate: '翻译', subtitles: '字幕导出', tts: '字幕与配音', assemble: '合成成片',
}

export const STATUS_TEXT: Record<string, string> = {
  pending: '待运行', queued: '⏳ 排队中', running: '运行中', done: '✓ 完成',
  failed: '✗ 失败', stale: '⟳ 已过期', interrupted: '已中断', skipped: '已跳过',
  stopped: '已停止', pausing: '正在暂停', paused: '已暂停',
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
}

type Loader = ReturnType<typeof createLatestRefresh>
// 与领域 project_status 保持一致，复用已经取得的步骤状态，不另算一遍全部输入。
function projectStatus(steps: any[]): string {
  if (steps.some(step => ['running','queued'].includes(step.status))) return 'processing'
  const chosen = steps.filter(step => step.selected)
  if (chosen.some(step => step.status === 'failed')) return 'failed'
  return chosen.length && chosen.every(step => step.fresh) ? 'done' : 'created'
}
const pollers = new WeakMap<object, { project: Loader; steps: Loader; publication: Loader; workflow: Loader; activity: Loader; workflowPlan: Loader }>()
const executionWatches = new WeakSet<object>()
function cancelPollers(store: object) {
  const readers = pollers.get(store)
  if (readers) for (const reader of Object.values(readers)) reader.cancel()
}
function getPollers(store: any) {
  let readers = pollers.get(store)
  if (!readers) {
    const reader = (kind: 'project' | 'steps' | 'publication' | 'workflow') => createLatestRefresh(async signal => {
      const uid = store.uid
      const selectionVersion = store.autoStepsVersion
      const url = kind === 'publication' ? `/api/publishing/projects/${uid}/status` : `/api/projects/${uid}${kind === 'steps' ? '/steps' : kind === 'workflow' ? '/workflow/status' : '/navigation'}`
      const response = await fetch(url, {
        cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
      })
      const value = await response.json()
      if (!response.ok) throw new Error(value.message || '读取项目状态失败')
      return { uid, value, selectionVersion }
    }, ({ uid, value, selectionVersion }) => {
      if (uid !== store.uid) return
      store.connectionLost = false
      if (kind === 'steps') {
        if (store.autoStepsSaving || selectionVersion !== store.autoStepsVersion) {
          value = value.map((step: any) => ({ ...step, selected: store.autoSteps ? store.autoSteps.includes(step.key) : true }))
        }
        const previous = store.steps.filter((s: any) => s.status === 'running').map((s: any) => `${s.key}:${s.job_id}`).join(',')
        store.steps = value; store.stepsLoaded = true; store.stepsError = ''; store.status = projectStatus(value)
        const active = value.filter((s: any) => s.status === 'running' && s.job_id)
        store.activities = Object.fromEntries(Object.entries(store.activities).filter(([key, entry]: any) => active.some((s: any) => s.key === key && s.job_id === entry.jobId)))
        if (active.length) void getPollers(store).activity.refresh(previous !== active.map((s: any) => `${s.key}:${s.job_id}`).join(','))
        else getPollers(store).activity.cancel()
      }
      else if (kind === 'publication') { store.publication = value; store.publicationError = '' }
      else if (kind === 'workflow') {
        const changed = store.workflow?.run?.id !== value.run?.id
        store.workflow = value; store.workflowError = ''
        if (changed) { store.workflowPlan = null; getPollers(store).workflowPlan.cancel() }
        if (value.run?.id && store.workflowPlan?.id !== value.run.id) void getPollers(store).workflowPlan.refresh(false)
      }
      else {
        store.title = value.title; store.status = store.stepsLoaded ? projectStatus(store.steps) : value.status
        store.dirMissing = value.dir_exists === false
        if (!store.autoStepsSaving && selectionVersion === store.autoStepsVersion) store.autoSteps = Array.isArray(value.auto_steps) ? value.auto_steps : null
        store.chain = parseChain(value.chain)
        store.projectError = ''
      }
    }, error => {
      const detail = error instanceof Error && error.name === 'TimeoutError' ? '请求超时' : '连接失败'
      if (detail === '连接失败') store.connectionLost = true
      store[kind === 'steps' ? 'stepsError' : kind === 'publication' ? 'publicationError' : kind === 'workflow' ? 'workflowError' : 'projectError'] = `${detail}，未能读取${kind === 'steps' ? '步骤状态' : kind === 'publication' ? '发布状态' : kind === 'workflow' ? '自动流程状态' : '项目信息'}，请重试`
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
    readers = { project: reader('project'), steps: reader('steps'), publication: reader('publication'), workflow: reader('workflow'), activity, workflowPlan }
    pollers.set(store, readers)
  }
  return readers
}

export const useNavStore = defineStore('nav', {
  state: () => ({
    uid: '',
    suspended: false,
    title: '',
    status: '',
    steps: [] as any[],
    stepsLoaded: false,
    stepsError: '',
    projectError: '',
    publication: null as any,
    publicationError: '',
    connectionLost: false,             // 后端连接中断 (重启/更新), 由轮询自动恢复
    workflow: null as any,
    workflowError: '',
    workflowPlan: null as any,
    activities: {} as Record<string, { jobId: number; activity: ProgressActivity | null; unavailable: boolean }>,
    dirMissing: false,
    autoSteps: null as string[] | null,   // 勾选集 (null = 全部, 未自定义过)
    autoStepsSaving: false,
    autoStepsVersion: 0,
    chain: null as ChainInfo | null,      // 自动续跑计划 (null = 无链)
    timer: 0 as unknown as ReturnType<typeof setInterval> | 0,
  }),  getters: {
    stepMap(): Record<string, any> {
      return Object.fromEntries(this.steps.map((x: any) => [x.key, x]))
    },
    busy(): boolean {
      return WORKFLOW_BUSY.includes(this.workflow?.run?.status) || this.chain?.state === 'active'
        || ['queued','preparing','uploading','submitting','joining'].includes(this.publication?.status)
        || this.steps.some((x: any) => ['running','queued','stopping'].includes(x.status))
    },
    publicationDisplay(): ReturnType<typeof workflowPublication> {
      return workflowPublication(this.workflow, this.publication, this.publicationError || this.workflowError)
    },
    activityFor() {
      return (key: string): ProgressActivity | null => {
        const step = this.stepMap[key], entry = this.activities[key]
        const current = entry && entry.jobId === step?.job_id ? entry : null
        const value = stepActivity(step, current?.activity)
        return value && current?.unavailable ? { ...value, message: `${value.message} · 暂未取得最新日志`, unavailable: true } : value
      }
    },
    currentActivity(): ProgressActivity | null {
      const step = this.steps.find(s => s.status === 'running') || this.steps.find(s => s.status === 'queued')
      if (step) return this.activityFor(step.key)
      return WORKFLOW_BUSY.includes(this.workflow?.run?.status) && this.workflow.run.phase
        ? { label: '当前任务', message: this.workflow.run.phase } : null
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
    async refreshWorkflow(replace = true) {
      if (this.uid && !this.suspended) await getPollers(this).workflow.refresh(replace)
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
      if (this.uid && !this.suspended) await getPollers(this).steps.refresh(replace)
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
      const step = this.steps.find(s => s.key === key)
      getPollers(this).activity.cancel()
      delete this.activities[key]
      if (step) Object.assign(step, {status:'queued', progress:0, job_id:result.job_id, cancelling:false, stop_reason:null})
    },
    async poll(replace = true) {
      if (!this.uid || this.suspended) return
      // 后台标签页休眠轮询; 回到前台由下一个 tick 自然恢复。
      if (typeof document !== 'undefined' && document.hidden) return
      // 慢请求合并，各类状态独立呈现；发布失败不影响制作状态。
      const readers = getPollers(this)
      await Promise.all([readers.project.refresh(replace), readers.steps.refresh(replace), readers.publication.refresh(replace), readers.workflow.refresh(replace)])
    },
    enter(uid: string) {
      if (!executionWatches.has(this)) {
        executionWatches.add(this)
        watch(() => [this.uid, this.busy, this.suspended] as const,
          ([key, busy, suspended]) => setNavigationExecution(key, busy, suspended), { immediate: true, flush: 'sync' })
      }
      if (this.uid !== uid) {
        cancelPollers(this)
        this.uid = uid
        this.title = ''
        this.steps = []
        this.stepsLoaded = false; this.stepsError = ''; this.projectError = ''
        this.publication = null; this.publicationError = ''
        this.workflow = null; this.workflowError = ''
        this.workflowPlan = null; this.activities = {}
        this.status = ''; this.autoSteps = null; this.autoStepsSaving = false; this.autoStepsVersion++; this.chain = null; this.dirMissing = false
      }
      this.suspended = false
      this.connectionLost = false
      this.poll()
      // 与进度插值窗口一致：每 1.5 秒取得一次后端目标值，视觉上连续追赶，
      // 不会等待 2.5 秒后突然跳一大段。
      if (!this.timer) this.timer = setInterval(() => this.poll(false), 1500)
    },
    suspend() {
      // 全局页保留侧栏最近状态；离开项目后不继续读取旧项目或应用迟到响应。
      this.suspended = true
      if (this.timer) { clearInterval(this.timer); this.timer = 0 }
      cancelPollers(this)
    },
    reset() {
      this.suspend()
      this.suspended = false
      this.uid = ''
      this.title = ''
      this.steps = []
      this.stepsLoaded = false; this.stepsError = ''; this.projectError = ''
      this.publication = null; this.publicationError = ''
      this.workflow = null; this.workflowError = ''
      this.workflowPlan = null; this.activities = {}
      this.status = ''; this.autoSteps = null; this.autoStepsSaving = false; this.autoStepsVersion++; this.chain = null; this.dirMissing = false
      this.connectionLost = false
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
