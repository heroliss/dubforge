export const PUBLICATION_STATUS: Record<string, string> = {
  draft: '本地草稿', queued: '排队中', uploading: '上传文件中', uploaded: '已上传 · 待确认投稿',
  submitting: '正在提交稿件', unknown: '投稿结果待核对', submitted: '已投稿 · 查看平台状态',
  published: '已公开发布', private: '已通过 · 仅自己可见', reviewed: '已通过 · 保持原稿可见性', rejected: '未通过审核', paused: '已暂停', failed: '上传失败',
  joining: '正在加入合集', collection_pending: '已投稿 · 待加入合集', checking: '正在核对状态',
}
export const PUBLISH_ACTIVE = ['queued', 'uploading', 'submitting', 'joining', 'checking']
/** 后台仍可能推进的回执；等待审核不属于正在写入，不能复用操作锁的状态集合。 */
export function publicationReadbackPending(status: unknown): boolean {
  return typeof status==='string' && (PUBLISH_ACTIVE.includes(status) || ['submitted','collection_pending'].includes(status))
}
// 稿件已完成平台交付：公开、仅自己可见和保留原稿可见性都属于成功终态。
// 该维度只用于交付筛选，不改变项目本地制作 status。
export const PUBLISHED_PUBLICATION_STATUSES = ['published', 'private', 'reviewed'] as const
export function isPublicationComplete(status: unknown): boolean {
  return typeof status === 'string' && (PUBLISHED_PUBLICATION_STATUSES as readonly string[]).includes(status)
}
/**
 * 合集显示同时保留可读名称和稳定 ID；旧记录没有名称时仍给出明确的 ID 标签。
 * 名称只是平台资料的显示快照，不用于判断合集是否仍然有效。
 */
export function collectionDisplay(id: unknown, title?: unknown, sectionTitle?: unknown): string {
  if (id === null || id === undefined || id === '' || Number(id) <= 0) return '不加入合集'
  const parts = [title, sectionTitle].map(value => typeof value === 'string' ? value.trim() : '').filter(Boolean)
  parts.push(`ID ${id}`)
  return parts.length > 1 ? parts.join(' · ') : `合集 · ${parts[0]}`
}

/** 显示名称与发布身份分离；不为读取名称追加平台请求，也不影响运行指纹。 */
export function publicationTargetDisplay(plan: any) {
  const settings = plan?.settings || plan?.scope || plan?.snapshot?.settings || {}
  const accountId = plan?.account_id || plan?.recipe?.account_id || settings.account_id || plan?.snapshot?.account_id
  // 顺序批次的展示信息可能保存在各项目预检中；必须完整匹配目标身份后才可采用。
  const nested = [...(plan?.items || []), ...(plan?.plan?.items || [])]
    .map((item:any)=>item?.plan?.settings_display)
    .find((item:any)=>item && String(item.account_id)===String(accountId)
      && String(item.season_id ?? '')===String(settings.season_id ?? '')
      && String(item.section_id ?? '')===String(settings.section_id ?? ''))
  const raw = plan?.settings_display || nested || plan?.scope_display || plan?.plan?.scope_display || {}
  const matches = (key: string, value: unknown) => raw[key] === undefined || raw[key] === null || String(raw[key]) === String(value ?? '')
  const accountMatches = matches('account_id', accountId)
  const display = accountMatches ? raw : {}
  const legacy = plan?.scope_display || plan?.plan?.scope_display || {}
  const name = (value: unknown) => typeof value === 'string' ? value.trim() : ''
  const authoritative = ['platform','cache'].includes(name(display.source))
  const identity = (label: string, id: unknown, title: unknown) => {
    const readable = name(title)
    return readable ? `${readable}${id ? `（ID ${id}）` : ''}` : id ? `${label}（ID ${id}，名称未载入）` : ''
  }
  // 老记录的名称仅在其自身身份下采用；显式绑定不一致时不能借用其他账号或合集名称。
  const seasonMatches = accountMatches && matches('season_id', settings.season_id)
  const seasonTitle = seasonMatches ? name(display.season_title) || (!authoritative ? name(settings.season_title) || name(legacy.season_title) : '') : ''
  const sectionTitle = seasonMatches && matches('section_id', settings.section_id)
    ? name(display.section_title) || (!authoritative ? name(settings.section_title) || name(legacy.section_title) : '') : ''
  let collection = collectionDisplay(settings.season_id, seasonTitle, sectionTitle)
  if (settings.season_id && !seasonTitle) collection += '（名称未载入）'
  if (settings.season_id && settings.section_id && !sectionTitle) collection += ` · 小节（ID ${settings.section_id}，名称未载入）`
  const categoryId = settings.human_type2 || settings.tid
  const category = identity('分区', categoryId, matches('human_type2', categoryId) ? display.category_title : '')
  const account = identity('账号', accountId, name(plan?.account_name) || display.account_name)
  const source = accountMatches ? name(display.source) : ''
  const notice = source === 'cache' ? '名称来自最近读取的B站资料，仅供识别；本次仍按已确认的账号、分区和合集ID执行。'
    : source === 'platform' ? '名称来自本次读取的B站资料；发布目标仍以已确认的ID为准。' : ''
  const fields = [
    ...(account ? [{ key:'account', label:'账号', title:name(plan?.account_name) || name(display.account_name) || '名称未载入', id:accountId }] : []),
    ...(category ? [{ key:'category', label:'分区', title:matches('human_type2',categoryId) && name(display.category_title) || '名称未载入', id:categoryId }] : []),
    { key:'season', label:'合集', title:settings.season_id ? seasonTitle || '名称未载入' : '不加入合集', id:settings.season_id },
    ...(settings.season_id && settings.section_id ? [{ key:'section', label:'小节', title:sectionTitle || '名称未载入', id:settings.section_id }] : []),
  ]
  return { account, collection, category, notice, fields, cached:source === 'cache' }
}

export function publicationVisibility(value: unknown): string {
  return value === 'self' ? '仅自己可见' : value === 'public' || !value ? '公开可见' : '可见范围未识别，请核对发布设置'
}

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

