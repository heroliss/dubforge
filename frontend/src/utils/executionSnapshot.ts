// 高频执行快照只覆盖运行事实；设置、勾选和产物有效性仍由完整状态读取负责。
const ACTIVE = new Set(['queued', 'running', 'stopping', 'pausing'])
const TERMINAL = new Set(['done', 'failed', 'interrupted', 'cancelled', 'stopped', 'skipped', 'stale'])
const RUNTIME_FIELDS = ['progress', 'job_id', 'execution', 'eta_seconds', 'eta_scope',
  'progress_estimated', 'progress_phase', 'progress_indeterminate', 'elapsed_seconds',
  'progress_detail', 'estimate_source', 'estimate_overdue', 'estimate_retrying'] as const

function jobId(step: any): number | null {
  const value = step?.job_id ?? step?.execution?.job_id
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null
}
function older(next: any, current: any) {
  const nextJob = jobId(next), currentJob = jobId(current)
  return currentJob !== null && (nextJob === null || nextJob < currentJob)
}
function olderActivity(next: any, current: any) {
  if (jobId(next) !== jobId(current)) return false
  const nextTime = Date.parse(next?.execution?.updated_at || ''), currentTime = Date.parse(current?.execution?.updated_at || '')
  return Number.isFinite(nextTime) && Number.isFinite(currentTime) && nextTime < currentTime
}
function patch(current: any, snapshot: any) {
  const next = { ...current }
  for (const key of RUNTIME_FIELDS) if (Object.prototype.hasOwnProperty.call(snapshot, key)) next[key] = snapshot[key]
  // 原始 done 没有核对输入指纹和产物，不能直接把步骤计为有效完成。
  if (ACTIVE.has(snapshot.status)) next.status = snapshot.status
  else if ((ACTIVE.has(current.status) || current.status === 'checking') && terminalIdentity(snapshot, current)) {
    // 任务终态与产物有效性分开：不再冒充物理运行，也不借轻量 done 断言最新。
    next.status = 'checking'
    next.fresh = null
    next.freshness_known = false
  }
  return next
}

function terminalIdentity(snapshot: any, current: any) {
  const execution = snapshot?.execution, id = jobId(snapshot)
  return TERMINAL.has(snapshot?.status) && id !== null && id === jobId(current)
    && execution?.schema === 1 && (execution.owner == null || execution.owner === 'step')
    && execution.job_id === id && ['completed', 'failed', 'interrupted', 'cancelled', 'stopped', 'skipped'].includes(execution.state)
}

export function hasActiveExecution(steps: any[]): boolean {
  // 核验期间继续接收轻量身份/终态保护；checking 不代表 worker 仍在执行。
  return steps.some(step => ACTIVE.has(step?.status) || step?.status === 'checking')
}

/** 每个项目独立持有，完整读取开始时保存 revision，返回时调用 mergeFull。 */
export function createExecutionProjection() {
  let revision = 0
  const snapshots = new Map<string, { revision: number; step: any }>()
  const checkedTransitions = new Set<string>()
  const terminalJobs = new Map<string, number>()
  return {
    get revision() { return revision },
    reset() { revision++; snapshots.clear(); checkedTransitions.clear(); terminalJobs.clear() },
    apply(currentSteps: any[], runtimeSteps: any[]) {
      const runtime = new Map(runtimeSteps.filter(step => step && typeof step.key === 'string').map(step => [step.key, step]))
      let needsRefresh = false
      const steps = currentSteps.map(current => {
        const snapshot = runtime.get(current.key)
        if (!snapshot || older(snapshot, current) || olderActivity(snapshot, current)) return current
        const nextJob = jobId(snapshot), currentJob = jobId(current)
        const terminalJob = terminalJobs.get(current.key)
        if (ACTIVE.has(snapshot.status) && nextJob !== null && terminalJob !== undefined && nextJob <= terminalJob) return current
        if (ACTIVE.has(snapshot.status) && TERMINAL.has(current.status) && nextJob !== null && nextJob === currentJob) return current
        const changedJob = nextJob !== null && nextJob !== currentJob
        const ended = ACTIVE.has(current.status) && !ACTIVE.has(snapshot.status)
        if (changedJob || ended) {
          const transition = `${current.key}:${nextJob}:${ACTIVE.has(snapshot.status) ? 'active' : snapshot.status}`
          if (!checkedTransitions.has(transition)) { checkedTransitions.add(transition); needsRefresh = true }
        }
        if (TERMINAL.has(snapshot.status) && nextJob !== null) terminalJobs.set(current.key, nextJob)
        // 未变化的非运行记录不参与高频投影，避免把原始 done/stale 替代已核对结果。
        const newerTerminalWhileChecking = current.status === 'checking' && changedJob && terminalIdentity(snapshot, snapshot)
        if (!ACTIVE.has(current.status) && !ACTIVE.has(snapshot.status) && !newerTerminalWhileChecking) return current
        const next = patch(newerTerminalWhileChecking ? { ...current, job_id: nextJob } : current, snapshot)
        snapshots.set(current.key, { revision: ++revision, step: snapshot })
        return next
      })
      // 轻量响应不创建缺少设置和产物信息的步骤；下一次完整读取补齐。
      if (runtimeSteps.some(snapshot => snapshot?.key && !currentSteps.some(step => step.key === snapshot.key))) {
        const transition = `missing:${runtimeSteps.map(step => `${step.key}:${jobId(step)}`).join(',')}`
        if (!checkedTransitions.has(transition)) { checkedTransitions.add(transition); needsRefresh = true }
      }
      return { steps, needsRefresh }
    },
    mergeFull(fullSteps: any[], readRevision: number) {
      return fullSteps.map(step => {
        const latest = snapshots.get(step.key)
        const fullJob = jobId(step), latestJob = latest ? jobId(latest.step) : null
        const olderKnownJob = fullJob !== null && latestJob !== null && fullJob < latestJob
        const unidentifiedOlderRead = fullJob === null && latest && latest.revision > readRevision
        // 服务端旧读/晚到响应不能复活已确认结束的同一 Job；新 Job 仍正常进入。
        if (latest && terminalIdentity(latest.step, latest.step) && (olderKnownJob || unidentifiedOlderRead
          || ACTIVE.has(step.status) && (fullJob === null || fullJob === latestJob))) {
          return patch({ ...step, status: 'checking', job_id: latestJob, ...(olderKnownJob || unidentifiedOlderRead ? { error: null } : {}) }, latest.step)
        }
        if (latest && !older(latest.step, step) && (latest.revision > readRevision || olderKnownJob)) return patch(step, latest.step)
        // 读取开始于最新投影之后，完整终态可以解除运行显示；job_id 此时可能为空。
        // 旧数据可能没有详细执行记录；沿用已知任务代次建立终态屏障，防止迟到的同任务 running 复活。
        const endedJob = fullJob ?? (latest && latest.revision <= readRevision ? latestJob : null)
        if (TERMINAL.has(step.status) && endedJob !== null) terminalJobs.set(step.key, Math.max(terminalJobs.get(step.key) || 0, endedJob))
        if (latest && (fullJob === null || !older(step, latest.step))) snapshots.delete(step.key)
        return step
      })
    },
  }
}
