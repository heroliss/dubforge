// 扫码成功的凭据可能在旧版链接参数、轮询响应头或新版跨域票据响应头中。
// 只在内存内提取白名单字段，不返回/记录完整 URL、票据或响应头。
const COOKIE_NAMES = ['SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5']
const PASSPORT_HOSTS = new Set(['passport.bilibili.com', 'passport.biligame.com'])
const CROSS_DOMAIN_PATHS = new Set(['/crossDomain', '/x/passport-login/web/crossDomain'])

const invalid = () => Object.assign(new Error('登录凭据响应不完整'), { code: 'PUBLISH_QR_RESPONSE' })
const blocked = () => Object.assign(new Error('登录返回地址不在允许范围'), { code: 'PUBLISH_QR_DESTINATION' })

function complete(cookie) {
  return !!cookie.SESSDATA && !!cookie.bili_jct && /^[1-9]\d*$/.test(cookie.DedeUserID || '')
}

export function cookiesFromHeaders(headers) {
  const values = headers?.['set-cookie'] || []
  const cookie = {}
  // Axios 在 Node 中保留独立的 Set-Cookie 数组；不能按逗号分割 Expires 日期。
  for (const line of Array.isArray(values) ? values : [values]) {
    if (typeof line !== 'string') continue
    const pair = line.split(';', 1)[0], split = pair.indexOf('=')
    if (split < 0) continue
    const name = pair.slice(0, split).trim(), value = pair.slice(split + 1).trim()
    if (COOKIE_NAMES.includes(name) && value && !/[\x00-\x20\x7f]/.test(value)) cookie[name] = value
  }
  return cookie
}

export async function loginCredential(login, response) {
  const fromHeaders = cookiesFromHeaders(response.headers)
  if (complete(fromHeaders)) return fromHeaders
  let url
  try { url = new URL(response.data?.data?.url) }
  catch { throw invalid() }
  // 票据属于账号敏感信息，禁止跟随任意跳转、降级 HTTP 或把票据发送到非平台地址。
  if (url.protocol !== 'https:' || !PASSPORT_HOSTS.has(url.hostname)
      || !CROSS_DOMAIN_PATHS.has(url.pathname) || url.username || url.password || url.port) throw blocked()
  const cookie = { ...Object.fromEntries(COOKIE_NAMES.map(name => [name, url.searchParams.get(name)])
    .filter(([, value]) => value && !/[\x00-\x20\x7f;]/.test(value))), ...fromHeaders }
  if (complete(cookie)) return cookie
  if (!url.searchParams.get('ticket')) throw invalid()

  // 这是官方下发票据的正常兑现，不是重试扫码或切换登录通道；票据不自动重试。
  // 保留 302 自身的 Set-Cookie，禁止自动跳到其 Location（可能是外站）。
  const result = await login.request.get(url.href, {
    extra: { rawResponse: true }, maxRedirects: 0, timeout: 15000,
    // WebQrcodeLogin 自定义 headers 会覆盖 SDK 基类的 UA，不能依赖它自动继承。
    headers: { Referer: 'https://passport.bilibili.com/',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36' },
    validateStatus: status => status >= 200 && status < 400,
  })
  if (result.data && typeof result.data === 'object' && Number.isInteger(result.data.code) && result.data.code !== 0) {
    throw { code: result.data.code }
  }
  const issued = cookiesFromHeaders(result.headers)
  if (cookie.DedeUserID && issued.DedeUserID && cookie.DedeUserID !== issued.DedeUserID) {
    throw Object.assign(new Error('登录账号校验不一致'), { code: 'PUBLISH_ACCOUNT_MISMATCH' })
  }
  Object.assign(cookie, issued)
  if (!complete(cookie)) throw invalid()
  return cookie
}
