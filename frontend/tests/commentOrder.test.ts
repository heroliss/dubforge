import assert from 'node:assert/strict'
import test from 'node:test'
import { chronologicalComments, commentPublishedAt, commentTimestamp } from '../src/utils/commentOrder.ts'

test('评论时间只接受有限正数，无效时间不能排序为最早发布',()=>{
  for(const value of [null,undefined,'100',true,false,0,-1,NaN,Infinity,-Infinity])
    assert.equal(commentTimestamp(value),null,String(value))
  assert.equal(commentTimestamp(100),100)
  assert.equal(commentTimestamp(100.25),100.25)
})

test('已知时间从早到晚，同刻与未知时间遵循来源采集顺序，来源之外稳定追加',()=>{
  const sources=[{id:'late',timestamp:300},{id:'tie-first',timestamp:200},{id:'early',timestamp:100},
    {id:'tie-second',timestamp:200},{id:'unknown-first'},{id:'unknown-second',timestamp:0}]
  const items=[{id:'unknown-second'},{id:'outside-first'},{id:'tie-second'},{id:'late'},
    {id:'unknown-first'},{id:'outside-second'},{id:'tie-first'},{id:'early'}]
  const original=structuredClone(items),sourceOriginal=structuredClone(sources)
  const ordered=chronologicalComments(items,sources)
  assert.deepEqual(ordered.map(item=>item.id),['early','tie-first','tie-second','late',
    'unknown-first','unknown-second','outside-first','outside-second'])
  assert.deepEqual(items,original,'排序不修改传入草稿')
  assert.deepEqual(sources,sourceOriginal,'排序不修改来源快照')
  assert.ok(ordered.every(item=>items.includes(item)),'仅改变排列，不重写用户对象')
})

test('同一来源以服务端评论时间为准，草稿传来的时间不能改变上屏队列',()=>{
  const sources=[{id:'later',source_hash:'same-later',timestamp:300},{id:'earlier',source_hash:'same-earlier',timestamp:100}]
  const drafts=[{id:'later',source_hash:'same-later',timestamp:1,display_text:'保留人工文字'},
    {id:'earlier',source_hash:'same-earlier',timestamp:999,display_text:'保留另一条'}]
  assert.deepEqual(chronologicalComments(drafts,sources).map(item=>item.id),['earlier','later'])
  assert.equal(drafts[0].display_text,'保留人工文字')
  assert.equal(drafts[0].timestamp,1)
})

test('没有发布时间显示明确未知，有效时间显示日期而不是 Unix 数值',()=>{
  for(const value of [undefined,null,0,'1700000000',Infinity,1e300])
    assert.equal(commentPublishedAt(value),'发布时间未知')
  const formatted=commentPublishedAt(1700000000)
  assert.match(formatted,/2023/)
  assert.doesNotMatch(formatted,/1700000000/)
})
