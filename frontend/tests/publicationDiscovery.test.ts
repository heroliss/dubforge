import assert from 'node:assert/strict'
import test from 'node:test'
import { createPublicationDiscovery, publicationObservationChanged } from '../src/utils/publicationDiscovery.ts'

test('同记录较新状态或修订才触发详情核对，旧目录与未知顺序不覆盖当前回执',()=>{
  const current={id:'same',status:'draft',revision:3,updated_at:'2026-10-20T00:00:00'}
  assert.equal(publicationObservationChanged(current,{...current,status:'submitted',updated_at:'2026-10-20T08:00:01+08:00'}),true)
  assert.equal(publicationObservationChanged(current,{...current,revision:4}),true)
  assert.equal(publicationObservationChanged(current,{...current,id:'other',revision:4}),false)
  assert.equal(publicationObservationChanged(current,{...current,status:'submitted',updated_at:'2026-10-19T23:59:59Z'}),false)
  assert.equal(publicationObservationChanged({...current,revision:4},{...current,status:'submitted'}),false)
  assert.equal(publicationObservationChanged({id:'same',status:'draft'},{id:'same',status:'queued'}),true)
  assert.equal(publicationObservationChanged({id:'same',status:'published'},{id:'same',status:'submitted'}),false)
  assert.equal(publicationObservationChanged(current,{...current}),false)
})

test('只有明确执行事实触发发现读取，不在空闲或未知页面追加请求',()=>{
  const discovery=createPublicationDiscovery()
  discovery.observe('project:a',false);assert.equal(discovery.read(),null)
  discovery.observe('',true);assert.equal(discovery.read(),null)
  discovery.observe('project:a',true);assert.equal(discovery.read()?.active,true)
})

test('执行中的成功读取不能抵消执行结束后的尾读，尾读成功后停止',()=>{
  const discovery=createPublicationDiscovery()
  discovery.observe('project:a',true)
  const reading=discovery.read()!
  discovery.observe('project:a',false);discovery.complete(reading)
  const tail=discovery.read()!;assert.equal(tail.active,false)
  discovery.complete(tail);assert.equal(discovery.read(),null)
})

test('尾读失败保留重试，新一轮执行和项目切换使旧成功回执失效',()=>{
  const discovery=createPublicationDiscovery()
  discovery.observe('project:a',true);discovery.observe('project:a',false)
  const tail=discovery.read()!;assert.ok(discovery.read(),'失败时不完成读取，下一轮仍需重试')
  discovery.observe('project:a',true);discovery.observe('project:a',false)
  discovery.complete(tail);assert.ok(discovery.read())
  const current=discovery.read()!
  discovery.observe('project:b',false);discovery.complete(current);assert.equal(discovery.read(),null)
  discovery.observe('project:b',true);discovery.cancel();assert.equal(discovery.read(),null)
})
