import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { Platform } from '@renmu/bili-api'
import { studioOperation, archiveView } from './studio.mjs'
import { safeError, submissionOptions } from './protocol.mjs'

const BV='BV1234567890'
function fixture(){
  const raw={archive:{bvid:BV,aid:10,mid:123,title:'原标题',desc:'原简介',tag:'旅行',tid:21,human_type2:1001,
    copyright:3,source:'原来源',cover:'https://i0.hdslb.com/cover.jpg',dynamic:'动态',creation_statement:{id:1},
    desc_v2:[{type:1,raw_text:'原简介',biz_id:''}],state:0},
    watermark:{state:1},videos:[{cid:1,title:'原声',filename:'one'},{cid:2,title:'配音',filename:'two'}]}
  const calls=[]
  const platform=Object.create(Platform.prototype)
  platform.auth={authLogin(){},cookieObj:{bili_jct:'fixture-only'}}
  platform.getArchive=async()=>structuredClone(raw)
  platform.request={post:async(...args)=>{
    calls.push(args)
    const {videos, csrf, watermark, no_disturbance, ...archive} = args[1]
    Object.assign(raw.archive, structuredClone(archive))
    if (videos) raw.videos = structuredClone(videos)
    if (watermark) raw.watermark = structuredClone(watermark)
    if (no_disturbance !== undefined) raw.no_disturbance = no_disturbance
    return {aid:10,bvid:BV}
  }}
  const client={platform}
  const run=(op,p={})=>studioOperation(client,'studio_'+op,{account_id:'123',bvid:BV,...p},{bili_jct:'fixture-only'})
  return {raw,calls,platform,client,run}
}

test('编辑用真实SDK方法，保留分P、分区和水印，新简介不被旧富文本覆盖',async()=>{
  const {run,calls,raw}=fixture()
  raw.archive.human_type2={id:1001,name:'知识'}
  raw.archive.attrs={is_dolby:1,lossless_music:1,no_public:0}
  raw.archive.recreate=1
  const detail=await run('detail')
  await run('write',{kind:'edit',version:detail.version,confirmed:true,values:{description:'新的简介'},order:[2,1],videos:[]})
  assert.equal(calls.length,1)
  const [url,data,options]=calls[0]
  assert.match(url,/\/edit$/)
  assert.equal(data.desc,'新的简介')
  assert.equal(data.human_type2,1001)
  assert.equal(data.dolby,1)
  assert.equal(data.lossless_music,1)
  assert.equal(data.recreate,1)
  assert.equal(data.source,'原来源')
  assert.equal(data.watermark.state,1)
  assert.deepEqual(data.videos.map(p=>p.cid),[2,1])
  assert.equal(options.maxRedirects,0)
  assert.equal(options['axios-retry'].retries,0)
})
test('编辑接受B站官方HTTP或协议相对封面并在请求前升级为HTTPS',async()=>{
  for (const cover of ['http://i0.hdslb.com/bfs/archive/cover.png', '//i0.hdslb.com/bfs/archive/cover.png']) {
    const {raw,run,calls}=fixture()
    raw.archive.cover=cover
    const detail=await run('detail')
    await run('write',{kind:'edit',version:detail.version,confirmed:true,values:{title:'新标题'}})
    assert.equal(calls.length,1)
    assert.equal(calls[0][1].cover,'https://i0.hdslb.com/bfs/archive/cover.png')
  }
})
test('编辑拒绝非B站官方封面且不会发出写请求',async()=>{
  const {raw,run,calls}=fixture()
  raw.archive.cover='http://example.com/cover.png'
  const detail=await run('detail')
  await assert.rejects(run('write',{kind:'edit',version:detail.version,confirmed:true,values:{title:'新标题'}}),
    e=>e.code==='PUBLISH_RESPONSE_UNSUPPORTED')
  assert.equal(calls.length,0)
})
test('未发送写请求只保留受控阶段诊断',()=>{
  assert.deepEqual(safeError({code:'PUBLISH_WRITE_NOT_SENT',detail:{stage:'validate',secret:'x'}}),
    {code:'PUBLISH_WRITE_NOT_SENT',message:'尚未发送修改请求，请核对账号、资料和网络后重新预览',detail:{stage:'validate'}})
  assert.deepEqual(safeError({code:'PUBLISH_WRITE_NOT_SENT',detail:{stage:'secret'}}),
    {code:'PUBLISH_WRITE_NOT_SENT',message:'尚未发送修改请求，请核对账号、资料和网络后重新预览'})
})
test('特殊稿件不因SDK的字段删减而改变权限',async()=>{
  const {raw,run,calls}=fixture();raw.archive.attrs={no_public:1}
  const detail=await run('detail');assert.equal(detail.edit_supported,false)
  await assert.rejects(run('write',{kind:'edit',confirmed:true,version:detail.version,values:{title:'新标题'}}),e=>e.code==='PUBLISH_EDIT_UNSUPPORTED')
  assert.equal(calls.length,0)
})
test('追加只追加新分P，不新投稿或替换已有视频',async()=>{
  const {run,calls}=fixture(),detail=await run('detail')
  await run('write',{kind:'append',version:detail.version,confirmed:true,values:{},videos:[{cid:3,title:'新版本',filename:'new'}]})
  assert.deepEqual(calls[0][1].videos.map(p=>p.cid),[1,2,3])
  assert.equal(calls[0][1].title,'原标题')
})

test('原BV换片使用完整replace清单，保留其他P与设置，并真实回读确认',async()=>{
  const {raw,run,platform,calls}=fixture()
  raw.archive.attrs={is_dolby:1,lossless_music:1,no_public:0}
  raw.archive.human_type2={id:1001,name:'知识'}
  raw.archive.recreate=1
  const detail=await run('detail')
  const replacements=[{old_cid:1,cid:3,filename:'new-one',title:'新版原声'}]
  const videos=[{cid:3,filename:'new-one',title:'新版原声'},{cid:2,filename:'two',title:'配音'}]
  platform.request.post=async(...args)=>{
    calls.push(args)
    raw.videos=structuredClone(args[1].videos)
    raw.archive.desc=args[1].desc
    return {aid:10,bvid:BV}
  }
  const result=await run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,
    values:{description:'新简介'},replacements,videos})
  assert.equal(result.verified,true)
  assert.equal(result.bvid,BV)
  assert.equal(calls.length,1)
  assert.deepEqual(calls[0][1].videos,videos)
  assert.equal(calls[0][1].human_type2,1001)
  assert.equal(calls[0][1].dolby,1)
  assert.equal(calls[0][1].lossless_music,1)
  assert.equal(calls[0][1].recreate,1)
  assert.equal(calls[0][1].source,'原来源')
  assert.equal(calls[0][1].copyright,3)
  assert.equal(calls[0][1].creation_statement.id,1)
  assert.deepEqual(calls[0][1].watermark,{state:1})
  assert.equal(calls[0][2]['axios-retry'].retries,0)
  assert.equal((await run('detail')).videos[0].cid,3)
})

test('replace禁止漏P、重排、改写未替换P、CID冲突和目标绑定变化',async()=>{
  for (const change of [
    {videos:[{cid:3,filename:'new-one',title:'新版原声'}]},
    {videos:[{cid:2,filename:'two',title:'配音'},{cid:3,filename:'new-one',title:'新版原声'}]},
    {videos:[{cid:3,filename:'new-one',title:'新版原声'},{cid:2,filename:'wrong',title:'配音'}]},
    {replacements:[{old_cid:1,cid:2,filename:'new-one',title:'新版原声'}]},
    {replacements:[{old_cid:8,cid:3,filename:'new-one',title:'新版原声'}]},
    {replacements:[{old_cid:1,cid:3,filename:'one',title:'新版原声'}]},
    {replacements:[{old_cid:1,cid:3,filename:'new-one',title:'新版原声'},{old_cid:1,cid:4,filename:'new-two',title:'重复'}]},
    {bound_aid:11}, {order:[3,2]}
  ]) {
    const {run,calls}=fixture(),detail=await run('detail')
    await assert.rejects(run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
      replacements:[{old_cid:1,cid:3,filename:'new-one',title:'新版原声'}],
      videos:[{cid:3,filename:'new-one',title:'新版原声'},{cid:2,filename:'two',title:'配音'}],...change}),
      e=>e.code==='PUBLISH_REMOTE_CHANGED')
    assert.equal(calls.length,0)
  }
})

test('换片保留原P简介，官网只改P简介也会使旧预览失效',async()=>{
  const {raw,run,platform,calls}=fixture()
  raw.videos[0].desc='原P的说明'
  raw.videos[1].desc='未替换P的说明'
  const detail=await run('detail')
  raw.videos[1].desc='官网新说明'
  const request={kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
    replacements:[{old_cid:1,cid:3,filename:'new-one',title:'新版原声'}],
    videos:[{cid:3,filename:'new-one',title:'新版原声'},{cid:2,filename:'two',title:'配音'}]}
  await assert.rejects(run('write',request),e=>e.code==='PUBLISH_REMOTE_CHANGED')
  assert.equal(calls.length,0)
  platform.request.post=async(...args)=>{
    calls.push(args);raw.videos=structuredClone(args[1].videos);return {aid:10,bvid:BV}
  }
  const current=await run('detail')
  assert.equal((await run('write',{...request,version:current.version})).verified,true)
  assert.deepEqual(calls[0][1].videos.map(p=>p.desc),['原P的说明','官网新说明'])
})

test('换片接受后仍为旧清单或回读文件不一致，只发一次并返回未知',async()=>{
  for(const mode of ['delay','filename','title','settings','read-error','read-rejected','timeout']){
    const {raw,run,platform,calls}=fixture(),detail=await run('detail')
    platform.request.post=async(...args)=>{
      calls.push(args)
      if(mode==='timeout')throw new Error('timeout')
      if(mode!=='delay')raw.videos=structuredClone(args[1].videos)
      if(mode==='filename')raw.videos[0].filename='another-file'
      if(mode==='title')raw.videos[0].title='平台其他名称'
      if(mode==='settings')raw.archive.title='其他标题'
      return {aid:10,bvid:BV}
    }
    if(mode==='read-error' || mode==='read-rejected'){
      let reads=0
      platform.getArchive=async()=>{
        if(++reads>1)throw mode==='read-rejected' ? {code:-400} : new Error('read-timeout')
        return structuredClone(raw)
      }
    }
    await assert.rejects(run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
      replacements:[{old_cid:1,cid:3,filename:'new-one',title:'新版原声'}],
      videos:[{cid:3,filename:'new-one',title:'新版原声'},{cid:2,filename:'two',title:'配音'}]}),
      e=>e.code==='PUBLISH_WRITE_UNKNOWN')
    assert.equal(calls.length,1)
  }
})
test('没有确认、账号不符、远端资料变化、重复CID均在写请求前拒绝',async()=>{
  for(const kind of ['confirm','owner','version','duplicate']){
    const {raw,run,calls}=fixture(),detail=await run('detail')
    if(kind==='owner')raw.archive.mid=456
    if(kind==='version')raw.archive.title='官网新标题'
    await assert.rejects(run('write',{kind:'append',confirmed:kind!=='confirm',version:detail.version,
      values:{},videos:kind==='duplicate'?[{cid:1,title:'重复',filename:'one'}]:[]}))
    assert.equal(calls.length,0)
  }
})

test('远端分P回执不完整时禁止生成编辑资料或写入',async()=>{
  for(const value of [{cid:0,filename:'one'},{cid:1,filename:''},{cid:2,filename:'two'}]){
    const {raw,run,calls}=fixture(),detail=await run('detail')
    raw.videos[0]={...raw.videos[0],...value}
    await assert.rejects(run('detail'),e=>e.code==='PUBLISH_RESPONSE_UNSUPPORTED')
    await assert.rejects(run('write',{kind:'edit',confirmed:true,version:detail.version,values:{title:'新标题'}}))
    assert.equal(calls.length,0)
  }
})
test('超时不重试，明确拒绝与未发送分开返回',async()=>{
  for(const error of [new Error('timeout'),{code:-400}]){
    const {platform,run}=fixture(),detail=await run('detail');let attempts=0
    platform.request.post=async()=>{attempts++;throw error}
    await assert.rejects(run('write',{kind:'edit',confirmed:true,version:detail.version,values:{title:'新标题'}}),
      e=>e.code===(error.code?'PUBLISH_WRITE_REJECTED':'PUBLISH_WRITE_UNKNOWN'))
    assert.equal(attempts,1)
  }
})
test('状态与转码变化不误判资料冲突；真实改文案改变版本',()=>{
  const {raw}=fixture();const version=archiveView(raw).version
  raw.archive.state=-2;raw.archive.reject_reason='审核退回';raw.videos[0].status=-1
  assert.equal(archiveView(raw).version,version)
  assert.equal(archiveView(raw).reject_reason,'审核退回')
  raw.videos[0].title='新P名';assert.notEqual(archiveView(raw).version,version)
})
test('合集转移验证归属小节，移出明确发送null',async()=>{
  const {platform,run,calls}=fixture(),detail=await run('detail')
  platform.getSeasonDetail=async()=>({season:{mid:123},sections:{sections:[{id:8}]}})
  await assert.rejects(run('write',{kind:'collection',confirmed:true,version:detail.version,values:{season_id:7,section_id:99}}))
  assert.equal(calls.length,0)
  await run('write',{kind:'collection',confirmed:true,version:detail.version,values:{season_id:null,section_id:null}})
  assert.equal(calls[0][1].season_id,null)
})

test('合集读取不能把缺失in_season当作明确移出',async()=>{
  const {raw,run}=fixture()
  const unknown=await run('detail')
  assert.equal(unknown.in_season,null)
  assert.equal(Object.hasOwn(unknown,'collection'),false)
  raw.in_season=false
  const removed=await run('detail')
  assert.equal(removed.in_season,false)
  assert.equal(removed.collection,null)
  raw.in_season=0
  assert.equal((await run('detail')).collection,null)
})
test('标签校验包含不可用与请求失败，不把失败当通过',async()=>{
  const {platform,run}=fixture()
  platform.getRecommendTags=async()=>[{tag:'旅行'},{tag:'美食'}]
  platform.checkTag=async tag=>{if(tag==='超时')throw new Error();return {code:0,data:{code:tag==='非法'?1:0,content:'校验结果'}}}
  const result=await run('tags',{title:'旅行',description:'简介',tid:21,tags:['正常','非法','超时']})
  assert.deepEqual(result.checks.map(c=>c.valid),[true,false,null])
  assert.deepEqual(result.suggestions,['旅行','美食'])
})
test('投稿统计与模板仅返回白名单，不返回原始身份字段',async()=>{
  const {platform,run}=fixture()
  platform.getArchives=async()=>({page:{count:1},arc_audits:[{Archive:{bvid:BV,aid:10,title:'标题',state:0,reject_reason:'理由'},stat:{view:12,like:3,secret:'x'},secret:'x'}]})
  platform.getUploadTemplateList=async()=>[{tid:4,name:'模板',title:'标题',tags:'a,b',description:'简介',typeid:21,copyright:2,is_default:1,csrf:'secret'}]
  const list=await run('archives',{page:1,keyword:'',status:''})
  assert.equal(list.items[0].stats.view,12)
  const templates=await run('templates')
  assert.deepEqual(templates.items[0].values.tags,['a','b'])
  assert.doesNotMatch(JSON.stringify([list,templates]),/secret|csrf/)
})
test('话题搜索仅提供候选，只有选用的设置才映射投稿字段',async()=>{
  const {platform,run,calls}=fixture()
  platform.searchTopic=async()=>({result:[{page_info:{has_more:true},topics:[{id:12,name:'旅途',description:'简介',mission_id:20,act_protocol:'活动条件'}]}]})
  const result=await run('topics',{keyword:'旅行',page:1})
  assert.equal(result.items[0].conditions,'活动条件');assert.equal(calls.length,0)
  assert.equal(submissionOptions({tags:[]},'').topic_id,undefined)
  assert.equal(submissionOptions({tags:[],topic_id:12,topic_name:'旅途',mission_id:20},'').mission_id,20)
})
test('评论分页去重有上限，一级评论数不冒称包含全部回复',async()=>{
  const {client,run}=fixture();let reads=0
  client.video={info:async()=>({aid:10,title:'视频',owner:{mid:123}})}
  client.reply={list:async({pn})=>{reads++;return {page:{acount:40},replies:Array.from({length:20},(_,i)=>({rpid_str:String(pn===1?i+1:i+11),content:{message:'评论'},member:{uname:'观众'},like:1,mid:456}))}}}
  const result=await run('comments',{limit:25})
  assert.equal(result.items.length,25);assert.equal(reads,2);assert.equal(new Set(result.items.map(r=>r.id)).size,25)
  assert.match(result.scope,/一级评论/)
})
test('字幕精确选择分P与语言，不回退另一语言，不向任意主机发Cookie',async()=>{
  const {client,run}=fixture()
  let subtitleUrl='https://evil.example/subtitle'
  client.video={info:async()=>({pages:[{cid:1,part:'原片',duration:12}]}),playerInfo:async()=>({subtitle:{subtitles:[{id_str:'123',lan:'en',lan_doc:'英语',subtitle_url:subtitleUrl}]}})}
  await assert.rejects(run('subtitle',{cid:99,subtitle_id:'123'}))
  await assert.rejects(run('subtitle',{cid:1,subtitle_id:'456'}),e=>e.code==='PUBLISH_SUBTITLE_CHANGED')
  await assert.rejects(run('subtitle',{cid:1,subtitle_id:'123'}))
  subtitleUrl='https://aisubtitle.hdslb.com/subtitle.json'
  const original=globalThis.fetch
  globalThis.fetch=async(url,options)=>{
    assert.equal(options.redirect,'error');assert.equal(options.headers.cookie,undefined)
    return new Response(JSON.stringify({body:[{from:0,to:2,content:'Hello'}]}))
  }
  try{const result=await run('subtitle',{cid:1,subtitle_id:'123'});assert.equal(result.lines[0].text,'Hello')}
  finally{globalThis.fetch=original}
})
test('章节只读取播放器 view_points，并清洗无效行与分P边界',async()=>{
  const {client,run}=fixture()
  client.video={
    info:async()=>({pages:[{cid:1,part:'原片',duration:120},{cid:2,part:'配音',duration:80}]}),
    playerInfo:async({cid})=>({view_points:cid===1 ? [
      {from:0,to:12,content:'开场'},
      {from:12,to:30,content:'第二节'},
      {from:-1,to:8,content:'坏时间'},
      {from:40,to:20,content:'坏范围'},
      {from:31,to:35,content:''},
      {from:30,to:40,title:'旧字段'}
    ] : [{from:0,to:10,content:'配音开场'}]})
  }
  const all=await run('chapters')
  assert.equal(all.parts.length,2)
  assert.deepEqual(all.parts[0].items.map(v=>v.title),['开场','第二节','旧字段'])
  assert.equal(all.total,4)
  const one=await run('chapters',{cid:2})
  assert.equal(one.parts.length,1)
  assert.deepEqual(one.items.map(v=>v.title),['配音开场'])
})

test('原生章节读取通过官方 allcards 并保留分P边界', async () => {
  const {run, platform} = fixture()
  const requests = []
  platform.request.get = async (url, options) => {
    requests.push({url, options})
    return url.includes('/viewpoint/rule') ? {chapters:{limit:20,stay:1,space:0}}
      : {catalog:[{type:2,permanent:1,cards:[{from:0,to:10,content:'开场'},{from:10,to:20,content:'第二节'}]}]}
  }
  const result = await run('chapter_read', {aid:10, cid:1})
  assert.equal(result.source, 'bilibili.allcards')
  assert.deepEqual(result.items, [{start_time:0,end_time:10,title:'开场'},{start_time:10,end_time:20,title:'第二节'}])
  assert.equal(result.permanent, true)
  assert.equal(requests[0].options.params.aid, 10)
  assert.equal(requests[0].options.params.cid, 1)
})

test('官方 allcards 对未设置章节返回 data=null 时按空章节处理', async () => {
  const {run, platform} = fixture()
  platform.request.get = async () => ({code: 0, data: null})
  const result = await run('chapter_read', {aid:10, cid:1})
  assert.deepEqual(result.items, [])
  assert.equal(result.permanent, false)
})

test('章节预览按实时规则校验并生成快照指纹，未写入', async () => {
  const {run, platform, calls} = fixture()
  platform.request.get = async url => url.includes('/viewpoint/rule')
    ? {chapters:{limit:20,stay:1,space:0}}
    : {catalog:[{type:2,permanent:0,cards:[]}]}
  const result = await run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:12,content:'第一节'}],permanent:true})
  assert.equal(result.cards[0].content, '第一节')
  assert.equal(typeof result.fingerprint, 'string')
  assert.equal(result.snapshot.cards.length, 0)
  assert.equal(calls.length, 0)
})

test('章节写入只允许确认快照，禁用自动重试，并回读验证', async () => {
  const {run, platform, calls} = fixture()
  let current = [], permanent = false
  platform.request.get = async url => url.includes('/viewpoint/rule')
    ? {chapters:{limit:20,stay:1,space:0}}
    : {catalog:[{type:2,permanent,cards:current}]}
  const preview = await run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:12,content:'第一节'}],permanent:true})
  platform.request.post = async (...args) => {
    calls.push(args)
    current = JSON.parse(args[1].cards)
    permanent = args[1].permanent === true
    return {data:{code:0}}
  }
  const result = await run('chapter_write', {aid:10,cid:1,confirmed:true,prepared:preview.snapshot,
    cards:preview.cards,permanent:true})
  assert.equal(result.verified, true)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][1].aid, 10)
  assert.equal(calls[0][1].cid, 1)
  assert.equal(calls[0][1].type, 2)
  assert.equal(calls[0][1].permanent, true)
  assert.equal(calls[0][2]['axios-retry'].retries, 0)
})

test('章节写入未确认、远端变化和网络中断不会重复发送', async () => {
  const {run, platform, calls} = fixture()
  let current = []
  platform.request.get = async url => url.includes('/viewpoint/rule')
    ? {chapters:{limit:20,stay:1,space:0}}
    : {catalog:[{type:2,permanent:0,cards:current}]}
  const preview = await run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:12,content:'第一节'}]})
  await assert.rejects(run('chapter_write', {aid:10,cid:1,prepared:preview.snapshot,cards:preview.cards}), e => e.code === 'PUBLISH_CHAPTER_NOT_SENT')
  current = [{from:0,to:12,content:'官网修改'}]
  await assert.rejects(run('chapter_write', {aid:10,cid:1,confirmed:true,prepared:preview.snapshot,cards:preview.cards}), e => e.code === 'PUBLISH_CHAPTER_REMOTE_CHANGED')
  current = []
  const fresh = await run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:12,content:'第一节'}]})
  platform.request.post = async () => { calls.push(1); throw new Error('timeout') }
  await assert.rejects(run('chapter_write', {aid:10,cid:1,confirmed:true,prepared:fresh.snapshot,cards:fresh.cards}), e => e.code === 'PUBLISH_CHAPTER_UNKNOWN')
  assert.equal(calls.length, 1)
})







test('章节标题长度、规则数量和间隔在本地拒绝', async () => {
  const {run, platform, calls} = fixture()
  platform.request.get = async url => url.includes('/viewpoint/rule')
    ? {chapters:{limit:1,stay:5,space:2}}
    : {catalog:[{type:2,permanent:false,cards:[]}]}
  await assert.rejects(run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:5,content:'这一章标题超过十六个字符一定会被拒绝的长文本'}]}), e => e.code === 'PUBLISH_CHAPTER_TITLE_TOO_LONG')
  await assert.rejects(run('chapter_prepare', {aid:10,cid:1,cards:[{from:0,to:5,content:'第一章'},{from:6,to:10,content:'第二章'}]}), e => e.code === 'PUBLISH_CHAPTER_LIMIT_EXCEEDED')
  assert.equal(calls.length, 0)
})

test('章节预览把最后一章终点规范为分P总时长', async () => {
  const {run, platform} = fixture()
  platform.request.get = async url => url.includes('/viewpoint/rule')
    ? {chapters:{limit:10,stay:5,space:0}}
    : {catalog:[{type:2,permanent:0,cards:[]}]}
  const result = await run('chapter_prepare', {aid:10,cid:1,duration:120,
    cards:[{from:0,to:10,content:'第一节'},{from:10,to:20,content:'最后一节'}]})
  assert.equal(result.cards.at(-1).to, 120)
})

test('统一编辑映射封面、分区、版权来源和文案，全部回读后才确认', async () => {
  const {raw,run,calls}=fixture()
  raw.no_disturbance=1
  raw.archive.recreate=1
  raw.archive.attrs={is_dolby:1,lossless_music:1}
  const detail=await run('detail')
  assert.equal(detail.settings.cover,'https://i0.hdslb.com/cover.jpg')
  const values={title:'更新标题',description:'更新简介',tags:['历史','动画'],dynamic:'更新动态',
    declaration_id:4,tid:124,human_type2:1003,copyright:3,source:'https://www.youtube.com/watch?v=fixture',
    cover:'https://i1.hdslb.com/bfs/archive/new-cover.jpg'}
  assert.equal((await run('write',{kind:'edit',version:detail.version,confirmed:true,values})).verified,true)
  const body=calls[0][1]
  assert.equal(body.human_type2,1003)
  assert.equal(body.tid,124)
  assert.equal(body.cover,values.cover)
  assert.equal(body.source,values.source)
  assert.equal(body.creation_statement.id,4)
  assert.equal(body.no_disturbance,1)
  assert.equal(body.recreate,1)
  assert.equal(body.dolby,1)
  assert.deepEqual((await run('detail')).settings,{...detail.settings,...values})
})

test('仅改分P名和排序使用完整replace清单，P简介按CID保留', async () => {
  const {raw,run,calls}=fixture()
  raw.videos[0].desc='原声说明'
  raw.videos[1].desc='配音说明'
  const detail=await run('detail')
  const result=await run('write',{kind:'edit',version:detail.version,confirmed:true,values:{},
    part_titles:{'1':'新原声','2':'新配音'},order:[2,1],videos:[]})
  assert.equal(result.verified,true)
  assert.deepEqual(calls[0][1].videos,[{cid:2,filename:'two',title:'新配音',desc:'配音说明'},
    {cid:1,filename:'one',title:'新原声',desc:'原声说明'}])
})

test('同次换片、追加多个P、改名和重排按原CID身份重建，禁止隐式删除', async () => {
  const {raw,run,calls}=fixture()
  raw.videos[0].desc='原声说明'
  raw.videos[1].desc='配音说明'
  const detail=await run('detail')
  const replacements=[{old_cid:1,cid:3,filename:'new-source',title:'新原声'},
    {old_cid:0,cid:4,filename:'extra-one',title:'补充分P一'},
    {old_cid:0,cid:5,filename:'extra-two',title:'补充分P二'}]
  const videos=[{cid:5,filename:'extra-two',title:'补充分P二'},
    {cid:2,filename:'two',title:'保留配音'},
    {cid:3,filename:'new-source',title:'更新原声'},
    {cid:4,filename:'extra-one',title:'补充分P一'}]
  const result=await run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,
    values:{},replacements,part_titles:{'1':'更新原声','2':'保留配音'},order:[5,2,1,4],videos})
  assert.equal(result.verified,true)
  assert.deepEqual(calls[0][1].videos,videos.map(part => ({...part,
    ...(part.cid===2?{desc:'配音说明'}:part.cid===3?{desc:'原声说明'}:{})})))
})

test('追加模式也完整回读，不覆盖原P且不依赖SDK隐式排序', async () => {
  const {raw,run,calls}=fixture()
  raw.videos[0].desc='原P说明'
  const detail=await run('detail')
  const result=await run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
    replacements:[{old_cid:0,cid:3,filename:'extra',title:'新分P'}],
    videos:[{cid:1,filename:'one',title:'原声'},{cid:2,filename:'two',title:'配音'},{cid:3,filename:'extra',title:'新分P'}]})
  assert.equal(result.verified,true)
  assert.equal(calls[0][1].videos[0].desc,'原P说明')
  assert.deepEqual(calls[0][1].videos.map(part=>part.cid),[1,2,3])
})

test('非法改名、漏P顺序、重复CID及未授权改写最终清单均在写入前拒绝', async () => {
  for (const change of [{part_titles:{'8':'不存在'}},{part_titles:{'1':''}},
    {part_titles:{'1':'x'.repeat(81)}},{part_titles:['错误']},{order:[1]},
    {order:[1,1]},{order:[2,1,3]},
    {videos:[{cid:1,filename:'tampered',title:'原声'},{cid:2,filename:'two',title:'配音'}]}]) {
    const {run,calls}=fixture(),detail=await run('detail')
    await assert.rejects(run('write',{kind:'edit',version:detail.version,confirmed:true,values:{},...change}),
      error=>error.code==='PUBLISH_REMOTE_CHANGED')
    assert.equal(calls.length,0)
  }
})

test('换片新增的最终P数不能超过100，即使上传回执各自合法', async () => {
  const {raw,run,calls}=fixture()
  raw.videos=Array.from({length:100},(_,index)=>({cid:index+1,filename:`old-${index}`,title:`P${index+1}`}))
  const detail=await run('detail')
  await assert.rejects(run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
    replacements:[{old_cid:0,cid:101,filename:'extra',title:'额外P'}],
    videos:[...raw.videos,{cid:101,filename:'extra',title:'额外P'}]}),error=>error.code==='PUBLISH_REMOTE_CHANGED')
  assert.equal(calls.length,0)
})

test('非法封面、未知权限字段和无效版权来源组合不发写请求', async () => {
  const invalid=[{cover:'http://i0.hdslb.com/cover.jpg'},{cover:'https://evil.example/cover.jpg'},
    {cover:'https://i0.hdslb.com.evil.example/cover.jpg'},{cover:'https://user@i0.hdslb.com/cover.jpg'},
    {cover:'C:\\private\\cover.png'},{selected_comments:false},{close_comments:true},{close_danmaku:true},
    {visibility:'self'},{human_type2:-1},{tid:0},{copyright:2,source:''},
    {copyright:2,source:'来源'},{declaration_id:null}]
  for (const values of invalid) {
    const {run,calls}=fixture(),detail=await run('detail')
    await assert.rejects(run('write',{kind:'edit',version:detail.version,confirmed:true,values}),
      error=>error.code==='PUBLISH_WRITE_NOT_SENT')
    assert.equal(calls.length,0)
  }
})

test('转载来源可修改，版权合并有效后按新组合提交', async () => {
  const {raw,run,calls}=fixture()
  raw.archive.copyright=2
  raw.archive.creation_statement=null
  let detail=await run('detail')
  assert.equal((await run('write',{kind:'edit',version:detail.version,confirmed:true,values:{source:'新来源'}})).verified,true)
  assert.equal(calls[0][1].source,'新来源')
  detail=await run('detail')
  assert.equal((await run('write',{kind:'edit',version:detail.version,confirmed:true,
    values:{copyright:3,declaration_id:1}})).verified,true)
  assert.equal(calls[1][1].copyright,3)
})

test('资料或封面被平台忽略、P说明或动态推送选项丢失时只写一次并返回未知', async () => {
  for (const mode of ['cover','human_type2','source','part_title','part_desc','no_disturbance']) {
    const {raw,run,platform,calls}=fixture()
    raw.no_disturbance=1
    raw.videos[0].desc='需保留说明'
    const detail=await run('detail')
    const original=platform.request.post
    platform.request.post=async(...args)=>{
      const result=await original(...args)
      if (mode==='cover') raw.archive.cover='https://i0.hdslb.com/ignored.jpg'
      if (mode==='human_type2') raw.archive.human_type2=1001
      if (mode==='source') raw.archive.source='平台旧来源'
      if (mode==='part_title') raw.videos[0].title='平台其他标题'
      if (mode==='part_desc') delete raw.videos[0].desc
      if (mode==='no_disturbance') raw.no_disturbance=0
      return result
    }
    await assert.rejects(run('write',{kind:'edit',version:detail.version,confirmed:true,
      values:{cover:'https://i0.hdslb.com/updated.jpg',human_type2:1003,source:'新来源'},part_titles:{'1':'新P名'}}),
      error=>error.code==='PUBLISH_WRITE_UNKNOWN' && error.detail.stage==='verify')
    assert.equal(calls.length,1)
  }
})

test('纯文案和追加被平台延迟应用时不能将接受误报为成功', async () => {
  for (const kind of ['edit','append']) {
    const {run,platform,calls}=fixture(),detail=await run('detail')
    platform.request.post=async(...args)=>{calls.push(args);return {aid:10,bvid:BV}}
    await assert.rejects(run('write',{kind,version:detail.version,confirmed:true,
      values:kind==='edit'?{title:'新标题'}:{},videos:kind==='append'?[{cid:3,filename:'extra',title:'新分P'}]:[]}),
      error=>error.code==='PUBLISH_WRITE_UNKNOWN')
    assert.equal(calls.length,1)
  }
})

test('平台给新增P补充空说明属于等价结果，不误报未知', async () => {
  const {raw,run,platform}=fixture(),detail=await run('detail')
  const original=platform.request.post
  platform.request.post=async(...args)=>{
    const result=await original(...args)
    for (const part of raw.videos) part.desc ??= ''
    return result
  }
  const result=await run('write',{kind:'append',version:detail.version,confirmed:true,values:{},
    videos:[{cid:3,filename:'extra',title:'新增P'}]})
  assert.equal(result.verified,true)
})

test('详情仅提供P说明的受控摘要，用于未知写入恢复核验', async () => {
  const {raw,run}=fixture()
  raw.no_disturbance=1
  raw.videos[0].desc='内部说明，不应传回内容'
  const detail=await run('detail')
  assert.deepEqual(detail.preservation,{version:1,no_disturbance:1,parts:[
    {cid:1,description_hash:createHash('sha256').update(raw.videos[0].desc).digest('hex')},
    {cid:2,description_hash:createHash('sha256').update('').digest('hex')} ]})
  assert.doesNotMatch(JSON.stringify(detail),/内部说明/)
})

test('换片重排和追加的冻结保留摘要按原CID映射，并在发送前复核', async () => {
  for (const invalid of [false,true]) {
    const {raw,run,calls}=fixture()
    raw.videos[0].desc='原声说明'
    raw.videos[1].desc='配音说明'
    raw.no_disturbance=0
    const detail=await run('detail')
    const expected={version:1,no_disturbance:0,parts:[detail.preservation.parts[1],
      {cid:3,description_hash:detail.preservation.parts[0].description_hash},
      {cid:4,description_hash:createHash('sha256').update('').digest('hex')} ]}
    if (invalid) expected.parts[1].description_hash='0'.repeat(64)
    const action=run('write',{kind:'replace',bound_aid:10,version:detail.version,confirmed:true,values:{},
      replacements:[{old_cid:1,cid:3,filename:'new-source',title:'新原声'},
        {old_cid:0,cid:4,filename:'new-extra',title:'新增P'}],order:[2,1,4],
      videos:[{cid:2,filename:'two',title:'配音'},{cid:3,filename:'new-source',title:'新原声'},
        {cid:4,filename:'new-extra',title:'新增P'}],expected_preservation:expected})
    if (invalid) {
      await assert.rejects(action,error=>error.code==='PUBLISH_REMOTE_CHANGED')
      assert.equal(calls.length,0)
    } else {
      assert.equal((await action).verified,true)
      assert.equal(calls.length,1)
    }
  }
})
