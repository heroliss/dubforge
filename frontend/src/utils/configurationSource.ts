// 配置来源的唯一视觉词典；不通过模板名称或中文显示文案猜测类型。
export const SOURCE_STYLES = {
  global: { label: '全局默认', color: '#9ABFFF', background: '#202D41' },
  series: { label: '系列模板', color: '#C9B2F6', background: '#30283F' },
  project: { label: '项目设置', color: '#82D9BE', background: '#20352F' },
  version: { label: '版本设置', color: '#EFC880', background: '#3A3122' },
  line: { label: '本句设置', color: '#F0ADCE', background: '#3A2935' },
  publication: { label: '稿件设置', color: '#F2B492', background: '#3B2D27' },
  content: { label: '视频资料', color: '#AEC4D8', background: '#26323C' },
  snapshot: { label: '稿件快照', color: '#BCC4D0', background: '#2B3039' },
  unknown: { label: '来源待确认', color: '#BCC4D0', background: '#2B3039' },
} as const
export type ConfigurationSource = { kind: keyof typeof SOURCE_STYLES; name?: string }

export function scopeSource(scope: string, name?: string): ConfigurationSource {
  if (scope === 'global') return { kind: 'global' }
  if (scope.startsWith('series:')) return { kind: 'series', name }
  if (scope.startsWith('project:')) return { kind: 'project' }
  return { kind: 'unknown' }
}

export function sourcePresentation(source?: ConfigurationSource, inherited = false) {
  const kind = source && Object.hasOwn(SOURCE_STYLES, source.kind) ? source.kind : 'unknown'
  const style = SOURCE_STYLES[kind]
  const detail = source?.name ? `${style.label}「${source.name}」` : style.label
  const title = kind === 'snapshot' ? '稿件已冻结；此快照未记录更细的配置来源'
    : kind === 'unknown' ? '尚未取得配置来源信息，不推测为全局或项目设置'
    : `${inherited ? '继承自' : '来源：'}${detail}`
  return { ...style, kind, title }
}
