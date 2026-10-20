import { playbackTime } from './dubbingPlaybackTime.ts'

export type SubtitleMode = 'source' | 'target' | 'bilingual'
export type SubtitleLine = { id: number; start: number; end: number; source_text: string; text: string;
  playback_start?: number | null; playback_end?: number | null }

function escapeText(text: string) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// Chromium 的原生 WebVTT 渲染器通常会自动折行，但对很长的 CJK 行和
// 混合中英文行并不总是稳定：字幕 cue 可能被压成一行，超出画面后直接裁掉。
// 在生成预览文件时先插入保守的换行，仍保留 cue 的时间和文字内容。这里用
// 显示宽度而非字符串长度，CJK 字符按两个窄字符估算，避免英文短词被拆开。
const CUE_LINE_UNITS = 42
const CJK_CUE_CHAR = /[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af]/u

function cueUnits(value: string) {
  return Array.from(value).reduce((total, char) => total + (CJK_CUE_CHAR.test(char) ? 2 : 1), 0)
}

function wrapCueLine(value: string, maxUnits = CUE_LINE_UNITS) {
  const tokens = value.match(/\s+|[A-Za-z0-9À-ÖØ-öø-ÿ]+(?:['’][A-Za-z]+)*|./gu) || []
  const lines: string[] = []
  let current = ''
  const flush = () => {
    const line = current.trim()
    if (line) lines.push(line)
    current = ''
  }
  for (const token of tokens) {
    if (/^\s+$/u.test(token)) {
      if (current && !current.endsWith(' ')) current += ' '
      continue
    }
    const next = current ? current + token : token
    if (!current || cueUnits(next) <= maxUnits) {
      current = next
      continue
    }
    flush()
    // 极长英文 URL/标识符没有自然空格时仍要切开，否则它会撑破 cue。
    let rest = token
    while (rest && cueUnits(rest) > maxUnits) {
      let cut = 0
      let used = 0
      for (const char of Array.from(rest)) {
        const width = CJK_CUE_CHAR.test(char) ? 2 : 1
        if (used + width > maxUnits) break
        used += width
        cut += char.length
      }
      if (!cut) break
      lines.push(rest.slice(0, cut))
      rest = rest.slice(cut)
    }
    current = rest
  }
  flush()
  return lines.length ? lines : ['']
}

export function wrapCueText(text: string) {
  // 双语 cue 的显式换行代表语言边界，分别折行，避免一门语言过长时把另一门
  // 语言挤到同一行；原本的空行也不影响字幕内容。
  return text.split(/\r?\n/u).flatMap(line => wrapCueLine(line)).join('\n')
}
function timestamp(seconds: number) {
  const ms = Math.round(seconds * 1000)
  return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`
}

// 各语言共用服务端播放轴；没有有效排布时兼容源时间，不修改校对稿。
export function subtitlesToVtt(lines: readonly SubtitleLine[], mode: SubtitleMode, sourceFirst = false) {
  const cues = lines.flatMap(line => {
    const { start, end } = playbackTime(line)
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) return []
    const source = (line.source_text || '').replace(/\s+/g, ' ').trim()
    const target = (line.text || '').replace(/\s+/g, ' ').trim()
    const text = mode === 'source' ? source : mode === 'target' ? target
      : (sourceFirst ? [source, target] : [target, source]).filter(Boolean).join('\n')
    return text ? [`${line.id}\n${timestamp(start)} --> ${timestamp(end)}\n${escapeText(wrapCueText(text))}`] : []
  })
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

export function srtToVtt(text: string) {
  const blocks = text.replace(/^\uFEFF/, '').trim().split(/\r?\n\s*\r?\n/)
  const cues = blocks.flatMap(block => {
    const lines = block.split(/\r?\n/)
    const timing = lines.findIndex(line => /^\d{2,}:\d{2}:\d{2}[,.]\d{3} --> \d{2,}:\d{2}:\d{2}[,.]\d{3}/.test(line))
    if (timing < 0 || timing === lines.length - 1) return []
    return [`${lines[timing].replace(/,/g, '.')}\n${escapeText(wrapCueText(lines.slice(timing + 1).join('\n')))}`]
  })
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}
