// 一个命令一个进程；上传绝不调用投稿，投稿不自动重试、不切换接口绕过限制。
import { createInterface } from 'node:readline'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { Auth, Client, WebQrcodeLogin, WebVideoUploader } from '@renmu/bili-api'
import { declarationsFrom, KNOWN_DECLARATIONS, safeError, submissionOptions } from './protocol.mjs'
import { loginCredential } from './login-response.mjs'
import { archiveView, STUDIO_OPERATIONS, studioOperation } from './studio.mjs'
import { CollectionFailure, CollectionJoinFailure } from './collection-error.mjs'
import { joinCollection } from './collection-join.mjs'

const emit = (event, data) => process.stdout.write(JSON.stringify({ event, ...data }) + '\n')
const engineVersion = JSON.parse(readFileSync(new URL('./node_modules/@renmu/bili-api/package.json', import.meta.url), 'utf8')).version
const loginError = code => Object.assign(new Error(safeError({ code }).message), { code })

// 登录状态查询与空间资料分离；不能因昵称/空间资料结构变化误报扫码失败。
async function authenticatedAccount(client, expectedId) {
  const info = await client.user.request.get('https://api.bilibili.com/x/web-interface/nav', { timeout: 15000 })
  if (info?.isLogin !== true) throw { code: -101 }
  if (String(info.mid) !== String(expectedId)) throw loginError('PUBLISH_ACCOUNT_MISMATCH')
  return { id: String(info.mid), name: info.uname || `B站用户 ${info.mid}` }
}

// 只重试幂等读取的暂时网络故障，验证要求、证书错误和投稿均不重试。
async function loginRead(action, wait) {
  for (let attempt = 0; ; attempt++) {
    try { return await action() }
    catch (error) {
      const transient = ['ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED', 'EAI_AGAIN'].includes(error?.code)
        || [502, 503, 504].includes(error?.response?.status)
      if (!transient || attempt >= 2) throw error
      await wait(1000)
    }
  }
}

// 第三方调试信息不得混入协议或泄露 Cookie。
export async function execute({ operation, payload, credential }, sdk = { Auth, Client, WebQrcodeLogin, WebVideoUploader }, send = emit, wait = delay) {
  const { Auth, Client, WebQrcodeLogin, WebVideoUploader } = sdk
  if (operation === 'probe') return { engine: '@renmu/bili-api', version: engineVersion }
  if (operation === 'login') {
    const login = new WebQrcodeLogin()
    if (login.request?.defaults) login.request.defaults.timeout = 15000
    const qr = await login.getQrcode()
    if (!qr?.url || !qr.qrcode_key) throw loginError('PUBLISH_QR_RESPONSE')
    send('qr', { url: qr.url })
    for (let count = 0; count < 90; count++) {
      await wait(2000)
      // SDK 的 poll() 只返回 JSON，丢弃了新版登录凭据所在的响应头。
      const raw = await loginRead(() => login.request.get('https://passport.bilibili.com/x/passport-login/web/qrcode/poll', {
        params: { qrcode_key: qr.qrcode_key }, extra: { rawResponse: true }, maxRedirects: 0,
      }), wait)
      const response = raw.data
      if (!response || !Number.isInteger(response.code)) throw loginError('PUBLISH_QR_RESPONSE')
      if (response.code !== 0) throw { code: response.code }
      const data = response.data
      if (!data || !Number.isInteger(data.code)) throw loginError('PUBLISH_QR_RESPONSE')
      if (data.code === 86038) throw loginError('PUBLISH_QR_EXPIRED')
      if (data.code === 86090) send('scanned', {})
      if (data.code === 0) {
        send('verifying', {})
        const cookie = await loginCredential(login, raw)
        const client = new Client()
        await client.setAuth(cookie, Number(cookie.DedeUserID))
        const account = await loginRead(() => authenticatedAccount(client, cookie.DedeUserID), wait)
        return { credential: cookie, ...account }
      }
      if (![86101, 86090].includes(data.code)) throw { code: data.code }
    }
    throw loginError('PUBLISH_QR_EXPIRED')
  }
  const client = new Client(), auth = new Auth()
  try {
    await client.setAuth(credential, Number(credential.DedeUserID))
    auth.setAuth(credential, Number(credential.DedeUserID))
    await authenticatedAccount(client, payload.account_id)
  } catch (error) {
    if (operation === 'create_collection') throw new CollectionFailure('auth', error, credential)
    if (operation === 'collection') throw new CollectionJoinFailure('join_auth', error, credential)
    throw error
  }
  if (STUDIO_OPERATIONS.has(operation)) return await studioOperation(client, operation, payload)
  if (operation === 'create_collection') {
    // SDK 未封装创建合集；仅在此适配层调用已记录的网页接口，不改第三方包。
    // https://github.com/afiuh/bilibili-api-collect/blob/master/docs/creativecenter/season.md
    if (payload.confirmed !== true || typeof payload.title !== 'string' || !payload.title.trim()
        || [...payload.title].length > 20 || typeof payload.description !== 'string'
        || [...payload.description].length > 200 || !payload.cover_path || !credential.bili_jct)
      throw new CollectionFailure('input', null)
    let cover
    try {
      cover = (await client.platform.uploadCover(payload.cover_path))?.url
      const parsed = new URL(cover)
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password
          || !/(^|\.)(hdslb\.com|biliimg\.com)$/.test(parsed.hostname)) throw new Error('封面响应无效')
    } catch (error) { throw new CollectionFailure('cover', error, credential) }
    let seasonId
    try {
      seasonId = await client.platform.request.post('https://member.bilibili.com/x2/creative/web/season/add',
        { title: payload.title.trim(), desc: payload.description.trim(), cover, season_price: 0, csrf: credential.bili_jct },
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, timeout: 30000,
          maxRedirects: 0, 'axios-retry': { retries: 0 } })
    } catch (error) {
      // 有明确的平台拒绝回执才算失败；网络中断和异常响应必须先核对，绝不重发 POST。
      throw new CollectionFailure('create', error, credential)
    }
    if (!Number.isSafeInteger(seasonId) || seasonId <= 0) throw new CollectionFailure('receipt', null)
    // 先返回创建回执；读取小节由后续刷新完成，避免读接口失败掩盖已成功的写入。
    return { season_id: seasonId }
  }
  if (operation === 'metadata') {
    // 新版分区是必需能力；旧投稿预置只用于补充声明，不让旧接口阻断新版流程。
    const human = await client.platform.getHumanType2List()
    const pre = await client.platform.getArchivePre().catch(() => ({}))
    const seasons = []
    let complete = false, total = null
    const seen = new Set()
    try {
      for (let page = 1; page <= 100; page++) {
        const data = await client.platform.getSeasonList({ pn: page, ps: 30 })
        if (!Number.isSafeInteger(data?.total) || data.total < 0 || (total !== null && total !== data.total)) break
        total = data.total
        // 仅明确的空列表可认作没有合集；分页中断、重复或结构变化不能误报合集已删除。
        const rows = data.seasons === null && total === 0 ? [] : data.seasons
        if (!Array.isArray(rows)) break
        for (const item of rows) {
          const id = item?.season?.id, sections = item?.sections?.sections
          if (!Number.isSafeInteger(id) || id <= 0 || seen.has(id) || !Array.isArray(sections)
              || sections.some(s => !Number.isSafeInteger(s?.id) || s.id <= 0)) throw new Error('合集列表不完整')
          seen.add(id)
          seasons.push({ id, title: item.season.title, sections: sections.map(s => ({ id: s.id, title: s.title })) })
        }
        if (seasons.length === total) { complete = true; break }
        if (!rows.length || seasons.length > total) break
      }
    } catch { /* 保留已读取选项，但不据此判定其他合集不存在。 */ }
    const declarations = declarationsFrom(pre)
    return { categories: pre.typelist || pre.types || [], human_categories: human.type_list || [],
      declarations: declarations.length ? declarations : KNOWN_DECLARATIONS,
      declaration_source: declarations.length ? 'platform' : 'adapter-2026-05-24',
      seasons, collection_error: !complete, collections_complete: complete }
  }
  if (operation === 'upload') {
    const uploader = new WebVideoUploader({ path: payload.path, title: payload.title }, auth,
      { concurrency: 3, retryTimes: 2, retryDelay: 3000, bcutPreUpload: false })
    let previous = 0
    uploader.emitter.on('error', () => {})
    uploader.emitter.on('progress', progress => {
      if (Date.now() - previous < 500) return
      previous = Date.now()
      send('progress', { loaded: progress.data.loaded, total: progress.data.total })
    })
    const part = await uploader.upload()
    if (!part?.filename || !part.cid) throw new Error('上传结果不完整')
    return { cid: part.cid, filename: part.filename, title: part.title }
  }
  if (operation === 'cover') return await client.platform.uploadCover(payload.path)
  if (operation === 'submit') {
    if (payload.confirmed !== true) throw new Error('缺少投稿确认')
    const result = await client.platform.addMediaWebApiV3(payload.videos,
      submissionOptions(payload.settings, payload.cover))
    if (!result?.bvid || !result.aid) throw new Error('投稿回执不完整')
    return { bvid: result.bvid, aid: result.aid }
  }
  if (operation === 'status') {
    const result = await client.platform.getArchive({ bvid: payload.bvid })
    const archive = result.archive || result
    return { bvid: archive.bvid, aid: archive.aid, title: archive.title, state: archive.state,
      state_desc: archive.state_desc || '',
      reject_reason: archiveView(result).reject_reason,
      videos: archiveView({...result,videos:result.videos || archive.videos || []}).videos }
  }
  if (operation === 'collection') {
    return await joinCollection(client.platform, payload, credential)
  }
  throw new Error('未知发布操作')
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  console.log = console.warn = console.error = () => {}
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
  for await (const line of input) {
    if (engineVersion !== '2.15.0') {
      emit('error', { code:'PUBLISH_WORKER_VERSION', message:'发布组件版本与已验证版本不一致，请在 workers/bilibili 重新运行 npm ci --ignore-scripts' })
      process.exitCode = 1
      break
    }
    try { emit('result', { data: await execute(JSON.parse(line)) }) }
    catch (error) { emit('error', safeError(error)); process.exitCode = 1 }
    break
  }
}
