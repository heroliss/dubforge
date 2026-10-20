// 配音策略属于一次运行；保存参数本身不授权覆盖已有音频。
export type DubbingRunPolicy = {
  mode: 'fill' | 'changed' | 'all'
  target: 'new' | 'current' | 'slot'
  slot: number | null
  line_settings: 'preserve' | 'project'
  include_manual_sources: boolean
  // 仅识别旧运行快照；新请求由已保存的普通配音设置决定。
  repair_timing?: boolean
}

export type DubbingRunSummary = {
  policy?: DubbingRunPolicy
  needed?: boolean
  total?: number
  generate?: number
  repair?: number
  repair_ids?: number[]
  repair_blocked?: { id: number; reason: string }[]
  reuse?: number
  customized?: number
  original?: number
  silent?: number
  settings_changed?: number
  overwrite?: number
  confirmed?: number
  new_slot?: number | null
  slots?: number[]
  target_label?: string
  deferred?: boolean
  resumed?: boolean
  completed?: number
  resume_reset?: boolean
  repair_settings?: { enabled: boolean; configured: boolean; legacy: boolean }
}

export function defaultDubbingRunPolicy(): DubbingRunPolicy {
  return { mode: 'fill', target: 'current', slot: null, line_settings: 'preserve', include_manual_sources: false }
}

export function dubbingRunPolicy(value?: Partial<DubbingRunPolicy> | null): DubbingRunPolicy {
  const defaults = defaultDubbingRunPolicy()
  if (!value) return defaults
  const mode = ['fill', 'changed', 'all'].includes(value.mode ?? '') ? value.mode! : defaults.mode
  if (mode === 'fill') return defaults
  return {
    mode,
    target: ['new', 'current', 'slot'].includes(value?.target ?? '') ? value!.target! : defaults.target,
    slot: Number.isInteger(value?.slot) && Number(value?.slot) > 0 ? Number(value!.slot) : null,
    line_settings: value?.line_settings === 'project' ? 'project' : 'preserve',
    include_manual_sources: value?.include_manual_sources === true,
  }
}

export function dubbingPolicyKey(policy?: Partial<DubbingRunPolicy> | null): string {
  return JSON.stringify(dubbingRunPolicy(policy))
}

export function dubbingPolicyError(policy: DubbingRunPolicy): string {
  return policy.mode !== 'fill' && policy.target === 'slot'
    && (!Number.isInteger(policy.slot) || Number(policy.slot) < 1 || Number(policy.slot) > 10000)
    ? '请选择 1–10000 范围内的目标配音槽' : ''
}

export function dubbingPolicyLabel(policy?: Partial<DubbingRunPolicy> | null): string {
  return ({ fill: '继续已有配音', changed: '按当前内容与设置更新配音', all: '重新生成全部配音' })[dubbingRunPolicy(policy).mode]
}

export function dubbingPlanHasWork(summary?: DubbingRunSummary | null): boolean {
  return !!summary && (summary.deferred === true && summary.policy?.mode !== 'fill'
    || Number(summary.generate) > 0 || Number(summary.repair) > 0)
}
