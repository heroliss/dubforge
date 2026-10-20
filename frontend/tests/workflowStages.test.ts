import assert from 'node:assert/strict'
import test from 'node:test'
import { WORKFLOW_STAGES, adjacentStage, stageNumber,stagePath,stageSettingsPath,settingsStageKey,workflowStageSequence,commentStageDisplay } from '../src/utils/workflowStages.ts'

test('工作流9阶段共用名称顺序，评论在字幕配音后成片前，发布最后',()=>{
  assert.deepEqual(WORKFLOW_STAGES.map(stage=>stage.key),['download','preprocess','transcribe','split','translate','tts','comments','assemble','publish'])
  assert.equal(stageNumber('comments'),7);assert.equal(stageNumber('assemble'),8);assert.equal(stageNumber('publish'),9)
  assert.equal(adjacentStage('tts',1)?.key,'comments');assert.equal(adjacentStage('assemble',-1)?.key,'comments')
  assert.equal(adjacentStage('download',-1),undefined);assert.equal(adjacentStage('publish',1),undefined)
  assert.equal(WORKFLOW_STAGES.find(stage=>stage.key==='comments')?.taskKeys.length,0)
})
test('阶段地址与设置深链对应，旧字幕和发布配置key保持兼容',()=>{
  assert.equal(stagePath('a b','comments'),'/projects/a%20b/comments')
  assert.equal(stagePath('p','publish'),'/projects/p/publish')
  assert.equal(stagePath('p','subtitles'),'/projects/p/steps/tts')
  assert.equal(settingsStageKey('publish'),'publication');assert.equal(settingsStageKey('subtitles'),'tts')
  assert.equal(stageSettingsPath('publication','新闻'),'\/settings?section=production&template=%E6%96%B0%E9%97%BB&stage=publish')
})
test('自动计划投影合并字幕与配音内部任务，保留真实索引用于完成和当前状态',()=>{
  const sequence=workflowStageSequence(['download','dubtask','tts','subtitles','comments','assemble'])
  assert.deepEqual(sequence.map(row=>row.key),['download','tts','comments','assemble'])
  assert.deepEqual(sequence[1].indices,[1,2,3]);assert.equal(sequence[1].label,'字幕与配音')
  const cursor=3
  assert.equal(sequence[1].indices.every(index=>index<cursor),false)
  assert.equal(sequence[1].indices.includes(cursor),true)
})

test('未知或损坏的步骤范围整体不投影阶段，不截取字符串或伪造部分分母',()=>{
  for(const value of [undefined,null,'tts',{0:'tts',length:1},42,[],['tts',null],['tts',{}],['tts',' ']]) {
    assert.deepEqual(workflowStageSequence(value),[])
  }
})
test('评论阶段只投影真实工作区摘要，旧服务或读取失败不假称准备完成',()=>{
  assert.equal(commentStageDisplay(null).status,'unknown')
  assert.equal(commentStageDisplay({jobs:{}}).label,'状态待核对')
  assert.equal(commentStageDisplay({workspace:{state:'attention',label:'待确认方案',confirmed_count:30}}).type,'warning')
  assert.equal(commentStageDisplay({workspace:{state:'done',label:'已保存',detail:'已保存30条'}}).label,'已保存')
  assert.equal(commentStageDisplay({workspace:{state:'done',label:'已保存'}},'未能连接').status,'unknown')
})
