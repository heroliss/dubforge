// 自动流程排队时只保存配音策略，工作线程取得资源后才固定逐句范围。
export function normalizeDubbingWorkbench(value: any) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.lines)) {
    throw new Error('配音内容返回不完整，请重新读取；后台任务可能仍在继续。')
  }
  const task = value.synthesis_task
  if (!task) return value
  if (typeof task !== 'object' || Array.isArray(task)
    || task.lines !== undefined && !Array.isArray(task.lines)) {
    throw new Error('配音任务范围返回异常，请重新读取；后台任务可能仍在继续。')
  }
  return { ...value, synthesis_task: { ...task, lines: task.lines || [],
    range_prepared: task.range_prepared !== false && Array.isArray(task.lines) } }
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function jobId(value: unknown): number | null {
  if (typeof value !== 'number' && !(typeof value === 'string' && /^[1-9]\d*$/.test(value))) return null
  const result = Number(value)
  return Number.isSafeInteger(result) && result > 0 ? result : null
}

// 完整工作台和实时步骤可能来自不同代次；旧服务缺少身份时保留兼容。
export function currentDubbingTask(task: any, expectedJobId?: unknown) {
  if (!task) return null
  const expected = jobId(expectedJobId), actual = jobId(task.job_id)
  return expected !== null && actual !== null && actual !== expected ? null : task
}

export function dubbingTaskState(task: any, documentLines?: number | null, expectedJobId?: unknown) {
  task = currentDubbingTask(task, expectedJobId)
  if (!task && jobId(expectedJobId) === null) return null
  task ||= {}
  const reported = count(task.total)
  const rangePrepared = task.range_prepared !== false && Array.isArray(task.lines)
  // 旧服务未返回 lines 时，正数总量仍可显示；缺范围的零值代表尚未核定。
  const total = rangePrepared ? reported ?? task.lines.length : reported !== null && reported > 0 ? reported : null
  const documentCount = count(documentLines)
  return { total, rangePending: total === null, documentLines: documentCount !== null && documentCount > 0 ? documentCount : null,
    completed: count(task.completed), failed: count(task.failed), queued: task.queued === true,
    selected: task.scope === 'selected' }
}
