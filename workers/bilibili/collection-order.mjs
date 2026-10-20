import { createHash } from 'node:crypto'

// 字段和排序下标依据 biliup 的已实现接口；不是分P CID 的重排。
// https://github.com/biliup/biliup/blob/master/crates/biliup/src/uploader/season.rs
const HOST = 'https://member.bilibili.com/x2/creative/web/season'
const MAX_EPISODES = 2000
const positive = value => (typeof value === 'number' || typeof value === 'string' && /^[1-9]\d*$/.test(value))
  && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : 0
const fail = code => { throw Object.assign(new Error('合集顺序校验失败'), { code }) }
const signature = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right)

function target(payload) {
  if (![payload.season_id, payload.section_id].every(v => Number.isSafeInteger(v) && v > 0)
      || !/^[1-9]\d*$/.test(String(payload.account_id))) fail('PUBLISH_COLLECTION_ORDER_INVALID')
}

// 只接受完整、无重复、有明确顺序的成员；缺字段不被解释为空列表。
function normalizeSection(data, payload) {
  const s = data?.section
  if (positive(s?.id) !== payload.section_id || positive(s.seasonId) !== payload.season_id
      || positive(s.type) !== 1 || typeof s.title !== 'string' || !s.title.trim() || s.title.length > 200
      || !Number.isSafeInteger(s.epCount) || s.epCount < 0 || s.epCount > MAX_EPISODES)
    fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const lists = [data.episodes, s.episodes, s.Episodes].filter(v => v !== undefined)
  if (!lists.length || lists.some(v => v !== null && !Array.isArray(v))) fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const arrays = lists.filter(Array.isArray)
  if (!arrays.length && s.epCount !== 0) fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const source = arrays[0] || []
  if (source.length !== s.epCount) fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const convert = rows => {
    if (rows.length > MAX_EPISODES) fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
    const seenIds = new Set(), seenAids = new Set(), seenOrders = new Set()
    return rows.map(e => {
      const id = positive(e?.id), aid = positive(e?.aid), cid = positive(e?.cid), order = positive(e?.order)
      if (!id || !aid || !cid || !order || seenIds.has(id) || seenAids.has(aid) || seenOrders.has(order)
          || (e.seasonId !== undefined && positive(e.seasonId) !== payload.season_id)
          || (e.sectionId !== undefined && positive(e.sectionId) !== payload.section_id)
          || typeof e.title !== 'string' || e.title.length > 500
          || (e.bvid !== undefined && e.bvid !== '' && !/^BV[A-Za-z0-9]{10}$/.test(e.bvid)))
        fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
      seenIds.add(id); seenAids.add(aid); seenOrders.add(order)
      return { id, aid, cid, bvid: e.bvid || '', title: e.title, order }
    }).sort((a, b) => a.order - b.order).map((e, i) => ({ ...e, order: i + 1 }))
  }
  const episodes = convert(source)
  if (arrays.some(rows => !equal(convert(rows), episodes))) fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const section = { id: payload.section_id, season_id: payload.season_id, title: s.title, type: 1 }
  return { section, episodes, fingerprint: signature({ section, episodes }) }
}

async function read(platform, payload) {
  const options = params => ({ params, timeout: 15000, maxRedirects: 0, 'axios-retry': { retries: 0 } })
  const detail = await platform.request.get(HOST, options({ id: payload.season_id }))
  if (positive(detail?.season?.id) !== payload.season_id || String(detail.season.mid) !== String(payload.account_id)
      || !Array.isArray(detail.sections?.sections)
      || !detail.sections.sections.some(s => positive(s?.id) === payload.section_id))
    fail('PUBLISH_COLLECTION_ORDER_RESPONSE')
  const result = await platform.request.get(`${HOST}/section`, options({ id: payload.section_id }))
  return normalizeSection(result, payload)
}

function validateSnapshot(snapshot, payload) {
  if (!snapshot || typeof snapshot !== 'object' || !Array.isArray(snapshot.episodes)
      || snapshot.section?.id !== payload.section_id || snapshot.section?.season_id !== payload.season_id
      || snapshot.section?.type !== 1 || typeof snapshot.section.title !== 'string'
      || snapshot.episodes.length > MAX_EPISODES) fail('PUBLISH_COLLECTION_ORDER_INVALID')
  // 用与平台相同的校验重新构造快照，不能只相信传来的哈希字段。
  const normalized = normalizeSection({ section: { id: snapshot.section.id, seasonId: snapshot.section.season_id,
    type: snapshot.section.type, title: snapshot.section.title, epCount: snapshot.episodes.length },
    episodes: snapshot.episodes }, payload)
  if (!equal(normalized, snapshot)) fail('PUBLISH_COLLECTION_ORDER_INVALID')
  return normalized
}

export function expectedCollectionOrder(current, snapshot, order, allowApplied = false) {
  if (!Array.isArray(order) || !order.length || order.length > MAX_EPISODES
      || order.some(v => !Number.isSafeInteger(v) || v <= 0) || new Set(order).size !== order.length)
    fail('PUBLISH_COLLECTION_ORDER_INVALID')
  if (!equal(current.section, snapshot.section)) fail('PUBLISH_COLLECTION_ORDER_CHANGED')
  const baseline = new Map(snapshot.episodes.map(e => [e.aid, e]))
  const targets = new Set(order), members = new Map(current.episodes.map(e => [e.aid, e]))
  if (order.some(aid => !members.has(aid)) || current.episodes.length !== new Set([...baseline.keys(), ...order]).size
      || current.episodes.some(e => !baseline.has(e.aid) && !targets.has(e.aid)))
    fail('PUBLISH_COLLECTION_ORDER_CHANGED')
  // 允许本批次新增成员；其他既有成员的身份、资料和相对顺序必须保持确认时状态。
  const identity = e => ({ id: e.id, aid: e.aid, cid: e.cid, bvid: e.bvid, title: e.title })
  const existing = current.episodes.filter(e => baseline.has(e.aid)).map(identity)
  if (allowApplied) {
    // 只读核对允许本批次的已确认移动；不允许其他成员或既有资料发生变化。
    if (snapshot.episodes.some(e => !members.has(e.aid) || !equal(identity(e), identity(members.get(e.aid))))
        || !equal(current.episodes.filter(e => baseline.has(e.aid) && !targets.has(e.aid)).map(identity),
          snapshot.episodes.filter(e => !targets.has(e.aid)).map(identity)))
      fail('PUBLISH_COLLECTION_ORDER_CHANGED')
  } else if (!equal(existing, snapshot.episodes.map(identity))) fail('PUBLISH_COLLECTION_ORDER_CHANGED')
  return [...current.episodes.filter(e => !targets.has(e.aid)), ...order.map(aid => members.get(aid))]
    .map((e, i) => ({ ...e, order: i + 1 }))
}

export async function collectionOrder(platform, operation, payload, credential) {
  target(payload)
  if (operation === 'collection_order_read') return await read(platform, payload)
  const snapshot = validateSnapshot(payload.snapshot, payload)
  if (operation === 'collection_order_verify') {
    const current = await read(platform, payload)
    const episodes = expectedCollectionOrder(current, snapshot, payload.order, true)
    return { verified: equal(episodes, current.episodes), ...current }
  }
  if (payload.confirmed !== true || !credential?.bili_jct) fail('PUBLISH_COLLECTION_ORDER_INVALID')
  const current = await read(platform, payload)
  const episodes = expectedCollectionOrder(current, snapshot, payload.order)
  if (equal(episodes, current.episodes)) return { verified: true, changed: false, ...current }
  let response
  try {
    response = await platform.request.post(`${HOST}/section/edit`, {
      section: { id: current.section.id, type: 1, seasonId: current.section.season_id, title: current.section.title },
      sorts: episodes.map((e, i) => ({ id: e.id, sort: i + 1 })), captcha_token: '',
    }, { params: { csrf: credential.bili_jct }, headers: { 'Content-Type': 'application/json' },
      timeout: 15000, maxRedirects: 0, 'axios-retry': { retries: 0 }, extra: { rawResponse: true } })
  } catch (error) {
    // 只在明确业务拒绝时允许重新预览，断线和异常 HTTP 回执保留未知结果。
    const code = error?.rawResponse?.data?.code ?? error?.response?.data?.code ?? error?.code
    fail(Number.isSafeInteger(code) && code !== 0 ? 'PUBLISH_COLLECTION_ORDER_REJECTED' : 'PUBLISH_COLLECTION_ORDER_UNKNOWN')
  }
  if (Number.isSafeInteger(response?.data?.code) && response.data.code !== 0) fail('PUBLISH_COLLECTION_ORDER_REJECTED')
  if (response?.data?.code !== 0) fail('PUBLISH_COLLECTION_ORDER_UNKNOWN')
  let result
  try { result = await read(platform, payload) } catch { fail('PUBLISH_COLLECTION_ORDER_UNKNOWN') }
  if (!equal(result.section, current.section) || !equal(result.episodes, episodes)) fail('PUBLISH_COLLECTION_ORDER_UNKNOWN')
  return { verified: true, changed: true, ...result }
}
