<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import { NAlert, NButton, NCard, NInput, NSelect, NSpin, NSwitch, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import PublishSettingsForm from './PublishSettingsForm.vue'
import PublishCopyAssistant from './PublishCopyAssistant.vue'
import ProjectDeliverySettings from './ProjectDeliverySettings.vue'
import TextColorPresets from './TextColorPresets.vue'
import DubbingPresets from './DubbingPresets.vue'
import { indexTtsParameterGroups } from '../utils/dubbingPreset'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { scopeSource, type ConfigurationSource } from '../utils/configurationSource'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups, sourceStyleFieldVisible } from '../utils/textStyleFields'
import { PROJECT_COPY_FIELDS, resolveProjectCopy, copyRestoreLabels, type ProjectCopyPreview } from '../utils/projectPublishCopy'
import { useConfigurationEditability } from '../utils/configurationEditability'
import ConfigurationLockNotice from './ConfigurationLockNotice.vue'
import ParameterHelp from './ParameterHelp.vue'
import ParameterLabel from './ParameterLabel.vue'
import ParameterResetButton from './ParameterResetButton.vue'
import { parameterHelp, parameterDependencyMatches } from '../utils/parameterPresentation'
import { indexTtsFieldHelp, indexTtsFieldInactiveReason, indexTtsFieldInactiveSummary } from '../utils/indexTtsSettings'
import { assemblyVersionFieldSource } from '../utils/assemblyInheritance'
import { audioSettingGroups, audioFieldHelp } from '../utils/audioQuality'
import { encodingFieldHelp } from '../utils/assemblyEncoding'
import { isAudioSetting } from '../utils/assemblyAudio'
import { configurationStageKey, configurationSettingsKey, configurationStageSections } from '../utils/configurationStages'
const props=defineProps<{scope:string;sections?:string[];paths?:string[];stage?:string}>()
const editability=useConfigurationEditability(()=>props.scope)
const emit=defineEmits<{'saved':[data:any];'dirty-change':[dirty:boolean];'busy-change':[busy:boolean];'stage-change':[stage:string]}>()
const message=useMessage(),data=ref<any>(null),patch=ref<Record<string,any>>({}),saved=ref(''),name=ref(''),busy=ref(false),error=ref(''),tab=ref(''),catalog=ref<any>({accounts:[]})
const metadata=ref<any>({}),metadataLoading=ref(false),metadataError=ref(''),metadataReady=ref(false)
const copyPreview=ref<ProjectCopyPreview|null>(null),copyLoading=ref(false),copyError=ref(''),copyBusy=ref(false)
let copyGeneration=0,copyAbort:AbortController|undefined
let disposed=false,generation=0,metadataGeneration=0
const clone=(v:any)=>JSON.parse(JSON.stringify(v))
const dirty=computed(()=>!!data.value&&(JSON.stringify(patch.value)!==saved.value||name.value!==data.value.name))
const targetSelection=computed(()=>{
 const selection=data.value?.target_selection
 if(!props.scope.startsWith('project:')||!selection||typeof selection.has_history!=='boolean')return undefined
 if(Object.hasOwn(patch.value,'publish.project_target')){
  if(selection.source==='invalid'&&!fieldChanged('publish.project_target'))return selection
  return {...selection,source:patch.value['publish.project_target']?'project':'explicit_unset',explicit:true}
 }
 return {...selection,source:selection.has_history?'history_requires_choice':'first_publish',explicit:false}
})
const values=computed(()=>{
 const current={...data.value?.defaults,...patch.value}
 // 首次发布是按项目历史解析的有效目标，不把默认值写成项目覆盖。
 if(targetSelection.value&&!Object.hasOwn(patch.value,'publish.project_target'))current['publish.project_target']=targetSelection.value.source==='first_publish'?{kind:'new'}:null
 if(targetSelection.value?.source==='invalid')current['publish.project_target']=null
 return current
})
const presetBusy=ref(false),loading=ref(false),deliveryBusy=ref(false)
const pending=computed(()=>busy.value||presetBusy.value||copyBusy.value||deliveryBusy.value)
// 生成候选时仍可编辑；保存与离开由 pending 保护，候选会检查编辑版本。
const locked=computed(()=>loading.value||busy.value||presetBusy.value||data.value?.archived||editability.locked.value)
const pathLocked=(path:string)=>editability.isPathLocked(path)
const pathDisabled=(path:string)=>locked.value||pathLocked(path)
const anyPathLocked=(...paths:string[])=>paths.some(pathLocked)
const projectUid=computed(()=>props.scope.startsWith('project:')?props.scope.slice(8):'')
const scopeLabel=computed(()=>props.scope==='global'?'全局默认':props.scope.startsWith('series:')?'系列模板':'项目设置')
const resetLabel=computed(()=>props.scope==='global'?'撤销修改':projectUid.value?'恢复项目基础值':'恢复继承')
const resetHint=computed(()=>props.scope==='global'?'恢复为当前已保存的全局值，保留其他修改；不会自动保存。':projectUid.value?'移除项目单独设置，恢复创建或上次应用时保留的基础值；保存后生效。':'移除模板的单独设置，重新跟随全局默认；保存后生效。')
const changedFields=computed(()=>[...new Set([...Object.keys(data.value?.overrides||{}),...Object.keys(patch.value)])].filter(fieldChanged))
const sections=computed(()=>configurationStageSections(data.value?.sections,props.sections,props.paths))
const stageSections=computed(()=>sections.value.filter(([,section])=>section.category==='stage'))
const sharedSections=computed(()=>sections.value.filter(([,section])=>section.category==='shared'))
function selectTab(key:string){if(pending.value)return;tab.value=key;emit('stage-change',configurationStageKey(key)||key)}
function adoptStage(){const key=configurationSettingsKey(props.stage);if(key&&sections.value.some(([candidate])=>candidate===key))tab.value=key;else if(!sections.value.some(([candidate])=>candidate===tab.value))tab.value=sections.value[0]?.[0]||''}
const basePublishValues=computed(()=>Object.fromEntries(Object.entries(values.value).filter(([k])=>k.startsWith('publish.')).map(([k,v])=>[k.slice(8),v])))
const publishOverrides=computed(()=>Object.fromEntries(Object.entries(patch.value).filter(([k])=>k.startsWith('publish.')).map(([k,v])=>[k.slice(8),v])))
const publishResetOverrides=computed(()=>props.scope==='global'?Object.fromEntries(Object.entries(publishOverrides.value).filter(([key])=>fieldChanged('publish.'+key))):publishOverrides.value)
const copyContextChanged=computed(()=>!!copyPreview.value&&!!data.value&&(copyPreview.value.revision!==data.value.revision||copyPreview.value.fingerprint!==data.value.fingerprint))
const usableCopyPreview=computed(()=>copyContextChanged.value?null:copyPreview.value)
const resolvedPublish=computed(()=>resolveProjectCopy(basePublishValues.value,publishOverrides.value,
 Object.fromEntries(Object.keys(basePublishValues.value).map(k=>[k,fieldSource('publish.'+k)])),projectUid.value?usableCopyPreview.value:null))
const publishValues=computed(()=>resolvedPublish.value.values)
const publishSources=computed(()=>resolvedPublish.value.sources)
const restoreLabels=computed(()=>copyRestoreLabels(usableCopyPreview.value))
const platformFields=['human_type2','copyright','declaration_id','visibility','season_id','section_id','publish_at',
 'chapters_enabled','chapters_translate','chapters_permanent','chapters_instruction']
const accountOptions=computed(()=>catalog.value.accounts.map((a:any)=>({label:a.name,value:a.id})))
const publishedVersions=computed(()=>(values.value['publish.version_ids']??values.value['assembly.versions']?.map((v:any)=>v.id)??[]).map((id:string)=>values.value['assembly.versions'].find((v:any)=>v.id===id)).filter(Boolean))
const partOrderNotice=ref('')
const partOrderDisabled=computed(()=>pathDisabled('publish.version_ids')||pathLocked('assembly.versions'))
function movePublishedVersion(id:string,delta:number){
 if(partOrderDisabled.value||(delta!==-1&&delta!==1))return
 const ids=[...(values.value['publish.version_ids']??values.value['assembly.versions']?.map((v:any)=>v.id)??[])]
 const index=ids.indexOf(id),next=index+delta
 if(index<0||next<0||next>=ids.length)return
 // 只调整发布选择顺序，版本定义和以版本ID绑定的分P名称保持对应。
 ;[ids[index],ids[next]]=[ids[next],ids[index]]
 change('publish.version_ids',ids)
 const version=publishedVersions.value.find((row:any)=>row.id===id)
 partOrderNotice.value=`已将${version?.name||'此版本'}移至 P${next+1}，保存设置后生效。`
}
function copyBusyChanged(value:boolean){
 const finished=copyBusy.value&&!value;copyBusy.value=value
 // 只读核对本次生成占用是否已释放，不假定取消等待等于后台任务结束。
 if(finished&&!disposed&&editability.observing.value)void editability.refresh()
}
function partTitle(id:string,value:string){const titles={...values.value['publish.part_titles']};if(value.trim())titles[id]=value;else delete titles[id];change('publish.part_titles',titles)}
function savedPartTitles():Record<string,string>{return data.value?.overrides?.['publish.part_titles']??data.value?.defaults?.['publish.part_titles']??{}}
function partTitleChanged(id:string){const previous=savedPartTitles(),current=values.value['publish.part_titles']??{};return Object.hasOwn(previous,id)!==Object.hasOwn(current,id)||previous[id]!==current[id]}
function restorePartTitle(id:string){
 if(pathDisabled('publish.part_titles')||!partTitleChanged(id))return
 const previous=savedPartTitles(),titles={...values.value['publish.part_titles']}
 if(Object.hasOwn(previous,id))titles[id]=previous[id];else delete titles[id]
 const sameKeys=Object.keys(titles).length===Object.keys(previous).length&&Object.keys(previous).every(key=>titles[key]===previous[key])
 // 最后一项也撤销后回到原本的继承状态，避免留下同值对象覆盖。
 if(!Object.hasOwn(data.value?.overrides||{},'publish.part_titles')&&sameKeys)delete patch.value['publish.part_titles']
 else change('publish.part_titles',titles)
}
function fieldSource(key:string):ConfigurationSource|undefined{return Object.hasOwn(patch.value,key)?scopeSource(props.scope,name.value):data.value?.parent_sources?.[key]}
const versionDefaultSources=computed(()=>Object.fromEntries(Object.keys(values.value).map(path=>[path,fieldSource(path)])))
function versionFieldSource(version:any,path:string):ConfigurationSource|undefined{
 const persisted=(data.value?.overrides?.['assembly.versions']??data.value?.defaults?.['assembly.versions']??[]).find((row:any)=>row.id===version.id)
 return assemblyVersionFieldSource(version,path,versionDefaultSources.value,persisted,fieldSource('assembly.versions'))
}
function fieldResetLabel(key:string){return projectUid.value&&!data.value?.parent_sources?.[key]?.frozen?'恢复服务设置':resetLabel.value}
function fieldResetHint(key:string){return projectUid.value&&!data.value?.parent_sources?.[key]?.frozen?'移除项目单独设置，恢复共享服务的当前设置，用于之后的调用；保存后生效。':resetHint.value}
function change(key:string,value:any){if(!locked.value&&!pathLocked(key)){patch.value[key]=value;if(key==='publish.version_ids')partOrderNotice.value=''}}
function fieldChanged(key:string){
 const previous=data.value?.overrides||{},had=Object.hasOwn(previous,key),has=Object.hasOwn(patch.value,key)
 return had!==has||(has&&JSON.stringify(previous[key])!==JSON.stringify(patch.value[key]))
}
function canReset(key:string){return props.scope==='global'?fieldChanged(key):Object.hasOwn(patch.value,key)}
function inherit(key:string){
 if(locked.value||pathLocked(key))return
 if(key==='publish.version_ids')partOrderNotice.value=''
 // 全局是根设置：单项撤销只恢复已保存值，不移除原有的全局配置。
 if(props.scope==='global'&&Object.hasOwn(data.value?.overrides||{},key))patch.value[key]=clone(data.value.overrides[key])
 else delete patch.value[key]
}
function visibleFields(section:any){
 const useLlm=Boolean(values.value['split.use_llm']??true)
 return section.fields.filter((f:any)=>{
  if(f.path==='split.target_word_count')return useLlm
  if(f.path==='split.max_split_length')return !useLlm
  return !(f.depends_on||[]).some(([k,v]:any)=>k==='tts.backend'&&values.value[k]!==v)
 })
}
function fieldDisabled(field:any,fieldValues:Record<string,any>=values.value){
 const splitLength=['split.target_word_count','split.max_split_length'].includes(field.path)
 return locked.value||pathLocked(field.path)||!!indexTtsFieldInactiveReason(field.path,fieldValues)||(field.depends_on||[]).some(([k,v]:any)=>!parameterDependencyMatches(splitLength&&k==='split.use_llm'?Boolean(fieldValues[k]??true):fieldValues[k],v))
}
function fieldHelp(field:any,fieldValues:Record<string,any>=values.value){return parameterHelp(field,encodingFieldHelp({...field,help:audioFieldHelp({...field,help:indexTtsFieldHelp(field.path,field.help,fieldValues)},fieldValues,'configuration')},fieldValues,'configuration'))}
function versionValues(version:any){return {...values.value,...version.overrides}}
function styleGroups(section:any,key:any){
 // 章节开关与来源统一交给下方发布表单，避免 schema 字段重复呈现。
 const fields=visibleFields(section).filter((field:any)=>key!=='publication'||!field.path.startsWith('publish.chapters_')
  && field.path!=='publish.project_target' && !(projectUid.value && field.path==='run.mode'))
 if(key==='assemble'){
  const audio=fields.filter((field:any)=>isAudioSetting(field.path))
  return [...audioSettingGroups(audio),...textStyleFieldGroups(fields.filter((field:any)=>!isAudioSetting(field.path)),'assemble.subtitle_',values.value,'整体设置')]
 }
 if(key==='tts')return [...indexTtsParameterGroups(fields.filter((field:any)=>!field.path.startsWith('subtitles.'))),
  {name:'字幕导出与时间',fields:fields.filter((field:any)=>field.path.startsWith('subtitles.'))}].filter(group=>group.fields.length)
 return key==='comments'?[{name:'采集与评分',fields:fields.filter((field:any)=>['comments.mode','comments.limit','comments.prompt','comments.failure_policy'].includes(field.path))},
  ...textStyleFieldGroups(fields.filter((field:any)=>!['comments.mode','comments.limit','comments.prompt','comments.failure_policy'].includes(field.path)),'comments.',values.value,'上屏与布局')].filter(group=>group.fields.length)
  :textStyleFieldGroups(fields,key==='assemble'?'assemble.subtitle_':key==='comments'?'comments.':'',values.value,'整体设置')
}
function presetDisabled(section:any){return locked.value||pathLocked('tts')||!!indexTtsParameterGroups(section.fields).find(group=>group.name==='预设参数')?.fields.some((field:any)=>pathLocked(field.path))}
function versionFields(version:any){return data.value.version_fields.filter((f:any)=>(f.group==='字幕'?version.burn_subtitles:f.group==='评论'?version.include_comments:true)&&sourceStyleFieldVisible(f,{...values.value,...version.overrides}))}
async function request(url:string,body?:any,method=body?'PUT':'GET',signal?:AbortSignal){
 const r=await fetch(url,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,signal});const d=await r.json();if(!r.ok)throw new Error(d.message||'设置操作失败');return d
}
function adopt(d:any){data.value=d;name.value=d.name;patch.value=clone(d.overrides);saved.value=JSON.stringify(patch.value);partOrderNotice.value='';adoptStage()}
async function load(){const n=++generation;copyGeneration++;copyAbort?.abort();copyPreview.value=null;copyLoading.value=false;copyError.value='';loading.value=true;error.value='';try{const d=await request(`/api/configuration/${encodeURIComponent(props.scope)}`);if(disposed||n!==generation)return;adopt(d);void loadCopyPreview();if(!props.sections||props.sections.includes('publication')){const list=await request('/api/publishing/catalog');if(!disposed&&n===generation)catalog.value=list}}catch(e:any){if(n===generation)error.value=e.message}finally{if(n===generation)loading.value=false}}
async function save(){if(locked.value||pending.value||!dirty.value)return;busy.value=true;error.value='';const key=props.scope,ticket=generation;try{if(editability.observing.value&&!await editability.canEdit()){error.value=editability.reason.value||'运行中不能保存设置';return}if(disposed||key!==props.scope||ticket!==generation)return;
 const updates=Object.fromEntries(Object.entries(patch.value).filter(([field])=>fieldChanged(field)))
 const reset=Object.keys(data.value.overrides).filter(field=>!Object.hasOwn(patch.value,field)&&fieldChanged(field))
 const d=await request(`/api/configuration/${encodeURIComponent(key)}`,{revision:data.value.revision,fingerprint:data.value.fingerprint,updates,reset,...(key.startsWith('series:')?{name:name.value}:{})});if(disposed||key!==props.scope||ticket!==generation)return;adopt(d);copyGeneration++;copyAbort?.abort();copyLoading.value=false;void loadCopyPreview();emit('saved',d);message.success(key.startsWith('project:')?'已保存项目设置；不会自动运行':'已保存设置；已有项目设置保持不变')}catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}finally{if(ticket===generation)busy.value=false}}
async function loadCopyPreview(){
 if(disposed||!projectUid.value||!data.value||(!props.sections?.includes('publication')&&tab.value!=='publication')||copyLoading.value)return
 const uid=projectUid.value,ticket=++copyGeneration,controller=new AbortController(),revision=data.value.revision,fingerprint=data.value.fingerprint;copyAbort=controller
 copyLoading.value=true;copyError.value=''
 try{
  const d=await request(`/api/publishing/projects/${encodeURIComponent(uid)}/copy-preview`,undefined,'GET',AbortSignal.any([controller.signal,AbortSignal.timeout(30000)]))
  if(disposed||ticket!==copyGeneration||uid!==projectUid.value||controller.signal.aborted||revision!==data.value?.revision||fingerprint!==data.value?.fingerprint)return
  if(d.project_uid!==uid||!Number.isInteger(d.revision)||typeof d.fingerprint!=='string'||typeof d.content_fingerprint!=='string'||!d.content||!Array.isArray(d.parent_configured))throw new Error('基础资料预览不完整，请重新读取。')
  copyPreview.value=d
 }catch(e:any){if(!disposed&&ticket===copyGeneration&&!controller.signal.aborted)copyError.value=e.name==='TimeoutError'?'基础资料读取超时，请重试；当前修改已保留。':e.message||'基础资料读取失败，请重试。'}
 finally{if(ticket===copyGeneration){copyLoading.value=false;copyAbort=undefined}}
}
function applyCopy(input:Record<string,any>){for(const key of Object.keys(input))if(PROJECT_COPY_FIELDS.includes(key)&&key!=='source')change('publish.'+key,input[key])}
function retainCopy(){if(locked.value||!usableCopyPreview.value)return;for(const key of PROJECT_COPY_FIELDS.filter(k=>k!=='source'))change('publish.'+key,clone(publishValues.value[key]??(key==='tags'?[]:'')));message.info('当前标题、简介、标签与动态已设为项目值，包含留空项；保存后自动流程不会改写这四项。')}
async function loadMetadata(){const n=++metadataGeneration,account=values.value['publish.account_id'];metadata.value={};metadataReady.value=false;metadataError.value='';metadataLoading.value=false;if(!account||!props.sections?.includes('publication')&&tab.value!=='publication')return;metadataLoading.value=true;try{const d=await request(`/api/publishing/accounts/${account}/metadata`,{},'POST');if(n===metadataGeneration&&!disposed){metadata.value=d;metadataReady.value=true}}catch(e:any){if(n===metadataGeneration)metadataError.value=e.message}finally{if(n===metadataGeneration)metadataLoading.value=false}}
function collectionCreated(result:any){if(result.account_id!==values.value['publish.account_id']||result.context_key!==props.scope)return;change('publish.season_id',result.season_id);change('publish.section_id',result.section_id??null);void loadMetadata()}
async function cover(event:Event){const input=event.target as HTMLInputElement,file=input.files?.[0];input.value='';if(!file||locked.value)return;const ticket=generation;busy.value=true;error.value='';try{if(editability.observing.value&&!await editability.canEdit()){error.value=editability.reason.value;return}if(disposed||ticket!==generation)return;const body=new FormData();body.append('file',file);const r=await fetch('/api/publishing/covers',{method:'POST',body}),d=await r.json();if(!r.ok)throw new Error(d.message||'上传封面失败');if(!disposed&&ticket===generation)patch.value['publish.cover_id']=d.cover_id}catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}finally{if(ticket===generation)busy.value=false}}
// 预设只填入当前编辑范围内的字段，不复制继承值，也不暗中保存。
function applyPreset(input:Record<string,any>){
 if(locked.value)return
 const allowed=new Set(sections.value.flatMap(([,section]:any)=>section.fields.map((f:any)=>f.path)))
 for(const [key,value] of Object.entries(input))if(allowed.has(key)&&!pathLocked(key))patch.value[key]=clone(value)
}
function preset(id:string,prefix:string){if(locked.value)return;const p=textColorPatch(id,prefix);if(p)applyPreset(p)}
function enableSourceStyle(prefix:string){if(!locked.value)applyPreset({[prefix+'source_style_enabled']:true})}
function editVersion(index:number,key:string,value:any){const versions=clone(values.value['assembly.versions']);versions[index][key]=value;change('assembly.versions',versions)}
type VersionDefinitionKey='name'|'dubbing'|'burn_subtitles'|'include_comments'
function savedVersion(index:number){const id=values.value['assembly.versions']?.[index]?.id;return (data.value?.overrides?.['assembly.versions']??data.value?.defaults?.['assembly.versions']??[]).find((version:any)=>version.id===id)}
function versionDefinitionChanged(index:number,key:VersionDefinitionKey){const previous=savedVersion(index);return !!previous&&values.value['assembly.versions'][index][key]!==previous[key]}
function restoreVersionDefinition(index:number,key:VersionDefinitionKey){if(pathDisabled('assembly.versions')||!versionDefinitionChanged(index,key))return;editVersion(index,key,savedVersion(index)[key])}
function editVersionOverride(index:number,key:string,value:any,reset=false){if(pathDisabled('assembly.versions')||pathLocked(key))return;const versions=clone(values.value['assembly.versions']);if(reset)delete versions[index].overrides[key];else versions[index].overrides[key]=value;change('assembly.versions',versions)}
function addVersion(){change('assembly.versions',[...clone(values.value['assembly.versions']),{id:'v-'+crypto.randomUUID(),name:'新版本',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}}])}
function removeVersion(index:number){change('assembly.versions',values.value['assembly.versions'].filter((_:any,i:number)=>i!==index))}
function moveVersion(index:number,delta:number){const list=clone(values.value['assembly.versions']);[list[index],list[index+delta]]=[list[index+delta],list[index]];change('assembly.versions',list)}
watch(dirty,v=>emit('dirty-change',v),{flush:'sync'});watch(pending,v=>emit('busy-change',v),{flush:'sync'});watch(()=>props.scope,load,{immediate:true});watch(()=>[values.value['publish.account_id'],tab.value],loadMetadata);watch(tab,()=>{void loadCopyPreview()})
watch(()=>props.stage,adoptStage);watch(()=>sections.value.map(([key])=>key).join('|'),adoptStage)
onUnmounted(()=>{disposed=true;generation++;metadataGeneration++;copyGeneration++;copyAbort?.abort()})
defineExpose({dirty,save})
</script>
<template>
 <div class="configuration-form">
  <ConfigurationLockNotice v-if="editability.locked.value" :reason="editability.reason.value"
                           :refresh="editability.refresh" />
  <n-alert v-else-if="editability.lockedPaths.value.length" type="info" :show-icon="false" class="step-lock-note">
   当前正在运行{{ editability.runningSteps.value.map((step:any)=>`「${step.label || step.step_key}」`).join('、') }}；相关设置已冻结，其他步骤设置仍可编辑。
  </n-alert>
  <n-alert v-if="error" type="error">{{ error }} <n-button text :disabled="loading||pending||dirty" @click="load">重新读取</n-button><small v-if="dirty"> · 修改已保留；放弃修改后可重新读取。</small></n-alert>
  <div v-if="!data && loading" class="loading"><n-spin size="small"/> 读取设置…</div>
  <template v-if="data">
   <div class="scope-line"><ConfigurationSourceBadge :source="scopeSource(scope,name)"/><strong v-if="scope.startsWith('series:')">{{ name }}</strong><small>{{ scope==='global'?'新项目的默认起点；系列未覆盖项跟随这里':projectUid?'未单独设置的字段使用本项目保存的基础值':'新项目应用此模板；模板未设置项跟随全局默认' }}</small><span v-if="dirty" class="dirty">未保存</span></div>
   <p v-if="scope==='global'" class="note">全局没有上级设置。单项“撤销修改”回到当前已保存的全局值；保存后，新值成为下次撤销的基准。</p>
   <p v-if="scope==='global'||scope.startsWith('series:')" class="note">保存供新项目使用，已有项目的基础值与单独设置保持不变。需要更新已有项目时，在项目总览或列表中主动应用最新设置，先查看变化及影响再确认。</p>
   <p v-else-if="projectUid" class="note">制作参数保留创建或上次应用时的基础值；恢复项目基础值只移除项目覆盖，不读取最新全局或模板。连接、代理、等待超时等共享服务设置用于之后的调用；修改实际制作参数后，相关结果才可能需更新。</p>
   <n-input v-if="scope.startsWith('series:')" v-model:value="name" :disabled="locked" maxlength="80" :input-props="{'aria-label':'系列模板名称'}" style="max-width:320px;margin-bottom:16px"/>
   <n-alert v-if="data.archived" type="warning">模板已归档；已有项目保留原基础值，新项目不能选择。</n-alert>
   <div v-if="sections.length>1" class="configuration-navigation">
    <nav v-if="stageSections.length" class="config-tabs" aria-label="制作流程设置"><button v-for="([key,section]) in stageSections" :key="key" type="button" :disabled="pending" :aria-pressed="tab===key" :class="{selected:tab===key}" @click="selectTab(key)"><span class="stage-number" aria-hidden="true">{{ section.stageNumber }}</span>{{ section.label }}</button></nav>
    <nav v-if="sharedSections.length" class="config-tabs shared-tabs" aria-label="跨流程通用规则"><span class="shared-label">跨流程通用规则</span><button v-for="([key,section]) in sharedSections" :key="key" type="button" :disabled="pending" :aria-pressed="tab===key" :class="{selected:tab===key}" @click="selectTab(key)">{{ section.label }}</button></nav>
   </div>
   <div>
    <section v-for="([key,section]) in sections.filter(([key])=>key===tab)" :key="key">
     <header v-if="sections.length>1" class="configuration-stage-heading"><small>{{ section.category==='stage'?`阶段 ${section.stageNumber}`:'跨流程通用规则' }}</small><h2>{{ section.label }}</h2></header>
     <template v-if="key==='assemble'">
      <div class="list-heading"><h3>成片版本 <ConfigurationSourceBadge :source="fieldSource('assembly.versions')"/></h3><ParameterResetButton label="成片版本清单" :action="scope==='global'?'撤销清单修改':projectUid?'恢复项目基础清单':'恢复继承清单'" :help="resetHint" :disabled="!canReset('assembly.versions')||pathDisabled('assembly.versions')" @reset="inherit('assembly.versions')"/><n-button size="small" :disabled="locked||pathLocked('assembly.versions')" @click="addVersion">新增版本</n-button></div>
      <n-card v-for="(version,index) in values['assembly.versions']" :key="version.id" size="small" class="version-card">
       <div class="version-heading"><n-input :value="version.name" :disabled="locked||pathLocked('assembly.versions')" :input-props="{'aria-label':'版本名称'}" @update:value="editVersion(Number(index),'name',$event)"/><ParameterResetButton label="版本名称" action="撤销本项修改" help="恢复此版本已保存的名称；其他调整保留" :disabled="pathDisabled('assembly.versions')||!versionDefinitionChanged(Number(index),'name')" @reset="restoreVersionDefinition(Number(index),'name')"/><n-button size="tiny" :disabled="locked||pathLocked('assembly.versions')||index===0" @click="moveVersion(Number(index),-1)">↑</n-button><n-button size="tiny" :disabled="locked||pathLocked('assembly.versions')||index===values['assembly.versions'].length-1" @click="moveVersion(Number(index),1)">↓</n-button><n-button size="tiny" :disabled="locked||pathLocked('assembly.versions')||values['assembly.versions'].length<2" @click="removeVersion(Number(index))">移除</n-button></div>
       <div class="version-origin"><ConfigurationSourceBadge :source="fieldSource('assembly.versions')"/><span>声音、字幕与评论开关来自整份版本清单</span></div><div class="dimensions"><div v-for="(label,k) in {dubbing:'配音',burn_subtitles:'烧录字幕',include_comments:'叠加评论'}" :key="k" class="dimension-setting" role="group" :aria-label="label"><span>{{ label }}<ParameterResetButton :label="label" action="撤销本项修改" :help="`恢复此版本已保存的${label}开关；其他调整保留`" :disabled="pathDisabled('assembly.versions')||!versionDefinitionChanged(Number(index),k)" @reset="restoreVersionDefinition(Number(index),k)"/></span><n-switch :value="version[k]" :disabled="locked||pathLocked('assembly.versions')" :aria-label="label" @update:value="editVersion(Number(index),k,$event)" /></div></div>
       <details><summary>本版本呈现差异 · {{ Object.keys(version.overrides).length }} 项</summary><div class="field-grid"><div v-for="field in versionFields(version)" :key="field.path" class="config-field" role="group" :aria-label="`${field.group} · ${field.label}`"><ParameterHelp :help="fieldHelp(field,versionValues(version))" :label="field.label"><span class="field-heading">{{ field.group }} · {{ field.label }}<small><ConfigurationSourceBadge :source="versionFieldSource(version,field.path)" :inherited="!Object.hasOwn(version.overrides,field.path)" /><ParameterResetButton :label="field.label" :action="projectUid ? '恢复项目默认' : '恢复共同设置'" help="移除本版本专用值，跟随当前配置范围的共同设置；不会自动保存" :disabled="!Object.hasOwn(version.overrides,field.path)||pathDisabled(field.path)||pathLocked('assembly.versions')" @reset="editVersionOverride(Number(index),field.path,null,true)"/></small></span><ParamControl :field="field" :values="versionValues(version)" :model-value="version.overrides[field.path]??values[field.path]" :disabled="fieldDisabled(field,versionValues(version))||pathLocked('assembly.versions')" @update:model-value="editVersionOverride(Number(index),field.path,$event)" /></ParameterHelp></div></div></details>
      </n-card>
     </template>
     <h3 v-if="key==='assemble'" class="common-heading">共同样式与混音</h3>
     <TextColorPresets v-if="key==='assemble'" kind="字幕" :color="values['assemble.subtitle_color']" :outline-color="values['assemble.subtitle_outline_color']" :outline="values['assemble.subtitle_outline']" :source-enabled="!!values['assemble.subtitle_source_style_enabled']" :source-color="values['assemble.subtitle_source_color']" :source-outline-color="values['assemble.subtitle_source_outline_color']" :source-outline="values['assemble.subtitle_source_outline']" :disabled="pathDisabled('assemble.subtitle_color')||anyPathLocked('assemble.subtitle_outline_color','assemble.subtitle_source_style_enabled')" @select="(id,source)=>preset(id,source?'assemble.subtitle_source_':'assemble.subtitle_')" @enable-source="enableSourceStyle('assemble.subtitle_')" />
     <TextColorPresets v-if="key==='comments'&&(!paths||paths.includes('comments.color'))" kind="评论" :color="values['comments.color']" :outline-color="values['comments.outline_color']" :outline="values['comments.outline']" :source-enabled="!!values['comments.source_style_enabled']" :source-color="values['comments.source_color']" :source-outline-color="values['comments.source_outline_color']" :source-outline="values['comments.source_outline']" :disabled="pathDisabled('comments.color')||anyPathLocked('comments.outline_color','comments.source_style_enabled')" @select="(id,source)=>preset(id,source?'comments.source_':'comments.')" @enable-source="enableSourceStyle('comments.')" />
     <ProjectDeliverySettings v-if="key==='publication'&&projectUid" :uid="projectUid" :values="values" :target-selection="targetSelection" :disabled="locked||pathLocked('run.mode')||pathLocked('publish.project_target')" @change="change" @busy-change="deliveryBusy=$event" />
     <section v-if="key==='publication'&&projectUid" class="project-copy publication-panel" aria-label="本项目发布文案">
      <header class="publication-panel-heading"><div><h3>发布文案</h3><small>自动基础来自视频资料；编辑后成为本项目设置。</small></div><div class="copy-tools"><n-button size="small" :disabled="locked||!usableCopyPreview" title="将当前标题、简介、标签和动态（包含空值）填为项目设置，之后仍需保存。" @click="retainCopy">保留当前文案</n-button><n-button size="small" :disabled="copyLoading" :loading="copyLoading" @click="loadCopyPreview">刷新基础资料</n-button></div></header>
      <p class="note">刷新只重读视频基础资料，保留当前编辑，不生成 AI、不保存。这里用于新稿和仍继承项目设置的草稿；已冻结或已投稿的稿件请在稿件页处理。</p>
      <p v-if="copyLoading" class="note" role="status">正在读取基础资料，其他设置仍可编辑…</p>
      <n-alert v-if="copyError" type="warning" :show-icon="false" class="copy-notice">{{ copyError }} <n-button text :disabled="copyLoading" @click="loadCopyPreview">重新读取基础资料</n-button><span v-if="copyPreview"> · 暂显示上次读取的自动基础。</span></n-alert>
      <n-alert v-if="copyContextChanged" type="warning" :show-icon="false" class="copy-notice">项目或继承设置已变化；当前修改已保留，自动基础暂不合并。请保存当前修改或放弃修改后重新读取设置，再核对文案。</n-alert>
      <PublishSettingsForm :values="publishValues" :overrides="publishResetOverrides" :sources="publishSources" :source-published-date="usableCopyPreview?.source_published_date" :field-keys="PROJECT_COPY_FIELDS" :restore-labels="restoreLabels" :reset-label="resetLabel" :reset-hint="resetHint" :inheritance="scope!=='global'" :disabled="locked" :show-more="false" :show-platform-assist="false" @change="(k,v)=>change('publish.'+k,v)" @inherit="k=>inherit('publish.'+k)" />
      <PublishCopyAssistant :project-uid="projectUid" :revision="data.revision" :fingerprint="data.fingerprint" :content-fingerprint="copyPreview?.content_fingerprint" :values="publishValues" :source-links="copyPreview?.source_links || []" :disabled="locked||copyLoading||!copyPreview||copyContextChanged" @apply="applyCopy" @busy-change="copyBusyChanged" />
      <p class="note">自动流程的 AI 只补未单独设置的标题、简介、标签和动态。只修改标题时，其余未设置项仍可能生成；“保留当前文案”会同时固定四项，包括留空内容，保存后生效。</p>
     </section>
     <div :class="{'publication-panel':key==='publication'}">
     <header v-if="key==='publication'" class="publication-panel-heading"><h3>发布方式、账号与封面</h3></header>
      <section v-for="group in styleGroups(section,key)" :key="group.name" class="style-fields" :class="{'tts-parameter-group':key==='tts'&&values['tts.backend']==='indextts25'}" :aria-label="group.name"><h4 v-if="['assemble','comments'].includes(String(key))||key==='tts'&&(values['tts.backend']==='indextts25'||group.name==='字幕导出与时间')">{{ group.name }}</h4>
      <DubbingPresets v-if="key==='tts'&&values['tts.backend']==='indextts25'&&group.name==='预设参数'" :draft="values" :fields="section.fields" :disabled="presetDisabled(section)" :scope-label="scopeLabel" @load="applyPreset" @busy-change="presetBusy=$event" />
      <div class="field-grid df-settings-grid"><div v-for="field in group.fields" :key="field.path" class="config-field df-setting" role="group" :aria-label="field.label" :class="{wide:field.type==='text'||field.type==='vector'}"><ParameterHelp :help="fieldHelp(field)" :label="field.label">
      <ParameterLabel :field="field"><ConfigurationSourceBadge :source="fieldSource(field.path)" :inherited="scope!=='global'&&!Object.hasOwn(patch,field.path)"/><ParameterResetButton :label="field.label" :action="fieldResetLabel(field.path)" :help="fieldResetHint(field.path)" :disabled="!canReset(field.path)||pathDisabled(field.path)" @reset="inherit(field.path)"/></ParameterLabel>
      <n-select v-if="field.path==='publish.account_id'" :value="values[field.path]||null" :options="accountOptions" :disabled="pathDisabled(field.path)" clearable @update:value="change(field.path,$event||'')" />
      <ParamControl v-else :field="field" :values="values" :project-uid="projectUid" :model-value="values[field.path]" :disabled="fieldDisabled(field)" @update:model-value="change(field.path,$event)" />
      <small v-if="indexTtsFieldInactiveReason(field.path,values)" class="field-inactive" :title="indexTtsFieldInactiveReason(field.path,values)">{{ indexTtsFieldInactiveSummary(field.path,values) }}</small>
     </ParameterHelp></div></div></section>

      <div v-if="key==='publication'&&values['publish.cover_source']==='custom'" class="cover-setting"><img v-if="values['publish.cover_id']" :src="`/api/publishing/covers/${values['publish.cover_id']}`" alt="指定封面"/><label>选择封面<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="pathDisabled('publish.cover_id')" @change="cover"/></label><ConfigurationSourceBadge :source="fieldSource('publish.cover_id')"/><ParameterResetButton label="指定封面" :action="fieldResetLabel('publish.cover_id')" :help="fieldResetHint('publish.cover_id')" :disabled="!canReset('publish.cover_id')||pathDisabled('publish.cover_id')" @reset="inherit('publish.cover_id')"/></div>
     </div>
     <template v-if="key==='publication'">
      <section class="publication-panel" aria-label="分P设置">
      <header class="publication-panel-heading"><h3>分P清单</h3><ConfigurationSourceBadge :source="fieldSource('publish.version_ids')"/><ParameterResetButton label="分P清单" :action="fieldResetLabel('publish.version_ids')" :help="fieldResetHint('publish.version_ids')" :disabled="!canReset('publish.version_ids')||pathDisabled('publish.version_ids')||pathLocked('assembly.versions')" @reset="inherit('publish.version_ids')"/></header>
      <n-select :value="values['publish.version_ids']??values['assembly.versions'].map((v:any)=>v.id)" :options="values['assembly.versions'].map((v:any)=>({label:v.name,value:v.id}))" multiple :disabled="partOrderDisabled" @update:value="change('publish.version_ids',$event)" />
      <small class="note">自动流程按下方 P1、P2… 的顺序准备B站分P。可上移、下移调整，保存设置后生效。已有草稿请在发布记录中调整顺序；已确认的发布任务保留原顺序。</small>
      <ol class="publication-parts" aria-label="发布分P顺序">
       <li v-for="(version,index) in publishedVersions" :key="version.id" class="publication-part" :aria-label="`P${Number(index)+1} · ${version.name}`">
        <div class="publication-part-heading"><strong>P{{ Number(index)+1 }} · {{ version.name }}</strong><div class="publication-part-actions">
         <n-button size="small" secondary :disabled="partOrderDisabled||index===0" :aria-label="`上移 P${Number(index)+1}`" @click="movePublishedVersion(version.id,-1)">↑ 上移</n-button>
         <n-button size="small" secondary :disabled="partOrderDisabled||index===publishedVersions.length-1" :aria-label="`下移 P${Number(index)+1}`" @click="movePublishedVersion(version.id,1)">↓ 下移</n-button>
        </div></div>
        <div class="publication-part-name"><label :for="`configured-part-${version.id}`">B站分P名称</label><ConfigurationSourceBadge :source="fieldSource('publish.part_titles')"/><ParameterResetButton :label="`P${Number(index)+1}名称`" action="撤销本项修改" help="恢复本分P已保存的名称；其他分P名称保留，不会自动保存" :disabled="pathDisabled('publish.part_titles')||!partTitleChanged(version.id)" @reset="restorePartTitle(version.id)"/></div>
        <n-input :value="values['publish.part_titles']?.[version.id]||''" :placeholder="version.name" maxlength="80" :disabled="pathDisabled('publish.part_titles')" :input-props="{id:`configured-part-${version.id}`,'aria-label':`P${Number(index)+1}名称`}" @update:value="partTitle(version.id,$event)"/>
       </li>
      </ol>
      <small class="note" role="status" aria-live="polite">{{ partOrderNotice }}</small>
      <ParameterResetButton label="分P名称" :action="fieldResetLabel('publish.part_titles')" :help="fieldResetHint('publish.part_titles')" :disabled="!canReset('publish.part_titles')||pathDisabled('publish.part_titles')" @reset="inherit('publish.part_titles')"/>
      </section>
      <section class="publication-panel" :aria-label="projectUid?'更多平台选项':'发布资料'">
      <header class="publication-panel-heading"><h3>{{ projectUid?'更多平台选项':'发布资料' }}</h3><n-button size="small" :disabled="locked||!values['publish.account_id']" :loading="metadataLoading" @click="loadMetadata">刷新账号选项</n-button></header>
      <PublishSettingsForm :values="publishValues" :overrides="publishResetOverrides" :sources="publishSources" :field-keys="projectUid?platformFields:undefined" :reset-label="resetLabel" :reset-hint="resetHint" :inheritance="scope!=='global'" :metadata="metadata" :metadata-loading="metadataLoading" :metadata-error="metadataError" :metadata-ready="metadataReady" :metadata-account="values['publish.account_id']" :context-key="scope" :cover-id="values['publish.cover_id']" :disabled="locked" :template="!scope.startsWith('project:')" @change="(k,v)=>change('publish.'+k,v)" @inherit="k=>inherit('publish.'+k)" @refresh-metadata="loadMetadata" @collection-created="collectionCreated" />
      </section>
     </template>
    </section>
   </div>
   <div class="save-bar"><span>{{ scope==='global'?`${changedFields.length} 项修改`:`${Object.keys(patch).length} 项明确设置` }}</span><n-button :disabled="loading||pending||!dirty" @click="adopt(data)">放弃修改</n-button><n-button type="primary" :disabled="locked||pending||!dirty" :loading="busy" @click="save">保存修改</n-button></div>
  </template>
 </div>
</template>
<style scoped>
.loading{display:flex;align-items:center;gap:12px;padding:40px;color:var(--df-muted);font-size:13px}.scope-line>strong{font-weight:500}.scope-line>small{margin-right:auto}
.tts-parameter-group{margin-top:22px;padding:20px;border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel)}.tts-parameter-group>h4{margin:0 0 16px;font-size:14px;color:var(--df-accent)}.tts-parameter-group .field-grid{margin-bottom:0}
.common-heading{font-size:14px;margin:24px 0 14px}
.configuration-navigation{margin-bottom:22px}.configuration-stage-heading{margin:22px 0 18px}.configuration-stage-heading small{color:var(--df-accent);font-size:11px}.configuration-stage-heading h2{margin:5px 0 0;font-size:18px;font-weight:600}.stage-number{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;border-radius:50%;background:var(--df-accent-soft);margin-right:6px;font-size:10px;font-variant-numeric:tabular-nums}.shared-tabs{border-bottom:0!important;padding-top:8px}.shared-label{align-self:center;margin-right:4px;color:var(--df-muted);font-size:11px}
.dimension-setting,.dimension-setting>span{display:flex;align-items:center;gap:8px;font-size:12px}
.config-field :deep(.n-switch){justify-self:start}
.field-heading{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;font-size:12px;gap:6px 12px}.field-inactive{display:block;line-height:1.6}
.copy-tools { display:flex;gap:8px;flex-wrap:wrap; }.copy-notice { margin:12px 0; }.project-copy :deep(.publish-fields) { margin-top:18px; }
.config-tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--df-border);padding-bottom:12px}.config-tabs button{border:1px solid transparent;background:transparent;color:var(--df-muted);font:inherit;font-size:12px;padding:8px 12px;border-radius:7px;cursor:pointer}.config-tabs button:hover{color:var(--df-text);background:var(--df-accent-soft)}.config-tabs button.selected{background:var(--df-accent-soft);color:var(--df-accent);border-color:var(--df-border)}
.cover-setting{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.cover-setting img{width:180px;aspect-ratio:16/9;object-fit:cover;border-radius:8px}.cover-setting label{font-size:12px}
.publication-parts{list-style:none;padding:0;margin:16px 0;display:grid;gap:12px}.publication-part{min-width:0;display:grid;gap:9px;padding:14px 16px;border:1px solid var(--df-border);border-radius:8px}.publication-part-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.publication-part-heading strong{font-size:13px;overflow-wrap:anywhere;min-width:0;flex:1}.publication-part-actions{display:flex;gap:8px;flex-shrink:0}.publication-part-name{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--df-muted)}
.configuration-form{min-width:0}.scope-line,.save-bar,.list-heading,.version-heading{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.scope-line{justify-content:space-between;margin:12px 0 18px;font-size:13px}small,.note,.save-bar>span{font-size:11px;color:var(--df-muted)}.dirty{font-size:12px;color:var(--df-accent)}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px 28px;margin:18px 0}.config-field{display:grid;gap:9px;min-width:0}.config-field>span{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;font-size:12px;gap:6px 12px}.config-field small{display:flex;gap:8px;align-items:center}.config-field.wide{grid-column:1/-1}.save-bar{position:sticky;bottom:0;justify-content:flex-end;background:var(--df-bg);padding:16px 0;border-top:1px solid var(--df-border);z-index:1}.save-bar>span{margin-right:auto}.list-heading{margin-top:24px}.list-heading h3{font-size:14px;margin-right:auto}.version-origin{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;font-size:11px;color:var(--df-muted)}.version-card{margin:12px 0}.version-heading :deep(.n-input){flex:1;min-width:160px}.dimensions{display:flex;gap:24px;margin:16px 0}.dimensions label{display:flex;gap:8px;align-items:center;font-size:12px}summary{font-size:12px;cursor:pointer;color:var(--df-muted)}.note{display:block;margin-top:8px}@media(max-width:680px){.field-grid{grid-template-columns:1fr}.dimensions{gap:14px;flex-wrap:wrap}}
</style>
