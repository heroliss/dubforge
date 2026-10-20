<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { friendlyError } from '../utils/http'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NCheckbox, NDrawer, NDrawerContent, NProgress, NSpin, NTag, NText } from 'naive-ui'
import { versionSummary } from '../utils/assemblyVersions'
import { WORKFLOW_OPEN, workflowModeLabel, workflowRunStage } from '../utils/workflow'
import { useNavStore, STEP_LABELS, formatEta } from '../stores/nav'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { executionForWorkflow, executionStatusType, normalizeExecution, workflowExecutionStep } from '../utils/executionProgress'
import { executionEtaSeconds } from '../utils/executionEta'
import CommentWorkflowProgress from './CommentWorkflowProgress.vue'
import PublicationRightsHint from './PublicationRightsHint.vue'
import DubbingRunOptions from './DubbingRunOptions.vue'
import DubbingReadiness from './DubbingReadiness.vue'
import RecoveryPlan from './RecoveryPlan.vue'
import ProjectRunPanel from './ProjectRunPanel.vue'
import TtsServiceControl from './TtsServiceControl.vue'
import { commentFailure, commentWorkspaceLink } from '../utils/commentWorkflow'
import { workflowFailureStatus } from '../utils/workflowStatus'
import { publicationChapterScope } from '../utils/publicationChapters'
import PublicationTargetSummary from './PublicationTargetSummary.vue'
import { publicationVisibility } from '../utils/publishing'
import { workflowStageSequence } from '../utils/workflowStages'
import WorkflowUpdateSummary from './WorkflowUpdateSummary.vue'
import { updateProgress as summarizeUpdateProgress, updateStages, updateNeedsReconcile } from '../utils/publicationUpdate'

const props = defineProps<{uid:string;headless?:boolean}>()
const emit = defineEmits<{started: []; 'mode-change': [mode: string]}>()
const nav = useNavStore()
const route=useRoute(), router=useRouter()
const state=ref<any>(null), preview=ref<any>(null), show=ref(false), busy=ref(false), error=ref('')
const checking=ref(false)
const submitting=ref(false)
const requestLabel=computed(()=>selectedRun.value?'正在提交恢复请求':'正在提交执行请求')
const mode=ref('draft'), selectedRun=ref(''), requestedStart=ref('')
const newPublicationConfirmed=ref(false)
const updateProgress=computed(()=>latest.value?.mode==='update' && latest.value?.update_id ? updateStages(latest.value.publication):[])
const updateOverall=computed(()=>latest.value?.mode==='update' && latest.value?.update_id
  ? summarizeUpdateProgress(latest.value.publication) : null)
const existingPublication=computed(()=>state.value?.has_remote_publication ?? nav.workflow?.has_remote_publication
  ?? (!!nav.workflow?.existing_publication_id || !!nav.publication?.id))
const newPublicationWarning=computed(()=>['publish','upload'].includes(mode.value) && !selectedRun.value && existingPublication.value)
const ttsServiceError=ref('')
const latest=computed(()=>nav.workflow?.run)
const stageSequence=computed(()=>workflowStageSequence(nav.workflowPlan?.steps))
const hasStageSequence=computed(()=>!!latest.value?.id && nav.workflowPlan?.id===latest.value.id && stageSequence.value.length>0)
const completedStages=computed(()=>stageSequence.value.filter(stage=>stage.indices.every(index=>index<Number(latest.value?.cursor||0))).length)
const stepLabel=(key:string,labels?:Record<string,string>)=>labels?.[key] || STEP_LABELS[key] || (key==='comments'?'准备评论':'处理阶段')
const currentStep=computed(()=>workflowExecutionStep(latest.value,nav.workflowPlan,nav.steps))
// 当前进度与百分比使用同一投影模块，避免热更新时既有store getter仍持有旧函数。
const currentExecution=computed(()=>executionForWorkflow(latest.value,nav.workflowPlan,nav.steps))
const workflowHistory=computed(()=>currentStep.value ? normalizeExecution(latest.value?.execution)?.history || [] : [])
const active=computed(()=>WORKFLOW_OPEN.includes(latest.value?.status))
const failureStatus=computed(()=>submitting.value ? null : workflowFailureStatus(latest.value,nav.workflowPlan,nav.steps,nav.stepsLoaded&&!nav.stepsError))
const stage=computed(()=>submitting.value ? {text:requestLabel.value,tone:'info' as const} : failureStatus.value?.historical
  ? {text:'历史中断',tone:failureStatus.value.recovered?'default':'warning'} : workflowRunStage(latest.value))
const resumable=computed(()=>['paused','failed','attention'].includes(latest.value?.status)&&(!latest.value?.publication_id || latest.value?.mode==='update'))
const readyForPublication=computed(()=>nav.uid===props.uid && nav.stepsLoaded && !nav.stepsError && !nav.busy
  && !!nav.workflow && !nav.workflowError && !nav.publicationError && !nav.archived && !nav.dirMissing
  && nav.stepMap.assemble?.status==='done' && nav.stepMap.assemble?.fresh===true
  && !active.value && !latest.value?.publication_id && !nav.workflow?.existing_publication_id && !nav.publication?.id)
const commentsNeedAttention=computed(()=>!submitting.value && !!latest.value?.error && (commentFailure(latest.value.error)
  || nav.workflowPlan?.id===latest.value.id && nav.workflowPlan?.steps?.[latest.value.cursor]==='comments'))
const ttsServiceBlocker=computed(()=>preview.value?.blockers?.some((item:string)=>item==='配音服务未就绪，请先在配音页启动服务')===true)
const modeLabel=computed(()=>workflowModeLabel(nav.workflow?.mode))
const selectedVersions=computed(()=>preview.value?.version_specs || (state.value?.recipe?.version_ids||[]).map((id:string)=>state.value?.version_catalog?.versions.find((v:any)=>v.id===id)).filter(Boolean))
const publicationVersions=computed(()=>(preview.value?.recipe?.version_ids || []).map((id:string)=>selectedVersions.value.find((item:any)=>item.id===id) || state.value?.version_catalog?.versions.find((item:any)=>item.id===id) || {id,name:'未找到的版本'}))
const versionReadiness=(id:string)=>preview.value?.production_versions?.find((item:any)=>item.id===id)
const canConfirm=computed(()=>!busy.value&&!nav.archived&&!nav.busy&&!active.value&&!nav.dirMissing&&!!preview.value&&preview.value.recipe.mode===mode.value&&!preview.value.blockers.length&&(!newPublicationWarning.value || newPublicationConfirmed.value)&&(!selectedRun.value||(!!preview.value.recovery&&!!preview.value.recovery_fingerprint)))
// 预检只读；只有新稿重复发布保护需要明确确认。
const planEditingLocked=computed(()=>nav.archived || nav.busy || active.value || !state.value || busy.value&&!checking.value)
const newPlanEditingLocked=computed(()=>planEditingLocked.value || !!selectedRun.value)
const deliveryOnly=computed(()=>mode.value==='update' && !!preview.value?.publication_update)
const reconcileUpdate=computed(()=>deliveryOnly.value && updateNeedsReconcile(preview.value.publication_update))
const pendingDeliveryStages=computed(()=>deliveryOnly.value?updateStages(preview.value.publication_update).filter(item=>!['completed','success','skipped'].includes(item.status)):[])
const deliveryMode=computed(()=>preview.value?.recipe?.delivery_mode || (mode.value==='update'?'publish':mode.value))
const endpointLabel=computed(()=>workflowModeLabel(deliveryMode.value))
const confirmLabel=computed(()=>reconcileUpdate.value?'确认核对原稿更新结果':deliveryOnly.value?'确认继续原稿交付':
  (selectedRun.value?'确认恢复':'确认运行')+(deliveryMode.value==='publish'?(mode.value==='update'?'并更新原稿件':'并投稿'):deliveryMode.value==='upload'?'并上传（待确认投稿）':deliveryMode.value==='draft'?'并准备草稿':'制作成片'))
const deliveryDescription=computed(()=>deliveryMode.value==='publish'?(mode.value==='update'?'完成所列制作后，按已保存范围提交原稿更新，保留原BV。':'完成所列制作后上传并提交投稿。'):deliveryMode.value==='upload'?'完成所列制作后上传，等待你在发布页确认投稿。':deliveryMode.value==='draft'?'完成所列制作后准备本地草稿。':'生成目标成片，完成后结束执行。')
let disposed=false, generation=0, requestId='', requestFingerprint='', readAbort:AbortController|undefined
async function api(path='',body?:any,signal?:AbortSignal){
  const r=await fetch('/api/projects/'+encodeURIComponent(props.uid)+'/workflow'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:signal?AbortSignal.any([signal,AbortSignal.timeout(60000)]):undefined})
  const d=await r.json();if(!r.ok)throw Object.assign(new Error(d.message||'运行检查失败'),{code:d.code,status:r.status});return d
}
async function requestPlan(ticket:number){
  if(nav.archived){preview.value=null;error.value='项目已归档，请先恢复项目后再检查运行方案。';return}
  const result=await api('/plan',selectedRun.value?{resume_id:selectedRun.value}:{target_driven:true,...(requestedStart.value?{from_step:requestedStart.value}:{}),
    ...(newPublicationConfirmed.value?{confirm_new_publication:true}:{})},readAbort?.signal)
  if(!disposed&&ticket===generation){
    preview.value=result;mode.value=result.recipe.mode
    const signature=JSON.stringify([selectedRun.value,result.fingerprint,result.recovery_fingerprint])
    // 同一未知回执在重新检查后仍复用原请求，已确定受理或方案变化才换新身份。
    if(!requestId || requestFingerprint!==signature){requestId=crypto.randomUUID();requestFingerprint=signature}
    emit('mode-change',mode.value)
  }
}
function onNewPublicationConfirmation(value:boolean){
  if(newPlanEditingLocked.value)return
  newPublicationConfirmed.value=value;reinspect()
}
async function open(run='',fromStep=''){
  if(nav.archived){error.value='项目已归档，请先恢复项目后再检查运行方案。';return}
  if(busy.value||nav.busy||active.value||nav.dirMissing)return
  const ticket=++generation
  busy.value=true;checking.value=true;readAbort=new AbortController();error.value='';ttsServiceError.value='';preview.value=null;state.value=null;selectedRun.value=run
  show.value=true;requestedStart.value=run?'':fromStep;newPublicationConfirmed.value=false
  try{
    const data=await api('',undefined,readAbort.signal)
    if(disposed||ticket!==generation)return
    state.value=data
    mode.value=(run?data.runs.find((item:any)=>item.id===run)?.recipe.mode:null)||data.recipe.mode
    await requestPlan(ticket)
  }catch(e:any){if(!disposed&&ticket===generation)error.value=friendlyError(e)}
  finally{if(ticket===generation){busy.value=false;checking.value=false}}
}
function reinspect(check=true){
  if(disposed || planEditingLocked.value)return
  // 新选择立即撤销旧确认资格；迟到的响应只能属于它原来的预检代次。
  if(checking.value){generation++;readAbort?.abort();checking.value=false;busy.value=false}
  preview.value=null;requestId='';requestFingerprint='';if(check)void inspect()
}
async function inspect(){
  if(nav.archived){preview.value=null;error.value='项目已归档，请先恢复项目后再检查运行方案。';return}
  if(busy.value)return
  const ticket=++generation
  busy.value=true;checking.value=true;readAbort=new AbortController();error.value='';preview.value=null
  try{await requestPlan(ticket)}catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}
  finally{if(ticket===generation){busy.value=false;checking.value=false}}
}
function onTtsServiceStatus(status:any){
  ttsServiceError.value=status?.status==='failed'?status?.message||'配音服务启动失败':''
  // 服务由预检抽屉内的快捷按钮启动后，重新执行同一份预检；不直接启动制作。
  if(status?.online&&ttsServiceBlocker.value&&!busy.value) void inspect()
}
function close(){
  if(busy.value&&!checking.value)return
  generation++;readAbort?.abort();checking.value=false;busy.value=false;show.value=false;preview.value=null;ttsServiceError.value=''
}
async function confirmStart(){
  if(!canConfirm.value)return
  // 抽屉本身就是完整确认范围；同一预检重试沿用请求标识，响应丢失也不会重复启动。
  const snapshot=JSON.parse(JSON.stringify(preview.value)),run=selectedRun.value,uid=props.uid
  if(!requestId)requestId=crypto.randomUUID()
  busy.value=true;submitting.value=true;error.value=''
  try{
    const result=await api(run?'/'+run+'/resume':'/start',{recipe:snapshot.recipe,fingerprint:snapshot.fingerprint,...(run?{recovery_fingerprint:snapshot.recovery_fingerprint}:{}),confirmed:true,request_id:requestId})
    if(disposed)return
    nav.workflowStarted(uid,result);show.value=false;requestId='';requestFingerprint='';emit('started')
    void nav.refreshWorkflow()
  }catch(e:any){if(!disposed){error.value=friendlyError(e);if(e.status>=400&&e.status<500){requestId='';requestFingerprint=''}if(e.status===409||['WORKFLOW_PLAN_CHANGED','WORKFLOW_CONFLICT','PIPELINE_BUSY','REQUEST_CONFLICT'].includes(e.code)){preview.value=null;error.value+=' 请重新检查后确认。'}}}
  finally{busy.value=false;submitting.value=false}
}
async function stop(){
  if(busy.value||!latest.value)return
  busy.value=true;error.value=''
  try{const result=await api('/'+latest.value.id+'/stop',{});if(!disposed){nav.workflowStarted(props.uid,result);void nav.refreshWorkflow()}}
  catch(e:any){if(!disposed)error.value=e.message}finally{busy.value=false}
}
onUnmounted(()=>{disposed=true;generation++;readAbort?.abort()})
onMounted(async()=>{
  if(!['project','workflow','update'].includes(String(route.query.run)))return
  const legacyUpdate=route.query.run==='update'
  const query={...route.query};delete query.run;delete query.publication
  await router.replace({path:legacyUpdate?`/projects/${props.uid}/publish`:route.path,query,hash:legacyUpdate?'#project-rules':route.hash})
  if(!disposed&&!legacyUpdate)await open()

})
defineExpose({ open, close })
</script>
<template>
  <n-card v-if="!headless" size="small" title="任务运行与进度" class="run-card" id="automatic-workflow">
    <template #header-extra>
      <n-button v-if="active" :disabled="busy||latest.status==='stopping'" size="small" @click="stop">{{ latest.status==='monitoring'?'停止自动查询':'停止完整流程' }}</n-button>
      <ProjectRunPanel v-else :projects="[{uid,title:nav.title||uid}]" allow-workflow workflow-handler :disabled="busy||nav.archived||nav.busy||nav.autoStepsSaving||nav.dirMissing" @workflow="open" @started="nav.poll()" />
    </template>
    <div class="run-policy"><span class="muted">项目交付终点</span><strong>{{ modeLabel }}</strong><ConfigurationSourceBadge :source="nav.workflow?.mode_source"/><RouterLink :to="'/projects/'+uid+'/publish#project-rules'">交付设置</RouterLink></div>
    <div v-if="nav.workflow?.project_goal" class="run-goals"><span class="muted">目标成片</span><strong>{{ nav.workflow.project_goal.production_required===false?'本次仅同步原稿资料，无需制作成片':(nav.workflow.project_goal.target_versions || []).map((item:any)=>item.name).join('、') || '请到合成页选择' }}</strong><RouterLink v-if="nav.workflow.project_goal.production_required!==false" :to="`/projects/${uid}/steps/assemble`">调整目标</RouterLink><p v-if="nav.workflow.mode!=='produce' && nav.workflow.project_goal.target_kind" class="muted">稿件目标：{{ nav.workflow.project_goal.target_kind==='existing' ? '更新原稿 · '+(nav.workflow.project_goal.target_bvid || nav.workflow.project_goal.target_publication_id || '待核对') : nav.workflow.project_goal.target_selection?.source==='first_publish' ? '首次发布 · 默认创建新稿' : '创建新稿' }}</p></div>
    <p v-if="nav.archived" class="muted">项目已归档，请先恢复项目后再继续制作或投稿。</p>
    <p class="muted run-description">在各步骤保存参数，在合成成片页选择目标版本，在发布到B站页设置交付终点。点击「运行项目」检查依赖并复用有效结果。</p>
    <RouterLink v-if="nav.workflow?.existing_publication_id&&!latest?.publication_id" :to="'/projects/'+uid+'/publish'">查看或继续现有稿件 →</RouterLink>
    <div v-if="readyForPublication" class="publication-next" role="status"><strong>已有有效成片，可继续发布流程</strong><span class="muted">到发布步骤保存交付方式与稿件目标后，再运行项目。已有有效结果会复用。</span><RouterLink :to="'/projects/'+uid+'/publish'">查看发布资料 →</RouterLink></div>
    <div v-if="latest" class="run-state">
      <div class="run-row"><span class="muted">{{ active || submitting?'当前流程':'上次自动流程' }}</span><n-tag size="small" :type="stage.tone">{{ stage.text }}</n-tag><span v-if="!submitting && (latest.phase || failureStatus?.stepLabel)" class="run-phase">{{ failureStatus?.historical ? '停在：'+(failureStatus.stepLabel||latest.phase) : latest.phase }}</span><small class="muted run-endpoint">{{ active || submitting?'本次':'上次' }}终点：{{ workflowModeLabel(latest.delivery_mode || latest.mode,'未记录') }}</small></div>
      <div v-if="latest.total && hasStageSequence" class="run-progress"><span>{{ active?'已完成工作阶段':'上次已完成工作阶段' }}</span><span>{{ completedStages }} / {{ stageSequence.length }}</span></div>
      <ExecutionProgress v-if="submitting" :execution="{schema:1,state:'queued',phase:'workflow.request',label:requestLabel,detail:'正在核对并提交本次确认的方案，请等待后台回执。'}" indeterminate />
      <ExecutionProgress v-else-if="nav.commentsProgress?.state !== 'running'" :execution="currentExecution" :percentage="currentStep?.progress" :estimated="currentStep?.progress_estimated" :indeterminate="currentStep?.progress_indeterminate" :eta="currentStep ? formatEta(executionEtaSeconds(currentStep),currentStep.eta_scope) : ''" :activity="active ? nav.currentActivity : null" :inactivity-label="currentStep ? undefined : '距流程状态更新'" />
      <details v-if="workflowHistory.length" class="workflow-stage-history"><summary>流程阶段记录 · {{ workflowHistory.length }} 项</summary><ol><li v-for="(item,index) in workflowHistory" :key="index"><strong>{{ item.label }}</strong><n-text :type="item.statusType" :depth="item.statusType==='default'?3:undefined">{{ item.stateLabel }}</n-text><p v-if="item.detail">{{ item.detail }}</p></li></ol></details>
      <CommentWorkflowProgress v-if="nav.commentsProgress?.state === 'running'" :uid="uid" :automation="nav.commentsProgress" />
      <ol v-if="hasStageSequence" class="run-sequence" :aria-label="active?'本次流程的实际步骤':'上次流程的步骤记录'">
        <li v-for="stage in stageSequence" :key="stage.key" :class="{ done: stage.indices.every(index=>index<latest.cursor), current: stage.indices.includes(Number(latest.cursor)) && active }" :aria-current="stage.indices.includes(Number(latest.cursor)) && active?'step':undefined">{{ stage.label }}</li>
      </ol>
      <section v-if="updateProgress.length && !submitting" class="update-progress-summary" aria-label="自动原稿更新进度">
        <div class="update-progress-heading"><strong>原稿更新总体进度</strong><span v-if="updateOverall?.percentage !== null && !updateOverall?.indeterminate">{{ Math.round(updateOverall?.percentage || 0) }}%</span><span v-else class="muted">处理中</span></div>
        <n-progress v-if="updateOverall?.percentage !== null && !updateOverall?.indeterminate" type="line" :percentage="Math.round(updateOverall?.percentage || 0)" :show-indicator="false" processing />
        <p class="muted">已完成 {{ updateOverall?.completed || 0 }} / {{ updateOverall?.total || updateProgress.length }} 个阶段<span v-if="updateOverall?.active"> · 当前：{{ updateOverall.active.label }}<template v-if="updateOverall.active.progress !== null"> {{ Math.round(updateOverall.active.progress) }}%</template><template v-else> · 无可量化进度</template></span></p>
        <ol class="update-progress"><li v-for="item in updateProgress" :key="item.key" :data-status="item.status"><strong>{{ item.label }}</strong><n-text class="update-progress-state" :type="executionStatusType(item.status)" :depth="executionStatusType(item.status) === 'default' ? 3 : undefined" :data-status-type="executionStatusType(item.status)">{{ item.text }}</n-text><p v-if="item.error">{{ item.error }}</p></li></ol>
      </section>
      <RouterLink v-if="latest.publication_id || latest.update_id" :to="'/projects/'+uid+'/publish'">{{ latest.mode==='update'?'查看原稿更新详情 →':active?'查看发布进度 →':'查看或继续现有稿件 →' }}</RouterLink>
      <div v-if="failureStatus?.recovered" class="previous-failure" role="status">
        <p>{{ failureStatus.message }}</p><p class="muted">完整流程仍保留中断记录；从「运行项目」选择继续上次任务，核对其余步骤。</p>
        <details><summary>查看上次中断原因</summary><p>{{ failureStatus.reason }}</p></details>
      </div>
      <n-alert v-else-if="failureStatus" type="warning"><p>{{ failureStatus.message }}</p><p>{{ failureStatus.reason }}</p><p v-if="failureStatus.phaseLabel" class="muted"><span v-if="nav.busy">当前制作任务仍在进行；此提示属于上次自动流程。</span>从「运行项目」选择继续上次任务，核对当前制作结果后恢复发布资料准备。</p></n-alert>
      <div v-if="commentsNeedAttention" class="run-links"><RouterLink :to="commentWorkspaceLink(uid)">核对评论与上屏方案 →</RouterLink><RouterLink :to="commentWorkspaceLink(uid,true)">查看评论 AI 日志 →</RouterLink><small class="muted">处理后通过「运行项目」核对复用范围。</small></div>
    </div>
    <n-alert v-if="nav.workflowError" type="warning">{{ nav.workflowError }} <n-button text @click="nav.refreshWorkflow()">重新读取</n-button></n-alert>
    <n-alert v-if="error&&!show" type="error">{{ error }}</n-alert>
  </n-card>
  <n-drawer :show="show" width="min(760px,100vw)" :mask-closable="!busy||checking" :close-on-esc="!busy||checking" @update:show="value=>{if(!value)close()}">
    <n-drawer-content :title="selectedRun?'检查恢复方案':'检查执行方案'" :closable="!busy||checking">
      <div class="run-check">
        <div class="run-links"><RouterLink :to="'/projects/'+uid+'/steps/assemble'">成片版本</RouterLink><RouterLink :to="commentWorkspaceLink(uid)">评论设置</RouterLink><RouterLink :to="'/projects/'+uid+'/publish#project-rules'">发布规则</RouterLink></div>
        <section class="run-scope" aria-label="已保存的执行目标"><h3>{{ selectedRun?'沿用上次确认的计划':'根据项目设置执行' }}</h3><p>交付终点：<strong>{{ endpointLabel }}</strong></p><p class="muted">{{ selectedRun?'当前步骤设置不会改变上次任务的交付授权。':'需要调整目标或参数时，请到相应步骤保存后重新检查。' }}</p></section>
        <n-alert v-if="newPublicationWarning" type="warning" :show-icon="false"><p>此项目已有B站稿件。新发布会生成另一个BV；如需覆盖现有稿件，请到发布设置选择要更新的原稿件。</p><n-checkbox :checked="newPublicationConfirmed" :disabled="newPlanEditingLocked" @update:checked="onNewPublicationConfirmation">我确认另发新稿件，保留已有BV</n-checkbox></n-alert>
        <DubbingRunOptions v-if="!deliveryOnly && preview?.dubbing" :uid="uid" :summary="preview.dubbing" readonly />
        <WorkflowUpdateSummary v-if="mode==='update' && preview?.update_plan" :plan="preview.update_plan" :replace-videos="preview.recipe.update?.replace_videos!==false" :sync-fields="preview.recipe.update?.sync_fields" :delivery-mode="deliveryMode" readonly :delivery-only="deliveryOnly" />
        <section v-if="selectedVersions.length && !deliveryOnly" class="version-list" aria-label="制作与复用目标版本"><h3>制作与复用目标</h3><div v-for="version in selectedVersions" :key="version.id" class="version-row"><strong>{{ version.name }}</strong><span>{{ versionSummary(version) }}<template v-if="versionReadiness(version.id)"> · {{ versionReadiness(version.id).uses_source_directly?'直接使用原片，无需编码':versionReadiness(version.id).fresh?'复用有效成片':'需要制作或更新' }}</template></span></div></section>
        <section v-if="mode!=='produce' && mode!=='update' && publicationVersions.length" class="version-list" aria-label="发布分P范围"><h3>发布分P</h3><div v-for="(version,index) in publicationVersions" :key="version.id" class="version-row"><strong>P{{ index+1 }} · {{ preview?.recipe.part_titles[version.id]||version.name }}</strong><span>{{ version.name }}</span></div></section>
        <n-spin v-if="busy&&!preview" size="small" description="正在检查已保存设置和依赖…"/>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <p v-if="submitting" class="muted" role="status" aria-live="polite">正在核对并提交本次确认的方案，请等待后台回执。</p>
        <template v-if="preview">
          <RecoveryPlan v-if="selectedRun && preview.recovery && !deliveryOnly" :recovery="preview.recovery" :pending-steps="preview.pending_production_steps" />
          <DubbingReadiness v-if="!deliveryOnly && preview.dubbing?.needed && preview.dubbing.generate === undefined" :summary="preview.dubbing" />
          <section v-if="deliveryOnly" class="run-scope delivery-scope" aria-label="本次继续的原稿交付范围"><h3>{{ reconcileUpdate ? '先核对平台结果' : '继续未完成的交付' }}</h3><p v-if="reconcileUpdate">本次先只读核对B站实际结果，不盲目重发修改请求；核对清楚后再按已确认范围继续未完成步骤。</p><ul v-if="pendingDeliveryStages.length"><li v-for="item in pendingDeliveryStages" :key="item.key">{{ item.label }} · {{ item.text }}</li></ul><p v-else>所列交付阶段已完成，仅核对并收尾当前流程。</p><p class="muted">成片与上传使用已确认的版本；不会重新获取视频、重新合成或覆盖已经完成的交付。</p></section>
          <section v-else class="run-scope"><h3>执行范围</h3><p v-if="preview.requested_start_step">从「{{ stepLabel(preview.requested_start_step,preview.labels) }}」继续项目；必要的上游依赖仍会检查，有效结果复用。</p><p>{{ preview.steps.length ? preview.steps.map((key:string)=>stepLabel(key,preview.labels)).join(' → ') : '不运行制作步骤，直接执行下方已确认的交付范围。' }}</p><p v-if="preview.pending_production_steps?.length" class="muted">需要准备或更新：{{ preview.pending_production_steps.map((key:string)=>stepLabel(key,preview.labels)).join('、') }}；其余步骤核对并复用有效结果。</p><PublicationTargetSummary v-if="mode!=='produce' && mode!=='update' && preview.settings" :plan="preview" /><p v-if="mode==='publish'">{{ publicationVisibility(preview.settings.visibility) }} · {{ preview.settings.publish_at?new Date(preview.settings.publish_at*1000).toLocaleString('zh-CN')+' 定时发布':'完成上传后提交审核' }}</p><p class="muted">{{ deliveryDescription }}</p></section>
          <p v-if="mode==='publish'" class="muted">{{ publicationChapterScope(preview) }}</p>
          <n-alert v-if="preview.blockers.length" type="warning" title="还需处理">
            <div v-for="item in preview.blockers" :key="item" class="workflow-blocker">
              <span>{{ item }}</span>
              <TtsServiceControl v-if="item==='配音服务未就绪，请先在配音页启动服务'" :uid="uid" :disabled="false" :revision="0" @status="onTtsServiceStatus" />
              <RouterLink v-if="item==='配音服务未就绪，请先在配音页启动服务'" class="workflow-service-link" :to="'/projects/'+uid+'/steps/tts'">打开字幕与配音页查看设置和日志 →</RouterLink>
            </div>
            <div class="run-links"><RouterLink :to="`/projects/${uid}/steps/assemble`">检查成片目标 →</RouterLink><RouterLink :to="`/projects/${uid}/publish#project-rules`">检查交付设置 →</RouterLink></div>
            <n-alert v-if="ttsServiceError" type="error" :show-icon="false">{{ ttsServiceError }}
            </n-alert>
          </n-alert>
          <details v-if="preview.warnings.length"><summary>运行说明</summary><p v-for="item in preview.warnings" :key="item">{{ item }}</p></details>
        </template>
      </div>
      <template #footer><PublicationRightsHint v-if="['publish','upload'].includes(deliveryMode)"/><div class="run-row run-actions"><n-button :disabled="busy&&!checking" @click="close">{{ checking?'取消检查':'取消' }}</n-button><n-button :disabled="busy||nav.archived" @click="inspect">重新检查</n-button><n-button type="primary" :loading="busy&&!!preview" :disabled="!canConfirm" @click="confirmStart">{{ confirmLabel }}</n-button></div></template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.workflow-stage-history{font-size:12px;color:var(--df-muted)}.workflow-stage-history ol{padding-left:20px;margin:10px 0 0;max-height:280px;overflow:auto}.workflow-stage-history li{padding:4px 0;line-height:1.7}.workflow-stage-history strong{font-weight:500;margin-right:10px}.workflow-stage-history p{margin:3px 0 0;overflow-wrap:anywhere}
.previous-failure { padding:12px 14px; border:1px solid var(--df-border); border-radius:8px; background:var(--df-panel); font-size:12px; line-height:1.7; }
.previous-failure p { margin:0 0 6px; }.previous-failure details { color:var(--df-muted); }.previous-failure details p { margin:8px 0 0; }
.publication-next { display:grid; gap:6px; padding:12px 14px; border:1px solid var(--df-border); border-radius:8px; background:var(--df-panel); font-size:12px; line-height:1.7; }.publication-next strong { font-weight:500; }.publication-next a { color:var(--df-accent); text-decoration:none; }
.update-source-choice{display:grid;gap:8px}.update-source-choice p{margin:0;line-height:1.8}.update-progress-summary{display:grid;gap:8px;padding:12px 14px;border:1px solid var(--df-border);border-radius:10px;background:color-mix(in srgb,var(--df-panel) 72%,transparent)}.update-progress-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}.update-progress-heading>span{font-variant-numeric:tabular-nums;color:var(--df-primary,#8eb9ff);font-weight:600}.update-progress-summary>p{margin:0;font-size:12px}.update-progress{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;list-style:none;padding:0;margin:0}.update-progress li{display:grid;gap:6px;padding:10px 12px;border:1px solid var(--df-border);border-radius:8px;font-size:12px}.update-progress span:not(.update-progress-state){color:var(--df-muted)}.update-progress p{margin:0;color:var(--df-warning)}.update-progress [data-status="completed"] strong{color:#83c8ae}
.workflow-production-choice{display:grid;gap:10px;padding-bottom:16px;border-bottom:1px solid var(--df-border)}.workflow-production-choice p{margin:0;line-height:1.8}.production-step-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.run-actions { margin-top:12px; }
.run-sequence { list-style:none; display:flex; flex-wrap:wrap; gap:6px 12px; padding:10px 12px; margin:0; border:1px solid var(--df-border); border-radius:8px; background:var(--df-bg); font-size:11px; line-height:1.7; color:var(--df-muted); }
.run-sequence li:not(:last-child)::after { content:' →'; margin-left:8px; opacity:.5; }
.run-sequence .done { color:#83c8ae; } .run-sequence .current { color:var(--df-accent); font-weight:600; }
.workflow-blocker { display:grid; gap:4px; }
.workflow-blocker :deep(.service-control) { margin:4px 0 0; padding:0; border-bottom:0; }
.workflow-service-link { font-size:12px; }
.run-goals{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:12px;font-size:12px}.run-goals strong{font-weight:500}.run-goals a{color:var(--df-accent)}.run-goals p{flex-basis:100%;margin:0}.run-card{margin-top:16px;border-color:var(--df-border)}.muted,.version-row span{font-size:12px;color:var(--df-muted)}.run-row,.run-links,.run-policy{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.run-policy strong{font-size:15px;font-weight:600}.run-policy a{margin-left:auto;font-size:12px}.run-description{margin:12px 0 0;line-height:1.8}.run-state{display:grid;gap:12px;min-width:0;border-top:1px solid var(--df-border);margin-top:16px;padding-top:16px}.run-state>:deep(.progress-activity){margin-top:0}.run-phase{min-width:0;overflow-wrap:anywhere;font-size:13px;font-weight:500}.run-endpoint{margin-left:auto}.run-state>a{font-size:12px}.run-progress{display:flex;justify-content:space-between;align-items:center;gap:12px;min-width:0;max-width:680px;font-size:12px;color:var(--df-muted)}.run-progress .n-progress{min-width:0}.run-progress>span{white-space:nowrap;font-variant-numeric:tabular-nums}.run-check{display:grid;gap:20px;padding:18px 0}.run-check label{display:grid;gap:8px}.run-links{font-size:12px}.version-row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--df-border);padding:12px 0}.version-row strong{font-size:13px}.run-check h3{margin:0;font-size:14px}.run-scope{padding:16px;background:var(--df-panel);border:1px solid var(--df-border);border-radius:8px;font-size:13px;line-height:1.8}.run-scope p:last-child{margin-bottom:0}.run-actions{justify-content:flex-end}.run-card :deep(.n-alert){margin-top:10px}summary{cursor:pointer;font-size:12px}.run-check details{color:var(--df-muted);font-size:12px;line-height:1.8}
@media(max-width:650px){.run-policy a{margin-left:0}.run-row{gap:8px}.run-endpoint{flex-basis:100%;margin-left:0}.run-progress{gap:8px}.run-sequence{padding:10px}.run-card :deep(.n-card-header){flex-wrap:wrap;gap:12px}.run-card :deep(.n-card-header__extra){margin-left:0}.run-actions{gap:8px}}
@media(max-width:420px){.production-step-options{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
