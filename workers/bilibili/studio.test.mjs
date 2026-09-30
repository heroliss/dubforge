import test from 'node:test'
import assert from 'node:assert/strict'
import { Platform } from '@renmu/bili-api'
import { studioOperation, archiveView } from './studio.mjs'
import { submissionOptions } from './protocol.mjs'

const BV='BV1234567890'
function fixture(){
  const raw={archive:{bvid:BV,aid:10,mid:123,title:'原标题',desc:'原简介',tag:'旅行',tid:21,human_type2:1001,
    copyright:3,source:'原来源',cover:'https://i0.hdslb.com/cover.jpg',dynamic:'动态',creation_statement:{id:1},
    desc_v2:[{type:1,raw_text:'旧富文本',biz_id:''}],state:0},
    watermark:{state:1},videos:[{cid:1,title:'原声',filename:'one'},{cid:2,title:'配音',filename:'two'}]}
  const calls=[]
  const platform=Object.create(Platform.prototype)
  platform.auth={authLogin(){},cookieObj:{bili_jct:'fixture-only'}}
  platform.getArchive=async()=>structuredClone(raw)
  platform.request={post:async(...args)=>{calls.push(args);return {aid:10,bvid:BV}}}
  const client={platform}
  const run=(op,p={})=>studioOperation(client,'studio_'+op,{account_id:'123',bvid:BV,...p})
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
