export type LlmConversationFilter = { projectUid?: string; operation?: string; ownerId?: string }
export const LLM_DIALOG_EVENT = 'dubforge:llm-conversations'
export const LLM_OPERATION_OPTIONS = [
  { value: '', label: '全部任务' }, { value: 'summary', label: '视频标题与概要' },
  { value: 'split', label: '语义整理' }, { value: 'translate', label: '字幕翻译' },
  { value: 'comment_score', label: '评论评分' }, { value: 'comment_translate', label: '评论翻译' },
  { value: 'publish_copy', label: '发布文案' }, { value: 'chapters', label: '视频章节' },
  { value: 'provider_test', label: '模型连通性测试' },
]
export function openLlmConversations(filter: LlmConversationFilter = {}) {
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent(LLM_DIALOG_EVENT, { detail: filter }))
}
export function conversationListUrl(filter: LlmConversationFilter = {}) {
  const params = new URLSearchParams({ limit: '30' })
  if (filter.projectUid) params.set('project_uid', filter.projectUid)
  if (filter.operation) params.set('operation', filter.operation)
  if (filter.ownerId) params.set('owner_id', filter.ownerId)
  return `/api/llm/conversations?${params}`
}
export function conversationStatus(value: unknown) {
  return ({ running: '进行中', completed: '已完成', done: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' } as Record<string,string>)[String(value)] || '已记录'
}
