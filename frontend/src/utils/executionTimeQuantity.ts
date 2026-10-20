import { formatTimecode } from './timecode.ts'

// 只接受明确以秒为量纲的工作量；计数、帧率和百分比不得猜成时间。
const TIME_UNITS: Record<string, string> = {
  '成片秒': '成片时长', '音频秒': '音频时长', '片段秒': '片段时长',
  '秒': '', s: '', second: '', seconds: '',
}

function durationText(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return ''
  // 进度读数沿用现有时码的整秒舍入；原秒值仍供比例与 ETA 计算。
  return formatTimecode(value)
}

/** 仅形成呈现文本；保留生产者的秒值、原始单位、比例和未知量。 */
export function executionTimeQuantity(completed: unknown, total: unknown, unit: unknown) {
  if (typeof unit !== 'string' || !Object.prototype.hasOwnProperty.call(TIME_UNITS, unit)) return null
  return { completedText: durationText(completed), totalText: durationText(total), displayUnit: TIME_UNITS[unit] }
}
