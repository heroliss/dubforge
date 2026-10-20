<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NSpin, NTag, useDialog, useMessage } from 'naive-ui'
import PreviewTimeline from './PreviewTimeline.vue'
import VideoPreview from './VideoPreview.vue'
import ExecutionProgress from './ExecutionProgress.vue'
import ParameterHelp from './ParameterHelp.vue'
import AudioQualityReport from './AudioQualityReport.vue'
import { versionSpec } from '../utils/assemblyVersions'
import { createLatestRefresh, newestVideoFirst } from '../utils/preview'
import { useNavStore } from '../stores/nav'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { visualVersionSpec, visualValues } from '../utils/assemblyAudio'
import { executionForStep } from '../utils/executionProgress'
import { fmtSize } from '../utils/format'
import { previewCreated, previewRange, previewSettingsMatch, previewTaskOwns, stablePreviewValue } from '../utils/assemblyPreview'
import { formatTimecode } from '../utils/timecode'
import TimecodeInput from './TimecodeInput.vue'
const props = defineProps<{ uid:string; version:any; overlayRevision:string; disabled?:boolean; draft?:boolean;
  effectiveValues?:Record<string,any>;outputCuts?:{ranges:Array<{start:number;end:number}>}|null }>()
const nav=useNavStore(),message=useMessage(),dialog=useDialog()
const context=ref<any>({duration:0,subtitles:[],comments:[]}),seconds=ref(0),length=ref(10),contextError=ref('')
const startInvalid=ref(false),lengthInvalid=ref(false)
const image=ref(''),frameError=ref(''),frameLoading=ref(false),rendered=ref(''),scrubbing=ref(false),frameOpen=ref(false)
const submitting=ref(false),submissionUnknown=ref(false),reconciling=ref(false),trialError=ref(''),cancelPending=ref(false),history=ref<any>(null),historyError=ref(''),historyLoading=ref(false),historyReadSerial=ref(0)
const historyOpen=ref(false),historySelection=ref<string[]>([]),selectedName=ref(''),recycling=ref(false),audioReport=ref<any>(null),historyLimit=ref(20)
const requests=ref<Record<string,{jobId:number;signature:string;range:{start:number;end:number}}>>({})
let disposed=false,timer:ReturnType<typeof setTimeout>|undefined,controller:AbortController|undefined,pending=false,confirmDialog:any
const signature=computed(()=>stablePreviewValue([props.uid,visualVersionSpec(props.version),visualValues(props.effectiveValues || {}),props.overlayRevision,props.outputCuts,seconds.value]))
const range=computed(()=>previewRange(seconds.value,length.value,context.value.duration))
const trialSignature=computed(()=>stablePreviewValue([props.uid,versionSpec(props.version),props.effectiveValues,props.overlayRevision,props.outputCuts,range.value]))
const trialApplicable=computed(()=>props.version.dubbing || props.version.burn_subtitles || props.version.include_comments || props.outputCuts?.ranges.length
  || context.value.audio_redaction_required || context.value.audio_redaction_error
  || (props.effectiveValues?.['assemble.final_loudness_mode'] || props.version.overrides?.['assemble.final_loudness_mode'] || 'off')!=='off')
const frameApplicable=computed(()=>props.version.burn_subtitles || props.version.include_comments)
const frameOutdated=computed(()=>!!image.value && rendered.value!==signature.value)
const deletedPosition=computed(()=>props.outputCuts?.ranges.some(row=>seconds.value>=row.start && seconds.value<row.end))
const cues=computed(()=>[
  ...(props.version.burn_subtitles?context.value.subtitles.map((v:any,i:number)=>({...v,label:`字幕 ${i+1}`})):[]),
  ...(props.version.include_comments?context.value.comments.map((v:any,i:number)=>({...v,label:`评论 ${i+1}`})):[]),
])
const fallbackItems=computed(()=>(nav.stepMap.assemble?.artifacts || []).filter((a:any)=>a.exists!==false && a.name.startsWith(`preview_version_${props.version.id}_`)).sort(newestVideoFirst).map((a:any)=>({...a,size:a.disk_size,artifact_id:a.id,metadata_status:'unknown',url:`/api/projects/${props.uid}/steps/assemble/artifacts/${encodeURIComponent(a.name)}/raw?v=${a.sha256 || ''}`})))
const items=computed<any[]>(()=>history.value?.items || fallbackItems.value)
const existing=computed(()=>items.value.filter(item=>item.exists!==false))
const visibleHistory=computed(()=>existing.value.slice(0,historyLimit.value))
const preview=computed(()=>existing.value.find(item=>item.name===selectedName.value) || existing.value[0])
const previewUrl=computed(()=>preview.value?.url || '')
const settingsMatch=computed(()=>previewSettingsMatch(preview.value,props.version,props.effectiveValues || {},range.value,props.outputCuts,props.overlayRevision))
const localRequest=computed(()=>requests.value[props.version.id])
const step=computed(()=>nav.stepMap.assemble)
const stepBusy=computed(()=>['queued','running','stopping'].includes(step.value?.status))
// 运行句柄在终态会清空；仅已结束任务可用持久化执行投影的 job_id 核对错误归属。
const stepJobId=computed(()=>step.value?.job_id ?? (!stepBusy.value && step.value?.execution?.schema===1?step.value.execution.job_id:null))
function sameStepJob(jobId:any){return jobId!=null && stepJobId.value!=null && String(jobId)===String(stepJobId.value)}
const ownTask=computed(()=>{
  const latest=history.value?.latest_task
  const candidate=localRequest.value && sameStepJob(localRequest.value.jobId)?{
    ...(String(latest?.job_id)===String(localRequest.value.jobId)?latest:{}),job_id:localRequest.value.jobId,
    status:step.value.status,scope:'preview',version_ids:[props.version.id],range:localRequest.value.range,
    error_message:step.value.error?.message || (String(latest?.job_id)===String(localRequest.value.jobId)?latest?.error_message:''),
  }:previewTaskOwns(latest,props.version.id)?latest:null
  // 当前步骤错误属于同一个 job 时才能补入修复建议，避免把另一任务的建议附给历史试片。
  if(!candidate || !sameStepJob(candidate.job_id))return candidate
  const cancelled=!stepBusy.value && ['STEP_CANCELLED','STEP_FORCE_STOPPED'].includes(step.value.error?.code)
  const latestEnded=String(latest?.job_id)===String(candidate.job_id) && !['queued','running'].includes(latest?.status)
  return {...candidate,status:cancelled?'cancelled':!stepBusy.value && latestEnded?latest.status:candidate.status,
    error_suggestion:step.value.error?.suggestion || candidate.error_suggestion || ''}
})
const ownActive=computed(()=>stepBusy.value && (previewTaskOwns(history.value?.active_task,props.version.id,step.value?.job_id) || previewTaskOwns(ownTask.value,props.version.id,step.value?.job_id)))
const foreignActive=computed(()=>stepBusy.value && !ownActive.value)
const taskStopped=computed(()=>!ownActive.value && ['stopped','cancelled'].includes(ownTask.value?.status))
const taskExecution=computed(()=>submitting.value?{schema:1,state:submissionUnknown.value?'waiting':'queued',label:submissionUnknown.value?'正在核对试片是否已启动':'正在提交试片任务',detail:'正在核对输入和运行资源，请勿重复点击。'}:
  ownActive.value?executionForStep(step.value):taskStopped.value?{...ownTask.value?.execution,schema:1,state:'cancelled',label:'试片已停止',detail:'',completed:null,total:null}:
  ownTask.value?.execution || (ownTask.value?{schema:1,state:ownTask.value.status,label:ownTask.value.status==='done'?'本次试片已生成':ownTask.value.status==='failed'?'上次试片生成失败':'上次试片已中断',detail:ownTask.value.error_message || ''}:null))
const taskFailed=computed(()=>!ownActive.value && ['failed','dead','interrupted'].includes(ownTask.value?.status))
const taskSuggestion=computed(()=>{
  const value=typeof ownTask.value?.error_suggestion==='string'?ownTask.value.error_suggestion.trim().slice(0,1600):''
  return value!==ownTask.value?.error_message?.trim() && value!==ownTask.value?.execution?.detail?.trim()?value:''
})
const taskDescription=computed(()=>history.value?.active_task?.scope==='preview'?`正在生成${history.value.active_task.version_names?.join('、') || '其他版本'}的试片`:'正在生成正式成片或处理其他合成任务')
const changedDuringRun=computed(()=>ownActive.value && !!localRequest.value && String(localRequest.value.jobId)===String(step.value?.job_id) && localRequest.value.signature!==trialSignature.value)
const actionDisabled=computed(()=>props.disabled || submitting.value || submissionUnknown.value || reconciling.value || stepBusy.value || startInvalid.value || lengthInvalid.value || !context.value.duration || context.value.duration < .2 || range.value.end-range.value.start < .2-1e-9 || !!context.value.audio_redaction_error || !trialApplicable.value)
const resourceWarning=computed(()=>{
  const resources=history.value?.resources,notes=[]
  const gib=1024**3,mib=1024**2
  // 与后端 preflight_render 的轻量预警保持一致；未知数值不警报，也不以C盘状态禁用D盘合成。
  const commitThreshold=Math.max(2*gib,(typeof resources?.commit_limit_bytes==='number'?resources.commit_limit_bytes:0)*.08)
  if(typeof resources?.system_disk_free_bytes==='number' && resources.system_disk_free_bytes<gib)notes.push(`系统盘仅余 ${fmtSize(resources.system_disk_free_bytes)}`)
  if(typeof resources?.temp_disk_free_bytes==='number' && resources.temp_disk_free_bytes<gib)notes.push(`临时目录所在磁盘仅余 ${fmtSize(resources.temp_disk_free_bytes)}`)
  if(typeof resources?.disk_free_bytes==='number' && resources.disk_free_bytes<8*mib)notes.push(`试片所在磁盘仅余 ${fmtSize(resources.disk_free_bytes)}`)
  if(typeof resources?.commit_available_bytes==='number' && resources.commit_available_bytes<commitThreshold)notes.push(`系统可提交内存仅余 ${fmtSize(resources.commit_available_bytes)}`)
  if(typeof resources?.physical_available_bytes==='number' && resources.physical_available_bytes<512*mib)notes.push(`可用物理内存仅余 ${fmtSize(resources.physical_available_bytes)}`)
  return notes.join('；')
})
const selectedForRecycle=computed(()=>existing.value.filter(row=>historySelection.value.includes(row.name) && row.name!==existing.value[0]?.name && row.name!==preview.value?.name))
const selectedBytes=computed(()=>selectedForRecycle.value.reduce((total,row)=>total+(row.size || 0),0))
const reportReader=createLatestRefresh(async signal=>{
  if(!preview.value)return null
  const r=await fetch(preview.value.report_url || `/api/projects/${props.uid}/audio/reports/${encodeURIComponent(preview.value.name)}`,{cache:'no-store',signal:AbortSignal.any([signal,AbortSignal.timeout(15000)])})
  if(!r.ok)return null
  return (await r.json()).report
},report=>{audioReport.value=report},()=>{audioReport.value=null})
watch(previewUrl,()=>{audioReport.value=null;void reportReader.refresh(true)},{immediate:true})
const historyReader=createLatestRefresh(async signal=>{
  historyLoading.value=true
  const r=await fetch(`/api/projects/${props.uid}/assembly-versions/preview-history?version_id=${encodeURIComponent(props.version.id)}`,{cache:'no-store',signal:AbortSignal.any([signal,AbortSignal.timeout(15000)])})
  const data=await r.json();if(!r.ok)throw new Error(data.message || '试片历史读取失败')
  return data
},data=>{
  const oldLatest=history.value?.items?.find((row:any)=>row.exists!==false)?.name || fallbackItems.value[0]?.name
  history.value=data;historyError.value='';historyLoading.value=false;historyReadSerial.value++
  const nextLatest=data.items?.find((row:any)=>row.exists!==false)?.name
  const selectionExists=data.items?.some((row:any)=>row.name===selectedName.value && row.exists!==false)
  if(nextLatest && (!selectionExists || oldLatest && nextLatest!==oldLatest))selectedName.value=nextLatest
  historySelection.value=historySelection.value.filter(name=>data.items?.some((row:any)=>row.name===name && row.exists!==false))
},(e:any)=>{historyLoading.value=false;historyError.value=e?.message || '试片历史读取失败'})
const contextReader=createLatestRefresh(async signal=>{
  const r=await fetch(`/api/projects/${props.uid}/assembly-versions/preview-context`,{cache:'no-store',signal:AbortSignal.any([signal,AbortSignal.timeout(15000)])}),data=await r.json()
  if(!r.ok)throw new Error(data.message || '时间轴读取失败')
  return data
},data=>{context.value=data;contextError.value='';seconds.value=Math.min(seconds.value,Math.max(0,data.duration-.2));schedule()},(e:any)=>{contextError.value=e.message || '时间轴读取失败'})
function schedule(){if(timer)clearTimeout(timer);if(frameOpen.value && frameApplicable.value && !scrubbing.value && !disposed)timer=setTimeout(render,450)}
function beginScrub(){scrubbing.value=true;if(timer)clearTimeout(timer)}
function endScrub(){scrubbing.value=false;schedule()}
async function render(force=false){
  if(disposed || scrubbing.value || !frameOpen.value || !frameApplicable.value || !context.value.duration || props.disabled || stepBusy.value)return
  const stamp=signature.value
  // 轮询和元数据重读不能反复编码同一帧；明确点击重新取帧时才绕过复用。
  if(!force && image.value && rendered.value===stamp)return
  if(frameLoading.value){pending=true;return}
  frameLoading.value=true;frameError.value='';controller=new AbortController()
  try{
    const r=await fetch(`/api/projects/${props.uid}/assembly-versions/frame`,{method:'POST',signal:AbortSignal.any([controller.signal,AbortSignal.timeout(30000)]),headers:{'Content-Type':'application/json'},body:JSON.stringify({version:versionSpec(props.version),seconds:seconds.value,overlay_revision:props.overlayRevision})})
    if(!r.ok)throw new Error((await r.json()).message || '画面预览失败')
    const blob=await r.blob()
    if(!disposed && signature.value===stamp){if(image.value)URL.revokeObjectURL(image.value);image.value=URL.createObjectURL(blob);rendered.value=stamp}
  }catch(e:any){if(!disposed && signature.value===stamp && e.name!=='AbortError')frameError.value=e.message}
  finally{frameLoading.value=false;if(pending && !disposed){pending=false;schedule()}}
}
async function trial(){
  if(actionDisabled.value)return
  submitting.value=true;trialError.value=''
  const uid=props.uid,versionId=props.version.id,stamp=trialSignature.value,requestedRange={...range.value}
  let responseConfirmed=false
  try{
    const r=await fetch(`/api/projects/${uid}/assembly-versions/preview`,{method:'POST',headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(45000),body:JSON.stringify({version:versionSpec(props.version),...requestedRange,overlay_revision:props.overlayRevision})}),result=await r.json()
    if(!r.ok){responseConfirmed=true;throw new Error(result.message || '试片启动失败')}
    if(result.status!=='started'){responseConfirmed=true;throw new Error(result.message || '未能启动试片，请刷新任务状态后重试')}
    responseConfirmed=true
    requests.value[versionId]={jobId:result.job_id,signature:stamp,range:requestedRange}
    nav.stepStarted(uid,'assemble',result);refreshInBackground(()=>nav.poll());void historyReader.refresh(true)
  }catch(e:any){if(!disposed && props.uid===uid){
    if(!responseConfirmed)submissionUnknown.value=true
    if(props.version.id===versionId)trialError.value=!responseConfirmed?'未收到确定的启动结果，正在核对任务。核对完成前不会重复提交。':e.message
    if(!responseConfirmed)await verifySubmission()
    else refreshInBackground(()=>nav.poll())
  }}
  finally{submitting.value=false}
}
async function verifySubmission(){
  if(reconciling.value)return
  const uid=props.uid,previous=historyReadSerial.value
  reconciling.value=true
  try{
    await Promise.all([nav.refreshSteps(true),historyReader.refresh(true)])
    if(!disposed && props.uid===uid && nav.uid===uid && historyReadSerial.value>previous && !historyError.value && !nav.stepsError){
      submissionUnknown.value=false
      if(stepBusy.value)trialError.value=''
      else if(trialError.value.startsWith('未收到确定'))trialError.value='未发现仍在运行的试片，可以重试生成；上次成功结果保留。'
    }
  }finally{reconciling.value=false}
}
async function stop(){
  if(!ownActive.value || cancelPending.value || step.value?.cancelling)return
  const uid=props.uid,versionId=props.version.id,jobId=step.value.job_id;cancelPending.value=true;trialError.value=''
  try{
    const r=await fetch(`/api/projects/${uid}/steps/assemble/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId})}),data=await r.json()
    if(!r.ok)throw new Error(data.message || '停止请求失败')
    if(['not_running','stale_action'].includes(data.status)){cancelPending.value=false;message.info(data.message || '本次试片已经结束')}
    refreshInBackground(()=>nav.poll())
  }catch(e:any){if(!disposed && props.uid===uid && props.version.id===versionId){cancelPending.value=false;trialError.value=e.message || '停止请求失败，请重试'}}
}
function selectHistory(item:any){selectedName.value=item.name;historyOpen.value=false}
function selectRecycle(name:string,checked:boolean){historySelection.value=checked?[...historySelection.value.filter(n=>n!==name),name]:historySelection.value.filter(n=>n!==name)}
function recycle(){
  if(!selectedForRecycle.value.length || recycling.value || stepBusy.value || props.disabled)return
  const rows=selectedForRecycle.value.map(row=>({artifact_id:row.artifact_id ?? null,name:row.name,file_revision:row.file_revision})),uid=props.uid,versionId=props.version.id,keepName=preview.value?.name
  confirmDialog=dialog.warning({title:`将 ${rows.length} 份旧试片移入回收站？`,content:`试片大小约 ${fmtSize(selectedBytes.value)}。保留当前播放与最新试片、正式成片及声音缓存。可从 Windows 回收站还原到原位置；需要释放磁盘空间时，再自行清空回收站。`,positiveText:'移入回收站',negativeText:'取消',onPositiveClick:async()=>{
    if(props.uid!==uid || props.version.id!==versionId || stepBusy.value || props.disabled)return false
    recycling.value=true
    try{
      const r=await fetch(`/api/projects/${uid}/assembly-versions/preview-history/recycle`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true,version_id:versionId,items:rows,keep_name:keepName})}),data=await r.json()
      if(!r.ok)throw new Error(data.message || '试片清理失败')
      if(props.uid===uid && props.version.id===versionId){historyReader.cancel();historyLoading.value=false;history.value=data.history;historySelection.value=[];
        if(data.status==='partial' || data.failed_count){historyError.value=`已回收 ${data.recycled_count} 份，另有 ${data.failed_count || 0} 份未能回收：${(data.failures || []).map((row:any)=>row.message || row.reason || row.name || '文件正在使用').join('；')}`;message.warning('部分试片未能回收，请查看列表提示')}
        else {historyError.value='';message.success(`已将 ${data.recycled_count} 份试片移入回收站`)}
        refreshInBackground(()=>nav.poll())}
    }catch(e:any){if(!disposed && props.uid===uid && props.version.id===versionId)historyError.value=e.message || '试片清理失败';return false}finally{recycling.value=false}
  }})
}
function refreshHistory(){return historyReader.refresh(true)}
watch(signature,schedule)
watch(frameOpen,value=>{if(value)schedule();else{if(timer)clearTimeout(timer);pending=false;controller?.abort()}})
watch(frameApplicable,value=>{if(!value)frameOpen.value=false})
watch(()=>props.disabled,value=>{if(!value)schedule()})
watch(()=>props.overlayRevision,()=>contextReader.refresh(true))
watch(()=>stablePreviewValue(Object.fromEntries(Object.entries(props.effectiveValues || {}).filter(([key])=>key.startsWith('content_review.')))),()=>contextReader.refresh(true))
watch([()=>props.uid,()=>props.version.id],()=>{historyReader.cancel();reportReader.cancel();history.value=null;selectedName.value='';historySelection.value=[];historyLimit.value=20;trialError.value='';cancelPending.value=false;audioReport.value=null;confirmDialog?.destroy();void historyReader.refresh(true)})
watch([()=>step.value?.job_id,()=>step.value?.status,()=>step.value?.progress_phase],()=>{void historyReader.refresh();if(!stepBusy.value){cancelPending.value=false;schedule()}})
onMounted(()=>{void contextReader.refresh();void historyReader.refresh()})
onBeforeUnmount(()=>{disposed=true;contextReader.cancel();historyReader.cancel();reportReader.cancel();confirmDialog?.destroy();if(timer)clearTimeout(timer);controller?.abort();if(image.value)URL.revokeObjectURL(image.value)})
</script>
<template>
  <section class="version-preview" aria-label="试片验证工作区">
    <header class="preview-heading"><div><span class="stage-kicker">步骤 2 · 先验证效果</span><h3>试片验证</h3><p>用一小段检查画面、配音与背景音，再生成整片。</p></div><n-button size="small" secondary @click="historyOpen=true;refreshHistory()">历史试片 <span v-if="existing.length" class="count">{{ existing.length }}</span></n-button></header>
    <div v-if="trialApplicable" class="range-panel">
      <div class="range-heading"><ParameterHelp label="试片范围" help="时间按原片定位。试片使用当前表单的版本参数与已保存的剪辑、评论，不保存版本草稿，也不会替换正式成片。完整声音处理可能先读取整片音轨，后续相同输入会复用缓存。"><strong tabindex="0">选择源视频片段</strong></ParameterHelp><n-tag v-if="draft" size="small" type="warning" :bordered="false">使用未保存调整</n-tag></div>
      <PreviewTimeline v-model:value="seconds" :max="Math.max(0,context.duration-.2)" :cues="cues" :show-markers="false" label="试片开始位置" kind="字幕 / 评论" :disabled="disabled || submitting" @dragstart="beginScrub" @dragend="endScrub" />
      <div class="range-controls"><label>开始<TimecodeInput v-model:value="seconds" :min="0" :max="Math.max(0,context.duration-.2)" :disabled="disabled || submitting" aria-label="试片开始（时分秒）" @invalid-change="startInvalid=$event" /></label><label>长度<TimecodeInput v-model:value="length" :min=".2" :max="60" :disabled="disabled || submitting" aria-label="试片长度（时分秒）" @invalid-change="lengthInvalid=$event" /></label><span class="range-time">{{ formatTimecode(range.start,3) }} → {{ formatTimecode(range.end,3) }}</span></div>
      <small v-if="context.duration>0 && context.duration<.2" class="muted">原片不足 00:00:00.200，无法生成试片。</small>
      <small v-if="outputCuts?.ranges.length" class="muted">已保存剪辑将在试片中生效，删除范围不占成片时长。</small>
      <details v-if="frameApplicable" class="frame-preview" :open="frameOpen" @toggle="frameOpen=($event.target as HTMLDetailsElement).open">
        <summary><span>快速画面预览</span><small>可选 · 只看字幕、评论排版，不含声音</small></summary>
        <div class="frame-content">
          <div class="frame-toolbar">
            <div class="frame-position"><strong>原片 {{ formatTimecode(seconds,3) }}</strong><small>共用上方开始位置，不随下方试片播放。</small></div>
            <n-button size="tiny" :loading="frameLoading" :disabled="frameLoading || disabled || stepBusy" @click="render(true)">重新取帧</n-button>
          </div>
          <small class="frame-state" role="status">{{ frameLoading?'正在更新画面…':frameOutdated?'取景或排版已变，正在等待更新。':image?'取景或排版改变时自动更新。':'选择字幕或评论所在位置后展开预览。' }}</small>
          <img v-if="image" :src="image" :alt="`${version.name} · 原片 ${formatTimecode(seconds,3)} 的字幕与评论排版`" :class="{pending:frameLoading||frameOutdated}" />
          <n-spin v-else-if="frameLoading" size="small" />
          <p v-else class="muted">{{ context.duration?'在上方选择时间，查看这一帧的排版。':'原片就绪后可预览画面。' }}</p>
          <n-alert v-if="frameError" type="warning" :show-icon="false">{{ frameError }}</n-alert>
          <n-alert v-if="deletedPosition" type="info" :show-icon="false">此处已标记删除。画面仍显示原片位置，成片会移除此处。</n-alert>
        </div>
      </details>
      <div class="trial-action"><n-button type="primary" size="large" :disabled="actionDisabled" :loading="submitting || ownActive" @click="trial">{{ ownActive?'正在生成试片':taskFailed?'重试生成试片':previewUrl?'重新生成试片':'生成有声试片' }}</n-button><n-button v-if="ownActive" size="small" secondary type="warning" :loading="cancelPending || step?.cancelling" @click="stop">{{ cancelPending || step?.cancelling?'正在停止':'停止试片' }}</n-button><small>不替换正式成片 · 成功后显示在下方</small></div>
    </div>
    <div class="trial-feedback" role="region" aria-label="本版本试片任务" aria-live="polite"><n-alert v-if="context.audio_redaction_error" type="warning" :show-icon="false">{{ context.audio_redaction_error }} 请先检查本步骤的内容审查规则。</n-alert><n-alert v-if="contextError" type="warning" :show-icon="false">{{ contextError }} <n-button size="tiny" quaternary @click="contextReader.refresh(true)">重试读取范围</n-button></n-alert><n-alert v-if="historyError" type="warning" :show-icon="false">试片状态未能完整读取。 <n-button size="tiny" quaternary @click="refreshHistory">刷新试片状态</n-button></n-alert>
      <ExecutionProgress v-if="taskExecution && !foreignActive" :execution="taskExecution" :percentage="ownActive?step?.progress:null" :estimated="ownActive && step?.progress_estimated" :indeterminate="ownActive && step?.progress_indeterminate" :activity="ownActive?nav.activityFor('assemble'):null" compact />
      <n-alert v-if="foreignActive" type="info" :show-icon="false">{{ taskDescription }}。可播放已有试片，当前表单调整用于下次运行。</n-alert>
      <n-alert v-if="changedDuringRun" type="info" :show-icon="false">运行使用启动时的设置。本页的新调整与位置用于下一次试片。</n-alert>
      <n-alert v-if="trialError || submissionUnknown" :type="submissionUnknown?'warning':'error'" :show-icon="false">{{ trialError || '启动结果尚未确认，请先核对任务状态。' }} <n-button size="tiny" quaternary :loading="reconciling" @click="verifySubmission">{{ submissionUnknown?'核对启动结果':'刷新状态' }}</n-button></n-alert>
      <n-alert v-if="taskFailed && !foreignActive" type="warning" :show-icon="false" class="trial-recovery"><strong>处理建议</strong><p>{{ taskSuggestion || '请根据上方失败原因调整后重试。' }}</p><small v-if="previewUrl">上一次成功试片仍可播放，不会被这次失败替换。</small></n-alert>
      <n-alert v-if="taskStopped && !foreignActive" type="info" :show-icon="false">可重新生成试片。<span v-if="previewUrl">上一次成功试片仍可播放。</span></n-alert>
      <p v-if="resourceWarning" class="resource-warning">{{ resourceWarning }}，可能影响编码稳定性。 <n-button size="tiny" text @click="historyOpen=true;refreshHistory()">查看资源与存储</n-button></p>
    </div>
    <section v-if="trialApplicable || previewUrl" class="trial-result" aria-label="有声试片结果" :aria-busy="submitting || ownActive">
      <div class="result-heading"><div><strong>{{ previewUrl?'可播放试片':'试片结果' }}</strong><small v-if="preview">{{ previewCreated(preview.created_at) }} · {{ fmtSize(preview.size) }}<template v-if="preview.range"> · 原片 {{ formatTimecode(preview.range.start,3) }}–{{ formatTimecode(preview.range.end,3) }}</template></small></div><n-tag v-if="previewUrl" size="small" :type="settingsMatch?'success':'warning'" :bordered="false">{{ settingsMatch?'参数与范围一致':'历史结果' }}</n-tag></div>
      <VideoPreview v-if="previewUrl" :key="previewUrl" :src="previewUrl" />
      <div v-else class="trial-empty"><n-spin v-if="submitting || ownActive" size="medium" /><span v-else class="empty-symbol" aria-hidden="true">▷</span><strong>{{ ownActive?'正在制作这段试片':'还没有有声试片' }}</strong><small>{{ ownActive?'当前阶段与真实进度显示在上方，完成后自动展示。':'选好起点和长度，生成后可在这里试听与查看声音对照。' }}</small></div>
      <small v-if="previewUrl" class="result-note">{{ settingsMatch?'设置和范围与当前表单一致；上游素材是否变化仍需本次生成核对。':'这里保留上一次成功结果。调整参数、字幕、评论或范围后，请重新生成核对。' }}</small>
      <details v-if="previewUrl && audioReport" class="measurement"><summary>声音测量与原片对照 <small>同一片段 · 总体响度与真峰值</small></summary><AudioQualityReport :uid="uid" :report="audioReport" :outdated="!settingsMatch" /></details>
    </section>
    <n-alert v-if="!trialApplicable" type="info" :show-icon="false">此版本直接使用原片，无额外画面或声音处理，不需要生成试片。</n-alert>
    <n-drawer v-model:show="historyOpen" :width="'min(560px, 100vw)'" placement="right"><n-drawer-content title="历史试片与占用空间" closable><div class="history-summary"><strong>{{ existing.length }} 份可播放试片 · {{ fmtSize(history?.total_bytes) }}</strong><n-button size="small" :loading="historyLoading" @click="refreshHistory">刷新</n-button></div><p class="muted">仅当前版本的试片。最新与当前播放项保留；回收站仍占原磁盘，清空后才释放空间。</p><p v-if="history?.output_dir" class="disk-path">保存位置：{{ history.output_dir }}</p><n-alert v-if="historyError" type="warning">{{ historyError }}</n-alert><div v-if="!existing.length && !historyLoading" class="muted">暂无试片。</div><article v-for="(item,index) in visibleHistory" :key="item.name" class="history-row" :class="{chosen:preview?.name===item.name}"><n-checkbox :checked="historySelection.includes(item.name)" :disabled="index===0 || item.name===preview?.name || recycling || stepBusy || disabled || !item.file_revision" :aria-label="`选择清理${previewCreated(item.created_at)}试片`" @update:checked="selectRecycle(item.name,$event)" /><div><strong>{{ previewCreated(item.created_at) }} <n-tag v-if="index===0" size="tiny" :bordered="false" type="success">最新</n-tag><n-tag v-if="preview?.name===item.name" size="tiny" :bordered="false" type="info">当前播放</n-tag></strong><small>{{ fmtSize(item.size) }}<template v-if="item.range"> · {{ formatTimecode(item.range.start,3) }}–{{ formatTimecode(item.range.end,3) }}</template></small><small>{{ item.metadata_status==='ready'?'已保存生成时参数':'历史试片，未记录完整校验信息' }}</small></div><n-button size="small" secondary @click="selectHistory(item)">播放</n-button></article><n-button v-if="existing.length>historyLimit" block secondary size="small" style="margin-top:12px" @click="historyLimit+=20">再显示 {{ Math.min(20,existing.length-historyLimit) }} 份</n-button><details v-if="history?.resources" class="resource-details"><summary>存储与运行资源</summary><dl><div><dt>试片所在磁盘剩余</dt><dd>{{ fmtSize(history.resources.disk_free_bytes) }}</dd></div><div><dt>系统磁盘剩余</dt><dd>{{ fmtSize(history.resources.system_disk_free_bytes) }}</dd></div><div><dt>临时目录所在磁盘剩余</dt><dd>{{ fmtSize(history.resources.temp_disk_free_bytes) }}</dd></div><div><dt>系统可提交内存</dt><dd>{{ fmtSize(history.resources.commit_available_bytes) }}</dd></div></dl><p class="muted">系统可提交内存包含分页文件，物理内存空闲量不能单独代表编码容量。本表仅统计试片，完整声音缓存另存于项目 audio 文件夹。</p></details><template #footer><div class="history-footer"><span>已选 {{ selectedForRecycle.length }} 份 · {{ fmtSize(selectedBytes) }}</span><n-button type="warning" secondary :disabled="!selectedForRecycle.length || recycling || stepBusy || disabled" :loading="recycling" @click="recycle">移入回收站</n-button></div></template></n-drawer-content></n-drawer>
  </section>
</template>
<style scoped>
.range-panel .frame-preview{border:0;border-top:1px solid var(--df-border);border-radius:0;padding:12px 0 0;margin-top:12px}.frame-position{display:flex;flex-direction:column;gap:3px;min-width:0}.frame-position strong{font-weight:500;color:var(--df-text);font-variant-numeric:tabular-nums}.frame-state{display:block;margin-bottom:10px;color:var(--df-muted);font-size:11px}
.trial-recovery p{margin:5px 0;white-space:pre-line}.trial-recovery strong{font-size:12px}.trial-recovery small{color:var(--df-muted);font-size:11px}
.resource-warning{font-size:11px;line-height:1.7;color:var(--df-muted);padding-left:10px;border-left:2px solid var(--df-accent,#e3b76c);margin:12px 0}.resource-details{font-size:12px;color:var(--df-muted);margin-top:20px;padding-top:12px;border-top:1px solid var(--df-border)}.resource-details summary{cursor:pointer}.resource-details dl>div{display:flex;gap:16px;justify-content:space-between;margin-top:10px}.resource-details dd{margin:0;font-variant-numeric:tabular-nums}.version-preview{min-width:0;--df-preview-height:360px}.preview-heading,.range-heading,.range-controls,.trial-action,.result-heading,.frame-toolbar,.history-summary,.history-footer{display:flex;align-items:center;gap:12px}.preview-heading{justify-content:space-between;align-items:flex-start;margin-bottom:18px}.stage-kicker{font-size:11px;color:var(--df-accent,#e3b76c);font-weight:600;letter-spacing:.05em}h3{font-size:19px;margin:5px 0 6px}.preview-heading p,.muted,.result-note,.trial-action small{color:var(--df-muted);font-size:12px;line-height:1.65}.preview-heading p{margin:0}.count{margin-left:6px;font-variant-numeric:tabular-nums}.range-panel{padding:16px;border:1px solid var(--df-border);background:color-mix(in srgb,var(--df-accent,#e3b76c) 4%,transparent);border-radius:12px}.range-heading{justify-content:space-between;font-size:13px}.range-heading :deep(.parameter-help){width:auto}.range-controls{flex-wrap:wrap;margin:10px 0}.range-controls label{display:flex;align-items:center;gap:8px;color:var(--df-muted);font-size:12px}.range-controls :deep(.timecode-input){width:154px}.range-time{margin-left:auto;color:var(--df-muted);font-size:12px;font-variant-numeric:tabular-nums}.trial-action{flex-wrap:wrap;margin-top:16px}.trial-action :deep(.n-button--large-type){min-width:165px}.trial-action small{flex:1;min-width:150px}.trial-feedback:empty{display:none}.trial-feedback{margin-top:14px}.trial-feedback :deep(.n-alert){margin:8px 0}.trial-result{margin-top:18px;border:1px solid var(--df-border);border-radius:12px;overflow:hidden;background:color-mix(in srgb,var(--df-accent,#e3b76c) 2%,transparent)}.result-heading{justify-content:space-between;padding:14px 16px}.result-heading strong{font-size:13px}.result-heading small{display:block;color:var(--df-muted);font-size:11px;line-height:1.8;margin-top:3px}.trial-result :deep(.video-preview){margin:0;border-radius:0}.trial-empty{min-height:220px;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;padding:20px;text-align:center;background:#0002;box-sizing:border-box}.trial-empty strong{font-size:13px;font-weight:500}.trial-empty small{max-width:350px;color:var(--df-muted);font-size:12px;line-height:1.7}.empty-symbol{font-size:40px;font-weight:300;color:var(--df-muted);line-height:1}.result-note{display:block;margin:12px 16px}.measurement{border-top:1px solid var(--df-border);padding:12px 16px}.measurement summary,.frame-preview summary{font-size:12px;cursor:pointer;line-height:1.8}.measurement summary small,.frame-preview summary small{font-size:11px;color:var(--df-muted);margin-left:8px}.frame-preview{border:1px solid var(--df-border);border-radius:10px;padding:12px 14px;margin-top:16px}.frame-content{padding-top:12px}.frame-toolbar{justify-content:space-between;color:var(--df-muted);font-size:11px;margin-bottom:12px}.frame-content img{width:100%;max-height:360px;object-fit:contain;border-radius:6px;background:#111}.pending{opacity:.6}.history-summary{justify-content:space-between;font-size:13px}.disk-path{font-size:11px;color:var(--df-muted);overflow-wrap:anywhere;padding:10px;background:var(--df-accent-soft);border-radius:6px}.history-row{display:flex;gap:12px;align-items:center;border:1px solid var(--df-border);border-radius:9px;padding:12px;margin-top:10px}.history-row.chosen{background:var(--df-accent-soft)}.history-row>div{flex:1;min-width:0}.history-row strong{display:flex;align-items:center;flex-wrap:wrap;gap:6px;font-size:12px}.history-row small{display:block;margin-top:5px;color:var(--df-muted);font-size:11px}.history-footer{justify-content:space-between;width:100%;font-size:12px}.frame-preview summary:focus-visible{outline:2px solid var(--df-accent,#e3b76c);outline-offset:4px;border-radius:3px}@media(max-width:650px){.range-panel{padding:12px}.range-time{width:100%;margin-left:0}.preview-heading{gap:8px}.preview-heading :deep(.n-button){font-size:11px}.trial-action>small{flex-basis:100%}.trial-empty{min-height:170px}.result-heading{align-items:flex-start}.measurement summary small,.frame-preview summary small{display:block;margin-left:0}}
</style>
