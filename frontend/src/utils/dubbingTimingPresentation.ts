import { playbackTime } from './dubbingPlaybackTime.ts'
import { dubbingDurationReference } from './dubbingDurationReference.ts'

const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const range = (start: unknown, end: unknown, fallback: { start: number; end: number }) =>
  number(start) && number(end) && start >= 0 && end > start ? { start, end } : fallback

// 原始源稿、自动建议、人工覆盖和实际播放各自有来源，不能拿当前配置猜历史结果。
export function dubbingTimingView(line: any) {
  const timing = line?.timing ?? {}, current = { start: line.start, end: line.end }
  const source = range(line.base?.start, line.base?.end, current)
  const effective = playbackTime(line)
  const automatic = range(timing.automatic_start, timing.automatic_end,
    range(timing.applied_start, timing.applied_end, effective))
  const manual = number(timing.manual_start) ? timing.manual_start : null
  const reservedEnd = number(timing.manual_end) ? timing.manual_end : null
  const manualActive = !!timing.manual_active, stale = !!timing.manual_stale
  const shift = effective.start - current.start
  const moved = Math.abs(shift) >= .005
  const direction = moved ? `${shift < 0 ? '提前' : '延后'} ${Math.abs(shift).toFixed(2)} 秒` : '起点未移动'
  const mode = manualActive || timing.effective_mode === 'manual' ? 'manual'
    : timing.effective_mode === 'source' ? 'source' : 'automatic'
  const label = manualActive ? `人工${direction}` : stale ? '人工位置待核对'
    : moved ? `自动${direction}` : ''
  const seconds = line.wav?.seconds
  const editable = line.selection?.source === 'dub' && line.selection?.take != null
    && !!line.wav?.exists && number(seconds) && seconds > 0 && typeof timing.timing_revision === 'string'
  return { source, current, automatic, effective, manual, reservedEnd, manualActive, stale,
    shift, moved, direction, mode, label, editable, seconds: number(seconds) && seconds > 0 ? seconds : null,
    revision: timing.timing_revision }
}

export function dubbingFactorSummary(line: any) {
  const reference = dubbingDurationReference(line)
  const changed = reference.requested !== null && reference.used !== null && reference.requested !== reference.used
  if (!changed && !reference.retries) return ''
  const factor = changed ? `因子 ${reference.requested} → ${reference.used}`
    : reference.used !== null ? `因子 ${reference.used}` : '因子未记录'
  return `${factor}${reference.retries ? ` · 自动重试 ${reference.retries} 次` : ''}${reference.retryFailed ? ' · 保留有效音频' : ''}`
}

export function playbackDraftError(start: unknown, end: unknown, seconds: number | null) {
  if (!number(start) || start < 0) return '请输入大于或等于零的有效开始时间。'
  if (seconds === null) return '尚无有效配音，请先选择或生成完整音频。'
  if (end != null && (!number(end) || end < start + seconds - 1e-7))
    return `字幕结束时间不能早于完整配音结束（${(Math.ceil((start + seconds) * 1000) / 1000).toFixed(3)} 秒）；需要缩短声音请调整合成参数后重新生成。`
  return ''
}

export function preciseTime(value: number) {
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${(value % 60).toFixed(3).padStart(6, '0')}`
}

export function timeRange(value: { start: number; end: number }) {
  return `${preciseTime(value.start)} — ${preciseTime(value.end)}`
}
