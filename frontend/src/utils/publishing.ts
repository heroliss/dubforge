export const PUBLICATION_STATUS: Record<string, string> = {
  draft: '本地草稿', queued: '排队中', uploading: '上传文件中', uploaded: '已上传 · 待确认投稿',
  submitting: '正在提交稿件', unknown: '投稿结果待核对', submitted: '已投稿 · 查看平台状态',
  published: '已公开发布', private: '已通过 · 仅自己可见', reviewed: '已通过 · 保持原稿可见性', rejected: '未通过审核', paused: '已暂停', failed: '上传失败',
  joining: '正在加入合集', collection_pending: '已投稿 · 待加入合集', checking: '正在核对状态',
}
export const PUBLISH_ACTIVE = ['queued', 'uploading', 'submitting', 'joining', 'checking']
export function publicationUploadProgress(row: any) {
  if (!row || !['queued', 'uploading', 'paused', 'failed', 'uploaded', 'submitting', 'joining', 'checking'].includes(row.status)) return null
  const detail = row.progress_detail || {}
  const hasBytes = Number.isFinite(detail.loaded) && Number.isFinite(detail.total) && detail.total > 0
  const raw = hasBytes ? 100 * detail.loaded / detail.total : Number(row.progress || 0)
  const percentage = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 0
  const phases: Record<string, string> = {
    queued: '等待发布任务', validating: '正在核对账号与投稿资料', preparing: '正在准备上传线路',
    uploading: '正在上传视频文件', merging: 'B站正在合并上传分片', cover: '正在处理并上传封面',
    uploaded: '文件已上传，等待确认投稿', submitting: '正在提交稿件资料', joining: '正在加入目标合集', checking: '正在查询平台状态',
  }
  const phase = row.status === 'paused' ? '上传已暂停，继续时复用已完成分P'
    : row.status === 'failed' ? '上传未完成，请检查错误后继续'
    : row.status === 'uploaded' ? phases.uploaded
    : phases[detail.phase] || phases[row.status] || PUBLICATION_STATUS[row.status]
  const part = detail.current_part > 0 ? `P${detail.current_part} / ${detail.total_parts}${detail.current_title ? ` · ${detail.current_title}` : ''}` : ''
  return { percentage, hasBytes, loaded: detail.loaded, total: detail.total, phase, part,
    complete: percentage >= 100,
    merging: percentage >= 100 && detail.phase === 'merging',
    active: PUBLISH_ACTIVE.includes(row.status),
    completedParts: detail.completed_parts, totalParts: detail.total_parts }
}
export function publicationStage(status?: string) {
  return {
    text: PUBLICATION_STATUS[status || ''] || '待准备',
    tone: (['published', 'private', 'reviewed'].includes(status || '') ? 'success'
      : ['failed', 'rejected'].includes(status || '') ? 'error'
      : ['unknown', 'collection_pending', 'paused'].includes(status || '') ? 'warning'
      : status && status !== 'pending' ? 'info' : 'default') as 'success' | 'error' | 'warning' | 'info' | 'default',
    dot: ['published', 'private', 'reviewed'].includes(status || '') ? 'done'
      : ['failed', 'rejected'].includes(status || '') ? 'failed'
      : PUBLISH_ACTIVE.includes(status || '') ? 'running'
      : ['unknown', 'collection_pending', 'paused'].includes(status || '') ? 'stale' : 'pending',
  }
}
export const LOGIN_STATUS: Record<string, string> = {
  generating: '正在获取二维码…', waiting: '请使用B站App扫码', scanned: '已扫码，请在B站App确认登录',
  verifying: '手机端已确认，正在校验账号…', saving: '账号已验证，正在安全保存…',
  connected: '账号已连接', cancelled: '已取消登录', expired: '二维码已过期，请重新获取', failed: '连接失败，请重试',
}
export function collectionAvailability(values: any, metadata: any, state: {account?: string; ready?: boolean; loading?: boolean; error?: string}) {
  if (!values.season_id) return { status: 'none', message: '' }
  if (!state.account) return { status: 'unknown', message: '选择账号后核对目标合集；当前保留原 ID。' }
  if (state.loading) return { status: 'unknown', message: '正在核对目标合集…' }
  if (state.error || metadata?.collection_error || metadata?.collections_complete === false)
    return { status: 'unknown', message: '未能完整核对合集，请刷新重试；当前保留原选择。' }
  if (!state.ready || !Array.isArray(metadata?.seasons)) return { status: 'unknown', message: '目标合集尚未核对，请刷新账号选项。' }
  const season = metadata.seasons.find((s: any) => s.id === values.season_id)
  if (!season) return { status: 'missing', message: `合集 ID ${values.season_id} 已不可用（可能已删除或权限变化），请重新选择或清空为“不加入合集”。` }
  if (values.section_id && !season.sections?.some((s: any) => s.id === values.section_id))
    return { status: 'section_missing', message: `「${season.title}」中的原小节已不可用，请重新选择小节。` }
  return { status: 'valid', message: `当前B站名称：${season.title} · ID ${season.id}` }
}

export function retainSelectedOption(options: {label: string; value: number}[], selected: number | null | undefined, label: string) {
  return selected && !options.some(o => o.value === selected)
    ? [...options, { value: selected, label, disabled: true }] : options
}
export function movePart<T>(parts: T[], index: number, delta: number): T[] {
  const target = index + delta
  if (index < 0 || index >= parts.length || target < 0 || target >= parts.length) return parts
  const result = [...parts]
  ;[result[index], result[target]] = [result[target], result[index]]
  return result
}
export function categoryOptions(items: any[]): { label: string; value: number }[] {
  const result: { label: string; value: number }[] = []
  function walk(rows: any[], prefix = '') {
    for (const row of rows || []) {
      const label = prefix + (row.name || row.title || String(row.id))
      if (Array.isArray(row.children) && row.children.length) walk(row.children, label + ' / ')
      else if (row.id) result.push({ label, value: Number(row.id) })
    }
  }
  walk(items)
  return result
}
export function publicationScope(snapshot: any) {
  const s = snapshot.settings
  return `账号「${snapshot.account_name}」· 1个稿件 · ${snapshot.parts.length}个分P · ${s.visibility === 'self' ? '仅自己可见' : '公开可见'}${s.season_id ? ' · 加入所选合集' : ''} · ${s.publish_at ? new Date(s.publish_at * 1000).toLocaleString('zh-CN') + ' 定时发布' : '立即提交审核'}`
}

export function publicationAiNotice(preview: any, metadata: any) {
  const snapshot = preview.snapshot
  if (preview.ai_declaration_acknowledged || !snapshot?.parts?.some((p: any) => p.has_dubbing)) return ''
  const choice = metadata?.declarations?.find((d: any) => d.value === snapshot.settings.declaration_id)
  if (snapshot.settings.copyright !== 2 && /人工智能|AI|合成技术/i.test(choice?.label || '')) return ''
  return '所选分P含配音版本，建议核对AI声明。确认后按当前声明继续，不会自动更改；平台可能要求补标。'
}
export function withSourcePublishedDate(description: string, date: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return description
  const line = `原视频发布时间：${date}`
  const original = /^原视频发布时间[ \t]*[：:][ \t]*\d{4}-\d{2}-\d{2}[ \t]*\r?$/gm
  // 只更新程序来源行；手写的其他内容、作者与网址均保留。
  if (original.test(description)) {
    let found = false
    return description.replace(original, () => { if (found) return ''; found = true; return line })
  }
  return description.trimEnd() + (description.trim() ? '\n\n' : '') + line
}

