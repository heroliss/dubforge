<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import { NAlert, NButton, NCard, NInput, NPopover, NSelect, NSpin, NSwitch, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import PublishSettingsForm from './PublishSettingsForm.vue'
import PublishCopyAssistant from './PublishCopyAssistant.vue'
import TextColorPresets from './TextColorPresets.vue'
import DubbingPresets from './DubbingPresets.vue'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { scopeSource, type ConfigurationSource } from '../utils/configurationSource'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups, sourceStyleFieldVisible } from '../utils/textStyleFields'
import { PROJECT_COPY_FIELDS, resolveProjectCopy, copyRestoreLabels, type ProjectCopyPreview } from '../utils/projectPublishCopy'
import { useConfigurationEditability } from '../utils/configurationEditability'
const props=defineProps<{scope:string;sections?:string[];paths?:string[]}>()
const editability=useConfigurationEditability(()=>props.scope)
const emit=defineEmits<{'saved':[data:any];'dirty-change':[dirty:boolean];'busy-change':[busy:boolean]}>()
const message=useMessage(),data=ref<any>(null),patch=ref<Record<string,any>>({}),saved=ref(''),name=ref(''),busy=ref(false),error=ref(''),tab=ref(''),catalog=ref<any>({accounts:[]})
const metadata=ref<any>({}),metadataLoading=ref(false),metadataError=ref(''),metadataReady=ref(false)
const copyPreview=ref<ProjectCopyPreview|null>(null),copyLoading=ref(false),copyError=ref(''),copyBusy=ref(false)
let copyGeneration=0,copyAbort:AbortController|undefined
let disposed=false,generation=0,metadataGeneration=0
const clone=(v:any)=>JSON.parse(JSON.stringify(v))
const dirty=computed(()=>!!data.value&&(JSON.stringify(patch.value)!==saved.value||name.value!==data.value.name))
const values=computed(()=>({...data.value?.defaults,...patch.value}))
const presetBusy=ref(false),loading=ref(false)
const pending=computed(()=>busy.value||presetBusy.value||copyBusy.value)
// 生成候选时仍可编辑；保存与离开由 pending 保护，候选会检查编辑版本。
const locked=computed(()=>loading.value||busy.value||presetBusy.value||data.value?.archived||editability.locked.value)
const projectUid=computed(()=>props.scope.startsWith('project:')?props.scope.slice(8):'')
const scopeLabel=computed(()=>props.scope==='global'?'全局默认':props.scope.startsWith('series:')?'系列模板':'项目设置')
const sections=computed(()=>Object.entries(data.value?.sections||{}).filter(([key])=>!props.sections||props.sections.includes(key)).map(([key,section]:any)=>[key,{...section,fields:section.fields.filter((f:any)=>(!props.paths||props.paths.includes(f.path))&&f.path!=='assemble.burn_subtitles')}]))
const basePublishValues=computed(()=>Object.fromEntries(Object.entries(values.value).filter(([k])=>k.startsWith('publish.')).map(([k,v])=>[k.slice(8),v])))
const publishOverrides=computed(()=>Object.fromEntries(Object.entries(patch.value).filter(([k])=>k.startsWith('publish.')).map(([k,v])=>[k.slice(8),v])))
const copyContextChanged=computed(()=>!!copyPreview.value&&!!data.value&&(copyPreview.value.revision!==data.value.revision||copyPreview.value.fingerprint!==data.value.fingerprint))
const usableCopyPreview=computed(()=>copyContextChanged.value?null:copyPreview.value)
const resolvedPublish=computed(()=>resolveProjectCopy(basePublishValues.value,publishOverrides.value,
 Object.fromEntries(Object.keys(basePublishValues.value).map(k=>[k,fieldSource('publish.'+k)])),projectUid.value?usableCopyPreview.value:null))
const publishValues=computed(()=>resolvedPublish.value.values)
const publishSources=computed(()=>resolvedPublish.value.sources)
const restoreLabels=computed(()=>copyRestoreLabels(usableCopyPreview.value))
const platformFields=['human_type2','copyright','declaration_id','visibility','season_id','section_id','publish_at']
const accountOptions=computed(()=>catalog.value.accounts.map((a:any)=>({label:a.name,value:a.id})))
const publishedVersions=computed(()=>(values.value['publish.version_ids']??values.value['assembly.versions']?.map((v:any)=>v.id)??[]).map((id:string)=>values.value['assembly.versions'].find((v:any)=>v.id===id)).filter(Boolean))
function partTitle(id:string,value:string){const titles={...values.value['publish.part_titles']};if(value.trim())titles[id]=value;else delete titles[id];change('publish.part_titles',titles)}
function fieldSource(key:string):ConfigurationSource|undefined{return Object.hasOwn(patch.value,key)?scopeSource(props.scope,name.value):data.value?.parent_sources?.[key]}
function change(key:string,value:any){if(!locked.value)patch.value[key]=value}
function inherit(key:string){if(!locked.value)delete patch.value[key]}
function visibleFields(section:any){
 const useLlm=Boolean(values.value['split.use_llm']??true)
 return section.fields.filter((f:any)=>{
  if(f.path==='split.target_word_count')return useLlm
  if(f.path==='split.max_split_length')return !useLlm
  return !(f.depends_on||[]).some(([k,v]:any)=>k==='tts.backend'&&values.value[k]!==v)
 })
}
function fieldDisabled(field:any){
 const splitLength=['split.target_word_count','split.max_split_length'].includes(field.path)
 return locked.value||(field.depends_on||[]).some(([k,v]:any)=>(splitLength&&k==='split.use_llm'?Boolean(values.value[k]??true):values.value[k])!==v)
}
function styleGroups(section:any,key:any){return textStyleFieldGroups(visibleFields(section),key==='assemble'?'assemble.subtitle_':key==='comments'?'comments.':'',values.value,'整体设置')}
function versionFields(version:any){return data.value.version_fields.filter((f:any)=>(f.group==='字幕'?version.burn_subtitles:f.group==='评论'?version.include_comments:true)&&sourceStyleFieldVisible(f,{...values.value,...version.overrides}))}
async function request(url:string,body?:any,method=body?'PUT':'GET',signal?:AbortSignal){
 const r=await fetch(url,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,signal});const d=await r.json();if(!r.ok)throw new Error(d.message||'设置操作失败');return d
}
function adopt(d:any){data.value=d;name.value=d.name;patch.value=clone(d.overrides);saved.value=JSON.stringify(patch.value);if(!sections.value.some(([key])=>key===tab.value))tab.value=String(sections.value[0]?.[0]||'')}
async function load(){const n=++generation;copyGeneration++;copyAbort?.abort();copyPreview.value=null;copyLoading.value=false;copyError.value='';loading.value=true;error.value='';try{const d=await request(`/api/configuration/${encodeURIComponent(props.scope)}`);if(disposed||n!==generation)return;adopt(d);void loadCopyPreview();if(!props.sections||props.sections.includes('publication')){const list=await request('/api/publishing/catalog');if(!disposed&&n===generation)catalog.value=list}}catch(e:any){if(n===generation)error.value=e.message}finally{if(n===generation)loading.value=false}}
async function save(){if(locked.value||pending.value||!dirty.value)return;busy.value=true;error.value='';const key=props.scope,ticket=generation;try{if(editability.observing.value&&!await editability.canEdit()){error.value=editability.reason.value||'运行中不能保存设置';return}if(disposed||key!==props.scope||ticket!==generation)return;const reset=Object.keys(data.value.overrides).filter(k=>!Object.hasOwn(patch.value,k));const d=await request(`/api/configuration/${encodeURIComponent(key)}`,{revision:data.value.revision,fingerprint:data.value.fingerprint,updates:patch.value,reset,...(key.startsWith('series:')?{name:name.value}:{})});if(disposed||key!==props.scope||ticket!==generation)return;adopt(d);copyGeneration++;copyAbort?.abort();copyLoading.value=false;void loadCopyPreview();emit('saved',d);message.success('已保存设置；不会自动运行')}catch(e:any){if(!disposed&&ticket===generation)error.value=e.message}finally{if(ticket===generation)busy.value=false}}
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
 for(const [key,value] of Object.entries(input))if(allowed.has(key))patch.value[key]=clone(value)
}
function preset(id:string,prefix:string){if(locked.value)return;const p=textColorPatch(id,prefix);if(p)applyPreset(p)}
function enableSourceStyle(prefix:string){if(!locked.value)applyPreset({[prefix+'source_style_enabled']:true})}
function applyIndexProfile(id:string){
 if(locked.value||values.value['tts.backend']!=='indextts25')return
 const profile=data.value?.indextts_profiles?.find((p:any)=>p.id===id)
 if(!profile)return
 const settings={...profile.values}
 if(values.value['tts.indextts25.emo_mode']==='text')settings['tts.indextts25.emo_weight']=.6
 applyPreset(settings)
 message.info('推荐参数已填入，保存后生效；音色、语言与参考文件保持不变')
}
function editVersion(index:number,key:string,value:any){const versions=clone(values.value['assembly.versions']);versions[index][key]=value;change('assembly.versions',versions)}
function editVersionOverride(index:number,key:string,value:any,reset=false){const versions=clone(values.value['assembly.versions']);if(reset)delete versions[index].overrides[key];else versions[index].overrides[key]=value;change('assembly.versions',versions)}
function addVersion(){change('assembly.versions',[...clone(values.value['assembly.versions']),{id:'v-'+crypto.randomUUID(),name:'新版本',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}}])}
function removeVersion(index:number){change('assembly.versions',values.value['assembly.versions'].filter((_:any,i:number)=>i!==index))}
function moveVersion(index:number,delta:number){const list=clone(values.value['assembly.versions']);[list[index],list[index+delta]]=[list[index+delta],list[index]];change('assembly.versions',list)}
watch(dirty,v=>emit('dirty-change',v),{flush:'sync'});watch(pending,v=>emit('busy-change',v),{flush:'sync'});watch(()=>props.scope,load,{immediate:true});watch(()=>[values.value['publish.account_id'],tab.value],loadMetadata);watch(tab,()=>{void loadCopyPreview()})
onUnmounted(()=>{disposed=true;generation++;metadataGeneration++;copyGeneration++;copyAbort?.abort()})
defineExpose({dirty,save})
</script>
<template>
 <div class="configuration-form">
  <n-alert v-if="editability.locked.value" type="info" :show-icon="false">{{ editability.reason.value }} <n-button text :loading="editability.checking.value" @click="editability.refresh">重新核对</n-button></n-alert>
  <n-alert v-if="error" type="error">{{ error }} <n-button text :disabled="loading||pending||dirty" @click="load">重新读取</n-button><small v-if="dirty"> · 修改已保留；放弃修改后可重新读取。</small></n-alert>
  <div v-if="!data && loading" class="loading"><n-spin size="small"/> 读取设置…</div>
  <template v-if="data">
   <div class="scope-line"><ConfigurationSourceBadge :source="scopeSource(scope,name)"/><strong v-if="scope.startsWith('series:')">{{ name }}</strong><small>{{ scope==='global'?'未覆盖的系列与项目跟随这里':'未单独设置的字段跟随上级' }}</small><span v-if="dirty" class="dirty">未保存</span></div>
   <p v-if="scope==='global'||scope.startsWith('series:')" class="note">已有项目也会跟随实际继承的制作参数；变化后相关步骤及依赖它的结果显示需更新，已有文件保留，保存不会自动重跑。需要保留某项目的参数时，可在项目设置中明确覆盖。</p>
   <n-input v-if="scope.startsWith('series:')" v-model:value="name" :disabled="locked" maxlength="80" :input-props="{'aria-label':'系列模板名称'}" style="max-width:320px;margin-bottom:16px"/>
   <n-alert v-if="data.archived" type="warning">模板已归档；已绑定项目保留继承，新项目不能选择。</n-alert>
   <nav v-if="sections.length>1" class="config-tabs" aria-label="设置类别"><button v-for="([key,section]) in sections" :key="key" type="button" :disabled="presetBusy" :aria-pressed="tab===key" :class="{selected:tab===key}" @click="tab=key">{{ (section as any).label }}</button></nav>
   <div>
    <section v-for="([key,section]) in sections.filter(([key])=>key===tab)" :key="key">
     <div v-if="key==='tts'&&values['tts.backend']==='indextts25'" class="tts-tools">
      <DubbingPresets :draft="values" :disabled="locked" :scope-label="scopeLabel" @load="applyPreset" @busy-change="presetBusy=$event" />
      <div class="recommendations"><span>合成参数</span><n-button v-for="profile in data.indextts_profiles||[]" :key="profile.id" size="small" :disabled="locked" :title="profile.description" @click="applyIndexProfile(profile.id)">{{ profile.name }}</n-button>
       <n-popover trigger="click" style="max-width:360px"><template #trigger><n-button size="tiny" quaternary>选择建议</n-button></template>
        <p>均衡方案沿用 IndexTTS 2.5 的采样默认值；速度方案把搜索束数从 3 降至 1，建议在本机试听比较。</p>
        <p>参考情感权重从 0.65 起，文本情感从 0.6 起。只填入合成参数，不改音色、情感模式、语言、参考文件或超长重试策略。</p>
       </n-popover>
      </div>
     </div>
     <template v-if="key==='assemble'">
      <div class="list-heading"><h3>成片版本 <ConfigurationSourceBadge :source="fieldSource('assembly.versions')"/></h3><n-button v-if="Object.hasOwn(patch,'assembly.versions')" text :disabled="locked" @click="inherit('assembly.versions')">恢复继承清单</n-button><n-button size="small" :disabled="locked" @click="addVersion">新增版本</n-button></div>
      <n-card v-for="(version,index) in values['assembly.versions']" :key="version.id" size="small" class="version-card">
       <div class="version-heading"><n-input :value="version.name" :disabled="locked" @update:value="editVersion(Number(index),'name',$event)"/><n-button size="tiny" :disabled="locked||index===0" @click="moveVersion(Number(index),-1)">↑</n-button><n-button size="tiny" :disabled="locked||index===values['assembly.versions'].length-1" @click="moveVersion(Number(index),1)">↓</n-button><n-button size="tiny" :disabled="locked||values['assembly.versions'].length<2" @click="removeVersion(Number(index))">移除</n-button></div>
       <div class="dimensions"><label v-for="(label,k) in {dubbing:'配音',burn_subtitles:'烧录字幕',include_comments:'叠加评论'}" :key="k">{{ label }}<n-switch :value="version[k]" :disabled="locked" @update:value="editVersion(Number(index),k,$event)" /></label></div>
       <details><summary>本版本呈现差异 · {{ Object.keys(version.overrides).length }} 项</summary><div class="field-grid"><div v-for="field in versionFields(version)" :key="field.path" class="config-field" role="group" :aria-label="`${field.group} · ${field.label}`"><span :title="field.path">{{ field.group }} · {{ field.label }}<small><ConfigurationSourceBadge :source="Object.hasOwn(version.overrides,field.path)?{kind:'version',name:version.name}:fieldSource(field.path)" :inherited="!Object.hasOwn(version.overrides,field.path)" /><n-button v-if="Object.hasOwn(version.overrides,field.path)" text size="tiny" :disabled="locked" :aria-label="`${field.label}恢复继承`" @click="editVersionOverride(Number(index),field.path,null,true)">↶</n-button></small></span><ParamControl :field="field" :model-value="version.overrides[field.path]??values[field.path]" :disabled="locked" @update:model-value="editVersionOverride(Number(index),field.path,$event)" /></div></div></details>
      </n-card>
     </template>
     <h3 v-if="key==='assemble'" class="common-heading">共同样式与混音</h3>
     <TextColorPresets v-if="key==='assemble'" kind="字幕" :color="values['assemble.subtitle_color']" :outline-color="values['assemble.subtitle_outline_color']" :outline="values['assemble.subtitle_outline']" :source-enabled="!!values['assemble.subtitle_source_style_enabled']" :source-color="values['assemble.subtitle_source_color']" :source-outline-color="values['assemble.subtitle_source_outline_color']" :source-outline="values['assemble.subtitle_source_outline']" :disabled="locked" @select="(id,source)=>preset(id,source?'assemble.subtitle_source_':'assemble.subtitle_')" @enable-source="enableSourceStyle('assemble.subtitle_')" />
     <TextColorPresets v-if="key==='comments'&&(!paths||paths.includes('comments.color'))" kind="评论" :color="values['comments.color']" :outline-color="values['comments.outline_color']" :outline="values['comments.outline']" :source-enabled="!!values['comments.source_style_enabled']" :source-color="values['comments.source_color']" :source-outline-color="values['comments.source_outline_color']" :source-outline="values['comments.source_outline']" :disabled="locked" @select="(id,source)=>preset(id,source?'comments.source_':'comments.')" @enable-source="enableSourceStyle('comments.')" />
     <section v-if="key==='publication'&&projectUid" class="project-copy publication-panel" aria-label="本项目发布文案">
      <header class="publication-panel-heading"><div><h3>发布文案</h3><small>自动基础来自视频资料；编辑后成为本项目设置。</small></div><div class="copy-tools"><n-button size="small" :disabled="locked||!usableCopyPreview" title="将当前标题、简介、标签和动态（包含空值）填为项目设置，之后仍需保存。" @click="retainCopy">保留当前文案</n-button><n-button size="small" :disabled="copyLoading" :loading="copyLoading" @click="loadCopyPreview">刷新基础资料</n-button></div></header>
      <p class="note">刷新只重读视频基础资料，保留当前编辑，不生成 AI、不保存。这里用于新稿和仍继承项目设置的草稿；已冻结或已投稿的稿件请在稿件页处理。</p>
      <p v-if="copyLoading" class="note" role="status">正在读取基础资料，其他设置仍可编辑…</p>
      <n-alert v-if="copyError" type="warning" :show-icon="false" class="copy-notice">{{ copyError }} <n-button text :disabled="copyLoading" @click="loadCopyPreview">重新读取基础资料</n-button><span v-if="copyPreview"> · 暂显示上次读取的自动基础。</span></n-alert>
      <n-alert v-if="copyContextChanged" type="warning" :show-icon="false" class="copy-notice">项目或继承设置已变化；当前修改已保留，自动基础暂不合并。请保存当前修改或放弃修改后重新读取设置，再核对文案。</n-alert>
      <PublishSettingsForm :values="publishValues" :overrides="publishOverrides" :sources="publishSources" :source-published-date="usableCopyPreview?.source_published_date" :field-keys="PROJECT_COPY_FIELDS" :restore-labels="restoreLabels" :disabled="locked" :show-more="false" :show-platform-assist="false" @change="(k,v)=>change('publish.'+k,v)" @inherit="k=>inherit('publish.'+k)" />
      <PublishCopyAssistant :project-uid="projectUid" :revision="data.revision" :fingerprint="data.fingerprint" :content-fingerprint="copyPreview?.content_fingerprint" :values="publishValues" :disabled="locked||copyLoading||!copyPreview||copyContextChanged" @apply="applyCopy" @busy-change="copyBusy=$event" />
      <p class="note">自动流程的 AI 只补未单独设置的标题、简介、标签和动态。只修改标题时，其余未设置项仍可能生成；“保留当前文案”会同时固定四项，包括留空内容，保存后生效。</p>
     </section>
     <div :class="{'publication-panel':key==='publication'}">
     <header v-if="key==='publication'" class="publication-panel-heading"><h3>发布方式、账号与封面</h3></header>
     <section v-for="group in styleGroups(section,key)" :key="group.name" class="style-fields"><h4 v-if="['assemble','comments'].includes(String(key))">{{ group.name }}</h4><div class="field-grid"><div v-for="field in group.fields" :key="field.path" class="config-field" role="group" :aria-label="field.label" :class="{wide:field.type==='text'||field.type==='vector'}">
      <span :title="field.help">{{ field.label }}<small><ConfigurationSourceBadge :source="fieldSource(field.path)" :inherited="!Object.hasOwn(patch,field.path)"/><n-button v-if="Object.hasOwn(patch,field.path)" text size="tiny" :disabled="locked" :aria-label="`${field.label}恢复继承`" @click="inherit(field.path)">↶</n-button></small></span>
      <n-select v-if="field.path==='publish.account_id'" :value="values[field.path]||null" :options="accountOptions" :disabled="locked" clearable @update:value="change(field.path,$event||'')" />
      <ParamControl v-else :field="field" :values="values" :model-value="values[field.path]" :disabled="fieldDisabled(field)" @update:model-value="change(field.path,$event)" />
     </div></div></section>

      <div v-if="key==='publication'&&values['publish.cover_source']==='custom'" class="cover-setting"><img v-if="values['publish.cover_id']" :src="`/api/publishing/covers/${values['publish.cover_id']}`" alt="指定封面"/><label>选择封面<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="locked" @change="cover"/></label><ConfigurationSourceBadge :source="fieldSource('publish.cover_id')"/><n-button text size="small" :disabled="locked" @click="inherit('publish.cover_id')">恢复继承</n-button></div>
     </div>
     <template v-if="key==='publication'">
      <section class="publication-panel" aria-label="分P设置">
      <header class="publication-panel-heading"><h3>分P清单</h3><ConfigurationSourceBadge :source="fieldSource('publish.version_ids')"/><n-button text size="small" :disabled="locked" @click="inherit('publish.version_ids')">恢复继承</n-button></header>
      <n-select :value="values['publish.version_ids']??values['assembly.versions'].map((v:any)=>v.id)" :options="values['assembly.versions'].map((v:any)=>({label:v.name,value:v.id}))" multiple :disabled="locked" @update:value="change('publish.version_ids',$event)" />
      <small class="note">默认按成片版本顺序发布；单独选择后按选择顺序排列。</small>
      <div class="field-grid"><label v-for="(version,index) in publishedVersions" :key="version.id" class="config-field"><span>P{{ Number(index)+1 }} · {{ version.name }}<ConfigurationSourceBadge :source="fieldSource('publish.part_titles')"/></span><n-input :value="values['publish.part_titles']?.[version.id]||''" :placeholder="version.name" maxlength="80" :disabled="locked" @update:value="partTitle(version.id,$event)"/></label></div>
      <n-button v-if="Object.hasOwn(patch,'publish.part_titles')" text size="small" :disabled="locked" @click="inherit('publish.part_titles')">分P名称恢复继承</n-button>
      </section>
      <section class="publication-panel" :aria-label="projectUid?'更多平台选项':'发布资料'">
      <header class="publication-panel-heading"><h3>{{ projectUid?'更多平台选项':'发布资料' }}</h3><n-button size="small" :disabled="locked||!values['publish.account_id']" :loading="metadataLoading" @click="loadMetadata">刷新账号选项</n-button></header>
      <PublishSettingsForm :values="publishValues" :overrides="publishOverrides" :sources="publishSources" :field-keys="projectUid?platformFields:undefined" :metadata="metadata" :metadata-loading="metadataLoading" :metadata-error="metadataError" :metadata-ready="metadataReady" :metadata-account="values['publish.account_id']" :context-key="scope" :cover-id="values['publish.cover_id']" :disabled="locked" :template="!scope.startsWith('project:')" @change="(k,v)=>change('publish.'+k,v)" @inherit="k=>inherit('publish.'+k)" @refresh-metadata="loadMetadata" @collection-created="collectionCreated" />
      </section>
     </template>
    </section>
   </div>
   <div class="save-bar"><span>{{ Object.keys(patch).length }} 项明确设置</span><n-button :disabled="loading||pending||!dirty" @click="adopt(data)">放弃修改</n-button><n-button type="primary" :disabled="locked||pending||!dirty" :loading="busy" @click="save">保存修改</n-button></div>
  </template>
 </div>
</template>
<style scoped>
.loading{display:flex;align-items:center;gap:12px;padding:40px;color:var(--df-muted);font-size:13px}.tts-tools{margin-top:20px}.recommendations{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--df-muted)}.scope-line>strong{font-weight:500}.scope-line>small{margin-right:auto}
.common-heading{font-size:14px;margin:24px 0 14px}
.config-field :deep(.n-switch){justify-self:start}
.copy-tools { display:flex;gap:8px;flex-wrap:wrap; }.copy-notice { margin:12px 0; }.project-copy :deep(.publish-fields) { margin-top:18px; }
.config-tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--df-border);padding-bottom:12px}.config-tabs button{border:1px solid transparent;background:transparent;color:var(--df-muted);font:inherit;font-size:12px;padding:8px 12px;border-radius:7px;cursor:pointer}.config-tabs button:hover{color:var(--df-text);background:var(--df-accent-soft)}.config-tabs button.selected{background:var(--df-accent-soft);color:var(--df-accent);border-color:var(--df-border)}
.cover-setting{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.cover-setting img{width:180px;aspect-ratio:16/9;object-fit:cover;border-radius:8px}.cover-setting label{font-size:12px}
.configuration-form{min-width:0}.scope-line,.save-bar,.list-heading,.version-heading{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.scope-line{justify-content:space-between;margin:12px 0 18px;font-size:13px}small,.note,.save-bar>span{font-size:11px;color:var(--df-muted)}.dirty{font-size:12px;color:var(--df-accent)}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px 28px;margin:18px 0}.config-field{display:grid;gap:9px;min-width:0}.config-field>span{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;font-size:12px;gap:6px 12px}.config-field small{display:flex;gap:8px;align-items:center}.config-field.wide{grid-column:1/-1}.save-bar{position:sticky;bottom:0;justify-content:flex-end;background:var(--df-bg);padding:16px 0;border-top:1px solid var(--df-border);z-index:1}.save-bar>span{margin-right:auto}.list-heading{margin-top:24px}.list-heading h3{font-size:14px;margin-right:auto}.version-card{margin:12px 0}.version-heading :deep(.n-input){flex:1;min-width:160px}.dimensions{display:flex;gap:24px;margin:16px 0}.dimensions label{display:flex;gap:8px;align-items:center;font-size:12px}summary{font-size:12px;cursor:pointer;color:var(--df-muted)}.note{display:block;margin-top:8px}@media(max-width:680px){.field-grid{grid-template-columns:1fr}.dimensions{gap:14px;flex-wrap:wrap}}
</style>
