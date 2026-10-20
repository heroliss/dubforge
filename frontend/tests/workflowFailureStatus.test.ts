import assert from 'node:assert/strict'
import test from 'node:test'
import { workflowFailureStatus } from '../src/utils/workflowStatus.ts'

const run=()=>({id:'r',status:'failed',cursor:1,error:'tts 未完成：步骤被用户取消'})
const plan=()=>({id:'r',steps:['translate','tts','assemble'],labels:{tts:'字幕与配音'}})

test('历史自动流程中断与已完成的当前步骤分开说明，保留原始流程失败',()=>{
  const previous=run(),before=structuredClone(previous)
  const status=workflowFailureStatus(previous,plan(),[{key:'tts',label:'配音',status:'done',fresh:true}])!
  assert.equal(status.historical,true);assert.equal(status.recovered,true)
  assert.equal(status.message,'上次自动流程在字幕与配音中断；该步骤目前已完成。')
  assert.equal(status.reason,previous.error);assert.deepEqual(previous,before)
})

test('当前步骤运行、失败或过期时，不说历史中断点已恢复；可信旧数据允许缺少fresh字段',()=>{
  for(const step of [{status:'running',fresh:true},{status:'failed',fresh:false},{status:'done',fresh:false}]) {
    const status=workflowFailureStatus(run(),plan(),[{key:'tts',label:'配音',...step}])!
    assert.equal(status.recovered,false);assert.match(status.message,/上次自动流程在字幕与配音中断/)
    assert.doesNotMatch(status.message,/目前已完成/)
  }
  assert.equal(workflowFailureStatus(run(),plan(),[{key:'tts',status:'done'}])!.recovered,true)
  assert.equal(workflowFailureStatus(run(),plan(),[{key:'tts',status:'done',fresh:true}],false)!.recovered,false)
})

test('未取得匹配计划、不可信游标或非制作步骤时，不从错误文字猜恢复状态',()=>{
  const steps=[{key:'tts',label:'配音',status:'done',fresh:true}]
  for(const snapshot of [null,{...plan(),id:'old'},{...plan(),steps:[]},{...plan(),steps:['translate','comments']}]) {
    const status=workflowFailureStatus(run(),snapshot,steps)!
    assert.equal(status.recovered,false)
  }
  assert.equal(workflowFailureStatus({...run(),cursor:-1},plan(),steps)!.recovered,false)
  assert.equal(workflowFailureStatus({...run(),cursor:1.2},plan(),steps)!.recovered,false)
  assert.equal(workflowFailureStatus(run(),null,steps)!.stepLabel,undefined)
  assert.equal(workflowFailureStatus(run(),{...plan(),labels:{}},steps)!.stepLabel,'配音')
})

test('当前活跃流程及已经关联稿件的处理错误不能作为已恢复历史折叠',()=>{
  const steps=[{key:'tts',status:'done',fresh:true}]
  for(const current of [{...run(),status:'running'},{...run(),status:'monitoring'},{...run(),status:'attention',publication_id:'pub'}]) {
    const status=workflowFailureStatus(current,plan(),steps)!
    assert.equal(status.historical,false);assert.equal(status.recovered,false)
    assert.match(status.message,/当前自动流程需要处理/)
  }
  assert.equal(workflowFailureStatus({...run(),error:''},plan(),steps),null)
})

test('制作游标走完且明确停在准备发布资料时，说明历史完成但不冒充制作步骤恢复',()=>{
  const previous={...run(),cursor:3,phase:'准备发布资料',error:'AI 文案请求超时'},before=structuredClone(previous)
  const status=workflowFailureStatus(previous,plan(),[{key:'assemble',status:'running',fresh:false}])!
  assert.equal(status.historical,true);assert.equal(status.recovered,false)
  assert.equal(status.stepKey,undefined);assert.equal(status.stepLabel,undefined);assert.equal(status.phaseLabel,'准备发布资料')
  assert.equal(status.message,'上次自动流程在准备发布资料时中断；制作步骤已完成。')
  assert.doesNotMatch(status.message,/目前已完成|最新|正在失败/)
  assert.equal(status.reason,previous.error);assert.deepEqual(previous,before)
})

test('发布资料阶段只凭匹配计划、完整游标和明确phase识别，不根据AI超时错误猜阶段',()=>{
  const previous={...run(),cursor:3,phase:'准备发布资料',error:'AI 文案请求超时'}
  for(const [current,snapshot] of [
    [previous,{...plan(),id:'other'}], [{...previous,cursor:2},plan()], [{...previous,cursor:4},plan()],
    [{...previous,phase:''},plan()], [{...previous,phase:'其他阶段'},plan()],
  ]) {
    const status=workflowFailureStatus(current,snapshot)!
    assert.equal(status.phaseLabel,undefined);assert.doesNotMatch(status.message,/制作步骤已完成/)
  }
  const active=workflowFailureStatus({...previous,status:'running'},plan())!
  assert.equal(active.historical,false);assert.equal(active.phaseLabel,undefined);assert.equal(active.recovered,false)
  assert.equal(active.message,'当前自动流程需要处理。')
})
