import assert from 'node:assert/strict'
import test from 'node:test'
import { workflowPublication, workflowRunStage } from '../src/utils/workflow.ts'

test('自动投稿设置不冒充已创建稿件，三种终点的提示互不混淆',()=>{
  for(const [mode,note] of [['publish','已启用自动投稿'],['draft','准备草稿'],['produce','仅制作成片']]){
    const view=workflowPublication({mode,run:null},{status:'pending'})
    assert.equal(view.text,'尚未开始');assert.ok(view.note.includes(note));assert.equal(view.dot,'pending')
  }
})
test('制作、准备发布和实际上传依次衔接，本次确认范围不随新的默认终点漂移',()=>{
  const summary={mode:'produce',run:{mode:'publish',status:'running',cursor:1,total:3,publication:null as any}}
  let view=workflowPublication(summary,{id:'旧草稿',status:'draft'})
  assert.equal(view.text,'等待制作完成');assert.equal(view.dot,'running');assert.match(view.note,/自动投稿/)
  summary.run.cursor=3
  assert.equal(workflowPublication(summary,null).text,'准备发布资料')
  summary.run.publication={id:'本次稿件',status:'uploading'}
  assert.equal(workflowPublication(summary,{id:'其他稿件',status:'published'}).text,'上传文件中')
  summary.run.publication.status='uploaded'
  assert.equal(workflowPublication(summary,null).text,'上传完成 · 准备投稿')
  summary.run.status='paused'
  assert.equal(workflowPublication(summary,null).text,'已上传 · 待确认投稿')
})
test('暂停、失败、审核与合集异常不伪装成空闲或成功，制作重跑不隐藏旧发布记录',()=>{
  for(const status of ['paused','failed','attention'])assert.equal(workflowPublication({mode:'publish',run:{mode:'publish',status}},null).text,'流程待继续')
  assert.equal(workflowRunStage({status:'monitoring'}).text,'等待平台审核')
  assert.equal(workflowPublication({mode:'publish',run:{mode:'publish',status:'attention',publication:{id:'p',status:'collection_pending'}}},null).tone,'warning')
  assert.equal(workflowPublication({mode:'produce',run:{mode:'produce',status:'running'}},{id:'p',status:'published'}).text,'已公开发布')
  assert.equal(workflowPublication(null,null,'自动流程读取失败').text,'状态读取失败')
})
