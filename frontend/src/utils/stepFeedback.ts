type StepFeedback = {
  status: string; error?: { code: string } | string | null
  pausing?: boolean; pause_state?: 'none' | 'requested' | 'paused'; cancelling?: boolean
  job_id?: number | string | null; job_status?: string | null
  execution?: { schema?: number; owner?: string; job_id?: number | string | null; state?: string } | null
}

function currentJobStatus(step: StepFeedback): string | null {
  const validId = (value: unknown) => value !== null && value !== undefined && value !== ''
    && Number.isSafeInteger(Number(value)) && Number(value) > 0
  // 摘要的任务状态与 job_id 来自同一条最新 Job；不根据错误文案猜取消。
  if (validId(step.job_id) && typeof step.job_status === 'string') return step.job_status
  const execution = step.execution
  if (!execution || execution.schema !== 1 || execution.owner && execution.owner !== 'step'
    || !validId(execution.job_id)) return null
  if (step.job_id != null && (!validId(step.job_id) || Number(step.job_id) !== Number(execution.job_id))) return null
  // 完整读取的终态没有内存 job_id；execution 自带最新 Job 身份仍可证明停止。
  // 活动态则必须与当前 job_id 匹配，不能把孤立的旧 execution 当成新任务。
  return ['cancelled', 'stopped'].includes(execution.state || '') ? 'cancelled'
    : validId(step.job_id) && ['running', 'queued'].includes(execution.state || '') ? execution.state! : null
}

export function stepDisplayStatus(step: StepFeedback) {
  const jobStatus = currentJobStatus(step)
  const status = ['failed', 'interrupted'].includes(step.status) && ['running', 'queued'].includes(jobStatus || '')
    ? jobStatus! : step.status
  if (jobStatus === 'cancelled' && ['failed', 'interrupted', 'running', 'queued'].includes(status)) return 'stopped'
  if (['running', 'queued'].includes(status) && !step.cancelling) {
    if (step.pause_state === 'paused') return 'paused'
    if (step.pause_state === 'requested' || (!step.pause_state && step.pausing)) return 'pausing'
  }
  // 主动停止仍可重跑；展示与真正的失败分开，不改变执行器状态。
  const code = typeof step.error === 'object' ? step.error?.code : ''
  return status === 'failed' && !jobStatus && ['STEP_CANCELLED', 'STEP_FORCE_STOPPED'].includes(code ?? '')
    ? 'stopped' : status
}

export function showStepError(step: StepFeedback) {
  return !!step.error && ['failed', 'interrupted', 'stale'].includes(stepDisplayStatus(step))
}
