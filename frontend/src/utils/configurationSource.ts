// 配置来源的唯一视觉词典；不通过模板名称或中文显示文案猜测类型。
export const SOURCE_STYLES = {
  global: { label: '全局默认', color: '#9ABFFF', background: '#202D41' },
  series: { label: '系列模板', color: '#C9B2F6', background: '#30283F' },
  project: { label: '项目设置', color: '#82D9BE', background: '#20352F' },
  version: { label: '版本设置', color: '#EFC880', background: '#3A3122' },
  line: { label: '本句设置', color: '#F0ADCE', background: '#3A2935' },
  line_default: { label: '跟随本句译文', color: '#AEC4D8', background: '#26323C' },
  publication: { label: '稿件设置', color: '#F2B492', background: '#3B2D27' },
  content: { label: '视频资料', color: '#AEC4D8', background: '#26323C' },
  snapshot: { label: '稿件快照', color: '#BCC4D0', background: '#2B3039' },
  baseline: { label: '基准成片', color: '#BCC4D0', background: '#2B3039' },
  unknown: { label: '来源待确认', color: '#BCC4D0', background: '#2B3039' },
} as const
export type ConfigurationSource = { kind: keyof typeof SOURCE_STYLES; name?: string; frozen?: boolean; version_name?: string }

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
  const baseTitle = kind === 'snapshot' ? '稿件已冻结；此快照未记录更细的配置来源'
    : kind === 'baseline' ? `沿用${detail}已保存的值；这些历史值不随当前项目或全局默认变化`
    : kind === 'line_default' ? '未指定本句读音时，直接朗读当前译文；字幕保持原样'
    : kind === 'unknown' ? '尚未取得配置来源信息，不推测为全局或项目设置'
    : source?.frozen ? `项目基础值来自${detail}；保留创建或上次应用时的值，不随来源自动变化，不代表最新默认值`
    : `${inherited ? '继承自' : '来源：'}${detail}`
  const title = baseTitle + (source?.version_name ? `；版本「${source.version_name}」的专用参数` : '')
  const label = source?.frozen && kind === 'global' ? '已存全局值' : source?.frozen && kind === 'series' ? '已存模板值' : style.label
  return { ...style, kind, label, title }
}
