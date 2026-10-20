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

test('收尾也是实际执行阶段，不丢弃清理和释放资源动态', () => {
  assert.match(latestJobActivity(['[12:03:00] [任务#8] 收尾清理: 已删除临时文件'],8)!.message,/收尾清理/)
  assert.match(latestJobActivity(['[12:03:01] [任务#8] 🧹 释放模型资源'],8)!.message,/释放模型资源/)
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

test('评论提示由本次冻结计划决定，明确优先复用、按需获取及中断后恢复范围', () => {
  const run = {id:'r',status:'running',cursor:1}
  const plan = {id:'r',steps:['download','tts','comments','assemble'],labels:{tts:'字幕与配音'},recipe:{comment_limit:200,comment_count:30}}
  const waiting = workflowComments(run, plan)!
  assert.equal(waiting.state,'upcoming'); assert.match(waiting.message,/字幕与配音完成后/)
  assert.match(waiting.message,/200.*30/)
  assert.match(waiting.message,/优先复用已采集评论.*需要时最多获取/)
  assert.match(waiting.message,/仅按需翻译候选/)
  const running=workflowComments({...run,cursor:2},plan)!
  assert.equal(running.state,'running');assert.match(running.message,/复用已有采集和有效评分.*按需翻译候选/)
  assert.equal(workflowComments({...run,cursor:3},plan)!.state,'done')
  const stopped=workflowComments({...run,cursor:2,status:'failed'},plan)!
  assert.equal(stopped.state,'stopped');assert.match(stopped.message,/已完成结果保留.*复用有效结果.*仅补缺失或变化/)
  assert.match(stopped.message,/采集范围扩大或来源变化时可能重新获取/)
  assert.equal(workflowComments(run,{...plan,id:'old'}),null)
  assert.equal(workflowComments(run,{...plan,steps:['download','assemble']})!.state,'none')
})

test('损坏或未读取的冻结范围不崩溃，也不能断言本次未安排评论',()=>{
  const run={id:'r',status:'running',cursor:1}
  for(const steps of [undefined,null,'comments',{0:'comments',length:1},['tts',null],['tts',{}],[' ']]) {
    assert.equal(workflowComments(run,{id:'r',steps}),null)
  }
  assert.equal(workflowComments(run,{id:'r',steps:[]})?.state,'none','明确空范围仍与未知区分')
})

test('评论当前运行阶段直接显示后端真实动态与数量，不混入其他步骤旧阶段', () => {
  const run={id:'r',status:'running',cursor:1},plan={id:'r',steps:['tts','comments','assemble']}
  const phases=['评论采集：检查可复用资料，缺少或范围扩大时获取','评论评分：本次 200 条，复用有效评分',
    '评论筛选：核对内容与各成片版本的显示要求','评论翻译：已选 3/30 条，本批 5 条候选','评论确认：保存 30 条上屏方案']
  for(const phase of phases) assert.equal(workflowComments({...run,phase},plan)!.message,phase)
  const oldPhase='正在生成配音 · 已完成 20 / 90 句'
  assert.doesNotMatch(workflowComments({...run,phase:oldPhase},plan)!.message,/配音|20|90/)
  assert.equal(workflowComments({...run,cursor:0,phase:phases[3]},plan)!.state,'upcoming')
  assert.doesNotMatch(workflowComments({...run,cursor:0,phase:phases[3]},plan)!.message,/已选 3\/30/)
  assert.equal(workflowComments({...run,cursor:2,phase:phases[3]},plan)!.state,'done')
  assert.equal(workflowComments({...run,status:'paused',phase:phases[3]},plan)!.state,'stopped')
})

test('发布动态显示稿件和实际确认分P数量，不根据百分比猜当前上传文件', () => {
  const row = {status:'uploading',snapshot:{settings:{title:'旅行'},parts:[{title:'原声'},{title:'配音'}]}}
  assert.match(publicationActivity(row)!.message,/旅行.*2 个分P/)
  assert.doesNotMatch(publicationActivity(row)!.message,/正在上传.*P1|当前.*P2/)
  assert.match(publicationActivity({...row,status:'paused'})!.message,/已暂停/)
  assert.match(publicationActivity({...row,status:'uploaded'})!.message,/等待确认投稿/)
  assert.equal(publicationActivity({...row,status:'published'}),null)
})
