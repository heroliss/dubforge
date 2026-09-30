// 统一把网络层异常转成可操作的中文提示。
// 浏览器原始文案（Failed to fetch / NetworkError 等）对用户没有信息量；
// dev 模式下后端热重载、进程更新都会瞬断连接，需要说明发生了什么、
// 工作是否安全、恢复后怎么办。
const NETWORK_RE = /failed to fetch|networkerror|load failed|connection refused|err_connection|err_network|socket hang up/i

export function friendlyError(e: unknown, fallback = '操作失败'): string {
  const msg = e instanceof Error ? e.message : typeof e === 'string' ? e : ''
  if (NETWORK_RE.test(msg)) {
    return '无法连接后端服务（可能正在重启或更新）。运行中的步骤已被安全中断并保留计划，恢复连接后可「重试并继续」'
  }
  return msg || fallback
}
