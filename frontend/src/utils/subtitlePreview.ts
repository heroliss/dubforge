export type SubtitleMode = 'source' | 'target' | 'bilingual'
export type SubtitleLine = { id: number; start: number; end: number; source_text: string; text: string }

function escapeText(text: string) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
function timestamp(seconds: number) {
  const ms = Math.round(seconds * 1000)
  return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`
}

// 直接使用校对稿的时间，不应用仅用于文件导出的最短显示时长。
export function subtitlesToVtt(lines: readonly SubtitleLine[], mode: SubtitleMode, sourceFirst = false) {
  const cues = lines.flatMap(line => {
    if (!Number.isFinite(line.start) || !Number.isFinite(line.end) || line.start < 0 || line.end <= line.start) return []
    const source = (line.source_text || '').replace(/\s+/g, ' ').trim()
    const target = (line.text || '').replace(/\s+/g, ' ').trim()
    const text = mode === 'source' ? source : mode === 'target' ? target
      : (sourceFirst ? [source, target] : [target, source]).filter(Boolean).join('\n')
    return text ? [`${line.id}\n${timestamp(line.start)} --> ${timestamp(line.end)}\n${escapeText(text)}`] : []
  })
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

export function srtToVtt(text: string) {
  const blocks = text.replace(/^\uFEFF/, '').trim().split(/\r?\n\s*\r?\n/)
  const cues = blocks.flatMap(block => {
    const lines = block.split(/\r?\n/)
    const timing = lines.findIndex(line => /^\d{2,}:\d{2}:\d{2}[,.]\d{3} --> \d{2,}:\d{2}:\d{2}[,.]\d{3}/.test(line))
    if (timing < 0 || timing === lines.length - 1) return []
    return [`${lines[timing].replace(/,/g, '.')}\n${escapeText(lines.slice(timing + 1).join('\n'))}`]
  })
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}
