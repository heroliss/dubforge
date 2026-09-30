// 自动续跑链状态 (ADR-0072): 计划与活性分离, held = 挂起可恢复。
// 后端项目对象上的 chain = {remaining, state, hold?}; 兼容旧版 params._chain 数组。
export interface ChainHold { step: string | null; code: string | null; message: string | null }
export interface ChainInfo { remaining: string[]; state: 'active' | 'held'; hold?: ChainHold }
export type ChainKind = 'failed' | 'stopped' | 'interrupted'

export function parseChain(value: any): ChainInfo | null {
  if (value && typeof value === 'object' && Array.isArray(value.remaining)) {
    const remaining = value.remaining.filter((k: any) => typeof k === 'string')
    if (!remaining.length) return null
    const info: ChainInfo = { remaining, state: value.state === 'held' ? 'held' : 'active' }
    if (value.hold && typeof value.hold === 'object') {
      info.hold = { step: value.hold.step ?? null, code: value.hold.code ?? null, message: value.hold.message ?? null }
    }
    return info
  }
  // 旧后端只有数组 (一律视为运行中)。
  if (Array.isArray(value)) {
    const remaining = value.filter((k: any) => typeof k === 'string')
    return remaining.length ? { remaining, state: 'active' as const } : null
  }
  return null
}

/** 挂起原因分类: 失败 / 用户停止 / 进程中断 (用于文案与图标)。 */
export function chainKind(chain: ChainInfo | null): ChainKind | null {
  if (chain?.state !== 'held') return null
  const code = chain.hold?.code || ''
  if (code === 'STEP_INTERRUPTED') return 'interrupted'
  return code === 'STEP_CANCELLED' || code === 'STEP_FORCE_STOPPED' ? 'stopped' : 'failed'
}

export const HOLD_VERB: Record<ChainKind, string> = {
  failed: '失败', stopped: '已停止', interrupted: '被中断',
}

/** 挂起描述: 「配音」失败 [CODE]: 消息。 */
export function holdDescription(chain: ChainInfo, labels: Record<string, string>): string {
  const kind = chainKind(chain)
  if (!kind) return ''
  const head = chain.hold?.step && labels[chain.hold.step] ? `「${labels[chain.hold.step]}」` : ''
  const code = chain.hold?.code ? ` [${chain.hold.code}]` : ''
  const message = chain.hold?.message ? `：${chain.hold.message}` : ''
  return `${head}${HOLD_VERB[kind]}${code}${message}`
}

/** 剩余步骤文案: A → B → C。 */
export function chainSequence(chain: ChainInfo, labels: Record<string, string>): string {
  return chain.remaining.map(key => labels[key] || key).join(' → ')
}
