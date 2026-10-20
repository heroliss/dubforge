import { WORKFLOW_OPEN } from './workflow.ts'

export type WorkflowFailureStatus = {
  historical: boolean
  recovered: boolean
  stepKey?: string
  stepLabel?: string
  phaseLabel?: string
  message: string
  reason: string
}

// 自动流程保存中断时的记录；后来手动成功只用于说明当前步骤，不改写历史流程。
export function workflowFailureStatus(run: any, plan: any, steps: any[] = [], trustedSteps = true): WorkflowFailureStatus | null {
  if (!run?.error) return null
  const historical = !WORKFLOW_OPEN.includes(run.status) && !run.publication_id
    && ['failed','paused','attention','cancelled'].includes(run.status)
  const cursor = Number(run.cursor)
  const stepKey = plan?.id === run.id && Number.isSafeInteger(cursor) && cursor >= 0
    && Array.isArray(plan.steps) && typeof plan.steps[cursor] === 'string' ? plan.steps[cursor] : undefined
  const step = stepKey ? steps.find(item => item.key === stepKey) : undefined
  const label = stepKey ? typeof plan?.labels?.[stepKey] === 'string' ? plan.labels[stepKey] : step?.label : undefined
  const stepLabel = typeof label === 'string' && label.trim() ? label.trim() : undefined
  // 发布资料准备位于制作步骤之后；只认匹配计划与明确阶段，不从报错文字猜测。
  const phaseLabel = historical && plan?.id === run.id && Array.isArray(plan.steps)
    && Number.isSafeInteger(cursor) && cursor === plan.steps.length
    && typeof run.phase === 'string' && run.phase.trim() === '准备发布资料' ? '准备发布资料' : undefined
  const recovered = historical && trustedSteps && !!step && step.status === 'done' && step.fresh !== false
  const reason = typeof run.error === 'string' ? run.error : run.error.message || '请查看对应记录'
  return {
    historical, recovered, stepKey, stepLabel, phaseLabel,
    message: phaseLabel ? '上次自动流程在准备发布资料时中断；制作步骤已完成。'
      : historical ? `${stepLabel ? `上次自动流程在${stepLabel}中断` : '上次自动流程已中断'}${recovered ? '；该步骤目前已完成。' : '。'}`
      : '当前自动流程需要处理。',
    reason,
  }
}
