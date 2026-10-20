// 章节是取得投稿回执后的独立任务，失败或待核对不改变视频投稿状态。
const labels: Record<string, string> = {
  pending: '章节等待处理', running: '正在设置视频章节', completed: '视频章节已设置',
  skipped: '本次跳过视频章节', failed: '视频章节设置失败', unknown: '章节结果待核对',
}

export function publicationChapterScope(settings: any, update = false) {
  const values = settings?.recipe || settings
  if (values?.chapters_enabled !== true) return update ? '本次原稿更新不自动设置视频章节。' : '本次投稿不自动设置视频章节。'
  return `${update ? values?.replace_videos===false ? '按现有稿件已确认的视频时间轴整理并同步章节' : '替换视频按新成片时间轴整理章节，平台就绪后自动同步' : '投稿后自动设置视频章节'}${values?.chapters_translate === false ? '，保留源章节标题的原语言' : '，标题翻译为中文'}，按平台规则合并相邻主题并精简标题；源视频无章节时跳过。`
}

export function publicationChapterState(publication: any) {
  const sync = publication?.chapter_sync || {}, status = String(sync.status || '')
  const enabled = sync.enabled ?? publication?.snapshot?.chapter_options?.enabled
    ?? publication?.snapshot?.settings?.chapters_enabled ?? false
  return {
    status, enabled, label: labels[status] || (enabled ? '等待审核后设置章节' : '本次投稿未启用视频章节'),
    tone: (status === 'completed' ? 'success' : status === 'failed' ? 'error'
      : status === 'unknown' ? 'warning' : status === 'running' || status === 'pending' ? 'info' : 'default') as 'success' | 'error' | 'warning' | 'info' | 'default',
    message: String(sync.error || sync.message || sync.reason || ({
      waiting_review:'等待视频审核通过后设置章节。', awaiting_review:'等待视频审核通过后设置章节。',
      translating:'正在翻译并整理章节，合并相邻主题并校验平台规则。', adapting:'正在按当前分P规则整理章节。', preparing:'正在核对源视频章节与各分P时间轴。',
      reconciling:'正在核对B站已有章节。', applying:'正在设置各分P章节。',
    } as Record<string,string>)[String(sync.phase)] || ''),
    parts: Array.isArray(sync.parts) ? sync.parts : [],
    supplement: !enabled && (!status || status === 'skipped'),
    retry: status === 'failed' && sync.retryable !== false,
    reconcile: status === 'unknown',
    active: status === 'pending' || status === 'running',
  }
}

export function chapterPartStatus(status: unknown) {
  return labels[String(status)] || '尚未处理'
}

// 自动任务的候选已经按目标成片整理；只按确切CID对应，远端已有章节仍由面板优先保留。
export function preferredPublicationChapterParts(publication: any) {
  const base = publication?.chapter_parts || publication?.snapshot?.parts || []
  const prepared = Array.isArray(publication?.chapter_sync?.parts) ? publication.chapter_sync.parts : []
  return base.map((part: any) => {
    const cid = part?.chapter_cid ?? part?.cid
    const match = cid != null ? prepared.find((row: any) => row?.cid != null && String(row.cid) === String(cid)) : null
    return match?.items?.length ? { ...part, chapter_cid: cid, chapter_items: match.items.map((item: any) => ({ ...item })),
      chapter_adaptation: match.adaptation, chapter_timeline: { kind: 'adapted', label: '本次自动任务已整理的成片章节' } } : part
  })
}
