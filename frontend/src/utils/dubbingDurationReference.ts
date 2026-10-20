type DurationRecord = {
  used_factor?: unknown; retries?: unknown; error?: unknown
  attempts?: { factor?: unknown }[]
}

export interface DurationReferenceLine {
  selection?: { take?: number | null }
  wav?: { exists?: boolean; stale?: boolean; duration_retry?: DurationRecord | null }
}

function factor(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= .5 && value <= 2 ? value : null
}

// 配置和版本记录分别读取；缺少历史记录时不能用当前配置推测实际合成参数。
export function dubbingDurationReference(line?: DurationReferenceLine, configured?: unknown) {
  const take = line?.selection?.take ?? null
  const record = take === null ? undefined : line?.wav?.duration_retry
  const requested = factor(record?.attempts?.[0]?.factor), used = factor(record?.used_factor)
  const retries = typeof record?.retries === 'number' && Number.isInteger(record.retries) && record.retries > 0
    ? record.retries : 0
  return {
    take, requested, used, configured: factor(configured), retries,
    generated: take !== null && (!!line?.wav?.exists || requested !== null || used !== null),
    stale: take !== null && !!line?.wav?.stale,
    retryFailed: typeof record?.error === 'string' && !!record.error,
  }
}
