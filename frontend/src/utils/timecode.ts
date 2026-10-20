/** 先量化总时间再拆分，避免 59.9996 秒被显示为 00:00:60.000。 */
export function formatTimecode(value: number | null | undefined, precision = 0): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  const digits = Number.isFinite(precision) ? Math.max(0, Math.min(3, Math.trunc(precision))) : 0
  const scale = 10 ** digits
  const units = Math.round(value * scale)
  if (!Number.isSafeInteger(units)) return '—'
  const seconds = Math.floor(units / scale)
  const hours = String(Math.floor(seconds / 3600)).padStart(2, '0')
  const minutes = String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')
  const rest = String(seconds % 60).padStart(2, '0')
  const fraction = digits ? `.${String(units % scale).padStart(digits, '0')}` : ''
  return `${hours}:${minutes}:${rest}${fraction}`
}

/** 支持秒、分:秒、时:分:秒；冒号形式的分秒不得越界。 */
export function parseTimecode(value: string): number | null {
  const text = value.trim().replace(/：/g, ':')
  if (!/^\d+(?::\d+){0,2}(?:\.\d+)?$/.test(text)) return null
  const fields = text.split(':').map(Number)
  if (fields.length > 1 && fields.at(-1)! >= 60) return null
  if (fields.length === 3 && fields[1]! >= 60) return null
  const seconds = fields.reduce((total, part) => total * 60 + part, 0)
  return Number.isFinite(seconds) && seconds >= 0 && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null
}

export function timecodeError(text: string, min = 0, max?: number): string {
  const seconds = parseTimecode(text)
  if (seconds === null) return '请输入时:分:秒（可带毫秒），也支持分:秒或秒数。'
  if (seconds < min) return `时间不能早于 ${formatTimecode(min, 3)}。`
  if (typeof max === 'number' && Number.isFinite(max) && seconds > max)
    return `时间不能晚于 ${formatTimecode(max, 3)}。`
  return ''
}
