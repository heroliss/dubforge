import test from 'node:test'
import assert from 'node:assert/strict'
import { movePart, categoryOptions, publicationScope, publicationAiNotice, PUBLICATION_STATUS, PUBLISH_ACTIVE, publicationReadbackPending, collectionAvailability, collectionDisplay, retainSelectedOption, withSourcePublishedDate, isPublicationComplete, publicationTargetDisplay, publicationVisibility } from '../src/utils/publishing.ts'

test('等待审核与合集继续回读后台结果，但不当作正在写入锁定，成功和人工处理态不空转',()=>{
  for(const status of [...PUBLISH_ACTIVE,'submitted','collection_pending']) assert.equal(publicationReadbackPending(status),true)
  for(const status of ['submitted','collection_pending']) assert.equal(PUBLISH_ACTIVE.includes(status),false)
  for(const status of ['published','private','reviewed','uploaded','draft','failed','unknown','paused','rejected',null,undefined])
    assert.equal(publicationReadbackPending(status),false)
})

test('来源日期补入保留手写文案，不重复追加，空日期不虚构', () => {
  const original = '手写简介\n原作者：频道\n原视频：https://example.com/v'
  const next = withSourcePublishedDate(original, '2026-09-01')
  assert.equal(next, original + '\n\n原视频发布时间：2026-09-01')
  assert.equal(withSourcePublishedDate(next, '2026-09-01'), next)
  assert.equal(withSourcePublishedDate(next, '2026-09-02'), original + '\n\n原视频发布时间：2026-09-02')
  assert.equal(withSourcePublishedDate(original, ''), original)
  assert.equal(withSourcePublishedDate('', '2026-09-01'), '原视频发布时间：2026-09-01')
  const spaced = '原视频发布时间：2026-09-01\n\n原作者：频道'
  assert.equal(withSourcePublishedDate(spaced, '2026-09-01'), spaced)
})

test('分P调序不修改原始确认清单且不越界', () => {
  const input = [{id:1},{id:2}]
  assert.deepEqual(movePart(input,1,-1).map(p => p.id), [2,1])
  assert.deepEqual(input.map(p => p.id), [1,2])
  assert.equal(movePart(input,0,-1), input)
})
test('发布确认包含账号稿件P数和可见性，上传完成不是发布成功', () => {
  const text = publicationScope({account_name:'测试账号',parts:[{},{}],settings:{visibility:'self',season_id:3}})
  assert.match(text, /1个稿件.*2个分P.*仅自己可见.*合集/)
  assert.match(PUBLICATION_STATUS.uploaded, /待确认投稿/)
  assert.notEqual(PUBLICATION_STATUS.uploaded, PUBLICATION_STATUS.published)
})
test('交付筛选只把平台成功终态视为已发布，不改变本地制作状态', () => {
  assert.equal(isPublicationComplete('published'), true)
  assert.equal(isPublicationComplete('private'), true)
  assert.equal(isPublicationComplete('reviewed'), true)
  for (const status of ['draft', 'uploaded', 'submitted', 'collection_pending', 'failed', 'rejected', '', null, undefined])
    assert.equal(isPublicationComplete(status), false)
})
test('合集显示优先名称，同时保留可核对的稳定 ID；旧记录回退到明确 ID', () => {
  assert.equal(collectionDisplay(9202389, '旅行合集', '正片'), '旅行合集 · 正片 · ID 9202389')
  assert.equal(collectionDisplay(9202389), '合集 · ID 9202389')
  assert.equal(collectionDisplay(null), '不加入合集')
})

test('真实完整流程预检从recipe绑定账号，名称正文与ID辅助信息分开',()=>{
  const result=publicationTargetDisplay({recipe:{account_id:'12'},account_name:'旅行账号',
    settings:{human_type2:1029,season_id:34,section_id:56},
    settings_display:{account_id:'12',human_type2:1029,season_id:34,section_id:56,account_name:'旅行账号',
      category_title:'生活 / 旅行',season_title:'旅行合集',section_title:'正片',source:'platform'}})
  assert.match(result.collection,/旅行合集.*正片.*34/)
  assert.deepEqual(result.fields.map(field=>[field.label,field.title,field.id]),[
    ['账号','旅行账号','12'],['分区','生活 / 旅行',1029],['合集','旅行合集',34],['小节','正片',56],
  ])
  assert.equal(result.cached,false);assert.match(result.notice,/本次读取/)
})

test('单稿预检读取snapshot内设置和账号，不丢合集身份',()=>{
  const result=publicationTargetDisplay({snapshot:{account_id:'12',settings:{season_id:34,section_id:56}},
    settings_display:{account_id:'12',season_id:34,section_id:56,account_name:'旅行账号',season_title:'旅行合集',section_title:'正篇'}})
  assert.match(result.account,/旅行账号/);assert.match(result.collection,/旅行合集.*正篇/)
})

test('旧记录保留合法ID且明确名称未载入，不猜测分区或账号名',()=>{
  const input={recipe:{account_id:'old'},settings:{human_type2:1011,season_id:34,section_id:56}}
  const before=structuredClone(input),result=publicationTargetDisplay(input)
  assert.match(result.account,/账号.*old.*名称未载入/)
  assert.match(result.collection,/34.*名称未载入.*56.*名称未载入/)
  assert.ok(result.fields.every(field=>field.title==='名称未载入'))
  assert.deepEqual(input,before)
  assert.equal(publicationTargetDisplay({settings:{season_id:null}}).collection,'不加入合集')
})

test('不同账号、不同合集和不同小节的名称快照不能串用',()=>{
  const base={recipe:{account_id:'12'},settings:{human_type2:1029,season_id:34,section_id:56},
    settings_display:{account_id:'other',human_type2:1029,season_id:34,section_id:56,
      account_name:'其他账号',category_title:'错误分区',season_title:'错误合集',section_title:'错误小节',source:'cache'}}
  let result=publicationTargetDisplay(base)
  assert.doesNotMatch(JSON.stringify(result),/其他账号|错误/);assert.equal(result.cached,false)
  result=publicationTargetDisplay({...base,settings_display:{...base.settings_display,account_id:'12',season_id:99}})
  assert.doesNotMatch(result.collection,/错误合集|错误小节/)
  result=publicationTargetDisplay({...base,settings_display:{...base.settings_display,account_id:'12',section_id:99}})
  assert.match(result.collection,/错误合集/);assert.doesNotMatch(result.collection,/错误小节/)
})

test('顺序批次从匹配账号与合集身份的预检提取名称，保留历史scope_display兼容',()=>{
  const display={account_id:'12',season_id:34,section_id:56,account_name:'旅行账号',season_title:'旅行合集',section_title:'正片',source:'cache'}
  const result=publicationTargetDisplay({scope:{account_id:'12',season_id:34,section_id:56},items:[
    {plan:{settings_display:{...display,account_id:'wrong',season_title:'错误合集'}}},{plan:{settings_display:display}},
  ]})
  assert.match(result.account,/旅行账号/);assert.match(result.collection,/旅行合集.*正片/)
  assert.equal(result.cached,true);assert.match(result.notice,/最近读取/)
  assert.match(publicationTargetDisplay({scope:{account_id:'12',season_id:34,section_id:56},scope_display:{season_title:'旧合集',section_title:'旧小节'}}).collection,/旧合集.*旧小节/)
})

test('本次读取或匹配缓存没有目标名称时不回填旧名称并冒充实时名称',()=>{
  for(const source of ['platform','cache']) {
    const result=publicationTargetDisplay({scope:{account_id:'12',season_id:34,section_id:56,season_title:'旧合集',section_title:'旧小节'},
      scope_display:{season_title:'更旧合集',section_title:'更旧小节'},
      settings_display:{account_id:'12',season_id:34,section_id:56,season_title:'',section_title:'',source}})
    assert.doesNotMatch(result.collection,/旧合集|旧小节/)
    assert.equal(result.fields.find(field=>field.key==='season')?.title,'名称未载入')
    assert.equal(result.fields.find(field=>field.key==='section')?.title,'名称未载入')
  }
})

test('未知可见性不冒充公开发布，不展示原始枚举值',()=>{
  assert.equal(publicationVisibility('self'),'仅自己可见');assert.equal(publicationVisibility('public'),'公开可见')
  assert.match(publicationVisibility('future_visibility'),/未识别.*核对/)
  assert.doesNotMatch(publicationVisibility('future_visibility'),/future_visibility/)
})
test('分区展示完整父子路径', () => {
  assert.deepEqual(categoryOptions([{id:1,name:'生活',children:[{id:2,name:'旅行'}]}]), [{value:2,label:'生活 / 旅行'}])
})


test('只有成功完整读取列表才显示合集失效，错误和加载中不作删除判断', () => {
  const values = {season_id:1,section_id:2}, state = {account:'123',ready:true}
  const meta = {seasons:[{id:1,title:'改名后的合集',sections:[{id:2,title:'正片'}]}]}
  assert.equal(collectionAvailability(values,meta,state).status,'valid')
  assert.match(collectionAvailability(values,meta,state).message,/改名后的合集/)
  assert.equal(collectionAvailability(values,{seasons:[]},state).status,'missing')
  assert.equal(collectionAvailability(values,{seasons:[{id:1,title:'旅行',sections:[]}]},state).status,'section_missing')
  for (const [data, s] of [[{},state],[{seasons:[],collection_error:true},state],
    [{seasons:[],collections_complete:false},state],[meta,{...state,error:'读取失败'}],
    [{seasons:[]},{...state,loading:true}],[{seasons:[]},{...state,ready:false}],
    [meta,{...state,account:''}]] as any[]) assert.equal(collectionAvailability(values,data,s).status,'unknown')
  assert.equal(collectionAvailability({season_id:null},{seasons:[]},state).status,'none')
  assert.deepEqual(values,{season_id:1,section_id:2})
})

test('下拉项缺失时保留已选ID的禁用占位，不偷偷取消合集绑定', () => {
  const options = [{value:9,label:'新合集'}]
  assert.deepEqual(retainSelectedOption(options,1,'原合集已不可用'), [...options,{value:1,label:'原合集已不可用',disabled:true}])
  assert.equal(retainSelectedOption(options,9,'占位'),options)
  assert.equal(retainSelectedOption(options,null,'占位'),options)
})

test('AI声明提醒只匹配选中配音与当前平台选项，不猜测编号、不覆盖选择', () => {
  const preview = {snapshot:{settings:{copyright:3,declaration_id:7},parts:[{has_dubbing:true}]},ai_declaration_acknowledged:false}
  assert.equal(publicationAiNotice(preview,{declarations:[{value:7,label:'含人工智能合成内容'}]}),'')
  assert.match(publicationAiNotice(preview,{declarations:[{value:7,label:'内容为个人观点'}]}),/建议核对AI声明/)
  assert.match(publicationAiNotice(preview,{}),/按当前声明继续/)
  preview.ai_declaration_acknowledged = true
  assert.equal(publicationAiNotice(preview,{}),'')
  preview.ai_declaration_acknowledged = false
  preview.snapshot.parts[0].has_dubbing = false
  assert.equal(publicationAiNotice(preview,{}),'')
  assert.equal(preview.snapshot.settings.declaration_id,7)
})
