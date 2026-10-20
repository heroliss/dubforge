import { finalVideoLabel } from './preview.ts'

const nonempty = (value: unknown) => typeof value === 'string' ? value.trim() : ''

// 服务端旧版数据库的无时区 ISO 时间为 UTC；不能按浏览器本地时间误读。
export function videoFileTime(value: unknown) {
  const raw = nonempty(value)
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(raw)) return null
  const normalized = raw.replace(' ', 'T') + (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? '' : 'Z')
  const date = new Date(normalized)
  if (!Number.isFinite(date.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return { iso: date.toISOString(), text: `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` }
}

/** 发布记录的旧无时区字段同样是 UTC；按本机日期显示，未知时间保持明确。 */
export function publicationRecordDate(value: unknown) {
  const time=videoFileTime(value)
  return time?new Date(time.iso).toLocaleDateString('zh-CN'):'时间未知'
}

export function videoFileInfo(file: any) {
  const filename = nonempty(file?.name) || nonempty(file?.artifact_name) || nonempty(file?.path).split(/[\\/]/).pop() || ''
  const generatedName = nonempty(file?.version_generated_name), currentName = nonempty(file?.version_name)
  const title = file?.is_source ? '原声原片' : generatedName || currentName || (filename ? finalVideoLabel(filename) : '视频文件')
  const generated = videoFileTime(file?.generated_at)
  const registered = videoFileTime(file?.registered_at) || videoFileTime(file?.created_at)
  const time = generated || registered
  const timeLabel = file?.is_source ? '获取时间' : generated ? '生成时间' : '登记时间'
  return { title, filename,
    summary: nonempty(file?.version_summary) || (file?.is_source ? '不叠加字幕或评论' : ''),
    currentName: generatedName && currentName && generatedName !== currentName ? currentName : '',
    timeLabel, timeValue: time?.text || '', timeIso: time?.iso || '', timeText: time ? `${timeLabel} ${time.text}` : '',
    status: file?.exists === false ? '文件缺失' : file?.review_ready === false ? '需重新审查或合成' : '',
  }
}

// 选择器的搜索包含名称、时间、设置摘要和文件名，显示层不再以文件名作为标题。
export function videoFileLabel(file: any) {
  const info = videoFileInfo(file)
  return [info.title, info.timeText, info.summary, info.currentName && `当前版本名：${info.currentName}`, info.filename, info.status].filter(Boolean).join(' · ')
}
