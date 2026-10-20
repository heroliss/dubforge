export async function publishApi(path: string, body?: any, method = body === undefined ? 'GET' : 'POST') {
  let response: Response
  try {
    // 发布读写接口都必须有上界；后端重启或代理失联时不能让发布页永久保持忙态。
    response = await fetch(`/api/publishing${path}`, {method,
      headers:body === undefined ? undefined : {'Content-Type':'application/json'},
      body:body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(30000)})
  } catch (error: any) {
    if (error?.name === 'TimeoutError') throw Object.assign(new Error('B站发布服务响应超时，请检查后端状态后重试'), {code:'PUBLISH_REQUEST_TIMEOUT'})
    throw error
  }
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Object.assign(new Error(data.message || 'B站操作失败，请重试'), {code:data.code})
  return data
}
