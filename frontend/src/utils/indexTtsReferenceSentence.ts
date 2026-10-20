export interface ReferenceSentence { id: number; start: number; end: number; text: string }

const PREFIX = 'tts.indextts25.'
export function isIndexTtsReferenceSentence(path: string): boolean {
  return path === PREFIX + 'timbre_reference_line_id' || path === PREFIX + 'emo_reference_line_id'
}

// 本句模式只派生显示值，不把打开表单当成一次参考句修改。
export function currentIndexTtsReferenceLine(path: string, values: Record<string, any>, lineId?: number): number | undefined {
  if (!isIndexTtsReferenceSentence(path) || !Number.isInteger(lineId) || (lineId ?? 0) <= 0) return
  const modePath = path.replace('_reference_line_id', '_mode')
  if (values[modePath] === 'dynamic') return lineId
}

// 显式从本句切到指定句时，沿用刚才看到的参考；载入、继承和完整预设不走此转换。
export function indexTtsReferenceModeCarry(path: string, next: any, values: Record<string, any>, lineId?: number) {
  if (![PREFIX + 'timbre_mode', PREFIX + 'emo_mode'].includes(path) || next !== 'stable') return
  const referencePath = path.replace('_mode', '_reference_line_id')
  const current = currentIndexTtsReferenceLine(referencePath, values, lineId)
  if (current != null) return { path: referencePath, value: current }
}

function timestamp(seconds: number): string {
  const tenths = Math.max(0, Math.round(seconds * 10))
  const value = Math.floor(tenths / 10)
  const hours = Math.floor(value / 3600), minutes = Math.floor(value / 60) % 60
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}.${tenths % 10}`
}

export function referenceSentenceOptions(lines: ReferenceSentence[], selected: number) {
  const options = lines.map(line => ({ value: line.id,
    label: `第 ${line.id} 句 · ${timestamp(line.start)}–${timestamp(line.end)} · ${line.text}` }))
  // 载入前或源稿更换后保留用户选择，不悄悄切换到另一位说话人。
  if (selected > 0 && !options.some(option => option.value === selected))
    options.unshift({ value: selected, label: `第 ${selected} 句` })
  return options
}

// 两个参考选择器共用正在进行的读取；完成后不缓存，重新打开表单可读到新源稿。
const pending = new Map<string, Promise<ReferenceSentence[]>>()
export function loadReferenceSentences(uid: string): Promise<ReferenceSentence[]> {
  const existing = pending.get(uid)
  if (existing) return existing
  const request = (async () => {
    const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/workbench/reference-lines`, { cache: 'no-store' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '读取参考句失败')
    return (Array.isArray(data.lines) ? data.lines : []) as ReferenceSentence[]
  })()
  pending.set(uid, request)
  const cleanup = () => { if (pending.get(uid) === request) pending.delete(uid) }
  void request.then(cleanup, cleanup)
  return request
}
