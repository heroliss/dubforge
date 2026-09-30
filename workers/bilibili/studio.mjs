// 创作管理适配：只读数据白名单；写操作必须绑定预览版本且不自动重试。
import { createHash } from 'node:crypto'

const text = (value, max = 2000) => typeof value === 'string' ? value.slice(0, max) : ''
const number = value => Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : 0
const fail = code => { throw { code } }
function stable(value) {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]))
  return value
}
const hash = value => createHash('sha256').update(JSON.stringify(stable(value))).digest('hex')
export function archiveView(data) {
  const a = data.archive || data
  const videos = (data.videos || []).map(p => ({ cid:number(p.cid), filename:text(p.filename, 300), title:text(p.title, 80),
    status:p.status, state_desc:text(p.status_desc, 200), reject_reason:text(p.reject_reason), xcode_state:p.xcode_state }))
  const settings = {title:text(a.title, 80), description:text(a.desc), tags:text(a.tag).split(',').filter(Boolean),
    source:text(a.source, 200), copyright:a.copyright, tid:a.tid, human_type2:number(a.human_type2?.id || a.human_type2),
    declaration_id:a.creation_statement?.id ?? null, dynamic:text(a.dynamic, 233)}
  // 状态/统计变化不使编辑失效，资料和分P变化必须重新确认。
  const version = hash({settings, videos:videos.map(({cid,filename,title}) => ({cid,filename,title})),
    cover:a.cover, watermark:data.watermark, attrs:a.attrs, desc_v2:a.desc_v2, dtime:a.dtime,
    mission_id:a.mission_id, topic_id:a.topic_id, creation_statement:a.creation_statement,
    recreate:a.recreate, reply:data.reply, no_disturbance:data.no_disturbance})
  const special = !!(a.is_only_self || a.no_public || a.attrs?.no_public || a.attrs?.is_coop || a.interactive
    || a.ugcpay || a.is_ugcpay_v2 || a.charging_pay || a.premiere || a.attrs?.is_live_playback)
  return {bvid:a.bvid, aid:number(a.aid), mid:String(a.mid || ''), settings, videos, version,
    edit_supported:!special, edit_limitation:special ? '私密、联合投稿、付费等特殊稿件请在官网修改，避免影响未适配设置' : '',
    state:a.state, state_desc:text(a.state_desc, 200), reject_reason:text(a.reject_reason), in_season:!!data.in_season}
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
const stats = value => Object.fromEntries(['view','like','coin','favorite','reply','share','danmuku'].map(k => [k, number(value?.[k])]))
function templates(data) {
  const rows = Array.isArray(data) ? data : data?.tpls || data?.list || data?.templates
  if (!Array.isArray(rows)) fail('PUBLISH_RESPONSE_UNSUPPORTED')
  return rows.slice(0, 100).map(t => ({id:number(t.tid), name:text(t.name, 80),
    values:{title:text(t.title,80), description:text(t.description), tags:text(t.tags).split(',').filter(Boolean)},
    legacy_tid:number(t.typeid), copyright:t.copyright, is_default:t.is_default === 1}))
}

export const STUDIO_OPERATIONS = new Set(['studio_tags','studio_archives','studio_detail','studio_templates',
  'studio_topics','studio_comments','studio_subtitles','studio_subtitle','studio_write'])

export async function studioOperation(client, operation, p) {
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
    } else view.collection = null
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
  if (operation === 'studio_write') {
    if (p.confirmed !== true) fail('PUBLISH_WRITE_NOT_SENT')
    let sent = false
    // 拦截本次实例唯一写请求以区分“尚未发送”与“结果不明”。
    const post = platform.request.post.bind(platform.request)
    platform.request.post = (...args) => { sent = true; return post(args[0],args[1],{...args[2],
      timeout:30000,maxRedirects:0,'axios-retry':{retries:0}}) }
    try {
      const {raw,view} = await owned(platform,p)
      const collection = view.in_season ? await platform.getSessionId(view.aid) : null
      if (hash([view.version, collection ? {id:number(collection.id),title:text(collection.title,80)} : null]) !== p.version)
        fail('PUBLISH_REMOTE_CHANGED')
      if (p.kind === 'collection') {
        if (p.values.season_id) {
          const detail = await platform.getSeasonDetail(p.values.season_id)
          if (String(detail.season?.mid) !== String(p.account_id)
              || !detail.sections?.sections?.some(v => v.id === p.values.section_id)) fail('PUBLISH_REMOTE_CHANGED')
        }
        await platform.editMediaSeason({aid:view.aid,title:view.settings.title,...p.values})
      } else if (p.kind === 'edit' || p.kind === 'append') {
        if (!view.edit_supported) fail('PUBLISH_EDIT_UNSUPPORTED')
        if (!/^https:\/\//.test(raw.archive.cover || '')) fail('PUBLISH_RESPONSE_UNSUPPORTED')
        const values = p.values || {}, options = {aid:view.aid}
        if ('title' in values) options.title = values.title
        if ('description' in values) { options.desc = values.description; options.desc_v2 = [{type:1,raw_text:values.description,biz_id:''}] }
        if ('tags' in values) options.tag = values.tags.join(',')
        if ('dynamic' in values) { options.dynamic = values.dynamic; options.dynamic_v2 = [{type:1,raw_text:values.dynamic,biz_id:''}] }
        if ('declaration_id' in values) options.creation_statement = {id:values.declaration_id}
        if (p.order) options.sortByCid = p.order
        const added = p.videos || []
        if (added.some(v => view.videos.some(old => old.cid === v.cid))) fail('PUBLISH_REMOTE_CHANGED')
        // SDK 会二次读取并丢弃新版分区；复用刚核对的快照，同时显式保留该字段。
        platform.getArchive = async () => structuredClone(raw)
        if (view.settings.human_type2) options.human_type2 = view.settings.human_type2
        if ([-1,1].includes(raw.archive.recreate)) options.recreate = raw.archive.recreate
        if (raw.archive.attrs?.is_dolby !== undefined) options.dolby = raw.archive.attrs.is_dolby
        if (raw.archive.attrs?.lossless_music !== undefined) options.lossless_music = raw.archive.attrs.lossless_music
        await platform.editMediaWebApi(added,options,'append')
      } else fail('PUBLISH_WRITE_NOT_SENT')
      return {bvid:view.bvid,aid:view.aid,accepted:true}
    } catch(e) {
      if (!sent) fail(['PUBLISH_REMOTE_CHANGED','PUBLISH_EDIT_UNSUPPORTED'].includes(e?.code) ? e.code : 'PUBLISH_WRITE_NOT_SENT')
      fail(typeof e?.code === 'number' && e.code !== 0 ? 'PUBLISH_WRITE_REJECTED' : 'PUBLISH_WRITE_UNKNOWN')
    }
  }
  fail('PUBLISH_RESPONSE_UNSUPPORTED')
}
