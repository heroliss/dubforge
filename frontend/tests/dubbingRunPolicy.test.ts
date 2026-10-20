import assert from 'node:assert/strict'
import test from 'node:test'
import { defaultDubbingRunPolicy, dubbingRunPolicy, dubbingPolicyKey, dubbingPolicyError, dubbingPlanHasWork } from '../src/utils/dubbingRunPolicy.ts'

test('默认只确定本次配音范围，不携带普通修复设置', () => {
  assert.deepEqual(dubbingRunPolicy(), {mode:'fill',target:'current',slot:null,line_settings:'preserve',include_manual_sources:false})
  const first=defaultDubbingRunPolicy();first.mode='all'
  assert.equal(defaultDubbingRunPolicy().mode,'fill','各次运行草稿互不共享')
})
test('恢复已有策略保留目标槽、参数来源和声源范围',()=>{
  const policy={mode:'all',target:'slot',slot:4,line_settings:'project',include_manual_sources:true} as const
  assert.deepEqual(dubbingRunPolicy(policy),policy)
  assert.equal(dubbingPolicyKey(policy),dubbingPolicyKey({...policy}))
  assert.notEqual(dubbingPolicyKey(policy),dubbingPolicyKey({...policy,slot:5}))
})
test('指定槽需有效槽号；仅补齐不依赖无关目标草稿',()=>{
  assert.match(dubbingPolicyError({...defaultDubbingRunPolicy(),mode:'all',target:'slot'}),/目标配音槽/)
  assert.match(dubbingPolicyError({...defaultDubbingRunPolicy(),mode:'changed',target:'slot',slot:10001}),/10000/)
  assert.equal(dubbingPolicyError({...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:2}),'')
  assert.equal(dubbingPolicyError({...defaultDubbingRunPolicy(),target:'slot'}),'')
})
test('显式重做任务不被旧步骤最新状态误判跳过',()=>{
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),generate:0}),false)
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),generate:5}),true)
  assert.equal(dubbingPlanHasWork({policy:{...defaultDubbingRunPolicy(),mode:'all'},deferred:true}),true)
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),deferred:true}),false)
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),generate:0,repair:2}),true)
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),generate:0,repair:0}),false)
  assert.equal(dubbingPlanHasWork({policy:defaultDubbingRunPolicy(),generate:0,repair_blocked:[{id:1,reason:'等待邻句完成'}]}),false)
})
test('旧运行快照的修复字段不进入新请求或本次范围身份',()=>{
  const legacy={...defaultDubbingRunPolicy(),repair_timing:false}
  assert.deepEqual(dubbingRunPolicy(legacy),defaultDubbingRunPolicy())
  assert.equal(dubbingPolicyKey(legacy),dubbingPolicyKey(defaultDubbingRunPolicy()))
  assert.deepEqual(dubbingRunPolicy({...legacy,target:'slot',slot:4,line_settings:'project',include_manual_sources:true}),defaultDubbingRunPolicy())
  assert.equal(Object.hasOwn(dubbingRunPolicy({mode:'all',repair_timing:false}),'repair_timing'),false)
  assert.equal(Object.hasOwn(dubbingRunPolicy({repair_timing:false}),'repair_timing'),false)
  assert.equal(Object.hasOwn(dubbingRunPolicy({mode:'all'}),'repair_timing'),false)
})
