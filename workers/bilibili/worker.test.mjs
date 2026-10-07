import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { execute } from './worker.mjs'
import { KNOWN_DECLARATIONS, safeError } from './protocol.mjs'

// 只替换外部 SDK，实际命令调度、校验与返回整形全部执行。
function fixture(platform = {}, mid = 123, events = [{ data: { loaded: 25, total: 100 } }], send = () => {}) {
  const calls = []
  const sdk = {
    Auth: class { setAuth() {} },
    Client: class {
      async setAuth() {}
      user = { request: { get: async () => ({ isLogin: true, mid, uname: '测试账号' }) } }
      platform = platform
    },
    WebVideoUploader: class {
      emitter = new EventEmitter()
      constructor(part, auth, options) { calls.push({ part, options }) }
      async upload() {
        for (const event of events) this.emitter.emit('progress', event)
        return { cid: 44, filename: 'upload-result', title: '配音版' }
      }
    },
  }
  const run = (operation, payload = {}) => execute({ operation,
    payload: { account_id: '123', ...payload }, credential: { DedeUserID: '123', bili_jct:'fixture-csrf' } }, sdk, send)
  return { run, calls }
}

test('真正执行上传分支，只返回一个分P，不调用投稿', async () => {
  const { run, calls } = fixture({ addMediaWebApiV3: () => assert.fail('上传不投稿') })
  assert.equal((await run('upload', {path:'fixture.mp4', title:'配音版'})).cid, 44)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.bcutPreUpload, false)
})

test('上传进度保留阶段变化与最终字节，过滤SDK地址与凭据', async () => {
  const received = []
  const events = [
    {event:'preupload-start',data:{loaded:0,total:100}},
    {event:'getUploadInfo-end',data:{loaded:0,total:100,auth:'secret',endpoint:'private'}},
    {event:'uploading',data:{loaded:25,total:100}},
    {event:'uploading',data:{loaded:26,total:100}},
    {event:'uploading',data:{loaded:100,total:100}},
    {event:'merge-start',data:{loaded:100,total:100}},
    {event:'merge-end',data:{loaded:100,total:100,auth:'secret'}},
    {event:'uploading',data:{loaded:NaN,total:100}},
  ]
  await fixture({},123,events,(event,data)=>received.push({event,...data})).run('upload')
  assert.deepEqual(received.map(e=>[e.phase,e.loaded]), [['preparing',0],['uploading',25],['uploading',100],['merging',100],['merging',100]])
  assert.doesNotMatch(JSON.stringify(received), /secret|private|auth|endpoint/)
})
test('确认缺失或账号不匹配时，不触达投稿方法', async () => {
  const platform = { addMediaWebApiV3: () => assert.fail('不应提交') }
  await assert.rejects(fixture(platform).run('submit'), /确认/)
  await assert.rejects(fixture(platform, 456).run('upload'), /账号校验不一致/)
})
test('提交使用有序分P列表；网络失败不重试、不切换接口', async () => {
  let requests = 0
  const { run } = fixture({ addMediaWebApiV3: async (videos, settings) => {
    requests++
    assert.deepEqual(videos.map(v => v.cid), [2, 1])
    assert.equal(settings.is_only_self, 1)
    assert.deepEqual(settings.creation_statement, {id:1})
    throw new Error('响应丢失')
  } })
  await assert.rejects(run('submit', { confirmed:true, videos:[{cid:2},{cid:1}],
    settings:{tags:['旅行'],visibility:'self',copyright:3,declaration_id:1} }), /响应丢失/)
  assert.equal(requests, 1)
})
test('读取新版分区、已知声明映射及分页合集，不读取或暴露凭据', async () => {
  const pages = []
  const { run } = fixture({
    getArchivePre: async () => ({typelist:[]}),
    getHumanType2List: async () => ({type_list:[{id:1001,name:'旅行'}]}),
    getSeasonList: async ({pn}) => {
      pages.push(pn)
      return {total:2,seasons:[{season:{id:pn,title:'系列'},sections:{sections:[{id:pn*10,title:'默认'}]}}]}
    },
  })
  const result = await run('metadata')
  assert.deepEqual(pages, [1, 2])
  assert.deepEqual(result.declarations, KNOWN_DECLARATIONS)
  assert.equal(result.human_categories[0].name, '旅行')
  assert.equal(result.seasons[1].sections[0].id, 20)
  assert.equal(result.declaration_source, 'adapter-2026-05-24')
  assert.equal(result.collections_complete, true)
  assert.equal(result.collection_error, false)
})

test('合集列表为空、失败、缺页、重复与分页上限有明确的完整性标记', async () => {
  const row = id => ({season:{id,title:'旅行'},sections:{sections:[{id:id*10,title:'正片'}]}})
  const cases = [
    {get:async()=>({total:0,seasons:[]}),complete:true},
    {get:async()=>({total:0,seasons:null}),complete:true},
    {get:async()=>{throw new Error('读取失败')},complete:false},
    {get:async()=>({total:1}),complete:false},
    {get:async()=>({seasons:[]}),complete:false},
    {get:async({pn})=>({total:2,seasons:pn===1?[row(1)]:[]}),complete:false},
    {get:async()=>({total:2,seasons:[row(1)]}),complete:false},
    {get:async({pn})=>({total:pn+1,seasons:[row(pn)]}),complete:false},
    {get:async({pn})=>({total:101,seasons:[row(pn)]}),complete:false},
    {get:async()=>({total:1,seasons:[{season:{id:1,title:'结构异常'}}]}),complete:false},
  ]
  for (const item of cases) {
    let requests = 0
    const {run} = fixture({getArchivePre:async()=>({}),getHumanType2List:async()=>({}),
      getSeasonList:async args=>{requests++;return item.get(args)}})
    const result = await run('metadata')
    assert.equal(result.collections_complete,item.complete)
    assert.equal(result.collection_error,!item.complete)
    assert.ok(requests<=100)
  }
})
test('合集重试检查顶层episodes，已存在则不重复加入；未知结构不盲重试', async () => {
  const platform = {
    getSeasonDetail: async () => ({sections:{sections:[{id:2}]}}),
    getSectionDetail: async () => ({section:{id:2},episodes:[{aid:99}]}),
    addMedia2Season: () => assert.fail('不能重复加入'),
  }
  assert.deepEqual(await fixture(platform).run('collection', {season_id:1,section_id:2,aid:99,cid:9}), {joined:true})
  platform.getSectionDetail = async () => ({section:{id:2}})
  await assert.rejects(fixture(platform).run('collection', {season_id:1,section_id:2,aid:99,cid:9}), /不完整/)
})

test('真实空合集的null成员可添加一次，加入后回读确认；重试不重复写', async () => {
  const payload={season_id:1,section_id:2,aid:99,cid:9,title:'旅行'}
  let added=false, writes=0, reads=0
  const platform={
    getSeasonDetail:async()=>({season:{id:1,mid:123},sections:{sections:[{id:2}]}}),
    getSectionDetail:async()=>{reads++;return {section:{id:2,seasonId:1,epCount:added?1:0,Episodes:null},episodes:added?[{aid:99,cid:9}]:null}},
    request:{post:async(url,body,options)=>{
      writes++;assert.equal(options.timeout,30000);assert.equal(options.maxRedirects,0)
      assert.equal(options['axios-retry'].retries,0)
      assert.deepEqual(body,{sectionId:2,episodes:[{aid:99,cid:9,title:'旅行'}]})
      added=true;return {}
    }},
    addMedia2Season:async function(body){return this.request.post('fixture',body,{params:{csrf:'test-only'}})},
    addMediaWebApiV3:()=>assert.fail('不能重复投稿'),
  }
  const original=platform.request.post
  assert.deepEqual(await fixture(platform).run('collection',payload),{joined:true})
  assert.equal(reads,2);assert.equal(writes,1);assert.equal(platform.request.post,original)
  assert.deepEqual(await fixture(platform).run('collection',payload),{joined:true})
  assert.equal(writes,1)
})

test('缺失列表、非零成员的null、数组冲突和目标不一致不能当成空合集', async () => {
  for(const data of [
    {section:{id:2,epCount:0}},
    {section:{id:2,epCount:1},episodes:null},
    {section:{id:2},episodes:null},
    {section:{id:2,episodes:[{aid:99}]},episodes:[]},
    {section:{id:2,epCount:3},episodes:[{aid:10}]},
    {section:{id:2,epCount:0},episodes:[{aid:99}]},
    {section:{id:2,epCount:0},episodes:{}},
    {section:{id:3,epCount:0},episodes:null},
    {section:{id:2,seasonId:3,epCount:0},episodes:null},
  ]) {
    const platform={getSeasonDetail:async()=>({sections:{sections:[{id:2}]}}),getSectionDetail:async()=>data,
      addMedia2Season:()=>assert.fail('不能误判后写入')}
    await assert.rejects(fixture(platform).run('collection',{season_id:1,section_id:2,aid:99,cid:9}),
      {code:'PUBLISH_COLLECTION_JOIN_NOT_SENT'})
  }
})

test('合集业务拒绝保留平台原因，写超时不自动重试；回读未确认不谎报成功', async () => {
  for(const failure of ['reject','timeout','unconfirmed','verify_error']) {
    let writes=0,reads=0
    const platform={
      getSeasonDetail:async()=>({sections:{sections:[{id:2}]}}),
      getSectionDetail:async()=>{if(++reads>1 && failure==='verify_error')throw {code:'ETIMEDOUT'};return {section:{id:2,epCount:0},episodes:null}},
      addMedia2Season:async()=>{writes++;if(failure==='reject')throw {code:21001,message:'该稿件不满足合集条件'};if(failure==='timeout')throw {code:'ETIMEDOUT'};return {}},
    }
    await assert.rejects(fixture(platform).run('collection',{season_id:1,section_id:2,aid:99,cid:9}),error=>{
      const safe=safeError(error)
      assert.equal(safe.code,failure==='reject'?'PUBLISH_COLLECTION_JOIN_REJECTED':'PUBLISH_COLLECTION_JOIN_UNKNOWN')
      assert.match(safe.message,/视频已投稿/);assert.match(safe.message,/无需重新上传或投稿/)
      if(failure==='reject')assert.match(safe.message,/21001.*该稿件不满足合集条件/)
      if(failure==='unconfirmed')assert.match(safe.message,/暂未在目标小节读到/)
      return true
    })
    assert.equal(writes,1)
  }
})

test('已有成员兼容嵌套数组；读取与登录失败保留具体阶段', async () => {
  const payload={season_id:1,section_id:2,aid:99,cid:9}
  for(const key of ['episodes','Episodes']) {
    const platform={getSeasonDetail:async()=>({sections:{sections:[{id:2}]}}),
      getSectionDetail:async()=>({section:{id:2,[key]:[{aid:99}]}}),
      addMedia2Season:()=>assert.fail('已在合集不能重复写')}
    assert.deepEqual(await fixture(platform).run('collection',payload),{joined:true})
  }
  await assert.rejects(fixture({getSeasonDetail:async()=>{throw {code:'ETIMEDOUT'}}}).run('collection',payload),error=>{
    assert.equal(safeError(error).detail.stage,'join_season');assert.match(error.message,/读取目标合集.*ETIMEDOUT/);return true
  })
  await assert.rejects(fixture({},456).run('collection',payload),error=>{
    assert.equal(safeError(error).detail.stage,'join_auth');assert.match(error.message,/登录校验/);return true
  })
})

test('新建合集必须确认，只上传封面并单次调用创建，不上传视频或投稿', async () => {
  const calls = []
  const platform = {
    uploadCover: async path => {calls.push('cover');assert.equal(path,'fixture.jpg');return {url:'https://archive.biliimg.com/bfs/archive/fixture.jpg'}},
    request:{post:async (url,body,options) => {
      calls.push('create')
      assert.equal(url,'https://member.bilibili.com/x2/creative/web/season/add')
      assert.deepEqual(Object.keys(body).sort(),['cover','csrf','desc','season_price','title'])
      assert.equal(body.season_price,0); assert.equal(body.csrf,'fixture-csrf')
      assert.equal(options['axios-retry'].retries,0); assert.equal(options.maxRedirects,0)
      assert.equal(options.headers['Content-Type'],'application/x-www-form-urlencoded')
      return 99
    }},
    addMediaWebApiV3: () => assert.fail('创建空合集不能投稿'),
  }
  const {run} = fixture(platform)
  const values = {title:'旅行',description:'简介',cover_path:'fixture.jpg'}
  await assert.rejects(run('create_collection',values),{code:'PUBLISH_COLLECTION_NOT_SENT'})
  assert.deepEqual(calls,[])
  assert.deepEqual(await run('create_collection',{...values,confirmed:true}),{season_id:99})
  assert.deepEqual(calls,['cover','create'])
})

test('创建响应丢失或结构异常不重试；平台明确拒绝和封面失败可区分', async () => {
  let creates = 0
  const platform = {uploadCover:async () => ({url:'https://i0.hdslb.com/fixture.jpg'}),
    request:{post:async () => {creates++;throw {code:'ECONNRESET'}}}}
  const payload = {confirmed:true,title:'旅行',description:'',cover_path:'fixture.jpg'}
  await assert.rejects(fixture(platform).run('create_collection',payload),{code:'PUBLISH_COLLECTION_UNKNOWN'})
  assert.equal(creates,1)
  platform.request.post = async () => {throw {code:-400}}
  await assert.rejects(fixture(platform).run('create_collection',payload),{code:'PUBLISH_COLLECTION_REJECTED'})
  platform.request.post = async () => ({unexpected:true})
  await assert.rejects(fixture(platform).run('create_collection',payload),{code:'PUBLISH_COLLECTION_UNKNOWN'})
  platform.uploadCover = async () => {throw new Error('图片失败')}
  platform.request.post = () => assert.fail('封面失败不能创建')
  await assert.rejects(fixture(platform).run('create_collection',payload),{code:'PUBLISH_COLLECTION_NOT_SENT'})
})
