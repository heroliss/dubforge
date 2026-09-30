<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NCheckbox, NInput, NSelect, NSwitch, NTag, NTooltip, useDialog, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import TextColorPresets from './TextColorPresets.vue'
import AssemblyVersionPreview from './AssemblyVersionPreview.vue'
import DependencyNotice from './DependencyNotice.vue'
import { useCommentOverlay } from '../composables/useCommentOverlay'
import { useNavStore } from '../stores/nav'
import { versionSpec, versionSummary, effectiveVersion, type AssemblyVersion } from '../utils/assemblyVersions'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups } from '../utils/textStyleFields'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { subtitleSettingsLink } from '../utils/subtitleWorkspace'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import type { ConfigurationSource } from '../utils/configurationSource'
const props = defineProps<{uid:string; disabled?:boolean; configureOnly?:boolean}>()
const emit = defineEmits<{'dirty-change':[value:boolean]; saved:[value:any]}>()
const nav = useNavStore(), dialog = useDialog(), message = useMessage()
const state = ref<any>(null), versions = ref<AssemblyVersion[]>([]), activeId = ref(''), selected = ref<string[]>([])
const busy = ref(false), error = ref(''), blockers = ref<any[]>([]), ready = ref(false)
const {revision:commentRevision, refresh:refreshComments} = useCommentOverlay(() => props.uid)
let disposed = false, confirmation: any
const dirty = computed(() => !!state.value && JSON.stringify(versions.value) !== JSON.stringify(state.value.versions.map(versionSpec)))
const running = computed(() => ['running','queued'].includes(nav.stepMap.assemble?.status))
const locked = computed(() => !!props.disabled || busy.value || running.value)
const active = computed(() => versions.value.find(v => v.id === activeId.value))
const values = computed(() => active.value ? effectiveVersion(active.value, state.value.defaults) : {})
const groups = computed(() => ['字幕','评论','声音','输出'].filter(g => g === '字幕' ? active.value?.burn_subtitles : g === '评论' ? active.value?.include_comments : g === '声音' ? active.value?.dubbing : active.value?.burn_subtitles || active.value?.include_comments))
const subtitleTrack = computed(() => state.value?.fields.find((f:any) => f.path === 'assemble.subtitle_track'))
function fieldSource(path:string):ConfigurationSource|undefined { return active.value && Object.hasOwn(active.value.overrides,path) ? {kind:'version',name:active.value.name} : state.value?.sources?.[path] }
function groupFields(group: string) { return state.value.fields.filter((f:any) => f.group === group && f.path !== 'assemble.subtitle_track' && (f.path !== 'subtitles.source_first' || values.value['assemble.subtitle_track'] === 'bilingual') && (f.path !== 'comments.source_first' || values.value['comments.language_mode'] === 'bilingual')) }
function styleGroups(group:string){return textStyleFieldGroups(groupFields(group),group==='字幕'?'assemble.subtitle_':group==='评论'?'comments.':'',values.value,'整体设置')}
function versionStatus(v:AssemblyVersion) {
  const saved=state.value.versions.find((row:any)=>row.id===v.id)
  return !saved?'新增版本':JSON.stringify(v)!==JSON.stringify(versionSpec(saved))?'未保存调整':saved.status
}
async function api(path='', body?:any, method=body === undefined ? 'GET' : 'POST') {
  const r = await fetch(`/api/projects/${props.uid}/assembly-versions${path}`, {method,headers:body === undefined ? undefined : {'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body)})
  const data = await r.json(); if(!r.ok) throw new Error(data.message || '成片版本操作失败'); return data
}
function adopt(data:any) {
  state.value=data; versions.value=data.versions.map(versionSpec)
  if(!versions.value.some(v => v.id === activeId.value)) activeId.value=versions.value[0]?.id || ''
  selected.value=selected.value.filter(id => versions.value.some(v => v.id===id))
}
async function refresh() {
  try { const data=await api(); if(disposed) return; if(!dirty.value) adopt(data); else state.value={...state.value, defaults:data.defaults, origins:data.origins, sources:data.sources}; ready.value=true }
  catch(e:any) { if(!disposed) error.value=e.message }
}
async function reloadSaved() {
  if(locked.value) return
  const load=async()=>{busy.value=true;try{const data=await api();if(!disposed){adopt(data);error.value=''}}catch(e:any){error.value=e.message}finally{busy.value=false}}
  if(!dirty.value){await load();return}
  confirmation=dialog.warning({title:'丢弃未保存调整并刷新版本？',content:'将读取其他页面最新保存的版本与默认值；不会生成或删除成品。',positiveText:'刷新已保存版本',negativeText:'继续编辑',onPositiveClick:load})
}
async function save() {
  if(locked.value) return
  busy.value=true;error.value=''
  try { const data=await api('',{versions:versions.value.map(versionSpec),revision:state.value.revision,fingerprint:state.value.fingerprint},'PUT'); if(!disposed){adopt(data);emit('saved',data);message.success('已保存成片版本；已有文件不会立即重做')} }
  catch(e:any){error.value=e.message}finally{busy.value=false}
}
function add(copy=false) {
  if(locked.value) return
  const v=copy && active.value ? versionSpec(active.value) : {name:'新版本',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}}
  const row={...v,id:`v-${crypto.randomUUID()}`,name:copy ? `${v.name.slice(0,70)} · 副本` : v.name}
  versions.value.push(row);activeId.value=row.id
}
function remove() {
  if(!active.value || locked.value || versions.value.length < 2) return
  const id=active.value.id
  dialog.warning({title:'移除此版本定义？',content:'保留磁盘成品和发布记录。若全流程仍引用此版本，需先调整分P清单。',positiveText:'移除定义',negativeText:'取消',onPositiveClick:()=>{versions.value=versions.value.filter(v=>v.id!==id);activeId.value=versions.value[0].id;selected.value=selected.value.filter(v=>v!==id)}})
}
function override(key:string,value:any) { if(active.value && !locked.value && value !== null) active.value.overrides[key]=value }
function inherit(key:string) { if(active.value && !locked.value) delete active.value.overrides[key] }
function preset(group:string,id:string,source=false) { const prefix=(group==='字幕'?'assemble.subtitle_':'comments.')+(source?'source_':''); const patch=textColorPatch(id,prefix); if(patch && active.value) Object.entries(patch).forEach(([k,v])=>override(k,v)) }
function enableSourceStyle(group:string) { override((group==='字幕'?'assemble.subtitle_':'comments.')+'source_style_enabled',true) }
function toggle(id:string,checked:boolean) { selected.value=checked?[...selected.value,id]:selected.value.filter(v=>v!==id) }
function restoreInheritance() {
  if(locked.value) return
  confirmation=dialog.warning({title:'恢复继承版本清单？',
    content:'移除本项目的版本清单覆盖，后续跟随系列模板或全局默认；保留磁盘成品。',positiveText:'恢复继承',negativeText:'取消',
    onPositiveClick:async()=>{
      busy.value=true
      try{const r=await fetch(`/api/configuration/project:${props.uid}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:['assembly.versions'],revision:state.value.revision,fingerprint:state.value.fingerprint})});const d=await r.json();if(!r.ok)throw new Error(d.message);await refresh();message.success('已恢复继承')}
      catch(e:any){error.value=e.message}finally{busy.value=false}
    }})
}
async function generate() {
  if(locked.value || dirty.value || !selected.value.length) return
  busy.value=true;error.value='';blockers.value=[]
  let awaiting=false, accepted=false
  try{
    await refreshComments()
    const body={version_ids:[...selected.value],overlay_revision:commentRevision.value},plan=await api('/plan',body)
    blockers.value=plan.blockers
    if(plan.blockers.length) return
    awaiting=true
    confirmation=dialog.warning({title:'生成选中的成片版本',content:plan.versions.map((v:any)=>`${v.name}：${versionSummary(v)}`).join('\n')+'\n\n未变化的版本直接复用；已变化的版本成功后替换自己的成品，失败保留原文件。',positiveText:'确认生成',negativeText:'返回调整',maskClosable:false,
      onAfterLeave:()=>{busy.value=false},onPositiveClick:async()=>{
        if(accepted || disposed) return
        accepted=true
        try{const result=await api('/run',{...body,fingerprint:plan.fingerprint});nav.stepStarted(props.uid,'assemble',result);refreshInBackground(()=>nav.poll());message.success('已开始生成所选版本')}
        catch(e:any){error.value=e.message}finally{busy.value=false}
      }})
  }catch(e:any){error.value=e.message}finally{if(!awaiting)busy.value=false}
}
watch(dirty,v=>emit('dirty-change',v),{immediate:true})
watch(()=>nav.stepMap.assemble?.status,(value,old)=>{if(value!==old && !['running','queued'].includes(value)) void refresh()})
watch(()=>props.disabled,(value,old)=>{if(old && !value) void refresh()})
function beforeUnload(event:BeforeUnloadEvent){if(dirty.value){event.preventDefault();event.returnValue=''}}
function confirmLeave(){return !dirty.value || window.confirm('成片版本设置尚未保存，确定离开？')}
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate((to,from)=>(to.params.uid===from.params.uid && to.params.key===from.params.key) || confirmLeave())
onMounted(()=>{void refresh();window.addEventListener('beforeunload',beforeUnload)})
onBeforeUnmount(()=>{disposed=true;confirmation?.destroy();window.removeEventListener('beforeunload',beforeUnload);emit('dirty-change',false)})
</script>
<template>
  <n-card title="成片版本" size="small" class="version-workspace">
    <template #header-extra><n-tooltip><template #trigger><span class="help" tabindex="0">继承规则 ⓘ</span></template>全局默认 → 系列模板 → 项目设置；版本内可保留呈现差异。分P只引用版本。已有版本再次生成只替换自身。</n-tooltip></template>
    <n-alert v-if="error" type="error" :show-icon="false">{{ error }} <n-button v-if="!ready" size="tiny" @click="refresh">重试</n-button></n-alert>
    <template v-if="state">
      <div class="workspace-links"><RouterLink :to="subtitleSettingsLink(uid)">字幕内容与项目默认 →</RouterLink><RouterLink :to="`/projects/${uid}/steps/download#comments-studio`">共享评论方案 →</RouterLink><n-button size="tiny" quaternary :disabled="locked" @click="refreshComments">刷新评论</n-button><n-button size="tiny" quaternary :disabled="locked" @click="reloadSaved">刷新版本</n-button></div>
      <div class="workspace-grid">
        <aside aria-label="成片版本清单">
          <div v-for="version in versions" :key="version.id" class="version-row" :class="{active:activeId===version.id}">
            <n-checkbox v-if="!configureOnly" :checked="selected.includes(version.id)" :disabled="locked" :aria-label="`生成${version.name}`" @update:checked="toggle(version.id,$event)" />
            <button class="version-choice" :aria-pressed="activeId===version.id" @click="activeId=version.id"><strong>{{ version.name }}</strong><small>{{ versionSummary(version,state.defaults) }}</small><small>{{ versionStatus(version) }}</small></button>
          </div>
          <div class="list-actions"><n-button size="tiny" :disabled="locked || versions.length>=32" @click="add()">新增版本</n-button><n-button size="tiny" :disabled="locked || !active || versions.length>=32" @click="add(true)">复制为新版本</n-button></div>
        </aside>
        <div v-if="active" class="version-editor">
          <div class="editor-heading"><n-input v-model:value="active.name" :disabled="locked" maxlength="80" placeholder="版本名称（不是P序号）" :input-props="{'aria-label':'版本名称'}" /><n-button size="tiny" quaternary :disabled="locked || versions.length<2" @click="remove">移除定义</n-button></div>
          <div class="dimensions"><label>声音<n-select :value="active.dubbing?'dub':'src'" :options="[{label:'原声',value:'src'},{label:'配音',value:'dub'}]" :disabled="locked" @update:value="active.dubbing=$event==='dub'" /></label><label>烧录字幕<n-switch v-model:value="active.burn_subtitles" :disabled="locked" aria-label="烧录字幕" /></label><label>叠加评论<n-switch v-model:value="active.include_comments" :disabled="locked" aria-label="叠加评论" /></label></div>
          <div v-if="active.burn_subtitles && subtitleTrack" class="field-row">
            <span>字幕语言</span><ParamControl :field="subtitleTrack" :model-value="values[subtitleTrack.path]" :disabled="locked" @update:model-value="override(subtitleTrack.path,$event)" />
            <span class="field-source"><ConfigurationSourceBadge :source="fieldSource(subtitleTrack.path)" :inherited="!Object.hasOwn(active.overrides,subtitleTrack.path)"/><n-button size="tiny" quaternary :disabled="locked" :aria-label="`${subtitleTrack.label}${Object.hasOwn(active.overrides,subtitleTrack.path)?'恢复继承':'固定当前值'}`" :title="Object.hasOwn(active.overrides,subtitleTrack.path)?'恢复继承':'固定当前值，后续不跟随上级变化'" @click="Object.hasOwn(active.overrides,subtitleTrack.path)?inherit(subtitleTrack.path):override(subtitleTrack.path,values[subtitleTrack.path])">{{ Object.hasOwn(active.overrides,subtitleTrack.path)?'↶':'固定' }}</n-button></span>
          </div>
          <details v-for="group in groups" :key="group" class="settings-group"><summary>{{ group }}设置 <small>{{ state.fields.filter((f:any)=>f.group===group && Object.hasOwn(active!.overrides,f.path)).length }} 项覆盖</small></summary>
            <TextColorPresets v-if="group==='字幕'||group==='评论'" :kind="group" :color="values[group==='字幕'?'assemble.subtitle_color':'comments.color']" :outline-color="values[group==='字幕'?'assemble.subtitle_outline_color':'comments.outline_color']" :source-enabled="!!values[group==='字幕'?'assemble.subtitle_source_style_enabled':'comments.source_style_enabled']" :source-color="values[group==='字幕'?'assemble.subtitle_source_color':'comments.source_color']" :source-outline-color="values[group==='字幕'?'assemble.subtitle_source_outline_color':'comments.source_outline_color']" :source-outline="values[group==='字幕'?'assemble.subtitle_source_outline':'comments.source_outline']" :disabled="locked" @select="(id,source)=>preset(group,id,source)" @enable-source="enableSourceStyle(group)" />
            <section v-for="styleGroup in styleGroups(group)" :key="styleGroup.name"><h5>{{ styleGroup.name }}</h5><div v-for="field in styleGroup.fields" :key="field.path" class="field-row"><n-tooltip :disabled="!field.help"><template #trigger><span>{{ field.label }}</span></template>{{ field.help }}</n-tooltip><ParamControl :field="field" :model-value="values[field.path]" :disabled="locked" @update:model-value="override(field.path,$event)" /><span class="field-source"><ConfigurationSourceBadge :source="fieldSource(field.path)" :inherited="!Object.hasOwn(active.overrides,field.path)"/><n-button size="tiny" quaternary :disabled="locked" :aria-label="`${field.label}${Object.hasOwn(active.overrides,field.path)?'恢复继承':'固定当前值'}`" :title="Object.hasOwn(active.overrides,field.path)?'恢复继承':'固定当前值，后续不跟随上级变化'" @click="Object.hasOwn(active.overrides,field.path)?inherit(field.path):override(field.path,values[field.path])">{{ Object.hasOwn(active.overrides,field.path)?'↶':'固定' }}</n-button></span></div></section>
          </details>
          <AssemblyVersionPreview v-if="!configureOnly" :uid="uid" :version="active" :overlay-revision="commentRevision" :disabled="locked" />
        </div>
      </div>
      <div class="footer"><div><n-tag v-if="dirty" type="warning" size="small">未保存</n-tag><n-button size="small" :disabled="locked || !dirty" @click="save">保存版本设置</n-button><n-button size="tiny" quaternary :disabled="locked || !dirty" @click="dialog.warning({title:'放弃本页未保存的版本调整？',positiveText:'恢复已保存',negativeText:'继续编辑',onPositiveClick:()=>adopt(state)})">恢复已保存</n-button></div><n-button v-if="!configureOnly" type="primary" :disabled="locked || dirty || !selected.length" @click="generate">生成勾选版本（{{ selected.length }}）</n-button></div>
      <DependencyNotice :uid="uid" :items="blockers" />
      <div class="list-actions"><small>版本清单：</small><ConfigurationSourceBadge :source="dirty?{kind:'project'}:state.source" :inherited="!dirty&&state.inherited"/><n-button v-if="!state.inherited" size="tiny" :disabled="locked||dirty" @click="restoreInheritance">恢复继承清单</n-button><RouterLink to="/settings?section=production">管理制作与发布设置</RouterLink></div>
    </template>
    <p v-else-if="!error" class="help">正在读取版本…</p>
  </n-card>
</template>
<style scoped>
.field-source{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.version-workspace{margin-top:14px}.workspace-links,.list-actions,.editor-heading,.footer,.footer>div{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.workspace-links{font-size:12px;margin-bottom:16px}.workspace-grid{display:grid;grid-template-columns:240px minmax(0,1fr);gap:22px}.version-row{display:flex;gap:10px;align-items:center;padding:10px;border:1px solid transparent;border-radius:8px;margin-bottom:6px}.version-row.active{background:var(--df-accent-soft);border-color:var(--df-border)}.version-choice{border:0;background:none;color:inherit;text-align:left;cursor:pointer;min-width:0;flex:1;padding:0}.version-choice strong,.version-choice small{display:block;overflow-wrap:anywhere}.version-choice small,summary small,.help,.templates small{color:var(--df-muted);font-size:11px;margin-top:5px}.version-editor{min-width:0}.editor-heading{flex-wrap:nowrap}.editor-heading :deep(.n-input){flex:1;min-width:0}.dimensions{display:grid;grid-template-columns:minmax(100px,1fr) 1fr 1fr;gap:14px;margin:18px 0}.dimensions label{display:flex;flex-direction:column;gap:8px;font-size:12px}.dimensions :deep(.n-switch){align-self:flex-start}.field-row{display:grid;grid-template-columns:100px minmax(0,1fr) auto;gap:12px;align-items:center;margin:14px 0;font-size:12px}.field-row :deep(.n-input-number){max-width:100%}.settings-group{border-top:1px solid var(--df-border);padding:12px 0}summary{cursor:pointer;font-size:12px}.settings-group summary small{margin-left:8px}.footer{justify-content:space-between;border-top:1px solid var(--df-border);padding-top:16px;margin-top:18px}.templates{margin-top:16px}.templates .list-actions{margin:12px 0}.list-actions{margin-top:12px}.help{cursor:help}@media(max-width:1100px){.workspace-grid{grid-template-columns:1fr}aside{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}}@media(max-width:600px){aside{grid-template-columns:1fr}.field-row{grid-template-columns:90px minmax(0,1fr)}.field-row>:last-child{grid-column:2;justify-self:start}.dimensions{gap:8px}}
</style>
