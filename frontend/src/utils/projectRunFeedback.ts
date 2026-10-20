// 提交反馈只说明已发生的请求阶段，不能替代步骤的真实执行进度。
export type ProjectRunFeedbackStage = 'checking' | 'submitting' | 'accepted' | 'unknown' | 'blocked' | 'clear'
export type ProjectRunFeedback = {
  id: string
  batchId?: string
  groupIds: string[]
  projectUids: string[]
  workflowId?: string
  stage: ProjectRunFeedbackStage
  message: string
}

// 请求阶段是展示层状态：覆盖旧失败的视觉提示，不写回步骤执行事实。
export function projectRequestStage(feedback?: Pick<ProjectRunFeedback, 'stage' | 'message'> | null) {
  if (!feedback || !['checking', 'submitting', 'accepted'].includes(feedback.stage)) return null
  return { text: feedback.stage === 'checking' ? '核对中' : feedback.stage === 'submitting' ? '正在提交' : '准备继续',
    message: feedback.message }
}

export function projectRequestSteps(stages: any[], feedback?: Pick<ProjectRunFeedback, 'stage' | 'message'> | null) {
  const pending = projectRequestStage(feedback)
  if (!pending) return stages
  return stages.map(stage => ['failed', 'interrupted', 'stopped', 'attention'].includes(stage.status)
    ? { ...stage, status: 'pending', statusText: pending.text, detail: `${pending.message}上次中断记录保留在步骤日志中。`,
      progress: undefined, progress_indeterminate: true }
    : stage)
}

export const PROJECT_RUN_SYNC_DELAYS = [500, 750, 1250, 2000, 3000, 4000] as const

// 短时间加快轻量状态读取；不提交任务，也不在后台无限轮询。
export function createProjectRunSync(poll: () => Promise<boolean>, visible: () => boolean = () => true) {
  let timer: ReturnType<typeof setTimeout> | undefined, revision = 0
  const stop = () => { revision++; if (timer !== undefined) clearTimeout(timer); timer = undefined }
  const start = () => {
    stop()
    const ticket = revision, deadline = Date.now() + 20000
    let cursor = 0
    const schedule = () => {
      if (ticket !== revision || cursor >= PROJECT_RUN_SYNC_DELAYS.length || Date.now() >= deadline) return
      timer = setTimeout(async () => {
        timer = undefined
        if (ticket !== revision || !visible() || Date.now() >= deadline) return
        let finished = false
        try { finished = await poll() } catch { /* 普通状态读取仍负责展示可恢复的读取错误。 */ }
        if (!finished && ticket === revision) schedule()
      }, PROJECT_RUN_SYNC_DELAYS[cursor++])
    }
    schedule()
  }
  return { start, stop }
}
