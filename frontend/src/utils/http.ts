// 统一把网络层异常转成可操作的中文提示。
// 浏览器原始文案（Failed to fetch / NetworkError 等）对用户没有信息量；
// 网络异常不能证明后台任务已停止；恢复后先核对服务端实际状态。
const NETWORK_RE = /failed to fetch|networkerror|load failed|connection refused|err_connection|err_network|socket hang up/i
export const BACKEND_CONNECTION_NOTICE = '暂时无法连接后端服务。后台任务可能仍在继续；恢复连接后请先核对状态，避免重复提交。'

export function isNetworkError(error: unknown): boolean {
  if (error && typeof error === 'object' && ('status' in error || 'readFailure' in error)) return false
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  return NETWORK_RE.test(message)
}

export function friendlyError(e: unknown, fallback = '操作失败'): string {
  const msg = e instanceof Error ? e.message : typeof e === 'string' ? e : ''
  if (isNetworkError(e)) return BACKEND_CONNECTION_NOTICE
  return msg || fallback
}
