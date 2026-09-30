// 字幕项目设置只有一个编辑入口；成片页仍保留流程开关及混音、画质。
export const SUBTITLE_STYLE_PATHS = [
  'assemble.subtitle_track', 'assemble.subtitle_font', 'assemble.subtitle_size',
  'assemble.subtitle_color', 'assemble.subtitle_outline_color',
  'assemble.subtitle_outline', 'assemble.subtitle_margin',
  'assemble.subtitle_background_color', 'assemble.subtitle_background_opacity',
  'assemble.subtitle_source_style_enabled', 'assemble.subtitle_source_font', 'assemble.subtitle_source_size',
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
  if (seconds == null || !Number.isFinite(seconds)) return '00:00.0'
  const tenths = Math.floor(Math.max(0, seconds) * 10)
  const minutes = Math.floor(tenths / 600), rest = tenths % 600
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(rest / 10)).padStart(2, '0')}.${rest % 10}`
}
export function subtitleSettingsLink(uid: string) {
  return { path: `/projects/${encodeURIComponent(uid)}/steps/tts`, query: { panel: 'subtitles', from: 'assemble' } }
}
export function subtitleStyleSummary(cfg: Record<string, any> = {}) {
  const track = ({ source: '原文', trans: '译文', bilingual: '双语' } as Record<string, string>)[cfg.subtitle_track || 'trans'] || '未设置'
  return `${track} · ${cfg.subtitle_font || 'Microsoft YaHei'} · 字号 ${cfg.subtitle_size ?? 48} · 底距 ${cfg.subtitle_margin ?? 50}`
}
