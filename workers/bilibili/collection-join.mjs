import { CollectionJoinFailure } from './collection-error.mjs'

const id = value => (typeof value === 'number' || typeof value === 'string' && /^\d+$/.test(value))
  && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : 0

// 空合集实际返回 episodes:null、section.Episodes:null、epCount:0；缺字段不等于空。
export function sectionEpisodes(data, sectionId, seasonId) {
  const section = data?.section
  const fail = reason => { throw new CollectionJoinFailure('join_section', null, {}, reason) }
  if (id(section?.id) !== sectionId || (section.seasonId !== undefined && id(section.seasonId) !== seasonId)) fail('target')
  const lists = [data.episodes, section.episodes, section.Episodes].filter(value => value !== undefined)
  if (!lists.length || lists.some(value => value !== null && !Array.isArray(value))) fail('members')
  const arrays = lists.filter(Array.isArray)
  if (!arrays.length) {
    if (section.epCount === 0 && lists.every(value => value === null)) return []
    fail('members')
  }
  const episodes = arrays[0]
  if (episodes.some(e => !e || !id(e.aid))) fail('members')
  // 两处都有数组时不能挑一个看似为空的列表，从而重复加入已有成员。
  const signature = rows => JSON.stringify(rows.map(e => id(e?.aid)).sort((a,b) => a-b))
  if (arrays.some(rows => signature(rows) !== signature(episodes))
      || (typeof section.epCount === 'number' && (section.epCount > episodes.length || section.epCount === 0 && episodes.length))) fail('members')
  return episodes
}

export async function joinCollection(platform, payload, credential) {
  if (![payload.season_id,payload.section_id,payload.aid,payload.cid].every(v => Number.isSafeInteger(v) && v > 0))
    throw new CollectionJoinFailure('join_input', null, {}, 'input')
  let detail
  try { detail = await platform.getSeasonDetail(payload.season_id) }
  catch (error) { throw new CollectionJoinFailure('join_season', error, credential) }
  if (!detail.sections?.sections?.some(s => id(s.id) === payload.section_id)
      || (detail.season?.id !== undefined && id(detail.season.id) !== payload.season_id)
      || (detail.season?.mid !== undefined && String(detail.season.mid) !== String(payload.account_id)))
    throw new CollectionJoinFailure('join_input', null, {}, 'target')

  async function members(stage) {
    try { return sectionEpisodes(await platform.getSectionDetail(payload.section_id), payload.section_id, payload.season_id) }
    catch (error) {
      if (stage === 'join_section' && error instanceof CollectionJoinFailure) throw error
      throw new CollectionJoinFailure(stage, error, credential)
    }
  }
  const contains = rows => rows.some(e => id(e.aid) === payload.aid)
  // 重试先读成员；已经关联就只恢复本地成功状态，不再次写入。
  if (contains(await members('join_section'))) return {joined:true}
  const request = platform.request
  const originalPost = request?.post
  if (originalPost) request.post = (...args) => originalPost.call(request, args[0],args[1],{
    ...args[2],timeout:30000,maxRedirects:0,'axios-retry':{retries:0},
  })
  try {
    await platform.addMedia2Season({sectionId:payload.section_id,
      episodes:[{aid:payload.aid,cid:payload.cid,title:payload.title}]})
  } catch (error) { throw new CollectionJoinFailure('join_add', error, credential) }
  finally { if (originalPost) request.post = originalPost }
  if (!contains(await members('join_verify')))
    throw new CollectionJoinFailure('join_verify', null, {}, 'unconfirmed')
  return {joined:true}
}
