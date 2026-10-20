// B站原生播放器章节适配：按官方创作中心编辑器协议读取、预览和单次写入。
// 写入默认关闭；调用方必须明确 confirmed，并在写后回读比对。
import { createHash } from 'node:crypto'

const RULE_URL = 'https://member.bilibili.com/x/web/viewpoint/rule'
const CARDS_URL = 'https://member.bilibili.com/x/web/allcards'
const SUBMIT_URL = 'https://member.bilibili.com/x/web/card/submit'
const MAX_CARDS = 100
// 官方编辑器的 chapters 表单 max=16（按 JavaScript 字符长度计数）；数量和时长规则仍动态读取。
const MAX_TITLE_LENGTH = 16

const asText = (value, max = 150) => typeof value === 'string' ? value.trim().slice(0, max) : ''
const asPositive = value => {
  if (typeof value !== 'number' && !(typeof value === 'string' && /^[1-9]\d*$/.test(value))) return 0
  const n = Number(value)
  return Number.isSafeInteger(n) && n > 0 ? n : 0
}
const asFinite = value => {
  if (typeof value !== 'number' && !(typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value))) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}
const fail = (code, stage = 'input', detail = {}) => {
  throw Object.assign(new Error(code), { code, detail: { stage, ...detail } })
}
const stable = value => {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]))
  return value
}
const fingerprint = value => createHash('sha256').update(JSON.stringify(stable(value))).digest('hex')

function payloadData(value) {
  if (value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, 'data')) return value.data
  return value
}
function responseCode(value) {
  const body = payloadData(value)
  const code = body?.code ?? value?.status?.code
  const parsed = typeof code === 'number' || typeof code === 'string' && /^-?\d+$/.test(code) ? Number(code) : NaN
  return Number.isSafeInteger(parsed) ? parsed : null
}

function target(payload, requireBvid = true) {
  const bvid = asText(payload?.bvid, 32)
  const aid = asPositive(payload?.aid)
  const cid = asPositive(payload?.cid)
  if ((requireBvid && !/^BV[A-Za-z0-9]{10}$/.test(bvid)) || !aid || !cid) fail('PUBLISH_CHAPTER_INVALID')
  return { bvid, aid, cid }
}

function requestOptions(params) {
  return { params, timeout: 15000, maxRedirects: 0, 'axios-retry': { retries: 0 } }
}

function unwrapRules(raw) {
  const body = payloadData(raw)
  const chapter = body?.chapters || body?.chapter || body?.rules?.chapters || body?.rules || body
  const limitRaw = chapter?.limit ?? chapter?.max_count ?? chapter?.maxCount
  const stayRaw = chapter?.stay ?? chapter?.min_duration ?? chapter?.minDuration
  const spaceRaw = chapter?.space ?? chapter?.min_space ?? chapter?.minSpace
  const parsedLimit = asFinite(limitRaw)
  const limit = Number.isSafeInteger(parsedLimit) && parsedLimit >= 0 ? parsedLimit : null
  const stay = asFinite(stayRaw)
  const space = asFinite(spaceRaw)
  // 规则结构发生变化时不把“空规则”解释为允许写入。
  if (limit === null || (stay !== null && (stay < 0 || stay > 86400)) || (space !== null && (space < 0 || space > 86400)))
    fail('PUBLISH_CHAPTER_RULE_UNAVAILABLE', 'rules')
  return { limit, stay, space, max_title_length: MAX_TITLE_LENGTH }
}

function normalizeCard(row, duration = null) {
  if (!row || typeof row !== 'object') return null
  const from = asFinite(row.from ?? row.start ?? row.start_time)
  const to = asFinite(row.to ?? row.end ?? row.end_time)
  const contentRaw = row.content ?? row.title ?? row.name
  const content = typeof contentRaw === 'string' ? contentRaw.trim() : ''
  if (from === null || to === null || !content) return null
  if ([...content].length > 150 || /[\r\n\u0000-\u001f\u007f]/.test(content)) return null
  const start = Math.round(from), end = Math.round(to)
  if (start < 0 || end <= start || end > 86400 || (duration !== null && end > duration + 1)) return null
  return { from: start, to: end, content }
}

export function normalizeChapterCards(value, duration = null, rules = null) {
  if (!Array.isArray(value) || value.length > MAX_CARDS) fail('PUBLISH_CHAPTER_INVALID')
  const rows = value.map(row => normalizeCard(row, duration))
  if (rows.some(row => !row)) fail('PUBLISH_CHAPTER_INVALID')
  rows.sort((a, b) => a.from - b.from || a.to - b.to || a.content.localeCompare(b.content))
  // 网页编辑器会把最后一章的结束时间规范为整段视频结尾；预览和回读统一使用该规范值。
  if (duration !== null && rows.length) rows.at(-1).to = Math.round(duration)
  if (rows.some(row => row.to <= row.from)) fail('PUBLISH_CHAPTER_TIME', 'validate')
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].from < rows[i - 1].to) fail('PUBLISH_CHAPTER_OVERLAP', 'validate', {index: i + 1})
  }
  if (rules) {
    for (const [index, row] of rows.entries()) if (row.content.length > MAX_TITLE_LENGTH)
      fail('PUBLISH_CHAPTER_TITLE_TOO_LONG', 'validate', {index: index + 1,
        title_length: row.content.length, max_title_length: MAX_TITLE_LENGTH})
    if (rules.limit === 0 && rows.length) fail('PUBLISH_CHAPTER_RULE_REJECTED')
    if (rules.limit !== null && rows.length > rules.limit) fail('PUBLISH_CHAPTER_LIMIT_EXCEEDED', 'validate',
      {count: rows.length, limit: rules.limit})
    if (rules.stay !== null) {
      for (const [index, row] of rows.entries()) {
        // 官方规则中的 1 秒限制针对从 0 秒开始的首章节长度，不能把首卡起点改成 1 秒。
        const minimum = row.from === 0 ? 1 : rules.stay
        if (row.to - row.from < minimum) fail('PUBLISH_CHAPTER_TIME', 'validate',
          {index: index + 1, minimum_seconds: minimum, actual_seconds: row.to - row.from})
      }
    }
    if (rules.space !== null) {
      for (let i = 1; i < rows.length; i++) if (rows[i].from - rows[i - 1].to < rules.space)
        fail('PUBLISH_CHAPTER_INTERVAL', 'validate', {index: i + 1,
          minimum_seconds: rules.space, actual_seconds: rows[i].from - rows[i - 1].to})
    }
  }
  return rows
}

function extractCards(raw) {
  const body = payloadData(raw)
  // B站对“尚未设置章节”的正常响应是 code=0、data=null；这不是结构错误，
  // 必须解释为空章节，写入预览仍会用独立的规则和稿件归属校验保护。
  if (body === null || body === undefined) return { cards: [], permanent: false }
  const catalog = body?.catalog
  // 缺失 catalog 不能当成空章节，否则可能覆盖未能读取到的远端设置。
  if (!Array.isArray(catalog)) fail('PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED', 'read')
  const entry = catalog.find(item => Number(item?.type) === 2)
  if (!entry) return { cards: [], permanent: false }
  if (entry.cards !== undefined && !Array.isArray(entry.cards)) fail('PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED', 'read')
  const cards = normalizeChapterCards(entry.cards || [])
  const permanent = entry.permanent === true || Number(entry.permanent) === 1
  return { cards, permanent }
}

async function getRules(platform, t) {
  const raw = await platform.request.get(RULE_URL, requestOptions({ bvid: t.bvid, aid: t.aid, cid: t.cid }))
  return unwrapRules(raw)
}

async function getCards(platform, t) {
  const raw = await platform.request.get(CARDS_URL, requestOptions({ aid: t.aid, cid: t.cid }))
  const value = extractCards(raw)
  return { ...value, fingerprint: fingerprint({ aid: t.aid, cid: t.cid, cards: value.cards, permanent: value.permanent }) }
}

async function ownedTarget(platform, payload) {
  const t = target(payload)
  const raw = await platform.getArchive({ bvid: t.bvid })
  const archive = raw?.archive || raw
  const videos = raw?.videos || archive?.videos
  if (!archive || archive.bvid !== t.bvid || asPositive(archive.aid) !== t.aid
      || String(archive.mid) !== String(payload.account_id)) fail('PUBLISH_REMOTE_OWNERSHIP', 'ownership')
  const part = Array.isArray(videos) ? videos.find(item => asPositive(item?.cid) === t.cid) : null
  if (!part)
    fail('PUBLISH_CHAPTER_REMOTE_CHANGED', 'ownership')
  const duration = asFinite(part.duration ?? part.length)
  return {...t, duration: duration !== null && duration > 0 ? duration : null}
}

function writeNotSent(error) {
  const allowed = ['PUBLISH_CHAPTER_INVALID', 'PUBLISH_CHAPTER_RULE_UNAVAILABLE',
    'PUBLISH_CHAPTER_RULE_REJECTED', 'PUBLISH_CHAPTER_LIMIT_EXCEEDED', 'PUBLISH_CHAPTER_TITLE_TOO_LONG',
    'PUBLISH_CHAPTER_TIME', 'PUBLISH_CHAPTER_OVERLAP', 'PUBLISH_CHAPTER_INTERVAL',
    'PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED', 'PUBLISH_CHAPTER_REMOTE_CHANGED', 'PUBLISH_REMOTE_OWNERSHIP']
  if (allowed.includes(error?.code)) throw error
  fail('PUBLISH_CHAPTER_NOT_SENT', 'preflight')
}

function snapshot(t, rules, cards) {
  const value = { target: { bvid: t.bvid, aid: t.aid, cid: t.cid }, rules, cards: cards.cards, permanent: cards.permanent }
  return { ...value, fingerprint: fingerprint(value) }
}

function validatePrepared(payload, t, current) {
  const prepared = payload?.prepared || payload
  if (!prepared || typeof prepared !== 'object' || prepared.target?.aid !== t.aid || prepared.target?.cid !== t.cid
      || prepared.target?.bvid !== t.bvid || typeof prepared.fingerprint !== 'string') fail('PUBLISH_CHAPTER_INVALID')
  const baseline = snapshot(t, prepared.rules, { cards: prepared.cards, permanent: prepared.permanent })
  if (baseline.fingerprint !== prepared.fingerprint || prepared.fingerprint !== current.fingerprint) fail('PUBLISH_CHAPTER_REMOTE_CHANGED', 'version')
  return normalizeChapterCards(payload.cards ?? prepared.cards, t.duration, current.rules)
}

export async function chapterOperation(platform, operation, payload, credential) {
  let t
  if (operation === 'studio_chapter_write' && (payload.confirmed !== true || !credential?.bili_jct))
    fail('PUBLISH_CHAPTER_NOT_SENT', 'preflight')
  try { t = await ownedTarget(platform, payload) }
  catch (error) {
    if (operation === 'studio_chapter_write') writeNotSent(error)
    throw error
  }
  const rules = operation === 'studio_chapter_rules' ? await getRules(platform, t) : null
  if (operation === 'studio_chapter_rules') return { ...t, rules, source: 'bilibili.viewpoint.rule' }
  if (operation === 'studio_chapter_read') {
    const cards = await getCards(platform, t)
    return { ...t, ...cards, source: 'bilibili.allcards', items: cards.cards.map(row => ({
      start_time: row.from, end_time: row.to, title: row.content,
    })) }
  }
  if (operation === 'studio_chapter_prepare') {
    const currentRules = await getRules(platform, t)
    const currentCards = await getCards(platform, t)
    const current = snapshot(t, currentRules, currentCards)
    const proposed = normalizeChapterCards(payload.cards ?? payload.items, payload.duration ?? t.duration, currentRules)
    return { ...t, rules: currentRules, current: currentCards.cards, current_permanent: currentCards.permanent,
      cards: proposed, permanent: payload.permanent === true, snapshot: current, fingerprint: current.fingerprint,
      source: 'bilibili.chapter.preview' }
  }
  if (operation === 'studio_chapter_write') {
    let proposed
    try {
      const currentRules = await getRules(platform, t)
      const currentCards = await getCards(platform, t)
      const current = snapshot(t, currentRules, currentCards)
      proposed = validatePrepared(payload, t, current)
    } catch (error) { writeNotSent(error) }
    const permanent = payload.permanent === true || Number(payload.permanent) === 1
    // 与官方编辑器保持一致，permanent 以布尔值通过表单序列化；不要把未勾选省略。
    const body = { aid: t.aid, cid: t.cid, type: 2,
      cards: JSON.stringify(proposed), permanent, csrf: credential.bili_jct }
    let response
    try {
      response = await platform.request.post(SUBMIT_URL, body, {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, timeout: 30000,
        maxRedirects: 0, 'axios-retry': { retries: 0 }, extra: { rawResponse: true },
      })
    } catch (error) {
      const code = error?.rawResponse?.data?.code ?? error?.response?.data?.code ?? error?.code
      const parsed = typeof code === 'number' || typeof code === 'string' && /^-?\d+$/.test(code) ? Number(code) : NaN
      if (Number.isSafeInteger(parsed) && parsed !== 0) fail('PUBLISH_CHAPTER_REJECTED', 'request', { remote_code: parsed })
      fail('PUBLISH_CHAPTER_UNKNOWN', 'request')
    }
    const code = responseCode(response)
    if (code !== 0) {
      if (code === null) fail('PUBLISH_CHAPTER_UNKNOWN', 'request')
      fail('PUBLISH_CHAPTER_REJECTED', 'request', { remote_code: code })
    }
    let after
    try { after = await getCards(platform, t) } catch { fail('PUBLISH_CHAPTER_UNKNOWN', 'verify') }
    if (fingerprint({ cards: after.cards, permanent: after.permanent }) !== fingerprint({ cards: proposed, permanent }))
      fail('PUBLISH_CHAPTER_UNKNOWN', 'verify')
    return { ...t, accepted: true, verified: true, changed: true, cards: after.cards,
      permanent: after.permanent, source: 'bilibili.allcards' }
  }
  fail('PUBLISH_RESPONSE_UNSUPPORTED')
}

export const CHAPTER_OPERATIONS = new Set([
  'studio_chapter_rules', 'studio_chapter_read', 'studio_chapter_prepare', 'studio_chapter_write',
])
