import assert from 'node:assert/strict'
import test from 'node:test'
import { workflowPublication, workflowRunStage, WORKFLOW_ENDS, WORKFLOW_RUN_ENDS, workflowModeLabel } from '../src/utils/workflow.ts'

test('原稿更新只属于单次运行选项，各更新阶段与原新稿状态区分',()=>{
  assert.ok(!WORKFLOW_ENDS.some(item=>item.value==='update'));assert.ok(WORKFLOW_RUN_ENDS.some(item=>item.value==='update'))
  assert.match(workflowModeLabel('update'),/保留原BV/)
  for(const [status,text] of [['uploading','上传替换视频'],['waiting','等待平台处理'],['delivering','同步章节与后续设置'],['unknown','原稿更新结果待核对'],['completed','原稿更新已核验']]){
    const view=workflowPublication({mode:'draft',run:{mode:'update',status:'monitoring',publication:{status}}}, {id:'old',status:'rejected'})
    assert.equal(view.text,text);assert.doesNotMatch(view.text,/尚未开始|等待制作完成|已公开发布/)
  }
})

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

const historicalUpdate = () => ({mode:'publish',run:{id:'previous-workflow',mode:'update',status:'attention',
  publication_id:'same-publication',phase:'旧换片审核退回，后续设置未同步',error:'保留这次中断的原始记录',
  publication:{id:'previous-update',source_publication_id:'same-publication',status:'paused',
    updated_at:'2026-01-01T01:00:00.000000'}}})
const updatedPublication = (patch:any={}) => ({id:'same-publication',status:'published',latest_update:{
  id:'later-update',status:'completed',phase:'最新原稿更新与章节已核验',updated_at:'2026-01-02T01:00:00.000000',...patch}})

test('同一稿件后来单独更新已核验时，共享发布导航显示最新结果并完整保留旧中断记录',()=>{
  const summary=historicalUpdate(),publication=updatedPublication(),original=structuredClone({summary,publication})
  const view=workflowPublication(summary,publication)
  assert.equal(view.text,'原稿更新已核验');assert.equal(view.tone,'success');assert.equal(view.dot,'done')
  assert.equal(view.note,'最新原稿更新与章节已核验');assert.doesNotMatch(view.note,/审核退回/)
  assert.deepEqual({summary,publication},original)
})

test('同稿件较新更新仍在执行或结果未知时，不能沿用之前的完成状态',()=>{
  for(const [status,label] of [['uploading','上传替换视频'],['waiting','等待平台处理'],['delivering','同步章节与后续设置'],['unknown','原稿更新结果待核对'],['failed','原稿更新需处理']]){
    const summary=historicalUpdate();summary.run.status='completed';summary.run.publication.status='completed'
    const view=workflowPublication(summary,updatedPublication({status,phase:`最新 ${status}`}))
    assert.equal(view.text,label,status);assert.notEqual(view.dot,'done',status);assert.equal(view.note,`最新 ${status}`)
  }
})

test('当前自动更新仍在执行、等待平台或有未核实写入时，独立更新的成功不遮盖处理提示',()=>{
  for(const state of ['queued','running','stopping','monitoring']){
    const summary=historicalUpdate();summary.run.status=state;summary.run.publication.status='uploading'
    assert.equal(workflowPublication(summary,updatedPublication()).text,'上传替换视频',state)
  }
  for(const patch of [
    {status:'unknown'}, {status:'waiting'}, {error_code:'PUBLISH_WRITE_UNKNOWN'},
    {execution:{edit_unverified:true}}, {execution:{outcome:'unknown'}},
    {execution:{stages:{edit:{status:'unknown'}}}},
    {execution:{stages:[{key:'chapters',status:'failed',error_code:'PUBLISH_CHAPTER_UNKNOWN'}]}},
    {execution:{delivery:{parts:[{status:'committing'}]}}},
    {execution:{delivery:{collection:{status:'unknown'}}}},
  ]){
    const summary=historicalUpdate();Object.assign(summary.run.publication,patch)
    assert.notEqual(workflowPublication(summary,updatedPublication()).dot,'done',JSON.stringify(patch))
  }
})

test('其他稿件、身份冲突、旧更新及缺少时间证据的成功不盖过原流程异常',()=>{
  const summary=historicalUpdate()
  const mismatched={...updatedPublication(),id:'different-publication'}
  assert.equal(workflowPublication(summary,mismatched).text,'原稿更新已暂停')
  for(const patch of [
    {source_publication_id:'different-publication'}, {updated_at:'2025-12-31T23:59:59Z'},
    {updated_at:'2026-01-01T01:00:00Z'}, {updated_at:undefined}, {updated_at:'invalid'},
    {status:'prepared'},
  ]) assert.equal(workflowPublication(summary,updatedPublication(patch)).text,'原稿更新已暂停',JSON.stringify(patch))
  delete (summary.run.publication as any).updated_at
  assert.equal(workflowPublication(summary,updatedPublication()).text,'原稿更新已暂停')
})

test('同一更新的较新投影可收尾，较旧投影与异源任务不会倒退当前状态',()=>{
  const summary=historicalUpdate()
  assert.equal(workflowPublication(summary,updatedPublication({id:'previous-update'})).text,'原稿更新已核验')
  assert.equal(workflowPublication(summary,updatedPublication({id:'previous-update',updated_at:'2025-12-31T23:59:59Z'})).text,'原稿更新已暂停')
  assert.equal(workflowPublication(summary,updatedPublication({id:'previous-update',updated_at:'2026-01-01T01:00:00Z'})).text,'原稿更新已暂停')
  summary.run.publication.source_publication_id='conflicting-publication'
  assert.equal(workflowPublication(summary,updatedPublication()).text,'原稿更新已暂停')
})

test('数据库无时区 UTC 与带时区投影混用时，按绝对时间比较而非浏览器本地时间',()=>{
  const summary=historicalUpdate();summary.run.publication.updated_at='2026-01-02T01:00:00Z'
  assert.equal(workflowPublication(summary,updatedPublication({updated_at:'2026-01-02T02:00:00.123456'})).text,'原稿更新已核验')
  assert.equal(workflowPublication(summary,updatedPublication({updated_at:'2026-01-02T00:00:00.123456'})).text,'原稿更新已暂停')
  summary.run.publication.updated_at='2026-01-02T07:00:00+08:00'
  assert.equal(workflowPublication(summary,updatedPublication({updated_at:'2026-01-02T00:00:00.123456'})).text,'原稿更新已核验')
})

test('普通投稿流程绑定同一稿件时，也能展示后来独立原稿更新的真实进展',()=>{
  const summary={mode:'publish',run:{mode:'publish',status:'completed',publication:{id:'same-publication',status:'published'}}}
  assert.equal(workflowPublication(summary,updatedPublication({status:'delivering'})).text,'同步章节与后续设置')
  assert.equal(workflowPublication(summary,updatedPublication({status:'unknown'})).text,'原稿更新结果待核对')
  assert.equal(workflowPublication(summary,updatedPublication()).text,'原稿更新已核验')
  assert.equal(workflowPublication(summary,{...updatedPublication({status:'unknown'}),id:'other-publication'}).text,'已公开发布')
})

test('正在执行的普通投稿保持本次上传与提交阶段，不被同稿件历史更新成功覆盖',()=>{
  const summary={mode:'publish',run:{mode:'publish',status:'running',publication:{id:'same-publication',status:'uploading'}}}
  assert.equal(workflowPublication(summary,updatedPublication()).text,'上传文件中')
  summary.run.publication.status='uploaded'
  assert.equal(workflowPublication(summary,updatedPublication()).text,'上传完成 · 准备投稿')
  summary.run.publication.status='submitting'
  assert.equal(workflowPublication(summary,updatedPublication()).text,'正在提交稿件')
})
