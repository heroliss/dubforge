import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { currentGenerationSteps, mergeProjectSummary, mergeVerifiedProjects } from '../src/utils/projectListProjection.ts'
import { projectFreshnessKnown } from '../src/utils/projectListProjection.ts'
import { projectWorkflowStages } from '../src/utils/projectWorkflowStages.ts'
import { WORKFLOW_STAGES } from '../src/utils/workflowStages.ts'
import { createExecutionProjection } from '../src/utils/executionSnapshot.ts'

const full = () => ({uid:'a',status:'done',summary_revision:'same',freshness_known:true,dir_exists:true,
  params:{auto_steps:['tts']},last_verified_at:10,steps:[{key:'tts',job_id:null,execution:{job_id:7},status:'done',fresh:true}],
  comments_workspace:{state:'done',label:'方案已确认'}})
const summary = (patch:any={}) => ({uid:'a',status:'checking',summary_revision:'same',projection:'summary',freshness_known:false,
  dir_exists:null,comments_workspace:null,steps:[{key:'tts',job_id:7,status:'done',fresh:null}],...patch})

test('同代次终态可用full execution身份保留已核验结果；摘要不把未知false冒充freshness',()=>{
  const prior=full(),next=mergeProjectSummary(summary(),prior)
  assert.equal(next.status,'done');assert.equal(next.steps[0].fresh,true);assert.equal(next.last_verified_at,10)
  assert.equal(next.dir_exists,true);assert.equal(prior.steps[0].job_id,null)
  assert.equal(mergeProjectSummary(summary(),undefined).freshness_known,false)
  assert.equal(mergeProjectSummary(summary({summary_revision:'new'}),prior).status,'checking')
})

test('修订相同仍防御新Job或活动边界，不借上次fresh/评论方案/目录结论',()=>{
  for(const steps of [[{key:'tts',job_id:8,status:'queued',fresh:null}],[{key:'tts',job_id:7,status:'running',fresh:null}]]){
    const next=mergeProjectSummary(summary({status:'processing',steps}),full())
    assert.equal(next.status,'processing');assert.equal(next.freshness_known,false)
    assert.equal(next.steps[0].fresh,null);assert.equal(next.comments_workspace,null);assert.equal(next.dir_exists,null)
  }
})

test('终态后的summary只保留核验，不以同代次done或旧Job解除，冷摘要仍允许未知规划',()=>{
  const projection=createExecutionProjection(),active={key:'tts',status:'running',job_id:7,fresh:false}
  const terminal={...active,status:'done',execution:{schema:1,owner:'step',job_id:7,state:'completed'}}
  const prior={...full(),steps:projection.apply([active],[terminal]).steps}
  for(const revision of ['same','new']) {
    const row=mergeProjectSummary(summary({summary_revision:revision,steps:[{key:'tts',job_id:7,job_status:'done',status:'done',fresh:null,execution:null}]}),prior)
    assert.equal(row.steps[0].status,'checking');assert.equal(row.steps[0].fresh,null)
    assert.equal(row.steps[0].execution?.job_id,7);assert.equal(row.freshness_known,false)
  }
  const old=mergeProjectSummary(summary({steps:[{key:'tts',job_id:6,status:'done',fresh:null,execution:null}]}),prior)
  assert.equal(old.steps[0].job_id,7);assert.equal(old.steps[0].status,'checking')
  const queued=mergeProjectSummary(summary({steps:[{key:'tts',job_id:8,status:'queued',fresh:null}]}),prior)
  assert.equal(queued.steps[0].status,'queued');assert.equal(queued.steps[0].job_id,8)
  const cold=mergeProjectSummary(summary(),undefined)
  assert.equal(cold.steps[0].status,'done');assert.equal(cold.freshness_known,false)
})

test('新terminal摘要接替旧核验身份但不借旧执行记录，完整旧代次不能冒绿',()=>{
  const prior={...full(),freshness_known:false,steps:[{key:'tts',job_id:7,status:'checking',fresh:null,execution:{schema:1,job_id:7,state:'completed'}}]}
  const row=mergeProjectSummary(summary({steps:[{key:'tts',job_id:8,status:'done',job_status:'done',fresh:null,execution:null}]}),prior)
  assert.equal(row.steps[0].job_id,8);assert.equal(row.steps[0].status,'checking');assert.equal(row.steps[0].execution,null)
  for(const incoming of [{key:'tts',job_id:7,status:'done',fresh:true},{key:'tts',job_id:8,status:'running',fresh:false}]) {
    const kept=mergeVerifiedProjects([{...full(),steps:[incoming]}],[row],new Map([['a','same']]),false,99)[0]
    assert.equal(kept,row);assert.equal(kept.steps[0].fresh,null)
  }
  for(const status of ['done','stale','failed']) {
    const accepted=mergeVerifiedProjects([{...full(),steps:[{key:'tts',job_id:null,execution:{job_id:8},status,fresh:status==='done'}]}],[row],new Map([['a','same']]),false,99)[0]
    assert.equal(accepted.steps[0].status,status);assert.equal(accepted.steps[0].fresh,status==='done')
  }
})

test('摘要跳过名称查询时同修订同账号合集小节保留已核验显示名，换scope或修订清空',()=>{
  const known={known:true,account_id:'account-a',season_id:12,section_id:34,season_title:'科学合集',section_title:'访谈'}
  const unknown={...known,season_id:'12',section_id:'34',season_title:'',section_title:''}
  const prior={...full(),project_settings:known}
  const same=mergeProjectSummary(summary({project_settings:unknown}),prior)
  assert.equal(same.project_settings.season_title,'科学合集');assert.equal(same.project_settings.section_title,'访谈')
  for(const patch of [{account_id:'other'},{season_id:99},{section_id:88},{known:false}]){
    const changed=mergeProjectSummary(summary({project_settings:{...unknown,...patch}}),prior)
    assert.equal(changed.project_settings.season_title,'');assert.equal(changed.project_settings.section_title,'')
  }
  const revision=mergeProjectSummary(summary({summary_revision:'new',project_settings:unknown}),prior)
  assert.equal(revision.project_settings.season_title,'');assert.equal(revision.project_settings.section_title,'')
  assert.equal(known.season_title,'科学合集');assert.equal(unknown.season_title,'','不修改来源对象')
})

test('新内存执行投影不继承旧Job的产物有效性或变化解释',()=>{
  const prior=[{key:'tts',job_id:7,status:'done',fresh:true}]
  const newer={key:'tts',job_id:8,status:'queued',fresh:true,freshness_label:'已是最新'}
  const result=currentGenerationSteps(prior,[newer])
  assert.equal(result.changed,true);assert.equal(result.steps[0].fresh,null);assert.equal(result.steps[0].freshness_label,'')
  assert.equal(newer.fresh,true,'不修改快照输入')
  assert.equal(currentGenerationSteps(prior,[prior[0]]).changed,false)
})

test('完整核验仅更新未换修订成员，保留最新成员且不改变源数组',()=>{
  const a=summary({summary_revision:'new'}),b=summary({uid:'b',summary_revision:'b'})
  const rows=[full()],current=[a,b]
  const merged=mergeVerifiedProjects(rows,current,new Map([['a','old']]),true,99)
  assert.deepEqual(merged,[a,b]);assert.equal(rows[0].last_verified_at,10)
  const accepted=mergeVerifiedProjects(rows,[summary()],new Map([['a','same']]),false,99)
  assert.equal(accepted[0].freshness_known,true);assert.equal(accepted[0].last_verified_at,99)
})

test('真实阶段说明挂载后由待核对切到完整结果，再保留上次核验；新Job回到未知而不显示零产物',async()=>{
  const source=await readFile(new URL('../src/views/ProjectsView.vue',import.meta.url),'utf8')
  const branch=source.match(/<div class="stage-summary">[\s\S]*?<\/div>/)?.[0]
  assert.ok(branch,'挂载真实卡片阶段说明，不复制模板')
  const render=new Function('Vue',compile(branch,{mode:'function',prefixIdentifiers:true}).code)(Vue)
  type Node={text:string;children:Node[];parent:Node|null}
  const host=(text=''):Node=>({text,children:[],parent:null})
  const remove=(node:Node)=>{if(node.parent)node.parent.children.splice(node.parent.children.indexOf(node),1);node.parent=null}
  const renderer=Vue.createRenderer<Node,Node>({createElement:()=>host(),createText:host,createComment:()=>host(),remove,
    insert(node,parent,anchor=null){remove(node);const at=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(at<0?parent.children.length:at,0,node);node.parent=parent},
    setText(node,text){node.text=text},setElementText(node,text){node.children=[];node.text=text},patchProp(){},
    parentNode:node=>node.parent,nextSibling:node=>node.parent?.children[node.parent.children.indexOf(node)+1]||null})
  const p=Vue.ref(mergeProjectSummary(summary(),null)),root=host()
  const app=renderer.createApp({setup:()=>({p,WORKFLOW_STAGES,projectFreshnessKnown,completedStages:(project:any)=>projectWorkflowStages(project).filter(stage=>stage.complete).length}),render})
  const text=(node:Node):string=>node.text+node.children.map(text).join('')
  app.mount(root)
  try{
    assert.match(text(root),/产物待核对/);assert.doesNotMatch(text(root),/已完成 0/)
    p.value=mergeVerifiedProjects([full()],[p.value],new Map([['a','same']]),false,99)[0];await Vue.nextTick()
    assert.match(text(root),/已完成 2 个/);assert.doesNotMatch(text(root),/上次核验/)
    p.value=mergeProjectSummary(summary(),p.value);await Vue.nextTick()
    assert.match(text(root),/上次核验 · 已完成 2 个/)
    p.value=mergeProjectSummary(summary({summary_revision:'new',status:'processing',steps:[{key:'tts',job_id:8,status:'queued',fresh:null}]}),p.value);await Vue.nextTick()
    assert.match(text(root),/产物待核对/);assert.doesNotMatch(text(root),/已完成/)
  }finally{app.unmount()}
})
