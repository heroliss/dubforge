import { formatTimecode } from './timecode.ts'

// 字幕项目设置只有一个编辑入口；成片页仍保留流程开关及混音、画质。
export const SUBTITLE_STYLE_PATHS = [
  'assemble.subtitle_track', 'assemble.subtitle_font', 'assemble.subtitle_size',
  'assemble.subtitle_auto_fit', 'assemble.subtitle_min_size',
  'assemble.subtitle_shrink_width_percent', 'assemble.subtitle_wrap_width_percent',
  'assemble.subtitle_color', 'assemble.subtitle_outline_color',
  'assemble.subtitle_outline', 'assemble.subtitle_margin',
  'assemble.subtitle_background_color', 'assemble.subtitle_background_opacity',
  'assemble.subtitle_source_style_enabled', 'assemble.subtitle_source_font', 'assemble.subtitle_source_size',
  'assemble.subtitle_source_min_size',
  'assemble.subtitle_source_color', 'assemble.subtitle_source_outline_color', 'assemble.subtitle_source_outline',
]
export const SUBTITLE_PREVIEW_PATHS = [...SUBTITLE_STYLE_PATHS,
  'subtitles.source_first', 'subtitles.min_duration']
export const SUBTITLE_WORKSPACE_PATHS = [...SUBTITLE_PREVIEW_PATHS, 'subtitles.mode']

export function subtitleTimelineDuration(duration: number | undefined, lines: Array<{ end: number }>) {
  if (duration != null && Number.isFinite(duration) && duration > 0) return duration
  return lines.reduce((end, line) => Number.isFinite(line.end) ? Math.max(end, line.end) : end, 0)
}
export function subtitleTimeLabel(seconds: number | null) {
  return formatTimecode(seconds, 1)
}
export function subtitleSettingsLink(uid: string) {
  return { path: `/projects/${encodeURIComponent(uid)}/steps/tts`, query: { panel: 'subtitles', from: 'assemble' } }
}
export function subtitleStyleSummary(cfg: Record<string, any> = {}) {
  const trackKey = cfg.subtitle_track || 'trans'
  const track = ({ source: '原文', trans: '译文', bilingual: '双语' } as Record<string, string>)[trackKey] || '未设置'
  const autoFit = cfg.subtitle_auto_fit !== false
  const independentSource = !!cfg.subtitle_source_style_enabled
  const sizeRange = (source = false) => {
    const size = source ? cfg.subtitle_source_size ?? 40 : cfg.subtitle_size ?? 48
    const minimum = Math.min(size, source ? cfg.subtitle_source_min_size ?? 28 : cfg.subtitle_min_size ?? 32)
    return autoFit ? `${minimum}–${size}` : `${size}`
  }
  const font = (trackKey === 'source' && independentSource ? cfg.subtitle_source_font : cfg.subtitle_font) || 'Microsoft YaHei'
  const sizing = trackKey === 'bilingual' && independentSource
    ? `译文 ${sizeRange()} / 原文 ${sizeRange(true)}` : `字号 ${sizeRange(trackKey === 'source' && independentSource)}`
  const widths = autoFit ? `缩小 ${cfg.subtitle_shrink_width_percent ?? 80}% / 换行 ${cfg.subtitle_wrap_width_percent ?? 90}%`
    : `换行 ${cfg.subtitle_wrap_width_percent ?? 90}%`
  return `${track} · ${font} · ${sizing} · ${widths} · 底距 ${cfg.subtitle_margin ?? 50}（1080 高） · ${autoFit ? '优先缩小，到下限后利用缓冲区再换行' : '自动换行'}；原文、译文各自排版`
}
