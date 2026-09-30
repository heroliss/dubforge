<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NDrawer, NDrawerContent, NProgress, NSelect, NSpin, NTag } from 'naive-ui'
import { versionSummary } from '../utils/assemblyVersions'
import { WORKFLOW_ENDS, WORKFLOW_OPEN, workflowModeLabel, workflowRunStage } from '../utils/workflow'
import { useNavStore } from '../stores/nav'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import ProgressActivity from './ProgressActivity.vue'
import PublicationRightsHint from './PublicationRightsHint.vue'
import DubbingReadiness from './DubbingReadiness.vue'

const props = defineProps<{uid:string}>()
const nav = useNavStore()
const state=ref<any>(null), preview=ref<any>(null), show=ref(false), busy=ref(false), error=ref('')
const checking=ref(false)
const mode=ref('draft'), selectedRun=ref('')
const latest=computed(()=>nav.workflow?.run)
const active=computed(()=>WORKFLOW_OPEN.includes(latest.value?.status))
const resumable=computed(()=>['paused','failed','attention'].includes(latest.value?.status)&&!latest.value?.publication_id)
const modeLabel=computed(()=>workflowModeLabel(nav.workflow?.mode))
const selectedVersions=computed(()=>preview.value?.version_specs || (state.value?.recipe?.version_ids||[]).map((id:string)=>state.value?.version_catalog?.versions.find((v:any)=>v.id===id)).filter(Boolean))
const canConfirm=computed(()=>!busy.value&&!!preview.value&&preview.value.recipe.mode===mode.value&&!preview.value.blockers.length)
const confirmLabel=computed(()=>mode.value==='publish'?'确认制作并投稿':mode.value==='draft'?'确认制作并准备草稿':'确认制作成片')
let disposed=false, generation=0, requestId='', readAbort:AbortController|undefined
async function api(path='',body?:any,signal?:AbortSignal){
  const r=await fetch('/api/projects/'+encodeURIComponent(props.uid)+'/workflow'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:signal?AbortSignal.any([signal,AbortSignal.timeout(60000)]):undefined})
  const d=await r.json();if(!r.ok)throw new Error(d.message||'运行检查失败');return d
}
async function requestPlan(ticket:number){
  const result=await api('/plan',{mode:mode.value,...(selectedRun.value?{resume_id:selectedRun.value}:{})},readAbort?.signal)
  if(!disposed&&ticket===generation){preview.value=result;requestId=crypto.randomUUID()}
}
async function open(run=''){
  if(busy.value||nav.busy||active.value||nav.dirMissing)return
  const ticket=++generation
  busy.value=true;checking.value=true;readAbort=new AbortController();error.value='';preview.value=null;selectedRun.value=run;show.value=true
  try{
    const data=await api('',undefined,readAbort.signal)
    if(disposed||ticket!==generation)return
    state.value=data
    mode.value=(run?data.runs.find((item:any)=>item.id===run)?.recipe.mode:null)||data.recipe.mode
    await requestPlan(ticket)
  }catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}
  finally{if(ticket===generation){busy.value=false;checking.value=false}}
}
async function inspect(){
  if(busy.value)return
  const ticket=++generation
  busy.value=true;checking.value=true;readAbort=new AbortController();error.value='';preview.value=null
  try{await requestPlan(ticket)}catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}
  finally{if(ticket===generation){busy.value=false;checking.value=false}}
}
function close(){
  if(busy.value&&!checking.value)return
  generation++;readAbort?.abort();checking.value=false;busy.value=false;show.value=false;preview.value=null
}
async function confirmStart(){
  if(!canConfirm.value)return
  // 抽屉本身就是完整确认范围；同一预检重试沿用请求标识，响应丢失也不会重复启动。
  const snapshot=JSON.parse(JSON.stringify(preview.value)),run=selectedRun.value,uid=props.uid
  if(!requestId)requestId=crypto.randomUUID()
  busy.value=true;error.value=''
  try{
    const result=await api(run?'/'+run+'/resume':'/start',{recipe:snapshot.recipe,fingerprint:snapshot.fingerprint,confirmed:true,request_id:requestId})
    if(disposed)return
    nav.workflowStarted(uid,result);show.value=false
    void nav.refreshWorkflow()
  }catch(e:any){if(!disposed)error.value=e.message}
  finally{busy.value=false}
}
async function stop(){
  if(busy.value||!latest.value)return
  busy.value=true;error.value=''
  try{const result=await api('/'+latest.value.id+'/stop',{});if(!disposed){nav.workflowStarted(props.uid,result);void nav.refreshWorkflow()}}
  catch(e:any){if(!disposed)error.value=e.message}finally{busy.value=false}
}
onUnmounted(()=>{disposed=true;generation++;readAbort?.abort()})
</script>
<template>
  <n-card size="small" title="自动运行" class="run-card" id="automatic-workflow">
    <template #header-extra>
      <n-button v-if="active" :disabled="busy||latest.status==='stopping'" size="small" @click="stop">{{ latest.status==='monitoring'?'停止自动查询':'停止完整流程' }}</n-button>
      <n-button v-else-if="resumable" :disabled="busy||nav.busy||nav.dirMissing" type="primary" size="small" @click="open(latest.id)">检查并继续流程…</n-button>
      <RouterLink v-else-if="nav.workflow?.existing_publication_id&&nav.workflow.mode!=='produce'" :to="'/projects/'+uid+'/publish'">查看或继续现有稿件 →</RouterLink>
      <n-button v-else :disabled="busy||nav.busy||nav.dirMissing||!nav.workflow||!!nav.workflowError" type="primary" size="small" @click="open()">检查并运行完整流程…</n-button>
    </template>
    <div class="run-policy"><span class="muted">执行终点</span><strong>{{ modeLabel }}</strong><ConfigurationSourceBadge :source="nav.workflow?.mode_source"/><RouterLink :to="'/projects/'+uid+'/publish#project-rules'">调整发布规则</RouterLink></div>
    <p class="muted run-description">使用各步骤已保存的设置，按成片版本补齐必要依赖；下方勾选仅用于手动制作。</p>
    <div v-if="latest" class="run-state">
      <div class="run-row"><span class="muted">{{ active?'当前流程':'最近一次' }}</span><n-tag size="small" :type="workflowRunStage(latest).tone">{{ workflowRunStage(latest).text }}</n-tag><span>{{ latest.phase }}</span><small class="muted">本次终点：{{ workflowModeLabel(latest.mode) }}</small></div>
      <div v-if="latest.total" class="run-progress"><span>制作步骤</span><n-progress type="line" :percentage="Math.min(100,Math.round(latest.cursor/latest.total*100))" :show-indicator="false"/><span>{{ Math.min(latest.cursor,latest.total) }} / {{ latest.total }}</span></div>
      <ProgressActivity v-if="active" :activity="nav.currentActivity" />
      <ol v-if="nav.workflowPlan?.id===latest.id" class="run-sequence" aria-label="本次流程的实际步骤">
        <li v-for="(step,index) in nav.workflowPlan.steps" :key="step" :class="{ done: Number(index)<latest.cursor, current: Number(index)===latest.cursor && active }" :aria-current="Number(index)===latest.cursor && active?'step':undefined">{{ nav.workflowPlan.labels?.[step] || step }}</li>
      </ol>
      <RouterLink v-if="latest.publication_id" :to="'/projects/'+uid+'/publish'">{{ active?'查看发布进度 →':'查看或继续现有稿件 →' }}</RouterLink>
      <n-alert v-if="latest.error" type="warning">{{ latest.error }}</n-alert>
    </div>
    <n-alert v-if="nav.workflowError" type="warning">{{ nav.workflowError }} <n-button text @click="nav.refreshWorkflow()">重新读取</n-button></n-alert>
    <n-alert v-if="error&&!show" type="error">{{ error }}</n-alert>
  </n-card>
  <n-drawer :show="show" width="min(760px,100vw)" :mask-closable="!busy||checking" :close-on-esc="!busy||checking" @update:show="value=>{if(!value)close()}">
    <n-drawer-content :title="selectedRun?'检查并继续流程':'检查完整流程'" :closable="!busy||checking">
      <div class="run-check">
        <div class="run-links"><RouterLink :to="'/projects/'+uid+'/steps/assemble'">成片版本</RouterLink><RouterLink :to="'/projects/'+uid+'/steps/download#comments-studio'">评论设置</RouterLink><RouterLink :to="'/projects/'+uid+'/publish#project-rules'">发布规则</RouterLink></div>
        <label>本次执行到<n-select v-model:value="mode" :options="WORKFLOW_ENDS" :disabled="busy" @update:value="inspect"/><small class="muted">仅影响本次运行，不修改项目或模板设置。</small></label>
        <div class="version-list"><div v-for="(version,index) in selectedVersions" :key="version.id" class="version-row"><strong>{{ mode==='produce'?'版本 '+(index+1):'P'+(index+1) }} · {{ preview?.recipe.part_titles[version.id]||state?.recipe.part_titles[version.id]||version.name }}</strong><span>{{ versionSummary(version) }}</span></div></div>
        <n-spin v-if="busy&&!preview" size="small" description="正在检查已保存设置和依赖…"/>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <template v-if="preview">
          <DubbingReadiness v-if="preview.dubbing" :summary="preview.dubbing" />
          <section class="run-scope"><h3>执行范围</h3><p>{{ preview.steps.map((key:string)=>preview.labels[key]).join(' → ') }}</p><p v-if="preview.account_name">账号：{{ preview.account_name }} · {{ preview.settings.season_id ? '合集 ID '+preview.settings.season_id : '不加入合集' }}</p><p v-if="mode==='publish'">{{ preview.settings.visibility==='self'?'仅自己可见':'公开可见' }} · {{ preview.settings.publish_at?new Date(preview.settings.publish_at*1000).toLocaleString('zh-CN')+' 定时发布':'完成上传后提交审核' }}</p><p class="muted">{{ mode==='publish'?'确认后连续完成制作、上传和投稿，不再二次询问。':mode==='draft'?'制作完成后准备本地草稿，不上传、不投稿。':'仅生成所列成片，不上传、不投稿。' }}</p></section>
          <n-alert v-if="preview.blockers.length" type="warning" title="还需处理"><div v-for="item in preview.blockers" :key="item">{{ item }}</div></n-alert>
          <details v-if="preview.warnings.length"><summary>运行说明</summary><p v-for="item in preview.warnings" :key="item">{{ item }}</p></details>
        </template>
      </div>
      <template #footer><PublicationRightsHint v-if="mode==='publish'"/><div class="run-row run-actions"><n-button :disabled="busy&&!checking" @click="close">{{ checking?'取消检查':'取消' }}</n-button><n-button :disabled="busy" @click="inspect">重新检查</n-button><n-button type="primary" :loading="busy&&!!preview" :disabled="!canConfirm" @click="confirmStart">{{ selectedRun?'确认继续 · ':'' }}{{ confirmLabel }}</n-button></div></template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.run-actions { margin-top:12px; }
.run-sequence { list-style:none; display:flex; flex-wrap:wrap; gap:6px 12px; padding:0; margin:0; font-size:11px; color:var(--df-muted); }
.run-sequence li:not(:last-child)::after { content:' →'; margin-left:8px; opacity:.5; }
.run-sequence .done { color:#83c8ae; } .run-sequence .current { color:var(--df-accent); font-weight:600; }
.run-card{margin-top:16px;border-color:var(--df-border)}.muted,.version-row span{font-size:12px;color:var(--df-muted)}.run-row,.run-links,.run-policy{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.run-policy strong{font-size:15px;font-weight:600}.run-policy a{margin-left:auto;font-size:12px}.run-description{margin:12px 0 0;line-height:1.8}.run-state{display:grid;gap:14px;border-top:1px solid var(--df-border);margin-top:16px;padding-top:16px}.run-state>a{font-size:12px}.run-progress{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--df-muted)}.run-progress .n-progress{flex:1;max-width:300px}.run-progress>span{white-space:nowrap}.run-check{display:grid;gap:20px;padding:18px 0}.run-check label{display:grid;gap:8px}.run-links{font-size:12px}.version-row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--df-border);padding:12px 0}.version-row strong{font-size:13px}.run-check h3{margin:0;font-size:14px}.run-scope{padding:16px;background:var(--df-panel);border:1px solid var(--df-border);border-radius:8px;font-size:13px;line-height:1.8}.run-scope p:last-child{margin-bottom:0}.run-actions{justify-content:flex-end}.run-card :deep(.n-alert){margin-top:10px}summary{cursor:pointer;font-size:12px}.run-check details{color:var(--df-muted);font-size:12px;line-height:1.8}
@media(max-width:650px){.run-policy a{margin-left:0}.run-card :deep(.n-card-header){flex-wrap:wrap;gap:12px}.run-card :deep(.n-card-header__extra){margin-left:0}.run-actions{gap:8px}}
</style>
