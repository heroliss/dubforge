<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NCheckbox, NInput, NSelect, NSwitch, NTag, NTooltip, useDialog, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import { assemblyEncodingValues, encodingFieldHelp, isVideoEncodingSetting } from '../utils/assemblyEncoding'
import TextColorPresets from './TextColorPresets.vue'
import AssemblyVersionPreview from './AssemblyVersionPreview.vue'
import AssemblyDefaultsUpdate from './AssemblyDefaultsUpdate.vue'
import OutputCutsEditor from './OutputCutsEditor.vue'
import AssemblyChangesPanel from './AssemblyChangesPanel.vue'
import DependencyNotice from './DependencyNotice.vue'
import { useCommentOverlay } from '../composables/useCommentOverlay'
import { useNavStore } from '../stores/nav'
import { versionSpec, versionSummary, effectiveVersion, assemblyFormalOutputSignature, mergeAssemblyOutputMetadata, type AssemblyVersion } from '../utils/assemblyVersions'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups } from '../utils/textStyleFields'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { createLatestRefresh } from '../utils/preview'
import { subtitleSettingsLink } from '../utils/subtitleWorkspace'
import { commentWorkspaceLink } from '../utils/commentWorkflow'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import type { ConfigurationSource } from '../utils/configurationSource'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { assemblyVersionFieldSource } from '../utils/assemblyInheritance'
import AudioQualityReport from './AudioQualityReport.vue'
import VideoPreview from './VideoPreview.vue'
import ParameterHelp from './ParameterHelp.vue'
import ParameterResetButton from './ParameterResetButton.vue'
import { audioSettingGroups, audioFieldHelp } from '../utils/audioQuality'
import { parameterDependencyMatches } from '../utils/parameterPresentation'
import { partialTime } from '../utils/assemblyPartial'
import { formatTimecode } from '../utils/timecode'
const props = defineProps<{uid:string; disabled?:boolean; configureOnly?:boolean; refreshToken?:number|string; partialRequest?:unknown}>()
const emit = defineEmits<{'dirty-change':[value:boolean]; 'busy-change':[value:boolean]; saved:[value:any]}>()
const nav = useNavStore(), dialog = useDialog(), message = useMessage()
const editability = useConfigurationEditability(() => `project:${props.uid}`)
const state = ref<any>(null), versions = ref<AssemblyVersion[]>([]), activeId = ref(''), selected = ref<string[]>([])
// 项目长期交付目标与本次手动生成勾选分开保存，历史版本不会自动全部加入。
const targetVersions = ref<string[]|null>(null)
const busy = ref(false), error = ref(''), blockers = ref<any[]>([]), ready = ref(false)
const selectedOperation = ref('')
const deleteTargetId = ref(''), deleteLoading = ref(false)
const cutsDirty = ref(false), cutsBusy = ref(false)
const cutsExpanded = ref(false)
const defaultsBusy = ref(false)
const partialBusy = ref(false)
const changesBusy = ref(false)
watch(changesBusy, value => { partialBusy.value = value }, { flush: 'sync' })
const partialDetails = ref<Record<string, any>>({})
const defaultsPanel = ref<InstanceType<typeof AssemblyDefaultsUpdate>|null>(null), defaultsExpanded = ref(false)
const {revision:commentRevision, refresh:refreshComments} = useCommentOverlay(() => props.uid)
const changesRevision = computed(() => JSON.stringify([state.value?.fingerprint, props.refreshToken, commentRevision.value,
  nav.stepMap.assemble?.status, nav.stepMap.assemble?.artifacts]))
let disposed = false, confirmation: any, contextGeneration = 0
let savedRead: AbortController | null = null
let externalRefreshQueued = false
let outputRefreshQueued = false
const formalOutputSignature = computed(()=>assemblyFormalOutputSignature(nav.uid===props.uid?nav.stepMap.assemble?.artifacts:[]))
const dirty = computed(() => !!state.value && (JSON.stringify(versions.value) !== JSON.stringify(state.value.versions.map(versionSpec))
  || JSON.stringify(targetVersions.value)!==JSON.stringify(state.value.target_version_ids ?? null)))
const displayedTargets = computed(() => targetVersions.value ?? state.value?.target_selection?.version_ids ?? [])
function setTargets(ids:string[]) { if(!settingsLocked.value && !isPathLocked('assembly.target_version_ids'))targetVersions.value=[...ids] }
const allDirty = computed(() => dirty.value || cutsDirty.value)
const running = computed(() => ['running','queued'].includes(nav.stepMap.assemble?.status))
const isPathLocked = (path:string) => editability.isPathLocked(path)
function settingsAreLocked() { return nav.archived || !!props.disabled || editability.locked.value
  || busy.value || defaultsBusy.value || partialBusy.value || isPathLocked('assembly.versions') }
const settingsLocked = computed(settingsAreLocked)
const actionLocked = computed(() => nav.archived || !!props.disabled || busy.value || cutsBusy.value || defaultsBusy.value || partialBusy.value || running.value)
const defaultsLocked = computed(() => nav.archived || !!props.disabled || editability.locked.value || busy.value || allDirty.value || cutsBusy.value || partialBusy.value
  || isPathLocked('assembly.versions') || (state.value?.fields || []).some((field:any)=>isPathLocked(field.path)))
const definitionSource = computed<ConfigurationSource|undefined>(() => dirty.value ? {kind:'project'} : state.value?.source)
const fieldLocked = (path:string) => settingsLocked.value || !!active.value?.update || !!active.value?.timeline || isPathLocked(path) || (!!active.value?.partial && isVideoEncodingSetting(path))
function fieldDisabled(field:any) {
  return fieldLocked(field.path) || (field.depends_on || []).some(([key, value]:[string,any]) => !parameterDependencyMatches(values.value[key],value))
}
const active = computed(() => versions.value.find(v => v.id === activeId.value))
const savedActive = computed(() => state.value?.versions.find((v:any) => v.id === activeId.value))
// 局部修订/所选更新不能生成短片试合成（它们必须以基准成片重建），
// 但已有的正式成片仍应在同一工作区可播放。步骤状态中的 artifact
// 会随任务轮询刷新，使用已保存 AssemblyResult 的 artifact_id 锁定当前版本，
// 避免把另一版本或旧历史文件误显示为本次修订结果。
const activeFormalArtifact = computed(() => {
  const artifactId = savedActive.value?.artifact_id
  if (artifactId == null) return null
  return (nav.stepMap.assemble?.artifacts || []).find((artifact:any) =>
    String(artifact.id) === String(artifactId) && artifact.exists !== false && !String(artifact.name || '').startsWith('preview_')) || null
})
const activeFormalPreviewUrl = computed(() => {
  const artifact = activeFormalArtifact.value
  if (!artifact?.name) return ''
  const stamp = artifact.sha256 ? `?v=${encodeURIComponent(artifact.sha256)}` : ''
  return `/api/projects/${encodeURIComponent(props.uid)}/steps/assemble/artifacts/${encodeURIComponent(artifact.name)}/raw${stamp}`
})
const cutBaselineOptions = computed(() => {
  const saved = Array.isArray(state.value?.output_cuts_baselines) ? state.value.output_cuts_baselines : []
  // 后端只保存旧成片的 artifact_id；编辑器需要把它解析为受管产物名称，
  // 才能提供只读参考播放器。剪辑时间轴仍然固定使用原视频。
  const artifactName = (artifactId:any) => (nav.stepMap.assemble?.artifacts || [])
    .find((artifact:any) => String(artifact.id) === String(artifactId))?.name || ''
  if (saved.length) return saved.map((row:any) => ({ id: row.version_id || row.id, version_id: row.version_id || row.id,
    name: row.name || row.version_name || row.version_id || row.id, generated_at: row.generated_at, status: row.status, fresh: row.fresh,
    output_cuts: row.output_cuts, artifact_id: row.artifact_id, artifact_name: artifactName(row.artifact_id), artifact_hash: row.artifact_hash })).filter((row:any) => row.id && row.id !== activeId.value && row.artifact_id)
  return (state.value?.versions || []).filter((row:any) => row.id !== activeId.value && row.artifact_id)
    .map((row:any) => ({ id: row.id, version_id: row.id, name: row.name, generated_at: row.generated_at, status: row.status, fresh: row.fresh, output_cuts: row.output_cuts, artifact_id: row.artifact_id, artifact_name: artifactName(row.artifact_id), artifact_hash: row.artifact_hash }))
})
const activePartial = computed(() => savedActive.value?.partial_summary || partialDetails.value[activeId.value])
const activeUpdate = computed(() => savedActive.value?.update_summary || partialDetails.value[activeId.value])
const activeTimelineEncoding = computed(() => savedActive.value?.timeline_summary?.actual_encoding || savedActive.value?.timeline_summary?.encoding)
const activeTimelineEncodingIsActual = computed(() => !!savedActive.value?.timeline_summary?.actual_encoding)
function timelineSeconds(value: unknown) { if(value===null||value===undefined)return '待核对';const seconds=Number(value);return Number.isFinite(seconds)&&seconds>=0?formatTimecode(seconds,3):'待核对' }
const preservesAudio = computed(() => !!active.value?.timeline || (!!active.value?.partial && !activePartial.value?.replace_audio))
const parameterOverrides = computed(() => Object.keys(active.value?.overrides || {}))
const restorableOverrides = computed(() => parameterOverrides.value.filter(path => !(active.value?.partial && isVideoEncodingSetting(path))))
const restoreParametersLocked = computed(() => settingsLocked.value || !restorableOverrides.value.length || restorableOverrides.value.some(fieldLocked))
const values = computed(() => {
  if ((active.value?.update || active.value?.timeline) && savedActive.value) return assemblyEncodingValues(savedActive.value.values || {})
  if (!active.value) return {}
  const effective = effectiveVersion(active.value, state.value.defaults)
  return assemblyEncodingValues(effective, active.value.partial && savedActive.value ? savedActive.value.values || {} : undefined)
})
const hasOutputCuts = computed(() => !!state.value?.versions.some((row:any)=>!!row.output_cuts))
const dubbingAudioPaths=new Set(['assemble.voice_volume','assemble.bgm_volume','assemble.ducking','assemble.loudnorm','assemble.sentence_leveling','assemble.sentence_leveling_max_db'])
const groups = computed(() => ['字幕','评论','声音','输出'].filter(g => g === '字幕' ? active.value?.burn_subtitles : g === '评论' ? active.value?.include_comments : g === '声音' ? !preservesAudio.value && groupFields(g).length>0 : active.value?.burn_subtitles || active.value?.include_comments || hasOutputCuts.value || active.value?.partial))
const subtitleTrack = computed(() => state.value?.fields.find((f:any) => f.path === 'assemble.subtitle_track'))
function fieldSource(path:string):ConfigurationSource|undefined {
  if(!active.value) return state.value?.sources?.[path]
  if(active.value.partial && isVideoEncodingSetting(path)) return {kind:'baseline',frozen:true,
    ...(activePartial.value?.base_name ? {name:activePartial.value.base_name} : {})}
  return assemblyVersionFieldSource(active.value,path,state.value?.sources,state.value?.versions.find((row:any)=>row.id===active.value?.id),state.value?.source)
}
function groupFields(group: string) { return state.value.fields.filter((f:any) => f.group === group && f.path !== 'assemble.subtitle_track' && (active.value?.dubbing || !dubbingAudioPaths.has(f.path)) && (f.path !== 'subtitles.source_first' || values.value['assemble.subtitle_track'] === 'bilingual') && (f.path !== 'comments.source_first' || values.value['comments.language_mode'] === 'bilingual')) }
function styleGroups(group:string){return group==='声音'?audioSettingGroups(groupFields(group)):textStyleFieldGroups(groupFields(group),group==='字幕'?'assemble.subtitle_':group==='评论'?'comments.':'',values.value,'整体设置')}
function versionFieldHelp(field:any) { return encodingFieldHelp({...field,help:audioFieldHelp(field,values.value)},values.value) }
function versionStatus(v:AssemblyVersion) {
  const saved=state.value.versions.find((row:any)=>row.id===v.id)
  return !saved?'新增版本':JSON.stringify(v)!==JSON.stringify(versionSpec(saved))?'未保存调整':saved.status
}
async function api(path='', body?:any, method=body === undefined ? 'GET' : 'POST', signal?:AbortSignal, uid=props.uid) {
  const r = await fetch(`/api/projects/${uid}/assembly-versions${path}`, {method,signal,cache:'no-store',headers:body === undefined ? undefined : {'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body)})
  const data = await r.json(); if(!r.ok) throw new Error(data.message || '成片版本操作失败'); return data
}
function adopt(data:any) {
  state.value=data; versions.value=data.versions.map(versionSpec)
  targetVersions.value=data.target_version_ids ? [...data.target_version_ids] : null
  if(!versions.value.some(v => v.id === activeId.value)) activeId.value=versions.value[0]?.id || ''
  selected.value=selected.value.filter(id => versions.value.some(v => v.id===id))
}
function currentContext(uid:string, generation:number) { return !disposed && uid===props.uid && generation===contextGeneration }
function readError(e:any) { error.value=e?.name==='TimeoutError'?'读取成片版本超时，请重试':e?.message || '读取成片版本失败，请重试' }
const versionReader=createLatestRefresh(async signal=>{
  const uid=props.uid, generation=contextGeneration
  const outputs=formalOutputSignature.value
  const data=await api('',undefined,'GET',AbortSignal.any([signal,AbortSignal.timeout(15000)]),uid)
  return {uid,generation,data,outputs}
},({uid,generation,data,outputs})=>{
  if(!currentContext(uid,generation) || busy.value || defaultsBusy.value || outputs!==formalOutputSignature.value) return
  // 编辑中的稀疏定义和提交版本号保留；上级默认变化不能悄悄覆盖本页草稿。
  if(!dirty.value) adopt(data)
  else state.value={...state.value,defaults:data.defaults,origins:data.origins,sources:data.sources,inheritance:data.inheritance,
    versions:mergeAssemblyOutputMetadata(state.value.versions,data.versions)}
  ready.value=true;error.value=''
},readError)
function refresh() {
  return disposed || busy.value || defaultsBusy.value ? Promise.resolve() : versionReader.refresh()
}
function refreshFormalOutputs() {
  if(disposed || nav.uid!==props.uid) return
  outputRefreshQueued=true
  if(busy.value || defaultsBusy.value) return
  outputRefreshQueued=false
  void refresh()
}
function refreshSavedSettings() {
  if (disposed) return
  externalRefreshQueued = true
  if (busy.value || defaultsBusy.value) return
  externalRefreshQueued = false
  versionReader.cancel()
  void refresh()
}
function openDefaults() {
  if (defaultsLocked.value) return
  defaultsExpanded.value = true
  void defaultsPanel.value?.inspect()
}
function cancelReads() { versionReader.cancel();savedRead?.abort();savedRead=null }
async function readSaved(uid:string, generation:number) {
  const controller=new AbortController();savedRead=controller
  try {
    const data=await api('',undefined,'GET',AbortSignal.any([controller.signal,AbortSignal.timeout(15000)]),uid)
    if(currentContext(uid,generation) && savedRead===controller) {
      versionReader.cancel();adopt(data);ready.value=true;error.value=''
    }
  } finally { if(savedRead===controller) savedRead=null }
}
async function reloadSaved() {
  if(settingsLocked.value) return
  const uid=props.uid, generation=contextGeneration
  const load=async()=>{
    if(!currentContext(uid,generation) || settingsLocked.value) return
    cancelReads();busy.value=true
    try{await readSaved(uid,generation)}
    catch(e:any){if(currentContext(uid,generation) && e?.name!=='AbortError')readError(e)}
    finally{if(currentContext(uid,generation))busy.value=false}
  }
  if(!dirty.value){await load();return}
  confirmation=dialog.warning({title:'丢弃未保存调整并刷新版本？',content:'将读取其他页面最新保存的版本与默认值；不会生成或删除成品。',positiveText:'刷新已保存版本',negativeText:'继续编辑',onPositiveClick:load})
}
async function save() {
  if(settingsLocked.value) return
  if(targetVersions.value!==null && !targetVersions.value.length){error.value='请至少选择一个项目目标成片，再保存版本与目标。';return}
  const uid=props.uid, generation=contextGeneration
  cancelReads();busy.value=true;error.value=''
  try { const data=await api('',{versions:versions.value.map(versionSpec),target_version_ids:targetVersions.value,revision:state.value.revision,fingerprint:state.value.fingerprint},'PUT',undefined,uid); if(currentContext(uid,generation)){versionReader.cancel();adopt(data);emit('saved',data);message.success('已保存版本与项目目标成片；已有文件不会立即重做')} }
  catch(e:any){if(currentContext(uid,generation))error.value=e.message}finally{if(currentContext(uid,generation))busy.value=false}
}
function add(copy=false) {
  if(settingsLocked.value) return
  const v=copy && active.value ? versionSpec(active.value) : {name:'新版本',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}}
  if(copy && 'update' in v) delete v.update
  const row={...v,id:`v-${crypto.randomUUID()}`,name:copy ? `${v.name.slice(0,70)} · 副本` : v.name}
  versions.value.push(row);activeId.value=row.id
}
function followCurrentContent() {
  if(settingsLocked.value || running.value || !active.value?.update) return
  delete active.value.update
  message.info('已切换为采用全部当前内容。保存版本设置后，后续生成和自动流程将采用最新工作稿；已有成片保持原样。')
}
function baselineChanged(id: string) {
  // The version list is the single source of truth for the comparison target.
  // The embedded range editor may bring in a draft based on another saved
  // version; switch the active row so the rest of this workbench follows it.
  if (!id || allDirty.value || !versions.value.some(row => row.id === id)) return
  activeId.value = id
}
async function partialCreated(version: any) {
  if(disposed || (!version.update && versions.value.length >= 32)) return
  const row=versionSpec(version)
  partialDetails.value[row.id]=version.update_summary || version.partial_summary
  const existing=versions.value.findIndex(value=>value.id===row.id)
  if(version.update && existing>=0) versions.value.splice(existing,1,row)
  else versions.value.push(row)
  activeId.value=row.id;selected.value=[row.id]
  if (version.update || version.partial) {
    const exactUpdate = !!version.update
    const uid=props.uid, generation=contextGeneration
    cancelReads();busy.value=true;error.value='';blockers.value=[]
    selectedOperation.value=exactUpdate?'正在保存本次勾选的更新方案…':'正在保存片段修订版…'
    try {
      const saved=await api('',{versions:versions.value.map(versionSpec),target_version_ids:targetVersions.value,revision:state.value.revision,fingerprint:state.value.fingerprint},'PUT',undefined,uid)
      if(!currentContext(uid,generation)) return
      adopt(saved);activeId.value=row.id;selected.value=[row.id];emit('saved',saved)
      selectedOperation.value='正在核对基准成片与本次采用的内容…'
      const body={version_ids:[row.id]}, checked=await api('/plan',body,'POST',undefined,uid)
      if(!generationReady(uid,generation)) return
      blockers.value=checked.blockers
      if(blockers.value.length) {error.value='修订方案已保存，请处理下方提示后生成此版本。';return}
      const result=await api('/run',{...body,fingerprint:checked.fingerprint},'POST',undefined,uid)
      if(currentContext(uid,generation)) {
        nav.stepStarted(uid,'assemble',result);refreshInBackground(()=>nav.poll())
        message.success(exactUpdate?'已开始应用所选改动；未勾选内容保留，完成后继续显示待更新':'已开始生成片段修订版；基准成片保留不变')
      }
    } catch(e:any) {if(currentContext(uid,generation))error.value=e.message}
    finally {if(currentContext(uid,generation)){busy.value=false;selectedOperation.value=''}}
    return
  }
}
async function timelineCreated(version: any) {
  if (disposed || !version?.id) return
  const uid = props.uid, generation = contextGeneration
  cancelReads(); busy.value = true; error.value = ''; blockers.value = []
  selectedOperation.value = '正在保存现有成片的剪辑版本…'
  const row = versionSpec(version)
  versions.value.push(row); activeId.value = row.id; selected.value = [row.id]; cutsDirty.value = false
  try {
    const data = await api('', { versions: versions.value.map(versionSpec), target_version_ids:targetVersions.value, revision: state.value.revision, fingerprint: state.value.fingerprint }, 'PUT', undefined, uid)
    if (!currentContext(uid, generation)) return
    adopt(data); activeId.value = version.id; selected.value = [version.id]; cutsDirty.value = false
    emit('saved', data)
    const body = { version_ids: [version.id] }, plan = await api('/plan', body, 'POST', undefined, uid)
    // 切换到新版本会挂载剪辑编辑器并只读加载其清单；本次冻结计划
    // 已保存且整个表单仍锁定，这个读取不能打断刚确认的生成操作。
    if (!generationReady(uid, generation, { ignoreCutsBusy: true })) return
    blockers.value = plan.blockers || []
    if (blockers.value.length) { error.value = '剪辑版本已保存，请处理下方提示后生成。'; return }
    const result = await api('/run', { ...body, fingerprint: plan.fingerprint }, 'POST', undefined, uid)
    if (currentContext(uid, generation)) {
      nav.stepStarted(uid, 'assemble', result); refreshInBackground(() => nav.poll())
      message.success('已开始生成剪辑版；仅拼接已选成片与恢复来源，旧成片保留')
    }
  } catch (e: any) { if (currentContext(uid, generation)) error.value = e.message }
  finally { if (currentContext(uid, generation)) { busy.value = false; selectedOperation.value = '' } }
}
async function fullUpdate(id: string) {
  if (actionLocked.value || settingsLocked.value || allDirty.value) return
  const base = versions.value.find(row => row.id === id)
  if (!base) return
  if (base.update) {
    activeId.value=id;selected.value=[id]
    await generate()
    return
  }
  if (base.partial || base.timeline) {
    if (versions.value.length >= 32) { message.warning('版本已达到上限，请先移除不再使用的版本定义。'); return }
    const row = versionSpec(base); delete row.partial; delete row.update; delete row.timeline
    row.id = `v-${crypto.randomUUID()}`; row.name = `${base.name.slice(0,65)} · 完整更新`
    versions.value.push(row); activeId.value = row.id; selected.value = [row.id]
    message.info('已建立完整更新版本，请保存版本设置后生成；旧局部成片保留。')
  } else {
    activeId.value = id; selected.value = [id]
    await generate(true)
  }
}
function deletionLocked() {
  return settingsAreLocked() || cutsBusy.value || running.value
}
function deletionHint(id:string) {
  if(versions.value.length<2) return '至少保留一个成片版本'
  if(running.value) return '生成任务正在运行或排队，完成后再删除版本'
  if(cutsDirty.value) return '请先保存或放弃剪辑草稿，再删除版本'
  if(state.value?.versions.some((row:any)=>row.id===id) && dirty.value) return '请先保存或放弃版本草稿，再删除已保存版本'
  return '删除版本定义；已有成品与发布记录保留'
}
async function remove(id=activeId.value) {
  const target=versions.value.find(row=>row.id===id)
  if(!target || deletionLocked()) return
  if(versions.value.length<2) {message.warning('至少保留一个成片版本。');return}
  const saved=state.value?.versions.some((row:any)=>row.id===id)
  if(cutsDirty.value || (saved && dirty.value)) {
    message.warning('请先保存或放弃版本与剪辑草稿，再删除已保存版本；其他调整不会自动保存。')
    return
  }
  const uid=props.uid, generation=contextGeneration, snapshot=JSON.stringify(versions.value)
  const revision=state.value.revision, fingerprint=state.value.fingerprint
  const stillReady=()=>currentContext(uid,generation) && !nav.archived && !props.disabled
    && !editability.locked.value && !defaultsBusy.value && !partialBusy.value && !cutsBusy.value
    && !isPathLocked('assembly.versions') && !running.value && !cutsDirty.value
    && versions.value.length>1 && JSON.stringify(versions.value)===snapshot
    && state.value?.revision===revision && state.value?.fingerprint===fingerprint
  const release=()=>{if(currentContext(uid,generation)){busy.value=false;deleteTargetId.value='';deleteLoading.value=false}}
  confirmation?.destroy();confirmation=null;cancelReads()
  busy.value=true;deleteTargetId.value=id;deleteLoading.value=!!saved;error.value=''
  let awaiting=false, accepted=false
  try {
    const plan=saved?await api('/delete-plan',{version_id:id},'POST',undefined,uid):null
    if(!currentContext(uid,generation)) return
    if(!stillReady()) {error.value='当前状态或设置已变化，请重新检查后删除版本。';return}
    if(plan && (plan.revision!==revision || plan.fingerprint!==fingerprint)) {
      error.value='已保存的版本清单或设置已变化，请刷新版本后重新删除。'
      return
    }
    if(plan && (!plan.can_delete || plan.blockers?.length)) {
      error.value=(plan.blockers || []).join('；') || '此版本暂时无法删除，请刷新版本后重试。'
      return
    }
    deleteLoading.value=false
    const content=saved
      ? [`删除“${plan.name || target.name}”的版本定义并立即保存，后续生成与自动流程不再使用此版本。`,
        `保留 ${plan.formal_count || 0} 份正式成片、${plan.preview_count || 0} 份试片，以及发布记录和后续修订需要的基准文件。${plan.dependent_versions?.length?`仍有 ${plan.dependent_versions.length} 个后续版本使用其基准文件，可继续使用。`:''}文件可在下方“正式成品与原片”中回收。`,
        '发布清单中对此版本的引用和对应分P标题会一并移除。'].join('\n\n')
      : `“${target.name}”尚未保存，只移除此本地版本草稿；其他版本草稿保留，不会修改已保存版本或删除任何文件。`
    const removal=dialog.warning({title:saved?`删除版本“${plan.name || target.name}”？`:'移除未保存的新版本？',content,
      positiveText:saved?'确认删除版本':'移除此草稿',negativeText:'取消',maskClosable:false,
      positiveButtonProps:{type:'error'},
      onAfterLeave:()=>{if(!accepted && confirmation===removal){confirmation=null;release()}},
      onPositiveClick:async()=>{
        // 确认可能跨越项目切换、任务启动或草稿变化；只处理预检时的准确清单。
        if(accepted || !currentContext(uid,generation) || confirmation!==removal) return false
        if(!stillReady()) {error.value='当前状态或设置已变化，请取消后重新检查删除。';return false}
        accepted=true;deleteLoading.value=true;error.value=''
        try {
          if(saved) {
            const data=await api('/delete',{version_id:id,revision:plan.revision,fingerprint:plan.fingerprint,confirmed:true},'POST',undefined,uid)
            if(currentContext(uid,generation)) {
              versionReader.cancel();adopt(data);delete partialDetails.value[id];emit('saved',data)
              refreshInBackground(()=>nav.poll())
              message.success('已删除版本，已有成片、试片与发布记录保留。')
            }
          } else {
            versions.value=versions.value.filter(row=>row.id!==id)
            if(activeId.value===id) activeId.value=versions.value[0]?.id || ''
            selected.value=selected.value.filter(value=>value!==id)
            if(targetVersions.value?.includes(id))targetVersions.value=targetVersions.value.filter(value=>value!==id)
            delete partialDetails.value[id]
            message.success('已移除未保存的新版本；其他草稿保留。')
          }
          release()
        } catch(e:any) {
          if(currentContext(uid,generation)) {error.value=e.message;message.error(e.message);deleteLoading.value=false}
          accepted=false
          return false
        }
      }})
    confirmation=removal;awaiting=true
  } catch(e:any) {if(currentContext(uid,generation))error.value=e.message}
  finally {if(!awaiting)release()}
}
function override(key:string,value:any) { if(active.value && !fieldLocked(key) && value !== null) active.value.overrides[key]=value }
function inherit(key:string) { if(active.value && !fieldLocked(key)) delete active.value.overrides[key] }
type DefinitionKey = 'name' | 'dubbing' | 'burn_subtitles' | 'include_comments'
function definitionChanged(key:DefinitionKey) { return !!savedActive.value && active.value?.[key] !== savedActive.value[key] }
function restoreDefinition(key:DefinitionKey) {
  if(active.value && savedActive.value && !settingsLocked.value && (!(active.value.update || active.value.timeline) || key==='name')) (active.value as any)[key] = savedActive.value[key]
}
function restoreVersionDefaults() {
  if(active.value && !restoreParametersLocked.value) active.value.overrides = active.value.partial
    ? Object.fromEntries(Object.entries(active.value.overrides).filter(([path])=>isVideoEncodingSetting(path))) : {}
}
function preset(group:string,id:string,source=false) { const prefix=(group==='字幕'?'assemble.subtitle_':'comments.')+(source?'source_':''); const patch=textColorPatch(id,prefix); if(patch && active.value && !fieldLocked(group==='字幕'?'assemble.subtitle_color':'comments.color')) Object.entries(patch).forEach(([k,v])=>override(k,v)) }
function enableSourceStyle(group:string) { override((group==='字幕'?'assemble.subtitle_':'comments.')+'source_style_enabled',true) }
function toggle(id:string,checked:boolean) { if(actionLocked.value)return; selected.value=checked?[...selected.value,id]:selected.value.filter(v=>v!==id) }
function restoreInheritance() {
  if(settingsLocked.value) return
  const uid=props.uid, generation=contextGeneration
  confirmation=dialog.warning({title:'恢复项目基础版本清单？',
    content:'移除本项目的版本清单覆盖，恢复创建或上次应用时保留的基础清单；已有成品保留，不读取最新全局或模板。',positiveText:'恢复基础清单',negativeText:'取消',
    onPositiveClick:async()=>{
      if(!currentContext(uid,generation) || settingsLocked.value) return
      cancelReads();busy.value=true
      try{const r=await fetch(`/api/configuration/project:${uid}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:['assembly.versions'],revision:state.value.revision,fingerprint:state.value.fingerprint})});const d=await r.json();if(!r.ok)throw new Error(d.message);if(currentContext(uid,generation)){await readSaved(uid,generation);if(currentContext(uid,generation))message.success('已恢复继承')}}
      catch(e:any){if(currentContext(uid,generation) && e?.name!=='AbortError')readError(e)}finally{if(currentContext(uid,generation))busy.value=false}
    }})
}
function generationReady(uid=props.uid, generation=contextGeneration, options: { ignoreCutsBusy?: boolean } = {}) {
  if(!currentContext(uid,generation)) return false
  if(nav.archived) {error.value='项目已归档，请先恢复项目后再生成成片版本。';return false}
  // 预检等待和确认窗口期间，父页的审查草稿或运行状态仍可能变化。
  // 此时 busy 属于本次预检，不能用包含 busy 的 locked 判定。
  if(props.disabled || allDirty.value || (cutsBusy.value && !options.ignoreCutsBusy) || defaultsBusy.value || running.value) {
    error.value='当前状态或设置已变化，请先保存审查、版本与剪辑设置，再重新确认生成'
    return false
  }
  return true
}
async function generate(forceRebuild = false) {
  if(actionLocked.value || allDirty.value || !selected.value.length) return
  const uid=props.uid, generation=contextGeneration
  busy.value=true;error.value='';blockers.value=[]
  let awaiting=false, accepted=false
  try{
    const fixedOnly=selected.value.every(id=>{const version=versions.value.find(v=>v.id===id);return version?.update || version?.timeline})
    if(!fixedOnly) await refreshComments()
    if(!generationReady(uid,generation)) return
    const rebuild = forceRebuild === true
    const body={version_ids:[...selected.value],...(!fixedOnly?{overlay_revision:commentRevision.value}:{}),...(rebuild?{force_rebuild:true}:{})},plan=await api('/plan',body,'POST',undefined,uid)
    if(!generationReady(uid,generation)) return
    blockers.value=plan.blockers
    if(plan.blockers.length) return
    awaiting=true
    confirmation=dialog.warning({title:rebuild?'完整更新所选成片':'生成选中的成片版本',content:plan.versions.map((v:any)=>`${v.name}：${versionSummary(v)}`).join('\n')+(rebuild?'\n\n完整重新生成所选版本并建立当前制作内容的对照记录。成功后替换自己的成品，失败保留原文件。':'\n\n未变化的版本直接复用；已变化的版本成功后替换自己的成品，失败保留原文件。'),positiveText:rebuild?'确认完整更新':'确认生成',negativeText:'返回调整',maskClosable:false,
      onAfterLeave:()=>{if(currentContext(uid,generation))busy.value=false},onPositiveClick:async()=>{
        if(accepted || !currentContext(uid,generation)) return
        if(!generationReady(uid,generation)) return false
        accepted=true
        try{const result=await api('/run',{...body,fingerprint:plan.fingerprint},'POST',undefined,uid);if(currentContext(uid,generation)){nav.stepStarted(uid,'assemble',result);refreshInBackground(()=>nav.poll());message.success('已开始生成所选版本')}}
        catch(e:any){if(currentContext(uid,generation))error.value=e.message}finally{if(currentContext(uid,generation))busy.value=false}
      }})
  }catch(e:any){if(currentContext(uid,generation))error.value=e.message}finally{if(!awaiting && currentContext(uid,generation))busy.value=false}
}
watch(allDirty,v=>emit('dirty-change',v),{immediate:true,flush:'sync'})
watch(()=>busy.value || cutsBusy.value || defaultsBusy.value || partialBusy.value,v=>emit('busy-change',v),{immediate:true,flush:'sync'})
function cutsSaved(data:any) { emit('saved',data);void refresh() }
function defaultsApplied(data:any) {
  cancelReads();adopt(data);ready.value=true;error.value='';emit('saved',data)
  refreshInBackground(()=>nav.poll());message.success('已应用最新合成基础设置；项目独立设置保留，生成成片时生效')
}
function focusRefresh(){if(typeof document==='undefined'||!document.hidden)void refresh()}
function resetContext() {
  contextGeneration++;cancelReads();confirmation?.destroy();confirmation=null
  externalRefreshQueued=false;outputRefreshQueued=false;defaultsExpanded.value=false
  state.value=null;versions.value=[];activeId.value='';selected.value=[];targetVersions.value=null;blockers.value=[];partialDetails.value={}
  ready.value=false;busy.value=false;deleteTargetId.value='';deleteLoading.value=false;error.value='';cutsDirty.value=false;cutsBusy.value=false;defaultsBusy.value=false;changesBusy.value=false;partialBusy.value=false
  if(!disposed) void refresh()
}
function disposeWorkspace() { disposed=true;contextGeneration++;cancelReads();confirmation?.destroy() }
watch(()=>props.uid,resetContext)
watch(()=>props.refreshToken,refreshSavedSettings)
watch([busy,defaultsBusy],()=>{
  if(busy.value || defaultsBusy.value) return
  if(externalRefreshQueued) {outputRefreshQueued=false;refreshSavedSettings()}
  else if(outputRefreshQueued) refreshFormalOutputs()
},{flush:'sync'})
watch(formalOutputSignature,refreshFormalOutputs,{flush:'sync'})
watch(()=>nav.stepMap.assemble?.status,(value,old)=>{if(value!==old && !['running','queued'].includes(value)) void refresh()})
watch(()=>props.disabled,(value,old)=>{if(old && !value) void refresh()})
function beforeUnload(event:BeforeUnloadEvent){if(allDirty.value || cutsBusy.value || defaultsBusy.value || partialBusy.value){event.preventDefault();event.returnValue=''}}
function confirmLeave(){if(cutsBusy.value || defaultsBusy.value || partialBusy.value){message.warning('正在处理剪辑、局部修订或合成基础设置，请完成当前操作后再离开');return false}return !allDirty.value || window.confirm('成片版本或剪辑设置尚未保存，确定离开？')}
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate((to,from)=>(to.params.uid===from.params.uid && to.params.key===from.params.key) || confirmLeave())
onMounted(()=>{void refresh();window.addEventListener('beforeunload',beforeUnload);window.addEventListener('focus',focusRefresh)})
onBeforeUnmount(()=>{disposeWorkspace();window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('focus',focusRefresh);emit('dirty-change',false);emit('busy-change',false)})
defineExpose({openDefaults})
</script>
<template>
  <n-card title="版本制作工作台" size="small" class="version-workspace">
    <template #header-extra><n-tooltip><template #trigger><span class="help" tabindex="0">继承规则 ⓘ</span></template>项目保存的基础值 → 项目设置 → 版本呈现差异；全局和模板变化需主动应用。分P只引用版本。已有版本再次生成只替换自身。</n-tooltip></template>
    <n-alert v-if="error" type="error" :show-icon="false">{{ error }} <n-button v-if="!ready" size="tiny" @click="refresh">重试</n-button></n-alert>
    <template v-if="state">
      <div class="workspace-links"><RouterLink :to="subtitleSettingsLink(uid)">字幕内容与项目默认 →</RouterLink><RouterLink :to="commentWorkspaceLink(uid)">共享评论方案 →</RouterLink><n-button size="tiny" quaternary :disabled="actionLocked" @click="refreshComments">刷新评论</n-button><n-button size="tiny" quaternary :disabled="settingsLocked" @click="reloadSaved">刷新版本</n-button></div>
      <details class="workspace-secondary" :open="defaultsExpanded" @toggle="defaultsExpanded=($event.target as HTMLDetailsElement).open"><summary>项目基础设置 <small>应用最新默认设置；成片剪辑在每个版本中单独保存</small></summary><AssemblyDefaultsUpdate ref="defaultsPanel" :key="uid" :uid="uid" :summary="state.inheritance" :disabled="defaultsLocked" @busy-change="defaultsBusy=$event" @applied="defaultsApplied" /></details>
      <AssemblyChangesPanel v-if="!configureOnly" :key="uid" :uid="uid" :revision="changesRevision" :preferred-version="activeId" :request-token="partialRequest" :version-limit="versions.length>=32" :disabled="nav.archived || !!disabled || editability.locked.value || isPathLocked('assembly.versions') || allDirty || busy || cutsBusy || defaultsBusy || running" @busy-change="changesBusy=$event" @created="partialCreated" @full="fullUpdate" @baseline-change="baselineChanged" />
      <section class="project-production-targets" aria-label="项目目标成片">
        <label><strong>项目目标成片</strong><n-select :value="displayedTargets" :options="versions.map(version=>({label:version.name,value:version.id}))" multiple :disabled="settingsLocked || isPathLocked('assembly.target_version_ids')" aria-label="参与项目运行的成片版本" placeholder="选择项目需要制作的版本" @update:value="setTargets" /></label>
        <p>保存后，总览与批次运行按这些目标补齐必要依赖；有效成片会复用。发布分P另在发布步骤选择。</p>
        <p v-if="targetVersions?.length===0 || !targetVersions && state.target_selection?.needs_confirmation" class="target-warning" role="status">尚未明确项目制作目标，请选择后保存；不会重做全部历史版本。</p>
        <p v-else-if="!targetVersions && state.target_selection?.source==='single_version'">当前项目只有一个版本，运行时使用该版本；可选择并保存为明确目标。</p>
      </section>
      <div class="workspace-grid">
        <aside aria-label="成片版本清单">
          <div class="version-list-heading"><strong>成片版本与呈现设置</strong><small v-if="!configureOnly">点击版本名称选择当前基准成片；左侧勾选框仅控制本次手动生成，可多选，不改写项目目标</small></div>
          <div class="version-list">
          <div v-for="version in versions" :key="version.id" class="version-row" :class="{active:activeId===version.id}">
            <n-checkbox v-if="!configureOnly" :checked="selected.includes(version.id)" :disabled="actionLocked" :aria-label="`生成${version.name}`" @update:checked="toggle(version.id,$event)" />
              <button class="version-choice" :disabled="busy || defaultsBusy || partialBusy || cutsBusy || allDirty" :title="allDirty ? '请先保存或放弃当前版本、剪辑调整，再切换基准成片' : undefined" :aria-pressed="activeId===version.id" @click="activeId=version.id"><strong>{{ version.name }}<span v-if="activeId===version.id" class="active-baseline-label">当前基准</span></strong><small>{{ versionSummary(version,state.defaults) }}</small><span class="version-source"><ConfigurationSourceBadge :source="definitionSource"/><small>{{ Object.keys(version.overrides).length }} 项专用参数</small></span><small>{{ versionStatus(version) }}</small></button>
              <n-button class="version-delete" size="tiny" quaternary :disabled="deletionLocked() || versions.length<2" :loading="deleteTargetId===version.id && deleteLoading" :title="deletionHint(version.id)" :aria-label="`删除版本：${version.name}`" @click="remove(version.id)">删除…</n-button>
          </div>
          </div>
          <div class="list-actions"><n-button size="tiny" :disabled="settingsLocked || versions.length>=32" @click="add()">新增版本</n-button><n-button size="tiny" :disabled="settingsLocked || !active || versions.length>=32" @click="add(true)">复制为新版本</n-button></div>
        </aside>
        <div class="workbench-body" :class="{'configure-only':configureOnly}">
          <OutputCutsEditor v-if="active" :key="`${uid}:${activeId}`" class="version-cut-workspace" :uid="uid" :version-id="activeId" :version-name="active.name" :baseline-options="cutBaselineOptions" :target-artifact-name="activeFormalArtifact?.name" :target-artifact-hash="activeFormalArtifact?.sha256" :target-artifact-fresh="savedActive?.fresh" :apply-disabled="dirty || configureOnly" :disabled="nav.archived || !!props.disabled || busy || defaultsBusy || partialBusy || running" :running="running" @dirty-change="cutsDirty=$event" @busy-change="cutsBusy=$event" @open-change="cutsExpanded=$event" @saved="cutsSaved" @timeline-created="timelineCreated" />
        <div v-if="active" v-show="!cutsExpanded" class="version-editor">
          <header class="editor-title"><h3>{{ active.timeline?'现有成片剪辑':active.update?'本次所选更新':'调整版本' }}</h3><p>{{ active.timeline?'按固定基准及恢复来源拼接；旧成片保留，工作稿不会混入。':active.update?'采用已确认的更新清单；后续修改继续显示在成片差异中。':active.partial?'改动只用于方案选定的范围，保存后生成独立修订成片。':'改动先用于试片，保存后才用于正式合成。' }}</p></header>
          <n-alert v-if="selectedOperation" type="info" :show-icon="false" role="status">{{ selectedOperation }}</n-alert>
          <n-alert v-if="active.update" type="info" :show-icon="false" class="partial-scope">{{ activeUpdate?.summary || '本方案只应用已勾选的内容，未选内容保留已有成片中的版本。' }}后续生成和自动流程都会沿用这份方案。需要继续调整时，在上方差异清单中选择下一次更新。<n-button size="small" text :disabled="settingsLocked || running" @click="followCurrentContent">改为采用全部当前内容…</n-button></n-alert>
          <n-alert v-if="active.partial" type="info" :show-icon="false" class="partial-scope">局部修订版本：只有方案中选定的范围应用当前内容与设置，其余沿用基准成片。{{ preservesAudio?'此版本保留基准音轨，声音参数不参与修订。':'声音也仅在所选范围更新。' }}声音类型和编码画质与基准一致。保存后可在全流程的成片版本中选用。<ul v-if="activePartial?.ranges" class="partial-scope-ranges"><li v-for="(row,index) in activePartial.ranges" :key="index">{{ partialTime(row.start) }} – {{ partialTime(row.end) }}</li></ul></n-alert>
          <div class="editor-heading"><n-input v-model:value="active.name" :disabled="settingsLocked" maxlength="80" placeholder="版本名称（不是P序号）" :input-props="{'aria-label':'版本名称'}" /><ParameterResetButton label="版本名称" action="撤销本项修改" :help="savedActive?'恢复此版本已保存的名称；其他调整保留':'新增版本尚无已保存的名称'" :disabled="settingsLocked || !definitionChanged('name')" @reset="restoreDefinition('name')" /><n-button size="tiny" quaternary :disabled="deletionLocked() || versions.length<2" :loading="deleteTargetId===activeId && deleteLoading" :title="deletionHint(activeId)" @click="remove()">删除版本…</n-button></div>
          <n-alert v-if="active.timeline" type="info" :show-icon="false" class="partial-scope">成片剪辑版：固定使用已选基准成片及恢复来源，保留它们已有的字幕、配音与评论，不重新套用当前工作稿。可继续在上方剪辑中删除或恢复片段，生成新的独立版本。</n-alert>
          <section v-if="active.timeline && activeTimelineEncoding" class="timeline-encoding-summary" aria-label="成片剪辑编码方式">
            <div><n-tag size="small" :type="activeTimelineEncoding.method === 'smart_cut' ? 'success' : 'info'" :bordered="false">{{ activeTimelineEncoding.method === 'smart_cut' ? '接缝编码，复用主体' : '完整重新编码' }}</n-tag><span>{{ activeTimelineEncodingIsActual ? '实际生成记录' : '生成前预估' }}</span></div>
            <p>视频重编码 <strong>{{ timelineSeconds(activeTimelineEncoding.encoded_seconds) }}</strong><span>直接复用 <strong>{{ timelineSeconds(activeTimelineEncoding.copied_seconds) }}</strong></span></p>
            <small>{{ activeTimelineEncoding.audio_mode === 'none' ? '此版本无音轨。' : '音频精确裁剪后编码一次。' }}<template v-if="activeTimelineEncoding.fallback_reason">需整片编码：{{ activeTimelineEncoding.fallback_reason }}</template></small>
          </section>
          <template v-if="!active.update && !active.timeline">
          <div class="version-definition"><span>版本定义</span><ConfigurationSourceBadge :source="definitionSource"/><small>声音、字幕与评论开关沿用此清单；参数专用值优先于项目默认</small></div>
          <div class="dimensions">
            <div class="dimension-setting" role="group" aria-label="版本声音"><span class="dimension-label">声音<ParameterResetButton label="声音" action="撤销本项修改" help="恢复此版本已保存的原声或配音选择；其他调整保留" :disabled="settingsLocked || !!active.partial || !definitionChanged('dubbing')" @reset="restoreDefinition('dubbing')" /></span><n-select aria-label="版本声音" :value="active.dubbing?'dub':'src'" :options="[{label:'原声',value:'src'},{label:'配音',value:'dub'}]" :disabled="settingsLocked || !!active.partial" @update:value="active.dubbing=$event==='dub'" /></div>
            <div v-for="(label,key) in {burn_subtitles:'烧录字幕',include_comments:'叠加评论'}" :key="key" class="dimension-setting" role="group" :aria-label="label"><span class="dimension-label">{{ label }}<ParameterResetButton :label="label" action="撤销本项修改" :help="`恢复此版本已保存的${label}开关；其他调整保留`" :disabled="settingsLocked || !definitionChanged(key)" @reset="restoreDefinition(key)" /></span><n-switch v-model:value="active[key]" :disabled="settingsLocked" :aria-label="label" /></div>
          </div>
          <div class="parameter-inheritance"><small>修改参数会设为版本专用；恢复后跟随本项目已保存的默认值。{{ parameterOverrides.length }} 项版本专用。</small><n-button size="tiny" quaternary :disabled="restoreParametersLocked" title="仅恢复当前版本的全部参数，包含折叠或隐藏的参数；名称、声音/字幕/评论开关与其他版本保留。保存版本设置后生效，不读取最新全局或模板。" @click="restoreVersionDefaults">全部参数恢复项目默认</n-button></div>
          <div v-if="active.burn_subtitles && subtitleTrack" class="field-row">
            <span>字幕语言</span><ParamControl :field="subtitleTrack" :model-value="values[subtitleTrack.path]" :disabled="fieldLocked(subtitleTrack.path)" @update:model-value="override(subtitleTrack.path,$event)" />
            <span class="field-source"><ConfigurationSourceBadge :source="fieldSource(subtitleTrack.path)" :inherited="!Object.hasOwn(active.overrides,subtitleTrack.path)"/><ParameterResetButton :label="subtitleTrack.label" action="恢复项目默认" :disabled="fieldLocked(subtitleTrack.path) || !Object.hasOwn(active.overrides,subtitleTrack.path)" :help="Object.hasOwn(active.overrides,subtitleTrack.path)?'移除此项版本专用值，恢复本项目已保存的默认值；保存后生效':'已继承本项目默认值，无需恢复'" @reset="inherit(subtitleTrack.path)" /></span>
          </div>
          <div v-if="!preservesAudio" class="sound-guide" aria-label="声音调整的三个作用范围"><ParameterHelp label="本机监听" help="播放器小喇叭与监听总音量只影响你当前设备的试听，不保存为项目声音，也不改变成片测量结果。"><span tabindex="0">本机监听</span><small>播放器音量，只影响本机</small></ParameterHelp><ParameterHelp label="人声与背景平衡" help="人声和背景的混入比例、背景避让及句间均衡决定声音是否协调。最终统一响度会一起调整混音，不会自动修复人声被背景盖住的问题。请用试片核对。"><span tabindex="0">人声与背景平衡</span><small>先让讲话清楚、背景合适</small></ParameterHelp><ParameterHelp label="统一输出响度" help="用相同目标校准不同项目的总体听感音量；原片只作对照，不作为追随目标。建议在全局设置同一目标，再主动应用到已有项目；项目和版本单独设置保留，需分别核对。"><span tabindex="0">统一输出响度</span><small>再统一项目之间的总体音量</small></ParameterHelp></div>
          <details v-for="group in groups" :key="group" class="settings-group" :open="group==='声音'"><summary>{{ group }}设置 <small>{{ state.fields.filter((f:any)=>f.group===group && Object.hasOwn(active!.overrides,f.path)).length }} 项覆盖</small></summary>
            <p v-if="group==='输出' && groupFields(group).some((field:any)=>field.path==='assemble.video_encoder')" class="encoding-note">CQ 与软件 CRF 不是同一质量刻度，数值相同不代表画质相同；建议先生成试片确认。<span v-if="active.partial">局部修订沿用基准成片的编码设置；更换编码方式请完整生成普通版本。</span></p>
            <TextColorPresets v-if="group==='字幕'||group==='评论'" :kind="group" :color="values[group==='字幕'?'assemble.subtitle_color':'comments.color']" :outline-color="values[group==='字幕'?'assemble.subtitle_outline_color':'comments.outline_color']" :source-enabled="!!values[group==='字幕'?'assemble.subtitle_source_style_enabled':'comments.source_style_enabled']" :source-color="values[group==='字幕'?'assemble.subtitle_source_color':'comments.source_color']" :source-outline-color="values[group==='字幕'?'assemble.subtitle_source_outline_color':'comments.source_outline_color']" :source-outline="values[group==='字幕'?'assemble.subtitle_source_outline':'comments.source_outline']" :disabled="fieldLocked(group==='字幕'?'assemble.subtitle_color':'comments.color')" @select="(id,source)=>preset(group,id,source)" @enable-source="enableSourceStyle(group)" />
            <section v-for="styleGroup in styleGroups(group)" :key="styleGroup.name"><h5>{{ styleGroup.name }}</h5><div v-for="field in styleGroup.fields" :key="field.path" class="version-setting"><ParameterHelp :label="field.label" :help="versionFieldHelp(field)"><div class="field-row"><span>{{ field.label }}</span><ParamControl :field="field" :values="values" :model-value="values[field.path]" :disabled="fieldDisabled(field)" @update:model-value="override(field.path,$event)" /><span class="field-source"><ConfigurationSourceBadge :source="fieldSource(field.path)" :inherited="!Object.hasOwn(active.overrides,field.path)"/><ParameterResetButton :label="field.label" action="恢复项目默认" :disabled="fieldLocked(field.path) || !Object.hasOwn(active.overrides,field.path)" :help="Object.hasOwn(active.overrides,field.path)?'移除此项版本专用值，恢复本项目已保存的默认值；保存后生效':'已继承本项目默认值，无需恢复'" @reset="inherit(field.path)" /></span></div></ParameterHelp></div></section>
          </details>
          </template>
          <details v-if="!configureOnly && state.versions.find((row:any)=>row.id===activeId)?.audio_report" class="formal-audio"><summary>已生成正式成片的声音报告 <small>本页试片报告见右侧结果</small></summary><AudioQualityReport :uid="uid" :report="state.versions.find((row:any)=>row.id===activeId)?.audio_report" :outdated="dirty || state.versions.find((row:any)=>row.id===activeId)?.fresh===false" /></details>
          <div class="editor-save"><n-tag v-if="dirty" type="warning" size="small">版本调整未保存</n-tag><n-button size="small" secondary :disabled="settingsLocked || !dirty" @click="save">保存版本设置</n-button><n-button size="tiny" quaternary :disabled="settingsLocked || !dirty" @click="dialog.warning({title:'放弃本页未保存的版本调整？',positiveText:'恢复已保存',negativeText:'继续编辑',onPositiveClick:()=>adopt(state)})">恢复已保存</n-button></div>
        </div>
        <div v-if="active && !configureOnly && !cutsExpanded" class="preview-column">
          <n-alert v-if="active.update" :type="savedActive?.fresh?'success':'info'" :show-icon="false">{{ savedActive?.fresh?'本次所选更新已生成；未应用的工作稿改动仍显示在上方差异清单。':'此方案已固定勾选内容，正在生成或等待执行；完成后在“正式成品与原片”检查。' }}</n-alert>
          <n-alert v-else-if="active.partial" :type="savedActive?.fresh && !dirty?'success':'info'" :show-icon="false">{{ savedActive?.fresh && !dirty?'局部修订已生成，可展开下方“正式成品与原片”检查替换范围与衔接。':'局部修订按固定范围生成。请保存并生成该版本，再从下方“正式成品与原片”检查替换范围与衔接。' }}</n-alert>
          <template v-if="active.update || active.partial || active.timeline">
            <section class="revision-output-preview" aria-label="当前修订版成片预览">
              <header class="revision-preview-heading">
                <div><span class="stage-kicker">步骤 2 · 检查修订结果</span><h3>当前修订版成片</h3><p>这里播放此版本已生成的正式成片；局部修订与成片剪辑均使用固定来源；在上方剪辑中可按删除清单试听。</p></div>
                <n-tag v-if="activeFormalArtifact" size="small" :bordered="false" type="success">已生成</n-tag>
                <n-tag v-else size="small" :bordered="false" type="warning">尚未生成</n-tag>
              </header>
              <VideoPreview v-if="activeFormalPreviewUrl" :key="activeFormalPreviewUrl" :src="activeFormalPreviewUrl" />
              <div v-else class="revision-preview-empty" role="status">
                <span class="empty-symbol" aria-hidden="true">▷</span>
                <strong>还没有可播放的修订成片</strong>
                <small>先保存修订方案并生成此版本；完成后会自动显示在这里。旧基准成片仍保留，可在下方正式成品列表播放。</small>
              </div>
              <small v-if="activeFormalArtifact" class="revision-preview-note">{{ activeFormalArtifact.version_summary || '当前版本的正式成片' }}<template v-if="activeFormalArtifact.created_at"> · {{ activeFormalArtifact.created_at }}</template></small>
            </section>
          </template>
          <AssemblyVersionPreview v-else :key="uid" :uid="uid" :version="active" :overlay-revision="commentRevision" :draft="dirty"
                                  :effective-values="values" :output-cuts="savedActive?.output_cuts || null" :disabled="nav.archived || !!props.disabled || busy || defaultsBusy || cutsBusy || cutsDirty" />
        </div>
        </div>
      </div>
      <div v-if="!configureOnly" class="footer"><div class="delivery-explanation"><span class="stage-kicker">步骤 3 · 确认后生成整片</span><strong>生成正式成片</strong><small>{{ dirty?'先保存版本调整，再生成正式成片。':cutsDirty?'请先保存或放弃上方剪辑调整。':selected.length?`已勾选 ${selected.length} 个版本，未变化的成片会复用。`:'在版本清单中勾选需要生成的版本。' }}</small></div><n-button secondary :disabled="actionLocked || allDirty || !selected.length" @click="generate">生成勾选版本（{{ selected.length }}）</n-button></div>
      <DependencyNotice :uid="uid" :items="blockers" />
      <div class="list-actions"><small>版本清单：</small><ConfigurationSourceBadge :source="dirty?{kind:'project'}:state.source" :inherited="!dirty&&state.inherited"/><n-button v-if="!state.inherited" size="tiny" :disabled="settingsLocked||dirty" @click="restoreInheritance">恢复项目基础清单</n-button><RouterLink to="/settings?section=production">管理制作与发布设置</RouterLink></div>
    </template>
    <p v-else-if="!error" class="help">正在读取版本…</p>
  </n-card>
</template>
<style scoped>
.encoding-note{font-size:12px;line-height:1.75;color:var(--df-muted);margin:12px 0}.encoding-note span{display:block}
.project-production-targets{display:grid;gap:8px;padding:16px;border:1px solid var(--df-border);border-radius:10px;margin-top:18px;background:var(--df-panel-raised)}.project-production-targets label{display:grid;gap:10px}.project-production-targets strong{font-size:14px}.project-production-targets p{margin:0;font-size:12px;line-height:1.7;color:var(--df-muted)}.project-production-targets .target-warning{color:var(--df-warning,#e3b76c)}
.timeline-encoding-summary{display:grid;gap:8px;margin:12px 0;padding:12px 14px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-panel-raised)}.timeline-encoding-summary>div{display:flex;align-items:center;gap:10px}.timeline-encoding-summary p{display:flex;gap:18px;flex-wrap:wrap;margin:0;font-size:12px}.timeline-encoding-summary strong{margin-left:5px;font-variant-numeric:tabular-nums}.timeline-encoding-summary small,.timeline-encoding-summary>div>span{color:var(--df-muted);font-size:11px;line-height:1.6}.version-source,.version-definition{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.version-source{margin-top:7px}.version-definition{margin-top:12px;font-size:12px}.version-definition small{color:var(--df-muted);font-size:11px}.field-source{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.parameter-inheritance{display:flex;align-items:center;justify-content:space-between;gap:8px 16px;flex-wrap:wrap;margin:12px 0}.parameter-inheritance small{flex:1 1 220px;font-size:11px;color:var(--df-muted);line-height:1.6}.version-workspace{margin-top:14px}.workspace-links,.list-actions,.editor-heading,.footer,.footer>div{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.workspace-links{font-size:12px;margin-bottom:16px}.workspace-grid{display:grid;grid-template-columns:240px minmax(0,1fr);gap:22px}.version-row{display:flex;gap:10px;align-items:center;padding:10px;border:1px solid transparent;border-radius:8px;margin-bottom:6px}.version-row.active{background:var(--df-accent-soft);border-color:var(--df-border)}.version-choice{border:0;background:none;color:inherit;text-align:left;cursor:pointer;min-width:0;flex:1;padding:0}.version-choice strong,.version-choice small{display:block;overflow-wrap:anywhere}.version-choice small,summary small,.help,.templates small{color:var(--df-muted);font-size:11px;margin-top:5px}.active-baseline-label,.version-choice strong .active-baseline-label{display:inline-flex;margin-left:8px;padding:2px 6px;border-radius:999px;background:var(--df-accent-soft);color:var(--df-accent,#e3b76c);font-size:10px;font-weight:500;vertical-align:middle}.version-editor{min-width:0}.editor-heading{flex-wrap:nowrap}.editor-heading :deep(.n-input){flex:1;min-width:0}.dimensions{display:grid;grid-template-columns:minmax(100px,1fr) 1fr 1fr;gap:14px;margin:18px 0}.dimensions .dimension-setting{display:flex;flex-direction:column;gap:8px;font-size:12px}.dimension-label{display:flex;align-items:center;justify-content:space-between;gap:4px;min-height:24px}.dimensions :deep(.n-switch){align-self:flex-start}.field-row{display:grid;grid-template-columns:100px minmax(0,1fr) auto;gap:12px;align-items:center;margin:14px 0;font-size:12px}.field-row :deep(.n-input-number){max-width:100%}.settings-group{border-top:1px solid var(--df-border);padding:12px 0}summary{cursor:pointer;font-size:12px}.settings-group summary small{margin-left:8px}.footer{justify-content:space-between;border-top:1px solid var(--df-border);padding-top:16px;margin-top:18px}.templates{margin-top:16px}.templates .list-actions{margin:12px 0}.list-actions{margin-top:12px}.help{cursor:help}@media(max-width:1100px){.workspace-grid{grid-template-columns:1fr}aside{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}}@media(max-width:600px){aside{grid-template-columns:1fr}.field-row{grid-template-columns:90px minmax(0,1fr)}.field-row>:last-child{grid-column:2;justify-self:start}.dimensions{gap:8px}}
.sound-guide{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:12px;background:var(--df-accent-soft);border-radius:8px;margin:14px 0;font-size:12px}.sound-guide small{font-size:11px;line-height:1.6;color:var(--df-muted)}.version-setting{margin:14px 0}.version-setting .field-row{margin:0;min-height:28px;grid-template-columns:minmax(130px,1fr) minmax(100px,1.2fr) auto}.version-setting :deep(.parameter-help-trigger){margin-top:5px}.settings-group h5{color:var(--df-muted);font-weight:500;margin:16px 0 8px}@media(max-width:720px){.sound-guide{grid-template-columns:1fr;gap:12px}.version-setting .field-row{grid-template-columns:minmax(100px,1fr) minmax(100px,1.2fr)}.version-setting .field-source{grid-column:2;justify-self:start}}
.workspace-grid{display:block}.version-list-heading{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:18px 0 10px}.version-list-heading strong{font-size:13px}.version-list-heading small,.editor-title p{font-size:12px;color:var(--df-muted);line-height:1.6}.version-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px}.version-row{padding:12px;border-color:var(--df-border);margin:0;align-items:flex-start}.version-row :deep(.n-checkbox){margin-top:3px}.version-row.active{border-color:color-mix(in srgb,var(--df-accent,#e3b76c) 65%,var(--df-border))}.version-choice{border-radius:3px}.version-choice:focus-visible{outline:2px solid var(--df-accent,#e3b76c);outline-offset:4px}.version-choice strong{font-size:13px;font-weight:600}.version-source small{margin-top:0}.workbench-body{display:grid;grid-template-columns:minmax(350px,.9fr) minmax(440px,1.2fr);gap:28px;margin-top:24px;padding-top:22px;border-top:1px solid var(--df-border);align-items:start}.workbench-body.configure-only{grid-template-columns:1fr}.editor-title{margin-bottom:18px}.stage-kicker{font-size:11px;font-weight:600;letter-spacing:.05em;color:var(--df-accent,#e3b76c)}.editor-title h3{font-size:19px;margin:5px 0 6px}.editor-title p{margin:0}.preview-column{position:sticky;top:16px;min-width:0}.version-editor{padding-right:24px;border-right:1px solid var(--df-border)}.configure-only .version-editor{border:0;padding:0}.sound-guide{grid-template-columns:1fr;gap:10px;padding:12px;margin:16px 0;font-size:11px}.sound-guide :deep(.parameter-help-target){display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}.sound-guide :deep(.parameter-help-target)>span{font-weight:500;min-width:96px}.version-setting .field-row{grid-template-columns:minmax(115px,1fr) minmax(100px,1.15fr);gap:7px 12px}.version-setting .field-source{grid-column:1/-1;justify-self:end}.version-setting{margin:16px 0}.field-row{grid-template-columns:80px minmax(0,1fr);gap:8px 12px}.field-row>.field-source{grid-column:1/-1;justify-self:end}.editor-save{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-top:1px solid var(--df-border);margin-top:14px;padding-top:16px}.formal-audio{border-top:1px solid var(--df-border);padding-top:12px;margin-top:12px}.formal-audio small{color:var(--df-muted);display:block;margin-top:4px}.workspace-secondary{border-bottom:1px solid var(--df-border);padding:0 0 14px}.workspace-secondary summary{font-size:12px;line-height:1.8}.workspace-secondary summary small{margin-left:10px;color:var(--df-muted);font-size:11px}.workspace-secondary summary :deep(.n-tag){margin-left:10px}.footer{padding:20px 0 2px;margin-top:24px;align-items:center}.delivery-explanation{display:flex;flex-direction:column;align-items:flex-start!important;gap:6px!important}.delivery-explanation strong{font-size:15px}.delivery-explanation small{color:var(--df-muted);font-size:12px}.workspace-grid>aside{display:block}.workspace-links{margin-bottom:14px}.dimensions{gap:10px}.dimensions .dimension-setting{font-size:11px}@media(max-width:1250px){.workbench-body{grid-template-columns:1fr;gap:24px}.version-editor{border:0;padding:0}.preview-column{position:static;border-top:1px solid var(--df-border);padding-top:22px}.version-setting .field-row{grid-template-columns:minmax(140px,1fr) minmax(120px,1.2fr) auto}.version-setting .field-source{grid-column:auto}.sound-guide{grid-template-columns:repeat(3,minmax(0,1fr))}.sound-guide :deep(.parameter-help-target){display:grid;gap:6px}.sound-guide :deep(.parameter-help-target)>span{min-width:0}}@media(max-width:650px){.version-list{grid-template-columns:1fr}.sound-guide{grid-template-columns:1fr}.version-setting .field-row{grid-template-columns:minmax(110px,1fr) minmax(100px,1.2fr)}.version-setting .field-source{grid-column:1/-1}.workspace-secondary summary small{display:block;margin-left:0}.footer{align-items:flex-start}.workspace-links{gap:8px 14px}}
@media(max-height:800px){.preview-column{position:static}}
.partial-scope{margin-bottom:16px}.partial-scope-ranges{margin:8px 0 0;padding-left:20px;font-variant-numeric:tabular-nums;max-height:140px;overflow:auto}
.revision-output-preview{min-width:0;border:1px solid var(--df-border);border-radius:12px;overflow:hidden;background:color-mix(in srgb,var(--df-accent,#e3b76c) 2%,transparent)}
.revision-preview-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px;border-bottom:1px solid var(--df-border)}
.revision-preview-heading h3{font-size:19px;margin:5px 0 6px}.revision-preview-heading p{margin:0;color:var(--df-muted);font-size:12px;line-height:1.7}
.revision-output-preview :deep(.video-preview){margin:0;border-radius:0}.revision-preview-note{display:block;margin:10px 16px 14px;color:var(--df-muted);font-size:11px;line-height:1.7}
.revision-preview-empty{min-height:280px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:24px;text-align:center;background:#0002}.revision-preview-empty strong{font-size:13px;font-weight:500}.revision-preview-empty small{max-width:360px;color:var(--df-muted);font-size:12px;line-height:1.7}.revision-preview-empty .empty-symbol{font-size:40px;font-weight:300;color:var(--df-muted);line-height:1}
.version-cut-workspace{grid-column:1/-1;min-width:0;margin-bottom:0}
.version-delete{flex-shrink:0}.version-row:focus-within{border-color:var(--df-accent,#e3b76c)}
</style>
