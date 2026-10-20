import assert from 'node:assert/strict'
import test from 'node:test'
import { batchStage, chronologicalItems, moveReleaseItem, releaseScopeKey, releasePublicationAction, releaseActionLabel, releaseVersionSelection, releaseProductionSummary, releaseBatchControls, releaseBatchLockMessage, releaseItemStage, releaseCurrentProgress } from '../src/utils/releaseBatch.ts'

test('按原视频日期稳定排序，未知日期需手动确认，不能当成导入时间', () => {
  const rows = [
    {project_uid:'unknown',title:'日期未知'}, {project_uid:'later',title:'较晚',source_date:'2026-05-02'},
    {project_uid:'same-a',title:'同日一',source_date:'2026-05-01'}, {project_uid:'same-b',title:'同日二',source_date:'2026-05-01'},
  ]
  assert.deepEqual(chronologicalItems(rows).map(x=>x.project_uid),['same-a','same-b','later','unknown'])
  assert.deepEqual(chronologicalItems(rows,true).map(x=>x.project_uid),['later','same-a','same-b','unknown'])
  assert.equal(rows[0].project_uid,'unknown')
})
test('手动移位保持每个项目唯一且不越界，顺序变化使原确认失效', () => {
  const rows = [{project_uid:'a',title:'一'},{project_uid:'b',title:'二'},{project_uid:'c',title:'三'}]
  const next = moveReleaseItem(rows,1,-1), scope = {account_id:'1',season_id:2,section_id:3}
  assert.deepEqual(next.map(x=>x.project_uid),['b','a','c'])
  assert.equal(moveReleaseItem(rows,0,-1),rows)
  assert.notEqual(releaseScopeKey(rows,scope,true),releaseScopeKey(next,scope,true))
  assert.notEqual(releaseScopeKey(rows,scope,true),releaseScopeKey(rows,scope,false))
})
test('审核等待与顺序完成明确区分，停止不会冒充完成', () => {
  assert.match(batchStage('monitoring').label,/等待审核/)
  assert.equal(batchStage('done').tone,'success')
  assert.equal(batchStage('stopped').tone,'warning')
  assert.equal(batchStage('failed').tone,'error')
  assert.equal(releaseBatchControls('failed').resume,true)
})
test('默认发布版本沿用项目选择，明确空清单不扩大发布范围',()=>{
  const versions=[{id:'a',name:'原声',dubbing:false,burn_subtitles:false,include_comments:false},{id:'b',name:'配音',dubbing:true,burn_subtitles:true,include_comments:false}]
  const row={project_uid:'p',title:'项目',versions}
  assert.deepEqual(releaseVersionSelection(row),['a','b'])
  assert.deepEqual(releaseVersionSelection({...row,selected_version_ids:['b']}),['b'])
  assert.deepEqual(releaseVersionSelection({...row,selected_version_ids:[]}),[])
})
test('选择已有BV只核对排序，草稿准备新成片，上传清单只能继续',()=>{
  const row={project_uid:'p',title:'项目',publication_choices:[{id:'bv',title:'旧稿',status:'rejected',bvid:'BV123'},{id:'draft',title:'修订',status:'draft'},{id:'uploaded',title:'待投稿',status:'uploaded'}]}
  assert.equal(releasePublicationAction(row,'bv'),'reuse_existing')
  assert.equal(releasePublicationAction(row,'draft'),'prepare')
  assert.equal(releasePublicationAction(row,'uploaded'),'continue')
  assert.match(releaseActionLabel('reuse_existing'),/保留原BV/)
})
test('版本与稿件处理方式都是确认范围的一部分',()=>{
  const rows=[{project_uid:'a',title:'项目'}],scope={account_id:'1',season_id:2,section_id:3}
  const initial=releaseScopeKey(rows,scope,true,{a:'draft'},{a:['one']},{a:'prepare'})
  assert.notEqual(initial,releaseScopeKey(rows,scope,true,{a:'draft'},{a:['two']},{a:'prepare'}))
  assert.notEqual(initial,releaseScopeKey(rows,scope,true,{a:'draft'},{a:['one']},{a:'continue'}))
})
test('紧凑计划使用实际执行和复用清单，显示断点且不重复计算已完成步骤',()=>{
  const production={start_label:'语义整理',execute_steps:['split','translate','tts','assemble'],reusable_steps:['download','asr'],rows:[
    {key:'download',label:'下载视频',action:'reuse'},{key:'asr',label:'语音识别',action:'reuse'},{key:'split',label:'语义整理',action:'retry'}]}
  assert.deepEqual(releaseProductionSummary({production}),{start:'语义整理',execute:4,reuse:2,hasProduction:true})
  assert.deepEqual(releaseProductionSummary({plan:{recovery:{rows:[{label:'语义整理',action:'retry'},{label:'下载',action:'reuse'}]}}}),{start:'语义整理',execute:1,reuse:1,hasProduction:true})
  assert.deepEqual(releaseProductionSummary({production:{rows:[{label:'下载',action:'reuse'}],execute_steps:[],reusable_steps:['download']}}),{start:'制作已就绪',execute:0,reuse:1,hasProduction:true})
  assert.equal(releaseProductionSummary({production:{start_step:'split',rows:[{key:'split',action:'retry'}]}}).start,'继续制作')
})
test('批次操作区分等待本项暂停、停止与断点继续，过渡状态不可重复发动作',()=>{
  assert.equal(releaseBatchControls('running').pause,true);assert.equal(releaseBatchControls('running').stop,true)
  assert.equal(releaseBatchControls('pausing').pause,false);assert.equal(releaseBatchControls('pausing').stop,true)
  assert.match(releaseBatchControls('pausing').hint,/本项目仍在执行/)
  assert.equal(releaseBatchControls('stopping').stop,false);assert.equal(releaseBatchControls('stopping').resume,false)
  assert.match(releaseBatchControls('stopping').hint,/等待已发送操作的回执/)
  assert.equal(releaseBatchControls('paused').resume,true);assert.equal(releaseBatchControls('done').resume,false)
  assert.equal(releaseItemStage('done').label,'已完成')
})
test('批次锁定提示区分排队、当前处理和收尾状态',()=>{
  const queued=releaseBatchLockMessage({id:'b',status:'running',position:3,total:7,phase:'等待前项完成'})
  assert.match(queued,/第 3\/7 项/);assert.match(queued,/等待前项完成/);assert.match(queued,/不能单独运行步骤、自动流程或修改制作资料/)
  const current=releaseBatchLockMessage({id:'b',status:'pausing',position:2,total:7,phase:'第2项：上传并等待审核'})
  assert.match(current,/本项完成后暂停/);assert.match(current,/上传并等待审核/)
  assert.equal(releaseBatchLockMessage({id:'b',status:'paused',position:2,total:7}), '')
})
test('当前阶段缺少真实进度不填0%，估算明确保留标记，结束无当前项',()=>{
  assert.equal(releaseCurrentProgress(null),null)
  assert.equal(releaseCurrentProgress({stage:'review',indeterminate:true,percentage:87}).percentage,null)
  assert.equal(releaseCurrentProgress({stage:'upload',percentage:null}).percentage,null)
  assert.equal(releaseCurrentProgress({stage:'production',percentage:0}).percentage,0)
  assert.equal(releaseCurrentProgress({stage:'production',percentage:NaN}).percentage,null)
  const progress=releaseCurrentProgress({position:1,completed_steps:3,total_steps:7,percentage:42.7,estimated:true})
  assert.equal(progress.completedSteps,3);assert.equal(progress.totalSteps,7);assert.equal(progress.percentage,42.7);assert.equal(progress.estimated,true)
  const ordering=releaseCurrentProgress({position:null,stage:'ordering',indeterminate:true})
  assert.equal(ordering.position,null);assert.equal(ordering.percentage,null)
})


test('顺序制作摘要不把需要更新输入后复核的步骤提前算作直接复用',()=>{
 const production={rows:[{key:'translate',label:'翻译',action:'reuse'},{key:'assemble',label:'合成',action:'update'}],execute_steps:['assemble'],reusable_steps:['translate']}
 const summary=releaseProductionSummary({production,plan:{pending_production_steps:['translate','assemble']}})
 assert.equal(summary.execute,2);assert.equal(summary.reuse,0);assert.equal(summary.start,'翻译')
})
