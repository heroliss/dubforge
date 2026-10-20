import { executionForStep } from './executionProgress.ts'

function jobId(value: unknown): number | null {
  if (typeof value !== 'number' && (typeof value !== 'string' || !/^\d+$/.test(value))) return null
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/** 合成的阶段 ETA 目前只由编码测速提供；切入复核等叶阶段后不能沿用。 */
export function executionEtaSeconds(step: any): unknown {
  const seconds = step?.eta_seconds
  // 其他步骤继续使用原合同；合成排队/终态或缺状态不能借旧 running 快照。
  if (step?.key !== 'assemble') return seconds
  if (step.status !== 'running') return 0
  // 正在运行的明确整步/旧接口 ETA 保留，不更改百分比或工作量。
  if (step.eta_scope !== 'phase') return seconds
  const current = executionForStep(step)
  const expectedJob = jobId(step.job_id ?? step.execution?.job_id)
  return current?.state === 'running' && !current.finished_at
    && (current.owner == null || current.owner === 'step')
    && expectedJob !== null && jobId(current.job_id) === expectedJob
    && ['encoding', 'encoding_timeline'].includes(current.phase)
    && typeof current.unit === 'string' && current.unit.trim() === '成片秒'
    ? seconds : 0
}
