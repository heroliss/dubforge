type StepFeedback = {
  status: string; error?: { code: string } | null
  pausing?: boolean; pause_state?: 'none' | 'requested' | 'paused'; cancelling?: boolean
}

export function stepDisplayStatus(step: StepFeedback) {
  if (['running', 'queued'].includes(step.status) && !step.cancelling) {
    if (step.pause_state === 'paused') return 'paused'
    if (step.pause_state === 'requested' || (!step.pause_state && step.pausing)) return 'pausing'
  }
  // 主动停止仍可重跑；展示与真正的失败分开，不改变执行器状态。
  return step.status === 'failed' && ['STEP_CANCELLED', 'STEP_FORCE_STOPPED'].includes(step.error?.code ?? '')
    ? 'stopped' : step.status
}

export function showStepError(step: StepFeedback) {
  return !!step.error && ['failed', 'interrupted', 'stale'].includes(stepDisplayStatus(step))
}
