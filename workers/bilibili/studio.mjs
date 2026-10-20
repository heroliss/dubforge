// 创作管理适配：只读数据白名单；写操作必须绑定预览版本且不自动重试。
import { createHash } from 'node:crypto'
import { chapterOperation, CHAPTER_OPERATIONS } from './chapter.mjs'

const text = (value, max = 2000) => typeof value === 'string' ? value.slice(0, max) : ''
const number = value => Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : 0
const fail = code => { throw { code } }

// B站播放器的 view_points 是只读章节数据。第三方接口偶尔会在同一响应中
// 混入提示卡或缺少边界的对象，因此只接受有界、单调的时间点，避免把原始
// 响应直接透传到前端。章节标题使用 content 字段（旧响应也可能使用 title）。
function chapterRows(value, duration = 0) {
  if (!Array.isArray(value)) return []
  const rows = []
  for (const item of value.slice(0, 100)) {
    if (!item || typeof item !== 'object') continue
    const start = Number(item.from ?? item.start ?? item.start_time)
    const hasEnd = item.to !== undefined || item.end !== undefined || item.end_time !== undefined
    const endRaw = Number(item.to ?? item.end ?? item.end_time)
    const title = text(item.content ?? item.title ?? item.name, 150).trim()
    if (!Number.isFinite(start) || start < 0 || !title) continue
    if (hasEnd && (!Number.isFinite(endRaw) || endRaw <= start)) continue
    const end = hasEnd ? endRaw
      : Number.isFinite(duration) && duration > start ? duration : null
    if (end !== null && end <= start) continue
    rows.push({start_time: Number(start.toFixed(3)), end_time: end === null ? null : Number(end.toFixed(3)), title})
  }
  rows.sort((a, b) => a.start_time - b.start_time || a.title.localeCompare(b.title))
  const unique = []
  for (const row of rows) {
    if (unique.length && unique.at(-1).start_time === row.start_time && unique.at(-1).title === row.title) continue
    unique.push(row)
  }
  return unique
}

async function chapters(client, p) {
  const info = await client.video.info({bvid:p.bvid})
  const pages = Array.isArray(info?.pages) ? info.pages : []
  const normalized = pages.map(v => ({cid:number(v.cid), title:text(v.part,80), duration:number(v.duration)}))
    .filter(v => v.cid)
  if (!normalized.length) fail('PUBLISH_RESPONSE_UNSUPPORTED')
  const selected = p.cid ? normalized.filter(v => v.cid === p.cid) : normalized
  if (!selected.length) fail('PUBLISH_RESPONSE_UNSUPPORTED')
  const parts = []
  for (const part of selected.slice(0, 100)) {
    const player = await client.video.playerInfo({bvid:p.bvid,cid:part.cid})
    const items = chapterRows(player?.view_points, part.duration)
    parts.push({...part, items})
  }
  return {bvid:p.bvid, parts, items:parts.length === 1 ? parts[0].items : [],
    total:parts.reduce((sum, part) => sum + part.items.length, 0), source:'bilibili.view_points'}
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]))
  return value
}
const hash = value => createHash('sha256').update(JSON.stringify(stable(value))).digest('hex')

// B站详情接口可能返回 http 或协议相对封面地址。编辑时只允许平台自己的
// 静态域名，并统一升级到 HTTPS；任意外部地址一律拒绝，避免透传给 SDK。
function normalizeCover(value) {
  if (typeof value !== 'string' || !/^(?:https?:)?\/\//i.test(value.trim())) return null
  let url
  try { url = new URL(value.trim(), 'https://www.bilibili.com') } catch { return null }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.port
      || !/(^|\.)(hdslb\.com|biliimg\.com|bilibili\.com)$/.test(url.hostname)) return null
  url.protocol = 'https:'
  return url.toString()
}

// 原P说明只输出摘要，不泄露说明内容；摘要用于跨进程冻结和未知写入的只读对账。
function preservationView(data) {
  return {version:1, no_disturbance:[0,1].includes(data.no_disturbance) ? data.no_disturbance : null,
    parts:(data.videos || []).map(part => ({cid:number(part.cid),
      description_hash:createHash('sha256').update(typeof part.desc === 'string' ? part.desc : '').digest('hex')}))}
}
function preservationMatches(expected, actual) {
  return actual?.version === 1 && hash(actual.parts) === hash(expected.parts)
    && (expected.no_disturbance === null || actual.no_disturbance === expected.no_disturbance)
}
export function archiveView(data) {
  const a = data.archive || data
  const videos = (data.videos || []).map(p => ({ cid:number(p.cid), filename:text(p.filename, 300), title:text(p.title, 80),
    status:p.status, state_desc:text(p.status_desc, 200), reject_reason:text(p.reject_reason), xcode_state:p.xcode_state }))
  const settings = {title:text(a.title, 80), description:text(a.desc), tags:text(a.tag).split(',').filter(Boolean),
    source:text(a.source, 200), copyright:a.copyright, tid:a.tid, human_type2:number(a.human_type2?.id || a.human_type2),
    declaration_id:a.creation_statement?.id ?? null, dynamic:text(a.dynamic, 233), cover:normalizeCover(a.cover) || ''}
  // 状态/统计变化不使编辑失效，资料和分P变化必须重新确认。
  const version = hash({settings, videos:videos.map(({cid,filename,title}) => ({cid,filename,title})),
    part_descriptions:(data.videos || []).map(p => ({cid:number(p.cid),desc:typeof p.desc === 'string' ? p.desc : null})),
    cover:a.cover, watermark:data.watermark, attrs:a.attrs, desc_v2:a.desc_v2, dtime:a.dtime,
    mission_id:a.mission_id, topic_id:a.topic_id, creation_statement:a.creation_statement,
    recreate:a.recreate, reply:data.reply, no_disturbance:data.no_disturbance})
  const special = !!(a.is_only_self || a.no_public || a.attrs?.no_public || a.attrs?.is_coop || a.interactive
    || a.ugcpay || a.is_ugcpay_v2 || a.charging_pay || a.premiere || a.attrs?.is_live_playback)
  return {bvid:a.bvid, aid:number(a.aid), mid:String(a.mid || ''), settings, videos, version,
    preservation:preservationView(data),
    edit_supported:!special, edit_limitation:special ? '私密、联合投稿、付费等特殊稿件请在官网修改，避免影响未适配设置' : '',
    state:a.state, state_desc:text(a.state_desc, 200), reject_reason:text(a.reject_reason),
    in_season:typeof data.in_season === 'boolean' ? data.in_season
      : data.in_season === 1 ? true : data.in_season === 0 ? false : null}
}
async function owned(platform, p) {
  const raw = await platform.getArchive({bvid:p.bvid})
  const view = archiveView(raw)
  if (view.mid !== String(p.account_id) || view.bvid !== p.bvid || !view.aid || !view.videos.length)
    fail('PUBLISH_REMOTE_OWNERSHIP')
  if (view.videos.some(v => !v.cid || !v.filename) || new Set(view.videos.map(v => v.cid)).size !== view.videos.length)
    fail('PUBLISH_RESPONSE_UNSUPPORTED')
  return {raw, view}
}

const videoShape = parts => parts.map(({cid, filename, title}) => ({cid, filename, title}))
const validPartTitle = value => typeof value === 'string' && !!value.trim() && value.length <= 80
  && !/[\x00-\x1f\x7f]/.test(value)

// SDK 的 replace 会覆盖全部分P。以原CID和本次上传回执重建；顺序只接受完整身份清单，禁止隐式删P。
function plannedVideos(view, p) {
  const replacing = p.kind === 'replace'
  const replacements = replacing ? p.replacements : []
  if ((replacing && p.bound_aid !== view.aid) || !Array.isArray(replacements)
      || (replacing && !replacements.length) || replacements.length > 100 || view.videos.length > 100
      || (p.videos !== undefined && !Array.isArray(p.videos)))
    fail('PUBLISH_REMOTE_CHANGED')
  const oldCids = new Set(view.videos.map(v => v.cid)), oldFiles = new Set(view.videos.map(v => v.filename))
  const seenOld = new Set(), seenNew = new Set(), seenFiles = new Set(), mapping = new Map(), appended = []
  for (const part of replacements) {
    if (!part || Object.keys(part).sort().join(',') !== 'cid,filename,old_cid,title'
        || !Number.isSafeInteger(part.old_cid) || part.old_cid < 0
        || (part.old_cid !== 0 && (!oldCids.has(part.old_cid) || seenOld.has(part.old_cid)))
        || !Number.isSafeInteger(part.cid) || part.cid <= 0 || oldCids.has(part.cid) || seenNew.has(part.cid)
        || typeof part.filename !== 'string' || !part.filename.trim() || part.filename.length > 300
        || oldFiles.has(part.filename) || seenFiles.has(part.filename)
        || !validPartTitle(part.title)
        || /[\x00-\x1f\x7f]/.test(part.filename + part.title)) fail('PUBLISH_REMOTE_CHANGED')
    seenOld.add(part.old_cid); seenNew.add(part.cid); seenFiles.add(part.filename)
    const uploaded = {cid:part.cid, filename:part.filename, title:part.title}
    if (part.old_cid === 0) appended.push(uploaded)
    else mapping.set(part.old_cid, uploaded)
  }
  const titles = p.part_titles === undefined ? {} : p.part_titles
  if (!titles || typeof titles !== 'object' || Array.isArray(titles)
      || Object.entries(titles).some(([key, value]) => !/^[1-9]\d*$/.test(key)
        || !oldCids.has(Number(key)) || !validPartTitle(value))) fail('PUBLISH_REMOTE_CHANGED')
  const byIdentity = new Map(view.videos.map(part => [part.cid,
    {...(mapping.get(part.cid) || videoShape([part])[0]),
      ...(Object.hasOwn(titles, String(part.cid)) ? {title:titles[String(part.cid)]} : {})}]))
  for (const part of appended) byIdentity.set(part.cid, part)
  const identities = [...view.videos.map(part => part.cid), ...appended.map(part => part.cid)]
  const order = p.order ?? identities
  if (!Array.isArray(order) || order.length !== identities.length || order.length > 100
      || new Set(order).size !== identities.length
      || order.some(cid => !Number.isSafeInteger(cid) || !byIdentity.has(cid))) fail('PUBLISH_REMOTE_CHANGED')
  const videos = order.map(cid => byIdentity.get(cid))
  if ((replacing || p.videos?.length) && (!Array.isArray(p.videos) || p.videos.length !== videos.length
      || hash(videoShape(p.videos)) !== hash(videos)))
    fail('PUBLISH_REMOTE_CHANGED')
  return videos
}

// Python领域层之外再守住工作进程边界，不把未知设置交给第三方SDK。
function editValues(values, settings) {
  const allowed = new Set(['title','description','tags','dynamic','declaration_id',
    'tid','human_type2','copyright','source','cover'])
  if (!values || typeof values !== 'object' || Array.isArray(values)
      || Object.keys(values).some(key => !allowed.has(key))) fail('PUBLISH_WRITE_NOT_SENT')
  for (const [key, value] of Object.entries(values)) {
    if (['title','description','dynamic','source'].includes(key)) {
      const max = {title:80,description:2000,dynamic:233,source:200}[key]
      if (typeof value !== 'string' || value.length > max || (key === 'title' && !value.trim())) fail('PUBLISH_WRITE_NOT_SENT')
    } else if (key === 'tags') {
      if (!Array.isArray(value) || !value.length || value.length > 10
          || value.some(tag => typeof tag !== 'string' || !tag.trim() || tag.length > 20 || /[,，]/.test(tag)))
        fail('PUBLISH_WRITE_NOT_SENT')
    } else if (key === 'cover') {
      if (typeof value !== 'string' || !value.startsWith('https://') || normalizeCover(value) !== value)
        fail('PUBLISH_WRITE_NOT_SENT')
    } else if (key === 'copyright') {
      if (![1,2,3].includes(value)) fail('PUBLISH_WRITE_NOT_SENT')
    } else if (!Number.isSafeInteger(value) || value < (key === 'declaration_id' ? -1 : key === 'tid' ? 1 : 0)) {
      fail('PUBLISH_WRITE_NOT_SENT')
    }
  }
  const merged = {...settings, ...values}
  if (!merged.title || !merged.tags.length || !Number.isSafeInteger(merged.tid) || merged.tid <= 0
      || ![1,2,3].includes(merged.copyright) || (merged.copyright === 2 && !merged.source.trim())
      || (merged.copyright === 2 && merged.declaration_id !== null && merged.declaration_id !== -1)
      || ('declaration_id' in values && merged.copyright === 2)) fail('PUBLISH_WRITE_NOT_SENT')
  return values
}

const stats = value => Object.fromEntries(['view','like','coin','favorite','reply','share','danmuku'].map(k => [k, number(value?.[k])]))
function templates(data) {
  const rows = Array.isArray(data) ? data : data?.tpls || data?.list || data?.templates
  if (!Array.isArray(rows)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
  return rows.slice(0, 100).map(t => ({id:number(t.tid), name:text(t.name, 80),
    values:{title:text(t.title,80), description:text(t.description), tags:text(t.tags).split(',').filter(Boolean)},
    legacy_tid:number(t.typeid), copyright:t.copyright, is_default:t.is_default === 1}))
}

export const STUDIO_OPERATIONS = new Set(['studio_tags','studio_archives','studio_detail','studio_templates',
  'studio_topics','studio_comments','studio_subtitles','studio_subtitle','studio_chapters','studio_write', ...CHAPTER_OPERATIONS])

export async function studioOperation(client, operation, p, credential) {
  const platform = client.platform
  if (operation === 'studio_tags') {
    const suggestions = await platform.getRecommendTags({title:p.title, description:p.description, subtype:p.tid})
    const checks = []
    for (const tag of p.tags) {
      try {
        const r = await platform.checkTag(tag)
        checks.push({tag, valid:r.code === 0 && r.data?.code === 0, message:text(r.data?.content || r.message,200)})
      } catch { checks.push({tag, valid:null, message:'校验请求失败，请重试'}) }
    }
    return {suggestions:(Array.isArray(suggestions) ? suggestions : []).map(r => text(r.tag,20)).filter(Boolean).slice(0,30), checks}
  }
  if (operation === 'studio_archives') {
    const r = await platform.getArchives({pn:p.page,ps:20,keyword:p.keyword,status:p.status || undefined})
    if (!Array.isArray(r.arc_audits)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    return {total:number(r.page?.count), page:p.page, items:r.arc_audits.map(row => ({bvid:row.Archive.bvid,
      aid:number(row.Archive.aid), title:text(row.Archive.title,80), state:row.Archive.state,
      state_desc:text(row.Archive.state_desc,200), reject_reason:text(row.Archive.reject_reason), stats:stats(row.stat)}))}
  }
  if (operation === 'studio_detail') {
    const {view} = await owned(platform,p)
    if (view.in_season) {
      const s = await platform.getSessionId(view.aid)
      if (!number(s.id)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
      view.collection = {id:number(s.id),title:text(s.title,80)}
    } else if (view.in_season === false) view.collection = null
    view.version = hash([view.version, view.collection])
    return view
  }
  if (operation === 'studio_templates') return {items:templates(await platform.getUploadTemplateList())}
  if (operation === 'studio_topics') {
    const r = p.keyword ? await platform.searchTopic({keywords:p.keyword,offset:(p.page-1)*20,page_size:20})
      : await platform.getTopic({pn:p.page-1,ps:20,title:p.title,type_id:p.tid})
    const results = Array.isArray(r.result) ? r.result : r.result ? [r.result] : []
    const rows = r.topics || results.flatMap(v => v.topics || [])
    return {items:rows.slice(0,20).map(t => ({id:number(t.topic_id || t.id), name:text(t.topic_name || t.name,100),
      description:text(t.description), conditions:text(t.activity_description || t.act_protocol),
      activity:text(t.activity_text), mission_id:number(t.mission_id)})),
      more:r.maxpage ? p.page < r.maxpage : results.some(v => v.page_info?.has_more)}
  }
  if (operation === 'studio_comments') {
    const info = await client.video.info({bvid:p.bvid})
    if (!number(info.aid)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    const rows = [], seen = new Set(); let total = 0, exhausted = false
    for (let page = 1; rows.length < p.limit && page <= Math.ceil(p.limit / 20) + 1; page++) {
      const r = await client.reply.list({oid:info.aid,type:1,pn:page,ps:20,sort:1,nohot:0})
      total = number(r.page?.acount ?? r.page?.count)
      const list = r.replies || []
      let added = 0
      for (const c of list) {
        const id = String(c.rpid_str || c.rpid || '')
        if (!/^\d+$/.test(id) || seen.has(id) || !text(c.content?.message).trim()) continue
        seen.add(id); added++
        rows.push({id:`bili:${p.bvid}:${id}`,text:text(c.content.message,3000),author:text(c.member?.uname,100),
          like_count:number(c.like),is_pinned:!!c.is_top,author_is_uploader:String(c.mid) === String(info.owner?.mid),
          url:`https://www.bilibili.com/video/${p.bvid}/#reply${id}`})
        if (rows.length >= p.limit) break
      }
      if (!list.length || !added || list.length < 20) { exhausted = true; break }
    }
    return {bvid:p.bvid,title:text(info.title,100),total,items:rows,exhausted,scope:'热门一级评论；总数可能包含回复'}
  }
  if (operation === 'studio_subtitles' || operation === 'studio_subtitle') {
    const info = await client.video.info({bvid:p.bvid})
    const parts = (info.pages || []).map(v => ({cid:number(v.cid),title:text(v.part,80),duration:number(v.duration)}))
    if (operation === 'studio_subtitles' && !p.cid) return {parts,items:[]}
    if (!parts.some(v => v.cid === p.cid)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    const player = await client.video.playerInfo({bvid:p.bvid,cid:p.cid})
    const rows = player.subtitle?.subtitles || []
    if (operation === 'studio_subtitles') return {parts,items:rows.map(v => ({id:String(v.id_str || v.id),language:text(v.lan,30),name:text(v.lan_doc,100)}))}
    const selected = rows.find(v => String(v.id_str || v.id) === p.subtitle_id)
    if (!selected) fail('PUBLISH_SUBTITLE_CHANGED')
    // 不使用 SDK 的直接文件写入：精确选择语言、不向任意下载主机发送 Cookie。
    const url = new URL(String(selected.subtitle_url || '').replace(/^\/\//,'https://'))
    if (url.protocol !== 'https:' || url.username || url.password || url.port
        || !/(^|\.)(hdslb\.com|biliimg\.com|bilibili\.com)$/.test(url.hostname)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    const response = await fetch(url, {redirect:'error', signal:AbortSignal.timeout(20000), headers:{Referer:'https://www.bilibili.com/'}})
    if (!response.ok) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    let size = 0; const chunks = []
    for await (const chunk of response.body) {
      size += chunk.length
      if (size > 512 * 1024) { await response.body.cancel().catch(() => {}); fail('PUBLISH_RESPONSE_UNSUPPORTED') }
      chunks.push(chunk)
    }
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    if (!Array.isArray(data.body)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
    return {bvid:p.bvid,cid:p.cid,language:text(selected.lan,30),subtitle_id:p.subtitle_id,
      lines:data.body.map(v => ({start:v.from,end:v.to,text:text(v.content,3000)}))}
  }
  if (operation === 'studio_chapters') return await chapters(client, p)
  if (CHAPTER_OPERATIONS.has(operation)) return await chapterOperation(client.platform, operation, p, credential)
  if (operation === 'studio_write') {
    if (p.confirmed !== true) fail('PUBLISH_WRITE_NOT_SENT')
    let sent = false
    let stage = 'preflight'
    // 拦截本次实例唯一写请求以区分“尚未发送”与“结果不明”。
    const post = platform.request.post.bind(platform.request)
    const getArchive = platform.getArchive.bind(platform)
    platform.request.post = (...args) => { sent = true; return post(args[0],args[1],{...args[2],
      timeout:30000,maxRedirects:0,'axios-retry':{retries:0}}) }
    try {
      stage = 'ownership'
      const {raw,view} = await owned(platform,p)
      stage = 'version'
      const collection = view.in_season ? await platform.getSessionId(view.aid) : null
      if (hash([view.version, collection ? {id:number(collection.id),title:text(collection.title,80)} : null]) !== p.version)
        fail('PUBLISH_REMOTE_CHANGED')
      if (p.kind === 'collection') {
        stage = 'validate'
        if (p.values.season_id) {
          const detail = await platform.getSeasonDetail(p.values.season_id)
          if (String(detail.season?.mid) !== String(p.account_id)
              || !detail.sections?.sections?.some(v => v.id === p.values.section_id)) fail('PUBLISH_REMOTE_CHANGED')
        }
        await platform.editMediaSeason({aid:view.aid,title:view.settings.title,...p.values})
      } else if (p.kind === 'edit' || p.kind === 'append' || p.kind === 'replace') {
        stage = 'validate'
        if (!view.edit_supported) fail('PUBLISH_EDIT_UNSUPPORTED')
        const cover = normalizeCover(raw.archive?.cover)
        if (!cover) fail('PUBLISH_RESPONSE_UNSUPPORTED')
        // editMediaWebApi 会二次读取 archive；复用快照时确保它拿到规范化地址。
        raw.archive.cover = cover
        const values = editValues(p.values || {}, view.settings), options = {aid:view.aid}
        if ('title' in values) options.title = values.title
        if ('description' in values) { options.desc = values.description; options.desc_v2 = [{type:1,raw_text:values.description,biz_id:''}] }
        if ('tags' in values) options.tag = values.tags.join(',')
        if ('dynamic' in values) { options.dynamic = values.dynamic; options.dynamic_v2 = [{type:1,raw_text:values.dynamic,biz_id:''}] }
        if ('declaration_id' in values) options.creation_statement = {id:values.declaration_id}
        for (const key of ['tid','human_type2','copyright','source','cover']) if (key in values) options[key] = values[key]
        const replacing = p.kind === 'replace'
        const appending = p.kind === 'append'
        const added = appending ? p.videos || [] : plannedVideos(view, p)
        if (appending && (!Array.isArray(added) || !added.length || p.order != null || p.part_titles !== undefined
            || added.length + view.videos.length > 100
            || added.some(part => !part || !Number.isSafeInteger(part.cid) || part.cid <= 0
              || !validPartTitle(part.title) || typeof part.filename !== 'string' || !part.filename.trim()
              || part.filename.length > 300 || /[\x00-\x1f\x7f]/.test(part.filename)
              || view.videos.some(old => old.cid === part.cid || old.filename === part.filename))
            || new Set(added.map(part => part.cid)).size !== added.length
            || new Set(added.map(part => part.filename)).size !== added.length)) fail('PUBLISH_REMOTE_CHANGED')
        // P简介跟随原CID而非数组位置；重排和换片时不会借用邻P说明。
        const originals = new Map(raw.videos.map(part => [number(part.cid), part]))
        const sourceCid = new Map((replacing ? p.replacements : []).map(part => [part.cid, part.old_cid]))
        const complete = appending ? [...raw.videos, ...added] : added.map(part => {
          const old = originals.get(sourceCid.get(part.cid) ?? part.cid)
          return {...part, ...(typeof old?.desc === 'string' ? {desc:old.desc} : {})}
        })
        const expectedPreservation = preservationView({videos:complete, no_disturbance:raw.no_disturbance})
        if (p.expected_preservation !== undefined && hash(p.expected_preservation) !== hash(expectedPreservation))
          fail('PUBLISH_REMOTE_CHANGED')
        // SDK 会二次读取并丢弃新版分区；复用刚核对的快照，同时显式保留该字段。
        platform.getArchive = async () => structuredClone(raw)
        if (!('human_type2' in values) && view.settings.human_type2) options.human_type2 = view.settings.human_type2
        if ([-1,1].includes(raw.archive.recreate)) options.recreate = raw.archive.recreate
        if (raw.archive.attrs?.is_dolby !== undefined) options.dolby = raw.archive.attrs.is_dolby
        if (raw.archive.attrs?.lossless_music !== undefined) options.lossless_music = raw.archive.attrs.lossless_music
        if ([0,1].includes(raw.no_disturbance)) options.no_disturbance = raw.no_disturbance
        stage = 'request'
        // 全量清单自行验证，避免SDK排序将遗漏P悄悄追加到末尾。
        await platform.editMediaWebApi(complete, options, 'replace')
        // 接受不等于生效；所有编辑都真实回读，延迟生效或不一致只读对账，不盲重发。
        stage = 'verify'
        platform.getArchive = getArchive
        const {view: after} = await owned(platform, p)
        const expectedSettings = {...view.settings, ...values}
        if (after.aid !== view.aid || hash(videoShape(after.videos)) !== hash(videoShape(complete))
            || hash(after.settings) !== hash(expectedSettings)
            || !preservationMatches(expectedPreservation, after.preservation))
          fail('PUBLISH_WRITE_UNKNOWN')
        return {bvid:view.bvid,aid:view.aid,accepted:true,verified:true}
      } else fail('PUBLISH_WRITE_NOT_SENT')
      return {bvid:view.bvid,aid:view.aid,accepted:true}
    } catch(e) {
      if (!sent) {
        const code = ['PUBLISH_REMOTE_CHANGED','PUBLISH_EDIT_UNSUPPORTED','PUBLISH_REMOTE_OWNERSHIP',
          'PUBLISH_RESPONSE_UNSUPPORTED'].includes(e?.code)
          ? e.code : 'PUBLISH_WRITE_NOT_SENT'
        throw {code, detail:{stage}}
      }
      // 写后回读即使收到明确错误，也不能证明此前写入被拒绝。
      throw {code:stage !== 'verify' && typeof e?.code === 'number' && e.code !== 0
        ? 'PUBLISH_WRITE_REJECTED' : 'PUBLISH_WRITE_UNKNOWN', detail:{stage}}
    } finally {
      platform.getArchive = getArchive
      platform.request.post = post
    }
  }
  fail('PUBLISH_RESPONSE_UNSUPPORTED')
}
