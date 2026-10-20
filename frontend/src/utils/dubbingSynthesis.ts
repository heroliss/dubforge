export type SynthesisLine = {
  id: number
  selection: { source: string; take: number | null }
  versions?: { id: number; confirmed?: boolean; url?: string | null }[]
  confirmed?: boolean
  customized?: boolean
  translation_available?: boolean
  edit_stale?: boolean
  unready_inputs?: unknown[]
  text?: string
  start?: number
  end?: number
  synthesis_revision?: string
}

export type DubbingSynthesisTarget = { mode: 'new' | 'current' | 'slot'; slot?: number | null }

/** 后端在启动锁内核对这些轻量版本，不再为确认操作重复扫描整个工作台。 */
export function dubbingSynthesisExpected(lines: SynthesisLine[], ids: number[], slots: unknown) {
  if (!Array.isArray(slots) || slots.some(id => !Number.isInteger(id) || id < 1 || id > 10000)) return null
  const selected = new Set(ids), targets = lines.filter(line => selected.has(line.id))
  if (targets.length !== selected.size || targets.some(line => typeof line.synthesis_revision !== 'string' || !line.synthesis_revision)) return null
  return { slots: [...slots], lines: Object.fromEntries(targets.map(line => [line.id, line.synthesis_revision!])) }
}

/** 汇总给定句子及其待生成槽；批量新建时共同使用本次范围内的下一个槽号。 */
export function dubbingSlots(lines: SynthesisLine[], reservedSlots: unknown = []): number[] {
  return [...new Set([...lines.flatMap(line => [line.selection.take, ...(line.versions ?? []).map(v => v.id)]),
    ...(Array.isArray(reservedSlots) ? reservedSlots : [])]
    .filter((id): id is number => Number.isInteger(id) && Number(id) > 0 && Number(id) <= 10000))].sort((a, b) => a - b)
}

/** 预留槽按句子记录，范围外的待生成任务不影响本次新槽编号。 */
export function dubbingReservationSlots(reservations: unknown, ids: number[]): number[] {
  if (!reservations || typeof reservations !== 'object' || Array.isArray(reservations)) return []
  const byLine = reservations as Record<string, unknown>
  return [...new Set(ids.filter(id => Number.isSafeInteger(id) && id > 0).flatMap(id =>
    Array.isArray(byLine[id]) ? byLine[id] as unknown[] : [])
    .filter((slot): slot is number => Number.isInteger(slot) && Number(slot) > 0 && Number(slot) <= 10000))]
    .sort((a, b) => a - b)
}

/** 接收任务后立即并入预留槽，避免工作台刷新前再次选择同一新槽。 */
export function mergeDubbingReservations(reservations: unknown, rows: { id: number; take: number }[]): Record<string, number[]> {
  const merged: Record<string, number[]> = {}
  if (reservations && typeof reservations === 'object' && !Array.isArray(reservations)) {
    for (const key of Object.keys(reservations)) {
      const id = Number(key)
      if (Number.isSafeInteger(id) && id > 0 && String(id) === key) {
        const slots = dubbingReservationSlots(reservations, [id])
        if (slots.length) merged[key] = slots
      }
    }
  }
  for (const { id, take } of rows) {
    if (!Number.isSafeInteger(id) || id < 1 || !Number.isInteger(take) || take < 1 || take > 10000) continue
    merged[id] = [...new Set([...(merged[id] ?? []), take])].sort((a, b) => a - b)
  }
  return merged
}

/** 冻结待确认内容；其它窗口改变文字、声源或音频时，必须重新确认。 */
export function dubbingSynthesisSignature(lines: SynthesisLine[], ids: number[], reservedSlots: unknown = [], includeSlots = true): string {
  const selected = new Set(ids)
  return JSON.stringify([includeSlots ? dubbingSlots(lines, reservedSlots) : [], lines.filter(line => selected.has(line.id)).map(line => [
    line.id, line.text, line.start, line.end, line.selection, line.confirmed, line.synthesis_revision,
    line.versions?.map(v => [v.id, v.url, v.confirmed]),
  ])])
}

export type DubbingSynthesisPlan = {
  ids: number[]
  takes: Record<string, number | null>
  total: number
  all: boolean
  overwrite: number
  create: number
  refill: number
  confirmed: number
  customized: number
  original: number
  blockedIds: number[]
  range: string
  target: DubbingSynthesisTarget
  newSlot: number
  destination: string
  invalidReason: string
}

/** 连续编号收成区间，避免大批量确认窗口被几百个编号撑满。 */
export function sentenceRanges(ids: number[]): string {
  const sorted = [...new Set(ids)].sort((a, b) => a - b)
  const ranges: string[] = []
  for (let index = 0; index < sorted.length;) {
    const start = sorted[index]
    let end = start
    while (++index < sorted.length && sorted[index] === end + 1) end = sorted[index]
    ranges.push(start === end ? String(start) : `${start}–${end}`)
  }
  return ranges.join('、')
}

/** 范围与版本操作一次性冻结，确认内容与提交请求使用同一份计划。 */
export function analyzeDubbingSynthesis(lines: SynthesisLine[], ids: number[], target: DubbingSynthesisTarget = { mode: 'current' }, reservedSlots: unknown = []): DubbingSynthesisPlan {
  const selected = new Set(ids)
  const targets = lines.filter(line => selected.has(line.id))
  const slots = dubbingSlots(targets, reservedSlots), newSlot = (slots.at(-1) ?? 0) + 1
  const takes = Object.fromEntries(targets.map(line => [line.id, target.mode === 'current'
    ? line.selection.take ?? newSlot : target.mode === 'slot' ? target.slot ?? null : newSlot]))
  const overwritten = targets.filter(line => line.versions?.some(v => v.id === takes[line.id]))
  const overwrite = overwritten.length
  const refill = target.mode === 'current' ? targets.filter(line => line.selection.take !== null
    && !line.versions?.some(v => v.id === takes[line.id])).length : 0
  const invalidReason = Object.values(takes).some(id => !Number.isInteger(id) || Number(id) < 1 || Number(id) > 10000)
    ? target.mode === 'slot' ? '请选择有效的目标配音槽' : '配音槽已达到上限，请选择已有槽覆盖' : ''
  return {
    ids: targets.map(line => line.id),
    takes,
    total: targets.length,
    all: targets.length > 0 && targets.length === lines.length,
    overwrite, create: targets.length - overwrite - refill, refill,
    confirmed: overwritten.filter(line => line.versions?.find(v => v.id === takes[line.id])?.confirmed
      ?? (line.selection.take === takes[line.id] && line.confirmed)).length,
    customized: targets.filter(line => line.customized).length,
    original: targets.filter(line => line.selection.source === 'original').length,
    blockedIds: targets.filter(line => !line.translation_available || line.edit_stale || line.unready_inputs?.length).map(line => line.id),
    range: sentenceRanges(targets.map(line => line.id)),
    target: { ...target }, newSlot,
    destination: target.mode === 'current' ? targets.length === 1 ? '当前配音槽' : '各句当前配音槽' : `配音槽 ${target.mode === 'slot' ? target.slot ?? '未选择' : newSlot}`,
    invalidReason,
  }
}
