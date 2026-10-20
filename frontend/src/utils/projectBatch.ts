export type ProjectBatchTarget = { uid: string; title: string }
export type ProjectBatchItem = {
  project_uid: string; title: string; source_date?: string; delivery_mode?: string
  recipe?: any; plan?: any; production?: any; target?: any; saved_scope?: any
  status?: string; phase?: string; error?: string; blockers?: string[]; warnings?: string[]
  [key: string]: any
}
export type ProjectBatchGroup = {
  id: string; kind: 'collection' | 'independent'; title?: string; scope?: any
  project_uids?: string[]; items: ProjectBatchItem[]; blockers?: string[]; warnings?: string[]
  status?: string; phase?: string; error?: string; [key: string]: any
}
export const PROJECT_BATCH_ACTIVE = ['running', 'queued', 'monitoring', 'pausing', 'stopping']

export function projectBatchTargets(input: (ProjectBatchTarget | string)[]): ProjectBatchTarget[] {
  return [...new Map(input.map(value => typeof value === 'string' ? { uid: value, title: value } : value)
    .filter(value => value?.uid).map(value => [value.uid, { uid: value.uid, title: value.title || value.uid }])).values()]
}

// 日期未知时保留原次序，不能把项目导入时间当作原视频日期。
export function chronologicalProjectItems(items: ProjectBatchItem[]): ProjectBatchItem[] {
  return items.map((item, index) => ({ item, index })).sort((a, b) => {
    const left = a.item.source_date || '', right = b.item.source_date || ''
    if (!left || !right) return left ? -1 : right ? 1 : a.index - b.index
    return left.localeCompare(right) || a.index - b.index
  }).map(row => row.item)
}

// 只交换本组所在位置，其他合集与独立项目的相对位置不变。
export function moveProjectBatchItem(items: ProjectBatchItem[], group: ProjectBatchGroup,
  index: number, offset: number): ProjectBatchItem[] {
  const ids = group.project_uids || group.items.map(item => item.project_uid)
  const target = index + offset
  if (index < 0 || target < 0 || index >= ids.length || target >= ids.length || offset === 0) return items
  const from = items.findIndex(item => item.project_uid === ids[index])
  const to = items.findIndex(item => item.project_uid === ids[target])
  if (from < 0 || to < 0) return items
  const next = [...items]; [next[from], next[to]] = [next[to], next[from]]
  return next
}

export function projectBatchDelivery(item: any): string {
  return item.recipe?.delivery_mode || item.plan?.recipe?.delivery_mode || item.delivery_mode
    || item.recipe?.mode || item.plan?.recipe?.mode || item.end || 'produce'
}
export function projectBatchPublishCount(groups: ProjectBatchGroup[]): number {
  return groups.flatMap(group => group.items || []).filter(item => projectBatchDelivery(item) === 'publish').length
}
export function projectBatchOrderedGroups(groups: ProjectBatchGroup[]): ProjectBatchGroup[] {
  return groups.filter(group => group.kind === 'collection' && (group.items || []).length > 1)
}
export function projectBatchNeedsDateCheck(group: ProjectBatchGroup): boolean {
  return group.kind === 'collection' && group.items.length > 1 && group.items.some(item => !item.source_date)
}
export function projectBatchStage(status?: string) {
  const stages: Record<string, [string, 'default' | 'info' | 'warning' | 'error' | 'success']> = {
    ready: ['待确认开始', 'default'], queued: ['等待执行', 'default'], running: ['后台执行中', 'info'],
    monitoring: ['等待审核与核验', 'info'], pausing: ['正在暂停', 'warning'], stopping: ['正在停止', 'warning'],
    paused: ['已暂停', 'warning'], stopped: ['已停止', 'warning'], failed: ['需要处理', 'error'],
    attention: ['需要处理', 'warning'], done: ['全部完成', 'success'], completed: ['已完成', 'success'],
  }
  const [label, tone] = stages[status || ''] || ['等待执行', 'default']
  return { label, tone }
}
export function projectBatchControls(status?: string) {
  return { pause: ['running', 'queued', 'monitoring'].includes(status || ''),
    stop: ['running', 'queued', 'monitoring', 'pausing'].includes(status || ''),
    resume: ['ready', 'paused', 'stopped', 'attention', 'failed'].includes(status || '') }
}
export function projectBatchGroupTitle(group: ProjectBatchGroup): string {
  if (group.title) return group.title
  const scope = group.scope_display || group.scope || {}
  return group.kind === 'collection'
    ? [scope.account_name, scope.season_title || '目标合集', scope.section_title].filter(Boolean).join(' · ')
    : group.items?.[0]?.title || '独立项目'
}
export function projectBatchProgress(batch: any): string {
  const groups = batch.groups || [], total = groups.flatMap((group: ProjectBatchGroup) => group.items || []).length
  const done = groups.reduce((count: number, group: ProjectBatchGroup) => count + (['done', 'completed'].includes(group.status || '')
    ? (group.items || []).length : (group.items || []).filter(item => ['done', 'completed'].includes(item.status || '')).length), 0)
  return `${done} / ${total} 个项目已完成`
}

type FailureKind = 'quota' | 'subscription' | 'limited' | 'auth' | 'unavailable' | 'unknown'
type ProviderFailure = { provider: string; kind: FailureKind; label: string }
export type ProjectBatchFailure = {
  step: string; summary: string; reason: string; technical: string; model: boolean; providers: ProviderFailure[]
}
const FAILURE_LABELS: Record<FailureKind, string> = {
  quota: '余额不足或可用配额已耗尽', subscription: '订阅无效、未生效或已过期',
  limited: '请求受限，需核对限流或配额', auth: '认证失败，请核对密钥与权限',
  unavailable: '服务暂不可用，请检查连接或服务商状态', unknown: '请求失败，请查看技术详情',
}
const STEP_LABELS: Record<string, string> = {
  download: '获取视频', preprocess: '音轨分离', transcribe: '语音转文字', split: '语义整理',
  translate: '翻译', tts: '字幕与配音', comments: '上屏评论', compose: '合成成片',
  assemble: '合成成片', publish: '发布到B站', upload: '上传视频',
}
const needsAttention = (status?: string) => ['attention', 'failed'].includes(status || '')
function failureKind(message: string): FailureKind {
  if (/LLM_QUOTA_EXHAUSTED|(?:code["'\s:：]*1113)|余额不足|无可用资源包|配额(?:已耗尽|耗尽|已用尽)|insufficient[_\s-]*(?:quota|credit|balance)/i.test(message)) return 'quota'
  if (/LLM_SUBSCRIPTION_INVALID|InvalidSubscription|订阅.{0,12}(?:无效|未生效|未开通|失效|过期)|(?:没有|无).{0,8}(?:有效)?订阅/i.test(message)) return 'subscription'
  if (/LLM_RATE_LIMITED|HTTP\s*429|限流|请求过于频繁|rate.?limit/i.test(message)) return 'limited'
  if (/LLM_HTTP_40[13]|HTTP\s*40[13]|密钥.{0,8}(?:无效|错误)|认证失败|权限不足|InvalidApiKey|Unauthorized/i.test(message)) return 'auth'
  if (/LLM_HTTP_50\d|HTTP\s*50\d|LLM_OFFLINE|连接失败|连接超时|服务不可用|ServiceUnavailable|ConnectionError/i.test(message)) return 'unavailable'
  return 'unknown'
}
function providerName(value: string): string {
  // 名称属于用户配置，主界面只保留可读名称，账户号与地址留在折叠详情。
  return value.replace(/https?:\/\/\S+/gi, '').replace(/\b\d{8,}\b/g, '').trim().slice(0, 60) || '模型服务'
}
function providerFailures(message: string): ProviderFailure[] {
  const markers = [...message.matchAll(/[【「\[]([^】」\]\r\n]{1,80})[】」\]]\s*[:：]/g)]
  const failures = markers.map((match, index) => {
    const start = (match.index || 0) + match[0].length, end = markers[index + 1]?.index ?? message.length
    const kind = failureKind(message.slice(start, end))
    return { provider: providerName(match[1]), kind, label: FAILURE_LABELS[kind] }
  })
  if (!failures.length) {
    const kind = failureKind(message)
    failures.push({ provider: '模型服务', kind, label: FAILURE_LABELS[kind] })
  }
  return [...new Map(failures.map(row => [`${row.provider.toLowerCase()}:${row.kind}`, row])).values()]
}
function interruptionStep(message: string, phase = ''): string {
  const key = message.match(/\b([a-z_]+)\s*未完成/i)?.[1]?.toLowerCase()
  if (key && STEP_LABELS[key]) return STEP_LABELS[key]
  const known = Object.values(STEP_LABELS).find(label => phase.includes(label) || message.startsWith(label))
  return known || ''
}
function publicationResultUncertain(message: string): boolean {
  return /(?:投稿|发布|上传)?(?:回执|结果).{0,8}(?:未知|未确认|待核对|待核验|不明)|PUBLISH_(?:RECEIPT|RESULT)_UNKNOWN/.test(message)
}
export function projectBatchErrorSummary(message: string): string {
  return publicationResultUncertain(message) ? '发布结果待核验，请先到项目发布页核对平台稿件，避免重复投稿。'
    : '批次上次执行遇到问题，可查看中断详情后检查并继续。'
}

export function projectBatchGroupFailure(group: ProjectBatchGroup): ProjectBatchFailure | null {
  if (!needsAttention(group.status)) return null
  const errors = [group.error, ...(group.items || []).filter(item => needsAttention(item.status)).map(item => item.error)]
    .filter((value): value is string => typeof value === 'string' && !!value.trim())
  const technical = [...new Set(errors)].join('\n\n')
  const step = interruptionStep(technical, group.phase || '')
  const model = /LLM|API 配置|模型|智谱|GLM|火山方舟|InvalidSubscription|余额不足|无可用资源包/i.test(technical)
  const uncertain = publicationResultUncertain(technical)
  return { step, summary: uncertain ? '发布结果待核验' : step ? `上次在${step}中断` : '上次执行中断',
    reason: uncertain ? '请先到项目发布页核对平台稿件，避免重复投稿。' : model ? '' : '可查看上次中断详情，再检查并继续本组。', technical,
    model, providers: model ? providerFailures(technical) : [] }
}

export function projectBatchGroupCounts(batch: any): { key: string; label: string; count: number }[] {
  const counts: Record<string, number> = {}
  for (const group of batch.groups || []) {
    const status = group.status || ''
    const key = needsAttention(status) ? 'attention' : ['done', 'completed'].includes(status) ? 'done'
      : ['paused', 'stopped', 'pausing', 'stopping'].includes(status) ? status
      : PROJECT_BATCH_ACTIVE.includes(status) ? 'running' : 'ready'
    counts[key] = (counts[key] || 0) + 1
  }
  const names: Record<string, string> = { running: '执行中', attention: '待处理', done: '已完成',
    pausing: '正在暂停', stopping: '正在停止', paused: '已暂停', stopped: '已停止', ready: '待运行' }
  return ['running', 'attention', 'pausing', 'stopping', 'paused', 'stopped', 'done', 'ready']
    .filter(key => counts[key]).map(key => ({ key, label: `${counts[key]} 组${names[key]}`, count: counts[key]! }))
}

export function projectBatchIssues(batch: any): { key: string; summary: string; groupIds: string[] }[] {
  // 先按提供商和原因去重，再把影响范围相同的同类问题排成一行。
  const unique = new Map<string, { provider: string; kind: FailureKind; groupIds: string[] }>()
  for (const group of batch.groups || []) {
    for (const failure of projectBatchGroupFailure(group)?.providers || []) {
      const key = `${failure.provider.toLowerCase()}:${failure.kind}${failure.kind === 'unknown' ? `:${group.id}` : ''}`
      const entry = unique.get(key) || { provider: failure.provider, kind: failure.kind, groupIds: [] }
      if (!entry.groupIds.includes(group.id)) entry.groupIds.push(group.id)
      unique.set(key, entry)
    }
  }
  const rows = new Map<string, { providers: string[]; kind: FailureKind; groupIds: string[] }>()
  for (const issue of unique.values()) {
    const key = `${issue.kind}:${[...issue.groupIds].sort().join(',')}`
    const row = rows.get(key) || { providers: [], kind: issue.kind, groupIds: issue.groupIds }
    row.providers.push(issue.provider); rows.set(key, row)
  }
  return [...rows].map(([key, row]) => ({ key, summary: `${row.providers.join('、')}：上次${FAILURE_LABELS[row.kind]}`, groupIds: row.groupIds }))
}

export function projectBatchParentError(batch: any): string {
  const message = typeof batch.error === 'string' ? batch.error.trim() : ''
  if (!message) return ''
  const children = (batch.groups || []).flatMap((group: ProjectBatchGroup) => [group.error, ...(group.items || []).map(item => item.error)])
    .filter((value: unknown): value is string => typeof value === 'string' && !!value.trim())
  let remaining = message
  for (const child of [...new Set<string>(children)].sort((a, b) => b.length - a.length)) remaining = remaining.split(child).join('')
  // 只去掉可确认来自分组聚合的错误，父级独有的调度或投稿回执问题必须保留。
  return remaining.replace(/[；;\s]/g, '') ? message : ''
}

export function projectBatchGroupPhase(group: ProjectBatchGroup): string {
  const failure = projectBatchGroupFailure(group)
  if (failure) return failure.summary
  // 历史错误不代表正在执行的组也失败，状态由调度器结果决定。
  return group.phase || projectBatchStage(group.status).label
}
export function projectBatchItemPhase(item: ProjectBatchItem, group: ProjectBatchGroup): string {
  if (needsAttention(item.status)) return '等待处理后继续'
  if (needsAttention(group.status) && !['done', 'completed'].includes(item.status || '')
    && (!item.phase || /暂停|等待处理|需要处理|重新确认/.test(item.phase))) return '等待继续'
  return item.phase || projectBatchStage(item.status).label
}
export function projectBatchFooter(batch: any): string {
  const counts = projectBatchGroupCounts(batch), running = counts.find(row => row.key === 'running')?.count || 0
  const attention = counts.find(row => row.key === 'attention')?.count || 0
  if (attention && running) return `待处理的组已中断，其余 ${running} 组继续执行。`
  if (attention) return '批次等待检查并继续；单独恢复的项目进度以项目页为准。'
  if (running) return '各组独立推进，合集组内按确认顺序执行。'
  if (counts.some(row => ['paused', 'stopped'].includes(row.key))) return '检查待继续的组，从已保存的进度恢复。'
  return '已完成的项目保留原有结果。'
}
