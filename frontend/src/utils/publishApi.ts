export async function publishApi(path: string, body?: any, method = body === undefined ? 'GET' : 'POST') {
  const response = await fetch(`/api/publishing${path}`, {method,
    headers:body === undefined ? undefined : {'Content-Type':'application/json'},
    body:body === undefined ? undefined : JSON.stringify(body)})
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Object.assign(new Error(data.message || 'B站操作失败，请重试'), {code:data.code})
  return data
}
