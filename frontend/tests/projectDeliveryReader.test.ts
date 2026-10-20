import assert from 'node:assert/strict'
import test from 'node:test'
import { createProjectDeliveryReader, projectDeliveryReadKey } from '../src/utils/projectDeliveryReader.ts'
import { projectCurrentDelivery, projectCurrentPublication, projectDeliverySummary } from '../src/utils/projectCard.ts'

const entry = (uid='one', patch:any={}) => ({ uid, workflow_status:'attention', steps:[], publication:{id:`publication-${uid}`,status:'published',
  execution:{schema:1,state:'completed',label:'原投稿过程'},latest_update:{id:`update-${uid}`,status:'completed',updated_at:'2026-01-02T00:00:00'}},...patch })
const summary = (uid='one', patch:any={}) => ({run:{id:`workflow-${uid}`,mode:'update',status:'attention',publication_id:`publication-${uid}`,
  publication:{id:`old-update-${uid}`,source_publication_id:`publication-${uid}`,status:'paused',updated_at:'2026-01-01T00:00:00'},...patch}})
const deferred = () => { let resolve:any, reject:any; const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject} }
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()}

test('补读只覆盖摘要不足的历史更新，首屏不等待且最多两个并发',async()=>{
  const requests:any[]=[],reader=createProjectDeliveryReader((uid,signal)=>{
    const pending=deferred();requests.push({uid,signal,...pending});return pending.promise
  })
  const rows=[entry('one'),entry('two'),entry('three'),entry('active',{workflow_status:'running'}),{uid:'empty',workflow_status:'attention'},
    entry('inline',{workflow_summary:summary('inline')})]
  assert.equal(reader.sync(rows,'root-a'),undefined)
  assert.equal(reader.get(rows[0]),null)
  await flush();assert.equal(requests.length,2)
  assert.equal(reader.get(rows[5]),rows[5].workflow_summary)
  requests[0].resolve(summary('one'));await flush();assert.equal(requests.length,3)
  assert.equal(reader.get(rows[0]).run.id,'workflow-one')
  requests[1].resolve(summary('two'));requests[2].resolve(summary('three'));await flush()
  reader.sync(rows,'root-a');await flush();assert.equal(requests.length,3,'普通列表刷新不能为同卡片重复补读')
  reader.clear()
})

test('更新状态或时间修订后重读，失败保留原提示，仅明确重试后再次请求',async()=>{
  let calls=0,fail=true
  const reader=createProjectDeliveryReader(async uid=>{calls++;if(fail)throw new Error('读取失败');return summary(uid)})
  const row=entry()
  reader.sync([row],'root-a');await flush();assert.equal(calls,1);assert.equal(reader.get(row),null)
  reader.sync([row],'root-a');await flush();assert.equal(calls,1)
  fail=false;reader.sync([row],'root-a',true);await flush();assert.equal(calls,2);assert.equal(reader.get(row).run.id,'workflow-one')
  const revised={...row,publication:{...row.publication,latest_update:{...row.publication.latest_update,status:'unknown'}}}
  reader.sync([revised],'root-a');await flush();assert.equal(calls,3);assert.equal(reader.get(row),null)
  revised.publication.latest_update.updated_at='2026-01-03T00:00:00'
  reader.sync([revised],'root-a');await flush();assert.equal(calls,4)
  reader.clear()
})

test('目录变化、离页和候选移除取消旧请求，迟到结果不能污染同UID新范围',async()=>{
  const requests:any[]=[],reader=createProjectDeliveryReader((uid,signal)=>{const pending=deferred();requests.push({uid,signal,...pending});return pending.promise})
  const row=entry()
  reader.sync([row],'root-a');await flush()
  reader.sync([row],'root-b');await flush();assert.equal(requests[0].signal.aborted,true);assert.equal(requests.length,2)
  requests[0].resolve(summary('old'));await flush();assert.equal(reader.get(row),null)
  requests[1].resolve(summary());await flush();assert.equal(reader.get(row).run.id,'workflow-one')
  const revised={...row,publication:{...row.publication,latest_update:{...row.publication.latest_update,id:'new-update'}}}
  reader.sync([revised],'root-b');await flush();reader.clear();assert.equal(requests[2].signal.aborted,true)
  requests[2].resolve(summary());await flush();assert.equal(reader.get(revised),null)
})

test('未知与活动流程不会仅凭较新成功更新被隐藏，损坏接口也不能冒充成功',async()=>{
  for(const status of ['queued','running','monitoring','unknown','stopping'])assert.equal(projectDeliveryReadKey(entry('one',{workflow_status:status})),null,status)
  const reader=createProjectDeliveryReader(async()=>({run:{status:'completed'}}))
  reader.sync([entry()],'root');await flush();assert.equal(reader.get(entry()),null)
  assert.equal(projectCurrentDelivery(entry(),summary('one',{status:'running'})),null)
  assert.equal(projectCurrentDelivery(entry(),summary('one',{publication:{...summary().run.publication,status:'unknown'}})),null)
  assert.equal(projectCurrentDelivery(entry(),summary('one',{publication_id:'different-publication'})),null)
  assert.equal(projectCurrentDelivery(entry(),summary('one',{publication:{...summary().run.publication,execution:{edit_unverified:true}}})),null)
  assert.equal(projectCurrentDelivery(entry(),summary())?.text,'原稿更新已核验')
  reader.clear()
})

test('运行中的交付只说明冻结方案，未知字段不被当作已发布或首次投稿',()=>{
  const settings={known:true,delivery_mode:'publish',target_kind:'',target_selection:{source:'history_requires_choice',has_history:true}}
  for(const state of ['running','queued','stopping','monitoring']){
    const view=projectDeliverySummary(settings,state)
    assert.match(view.delivery,/已确认方案/);assert.match(view.target,/无需重新选择/);assert.doesNotMatch(view.target,/已有发布记录|请选择新稿|默认创建/)
  }
  assert.match(projectDeliverySummary(settings).target,/已有交付任务或发布记录/)
  for(const has_history of [undefined,null,'false',0]){
    const view=projectDeliverySummary({...settings,target_selection:{has_history}})
    assert.equal(view.target,'发布目标待设置');assert.doesNotMatch(view.target,/首次发布|已有发布/)
  }
})

test('卡片只收起同记录已完成的原投稿面板，当前更新错误及原稿未知状态保留',()=>{
  const row=entry(),original=structuredClone(row)
  const current=projectCurrentPublication(row)
  assert.equal(current.execution,null);assert.equal(current.latest_update.status,'completed');assert.deepEqual(row,original)
  const failedUpdate={...row,publication:{...row.publication,latest_update:{...row.publication.latest_update,status:'unknown',error:'修改结果待核对'}}}
  assert.equal(projectCurrentPublication(failedUpdate).latest_update.error,'修改结果待核对')
  for(const status of ['unknown','failed','running']){
    const uncertain={...row,publication:{...row.publication,execution:{...row.publication.execution,state:status}}}
    assert.equal(projectCurrentPublication(uncertain),uncertain.publication,status)
  }
  const unrelated={...row,publication:{...row.publication,latest_update:{...row.publication.latest_update,source_publication_id:'other'}}}
  assert.equal(projectCurrentPublication(unrelated),unrelated.publication)
})

test('同修订核验只缓存30秒，到期中性待核对并重新读取，旧任务变unknown不会永久被成功遮盖',async()=>{
  let clock=0
  const requests:any[]=[],reader=createProjectDeliveryReader((uid,signal)=>{const pending=deferred();requests.push({uid,signal,...pending});return pending.promise},()=>{}, {now:()=>clock})
  const row=entry()
  reader.sync([row],'root');await flush();requests[0].resolve(summary());await flush()
  assert.equal(projectCurrentDelivery(row,reader.get(row))?.text,'原稿更新已核验')
  clock=29999;reader.sync([row],'root');await flush();assert.equal(requests.length,1);assert.equal(reader.rechecking(row),false)
  clock=30000;assert.equal(reader.get(row),null);assert.equal(reader.rechecking(row),true,'过期的成功证明不能继续当作当前成功')
  reader.sync([row],'root');await flush();assert.equal(requests.length,2);assert.equal(reader.rechecking(row),true)
  requests[1].resolve(summary('one',{publication:{...summary().run.publication,status:'unknown'}}));await flush()
  assert.equal(reader.rechecking(row),false);assert.equal(projectCurrentDelivery(row,reader.get(row)),null,'当前未知结果恢复原处理提示')
  clock=60000;reader.sync([row],'root');await flush()
  assert.equal(reader.rechecking(row),false,'真正未知的提示在后续核对中不能被中性标签吞掉')
  requests[2].resolve(summary('one',{publication:{...summary().run.publication,status:'unknown'}}));await flush()
  reader.clear()
})

test('显式fresh重新核对已ready的同修订，等待中性显示，失败恢复原提示',async()=>{
  const requests:any[]=[],reader=createProjectDeliveryReader((uid,signal)=>{const pending=deferred();requests.push({uid,signal,...pending});return pending.promise})
  const row=entry()
  reader.sync([row],'root');await flush();requests[0].resolve(summary());await flush()
  reader.sync([row],'root',true);await flush()
  assert.equal(requests.length,2);assert.equal(reader.get(row),null);assert.equal(reader.rechecking(row),true)
  requests[1].reject(new Error('暂时无法核对'));await flush()
  assert.equal(reader.get(row),null);assert.equal(reader.rechecking(row),false)
  reader.clear()
})
