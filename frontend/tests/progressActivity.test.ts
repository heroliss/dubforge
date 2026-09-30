import assert from 'node:assert/strict'
import test from 'node:test'
import { latestJobActivity, stepActivity, stoppingLabel, workflowComments, publicationActivity } from '../src/utils/progressActivity.ts'

test('步骤和总览共用停止原因文案，超时不能说成用户取消', () => {
  assert.equal(stoppingLabel({stop_reason:'timeout'}), '超时停止中')
  assert.equal(stoppingLabel({stop_reason:'user'}), '正在停止')
  assert.equal(stoppingLabel({}), '正在停止')
  const result = stepActivity({status:'running',cancelling:true,stop_reason:'timeout'})!
  assert.match(result.message, /超时停止中/)
  assert.doesNotMatch(result.message, /取消/)
})

test('进度摘要仅取当前任务，不把上一轮成功或后台配置输出当成正在处理', () => {
  const lines = ['[12:00:00] [任务#7] ✅ 步骤完成', '[12:01:00] [任务#8] 句 66: synthesized',
    '[12:01:01] [任务#8] 🔒 本次运行已固定配置快照', '[12:01:02] [任务#8] ⏱ 本步骤超时兜底: 3600s']
  assert.deepEqual(latestJobActivity(lines, 8), {message:'句 66: 配音已生成',time:'12:01:00',label:'最新动态'})
  assert.equal(latestJobActivity(lines, 9), null)
  assert.equal(latestJobActivity(lines, 0), null)
  assert.equal(latestJobActivity(null, 8), null)
})

test('摘要过滤媒体签名、代理凭据及冗长参考路径，但保留句子和时间信息', () => {
  const result = latestJobActivity(['[12:01:00] [任务#8] 句 66 音色参考：本句 12.0–15.0 秒 → D:\\项目\\tts\\refs\\id\\ref.wav；参考不足',
    '[12:01:01] [任务#7] 旧日志'], 8)!
  assert.match(result.message, /句 66.*12.0–15.0 秒.*参考不足/)
  assert.doesNotMatch(result.message, /ref.wav|D:/)
  const url = latestJobActivity(['[12:02:00] [任务#8] 下载失败 https://name:secret@example.com/path?signature=private'], 8)!
  assert.doesNotMatch(url.message, /secret|private/)
})

test('暂停和排队优先于日志；没有详细事件时展示真实阶段，不编造进度', () => {
  const row = {status:'running',label:'配音',synthesis_task:{total:90,completed:20}}
  const log = {label:'最新动态',message:'第 20 句已完成'}
  assert.equal(stepActivity(row, log), log)
  assert.match(stepActivity(row)!.message, /20 \/ 90/)
  assert.equal(stepActivity({...row,progress_phase:'合并音轨'})!.message, '合并音轨')
  assert.match(stepActivity({...row,pause_state:'paused'}, log)!.message, /已暂停/)
  assert.match(stepActivity({...row,cancelling:true}, log)!.message, /正在停止/)
  assert.match(stepActivity({...row,status:'queued'}, log)!.message, /等待执行/)
  assert.equal(stepActivity({...row,status:'done'}, log), null)
})

test('评论提示由本次冻结计划决定，明确配音后采集并区分完成和中断', () => {
  const run = {id:'r',status:'running',cursor:1}
  const plan = {id:'r',steps:['download','tts','comments','assemble'],labels:{tts:'字幕与配音'},recipe:{comment_limit:200,comment_count:30}}
  const waiting = workflowComments(run, plan)!
  assert.equal(waiting.state,'upcoming'); assert.match(waiting.message,/字幕与配音完成后/)
  assert.match(waiting.message,/200.*30/)
  assert.equal(workflowComments({...run,cursor:2},plan)!.state,'running')
  assert.equal(workflowComments({...run,cursor:3},plan)!.state,'done')
  assert.equal(workflowComments({...run,cursor:2,status:'failed'},plan)!.state,'stopped')
  assert.equal(workflowComments(run,{...plan,id:'old'}),null)
  assert.equal(workflowComments(run,{...plan,steps:['download','assemble']})!.state,'none')
})

test('发布动态显示稿件和实际确认分P数量，不根据百分比猜当前上传文件', () => {
  const row = {status:'uploading',snapshot:{settings:{title:'旅行'},parts:[{title:'原声'},{title:'配音'}]}}
  assert.match(publicationActivity(row)!.message,/旅行.*2 个分P/)
  assert.doesNotMatch(publicationActivity(row)!.message,/正在上传.*P1|当前.*P2/)
  assert.match(publicationActivity({...row,status:'paused'})!.message,/已暂停/)
  assert.match(publicationActivity({...row,status:'uploaded'})!.message,/等待确认投稿/)
  assert.equal(publicationActivity({...row,status:'published'}),null)
})
