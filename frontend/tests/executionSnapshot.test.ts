import assert from 'node:assert/strict'
import test from 'node:test'
import { createExecutionProjection, hasActiveExecution } from '../src/utils/executionSnapshot.ts'

const running = (job = 8, completed = 2) => ({key:'tts', status:'running', job_id:job, progress:completed,
  execution:{schema:1, job_id:job, state:'running', completed, total:20}, eta_seconds:12, progress_phase:'正在配音'})
const ended = (job = 8) => ({...running(job,20), status:'done', execution:{schema:1, job_id:job,state:'completed',completed:20,total:20},
  eta_seconds:0, progress_phase:'', progress_indeterminate:false, progress_estimated:false})

test('同Job终态立即进入中性核验，慢full和晚到running不能复活，真实full结果解除',()=>{
  for(const status of ['done','stale','failed']) {
    const projection=createExecutionProjection(),initial=[{...running(),fresh:true,selected:false,cfg:{voice:'保存值'},artifacts:[1]}]
    const before=projection.revision,terminal=projection.apply(initial,[ended()])
    assert.equal(terminal.steps[0].status,'checking');assert.equal(terminal.steps[0].fresh,null)
    assert.equal(terminal.steps[0].selected,false);assert.deepEqual(terminal.steps[0].artifacts,[1]);assert.deepEqual(terminal.steps[0].cfg,{voice:'保存值'})
    assert.equal(hasActiveExecution(terminal.steps),true)
    for(const revision of [before,projection.revision]) {
      const late=projection.mergeFull([{...running(),fresh:true}],revision)
      assert.equal(late[0].status,'checking');assert.equal(late[0].fresh,null)
    }
    assert.equal(projection.apply(terminal.steps,[running()]).steps[0].status,'checking')
    const checked=projection.mergeFull([{...ended(),status,job_id:null,fresh:status==='done',error:status==='failed'?{message:'实际核验失败'}:null}],projection.revision)
    assert.equal(checked[0].status,status);assert.equal(checked[0].fresh,status==='done')
    assert.equal(projection.apply(checked,[running()]).steps[0].status,status)
  }
})

test('核验投影只认同Job真实终态，旧Job和暂停不猜结束，新queued仍接纳',()=>{
  for(const snapshot of [ended(7),{...ended(),execution:{...ended().execution,state:'paused'}},
    {...ended(),execution:{...ended().execution,owner:'workflow'}},{...ended(),execution:{schema:1,state:'completed'}}]) {
    const projection=createExecutionProjection()
    assert.equal(projection.apply([running()],[snapshot]).steps[0].status,'running')
  }
  const projection=createExecutionProjection(),terminal=projection.apply([running()],[ended()])
  const next=projection.apply(terminal.steps,[{...running(9,0),status:'queued'}])
  assert.equal(next.steps[0].status,'queued');assert.equal(next.steps[0].job_id,9)
  const old=projection.apply(next.steps,[ended(8)])
  assert.equal(old.steps[0].status,'queued');assert.equal(old.steps[0].job_id,9)
})

test('更低Job的完整失败不能冒充当前终态或借旧错误解除核验',()=>{
  const projection=createExecutionProjection(),before=projection.revision
  const terminal=projection.apply([running()],[ended()])
  for(const revision of [before,projection.revision]) {
    const old=projection.mergeFull([{...ended(7),status:'failed',fresh:false,error:{message:'仅旧Job7失败'}}],revision)[0]
    assert.equal(old.status,'checking');assert.equal(old.fresh,null);assert.equal(old.freshness_known,false)
    assert.equal(old.job_id,8);assert.equal(old.execution.job_id,8);assert.equal(old.error,null)
  }
  const currentError={message:'当前Job8核验失败'}
  const checked=projection.mergeFull([{...ended(),status:'failed',job_id:null,fresh:false,error:currentError}],projection.revision)[0]
  assert.equal(checked.status,'failed');assert.deepEqual(checked.error,currentError)
  assert.equal(projection.apply(terminal.steps,[running(9)]).steps[0].job_id,9)
})

test('终态前开始的无身份旧full不能借新终态宣称最新，之后的新legacy读取仍可解除',()=>{
  for(const status of ['done','failed','stale']) {
    const projection=createExecutionProjection(),before=projection.revision
    projection.apply([running()],[ended()])
    const old=projection.mergeFull([{key:'tts',status,job_id:null,execution:null,fresh:status==='done',error:{message:'无身份旧读'},cfg:{voice:'完整配置'}}],before)[0]
    assert.equal(old.status,'checking');assert.equal(old.fresh,null);assert.equal(old.error,null)
    assert.equal(old.execution.job_id,8);assert.deepEqual(old.cfg,{voice:'完整配置'})
    const checked=projection.mergeFull([{key:'tts',status:'done',job_id:null,execution:null,fresh:true}],projection.revision)[0]
    assert.equal(checked.status,'done');assert.equal(checked.fresh,true);assert.equal(checked.execution,null)
  }
  const projection=createExecutionProjection(),before=projection.revision
  projection.apply([running()],[ended()])
  const actual=projection.mergeFull([{...ended(),job_id:null,fresh:true}],before)[0]
  assert.equal(actual.status,'done');assert.equal(actual.fresh,true,'真实full的execution同Job身份足够，不误阻断顶层空JobID')
})

test('核验时未采样活动而直接看到新Job终态，也必须换代且拒绝旧full冒绿',()=>{
  for(const status of ['done','stale','failed']) {
    const projection=createExecutionProjection(),old=projection.apply([running()],[ended()])
    const terminal9=status==='failed'?{...ended(9),status:'failed',execution:{...ended(9).execution,state:'failed'}}:ended(9)
    const readRevision=projection.revision,next=projection.apply(old.steps,[terminal9])
    assert.equal(next.needsRefresh,true);assert.ok(projection.revision>readRevision)
    assert.equal(next.steps[0].job_id,9);assert.equal(next.steps[0].execution.job_id,9)
    assert.equal(next.steps[0].status,'checking');assert.equal(next.steps[0].fresh,null)
    const late=projection.mergeFull([{...ended(),fresh:true}],readRevision)[0]
    assert.equal(late.job_id,9);assert.equal(late.status,'checking');assert.equal(late.fresh,null)
    const revision=projection.revision,repeated=projection.apply(next.steps,[terminal9])
    assert.equal(repeated.needsRefresh,false);assert.equal(projection.revision,revision)
    assert.equal(projection.apply(repeated.steps,[running(9)]).steps[0].status,'checking')
    const full=projection.mergeFull([{...ended(9),job_id:null,status,fresh:status==='done',error:status==='failed'?'Job9真实失败':null}],projection.revision)[0]
    assert.equal(full.status,status);assert.equal(full.fresh,status==='done');assert.equal(full.execution.job_id,9)
    if(status==='failed')assert.equal(full.error,'Job9真实失败')
  }
  const projection=createExecutionProjection(),checked={...ended(),fresh:true}
  const untouched=projection.apply([checked],[ended(9)])
  assert.equal(untouched.steps[0],checked,'不泛化其他非活动状态，只沿原合同请求完整核验')
  assert.equal(untouched.needsRefresh,true)
})

test('轻量执行只更新运行字段，不覆盖选择、配置、产物和已核对有效性', () => {
  const projection = createExecutionProjection()
  const current = [{...running(), selected:false, fresh:false, artifacts:[{id:1}], cfg:{voice:'本人'}, inputs:['保持']}]
  const result = projection.apply(current, [{...running(8,7), selected:true, fresh:true, artifacts:[], cfg:{voice:'别的'}, inputs:[]}])
  assert.equal(result.needsRefresh,false)
  assert.equal(result.steps[0].execution.completed,7)
  assert.equal(result.steps[0].selected,false); assert.equal(result.steps[0].fresh,false)
  assert.deepEqual(result.steps[0].cfg,{voice:'本人'}); assert.deepEqual(result.steps[0].artifacts,[{id:1}])
  assert.deepEqual(result.steps[0].inputs,['保持'])
})

test('终态只结束实时展示，完整核对完成前不把原始 done 提升为有效完成', () => {
  const projection = createExecutionProjection(), current = [{...running(),fresh:false}]
  const result = projection.apply(current,[ended()])
  assert.equal(result.needsRefresh,true)
  assert.equal(result.steps[0].status,'checking'); assert.equal(result.steps[0].fresh,null)
  assert.equal(result.steps[0].execution.state,'completed'); assert.equal(result.steps[0].eta_seconds,0)
  assert.equal(projection.apply(result.steps,[ended()]).needsRefresh,false,'同一终态不反复发起昂贵核对')
  const full = projection.mergeFull([{...ended(),job_id:null,fresh:true}],projection.revision)
  assert.equal(full[0].status,'done'); assert.equal(full[0].fresh,true)
})

test('新发起的完整终态读取即使缺少旧执行记录也能解除运行投影，迟到旧任务不能复活', () => {
  const projection = createExecutionProjection()
  projection.apply([running()],[running(8,4)])
  const checked = projection.mergeFull([{key:'tts',status:'done',job_id:null,execution:null,fresh:true}],projection.revision)
  assert.equal(checked[0].status,'done'); assert.equal(checked[0].fresh,true)
  assert.equal(checked[0].execution,null)
  const late = projection.apply(checked,[running(8,19)])
  assert.equal(late.steps[0].status,'done');assert.equal(late.steps[0].execution,null)
  assert.equal(late.needsRefresh,false)
  const newer = projection.apply(checked,[running(9,0)])
  assert.equal(newer.steps[0].job_id,9);assert.equal(newer.steps[0].status,'running')
})

test('完整慢请求不能覆盖读取期间到达的实时进度，仍保留完整接口的新配置', () => {
  const projection = createExecutionProjection(), revision = projection.revision
  projection.apply([running()],[running(8,9)])
  const merged = projection.mergeFull([{...running(8,1),selected:true,cfg:{voice:'最新设置'},artifacts:[1]}],revision)
  assert.equal(merged[0].execution.completed,9)
  assert.deepEqual(merged[0].cfg,{voice:'最新设置'}); assert.deepEqual(merged[0].artifacts,[1])
})

test('新任务替换只请求一次完整详情，旧任务响应和旧完整快照不能复活', () => {
  const projection = createExecutionProjection(), revision = projection.revision
  const next = projection.apply([running(8)],[running(9,0)])
  assert.equal(next.needsRefresh,true)
  assert.equal(projection.apply(next.steps,[running(9,1)]).needsRefresh,false)
  const rejected = projection.apply(next.steps,[running(8,19)])
  assert.equal(rejected.steps[0].job_id,9)
  assert.equal(projection.mergeFull([running(8,19)],revision)[0].job_id,9)
  assert.equal(projection.mergeFull([running(8,19)],projection.revision)[0].job_id,9)
})

test('新鲜度核对为 stale 的同一任务不被迟到 running 响应复活', () => {
  const projection = createExecutionProjection()
  const current = [{...ended(),status:'stale',job_id:null,fresh:false}]
  assert.equal(projection.apply(current,[running()]).steps[0].status,'stale')
})

test('较新的原始完成记录不能借用旧任务已校验的完成标记和产物身份', () => {
  const projection = createExecutionProjection()
  const current = [{...ended(8),job_id:null,fresh:true,artifacts:[{job_id:8}]}]
  const result = projection.apply(current,[ended(9)])
  assert.equal(result.needsRefresh,true)
  assert.equal(result.steps[0].execution.job_id,8)
  assert.equal(result.steps[0].job_id,null)
  assert.deepEqual(result.steps[0].artifacts,[{job_id:8}])
})

test('已看到终态的任务拒绝旧 running，即使完整核对还在等待', () => {
  const projection = createExecutionProjection()
  const terminal = projection.apply([running()],[ended()])
  const stale = projection.apply(terminal.steps,[running(8,5)])
  assert.equal(stale.steps[0].execution.state,'completed')
  assert.equal(stale.needsRefresh,false)
})

test('同任务活动时间较旧的响应不能倒退进度，但新阶段允许真实计数归零', () => {
  const projection = createExecutionProjection()
  const current = [{...running(8,8),execution:{...running(8,8).execution,updated_at:'2026-10-20T12:00:02Z'}}]
  const old = {...running(8,3),execution:{...running(8,3).execution,updated_at:'2026-10-20T12:00:01Z'}}
  assert.equal(projection.apply(current,[old]).steps[0].execution.completed,8)
  const next = {...running(8,0),execution:{...running(8,0).execution,updated_at:'2026-10-20T12:00:03Z',phase:'拼接'}}
  assert.equal(projection.apply(current,[next]).steps[0].execution.completed,0)
})

test('轻量响应缺失步骤时只请求一次完整核对，不创建缺少配置的占位项', () => {
  const projection = createExecutionProjection()
  assert.deepEqual(projection.apply([],[running()]),{steps:[],needsRefresh:true})
  assert.equal(projection.apply([],[running()]).needsRefresh,false)
  projection.reset(); assert.equal(projection.apply([],[running()]).needsRefresh,true)
})

test('完整接口确认后能接纳更高任务代次，重置不会保留其他项目的终态屏障', () => {
  const projection = createExecutionProjection()
  projection.mergeFull([{...ended(),job_id:null}],projection.revision)
  assert.equal(projection.apply([ended()],[running(9)]).steps[0].job_id,9)
  projection.reset()
  assert.equal(projection.apply([{key:'tts',status:'pending'}],[running(1)]).steps[0].job_id,1)
  assert.equal(hasActiveExecution([running()]),true); assert.equal(hasActiveExecution([ended()]),false)
})
