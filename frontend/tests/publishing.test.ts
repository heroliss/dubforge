import test from 'node:test'
import assert from 'node:assert/strict'
import { movePart, categoryOptions, publicationScope, publicationAiNotice, PUBLICATION_STATUS, collectionAvailability, retainSelectedOption, withSourcePublishedDate } from '../src/utils/publishing.ts'

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
