<script setup lang="ts">
// 所有步骤共用项目参数读写，避免试配页误改全局或各页保存语义不一致。
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NSpace, NSpin, NText, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import ParameterHelp from './ParameterHelp.vue'
import ParameterLabel from './ParameterLabel.vue'
import ParameterResetButton from './ParameterResetButton.vue'
import { parameterHelp, parameterDependencyMatches } from '../utils/parameterPresentation'
import { indexTtsFieldHelp, indexTtsFieldInactiveReason, indexTtsFieldInactiveSummary } from '../utils/indexTtsSettings'
import DubbingPresets from './DubbingPresets.vue'
import { INDEX_TTS_PRONUNCIATION_PATH, indexTtsParameterGroups } from '../utils/dubbingPreset'
import TextColorPresets from './TextColorPresets.vue'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups } from '../utils/textStyleFields'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { sourcePresentation, type ConfigurationSource } from '../utils/configurationSource'
import { useConfigurationEditability } from '../utils/configurationEditability'
import ConfigurationLockNotice from './ConfigurationLockNotice.vue'
import { currentIndexTtsReferenceLine, indexTtsReferenceModeCarry } from '../utils/indexTtsReferenceSentence'
import { audioSettingGroups, audioFieldHelp } from '../utils/audioQuality'
import { isAudioSetting } from '../utils/assemblyAudio'
import { encodingFieldHelp } from '../utils/assemblyEncoding'

const props = defineProps<{ uid: string; stepKey: string; disabled?: boolean; lineId?: number; executionState?: any; paths?: string[];
  additionalStepKeys?: string[]; sectionTitles?: Record<string, string>; excludePaths?: string[]; refreshToken?: number | string }>()
const editability = useConfigurationEditability(() => `project:${props.uid}`)
const endpoint = computed(() => props.lineId == null ? `/api/projects/${props.uid}/step_params`
  : `/api/projects/${props.uid}/workbench/lines/${props.lineId}/params`)
const scopeLabel = computed(() => props.lineId == null ? '项目' : '本句')
const resetLabel = computed(() => props.lineId == null ? '恢复项目默认设置' : '恢复项目设置')
const emit = defineEmits<{
  (e: 'draft-change', value: Record<string, any>): void
  (e: 'fields-change', value: string[]): void
  (e: 'dirty-change', value: boolean): void
  (e: 'busy-change', value: boolean): void
  (e: 'saved'): void
}>()
const message = useMessage()
const fields = ref<any[]>([])
const draft = ref<Record<string, any>>({})
const defaults = ref<Record<string, any>>({})
const overrides = ref<Record<string, boolean>>({})
const sources = ref<Record<string, ConfigurationSource>>({}), parentSources = ref<Record<string, ConfigurationSource>>({})
const revision = ref<number | string>(), fingerprint = ref<string>()
const lineExecution = ref<any>(null)
const lineText = ref('')
const edited = ref<Record<string, boolean>>({})
const resetKeys = ref(new Set<string>())
const saved = ref('')
const loading = ref(false), saving = ref(false), error = ref('')
const presetBusy = ref(false)
const pending = computed(() => saving.value || presetBusy.value)
const locked = computed(() => props.disabled || pending.value || loading.value || editability.locked.value)
const pathLocked = (path: string) => props.lineId != null && lineExecution.value?.editable
  ? false : editability.isPathLocked(path)
const updatesRunningVoice = computed(() => props.lineId != null && !!lineExecution.value?.active && !!lineExecution.value?.accepting)
const canRetryVoice = computed(() => updatesRunningVoice.value && ['failed', 'interrupted'].includes(lineExecution.value?.state)
  && !dirty.value && !pending.value && !loading.value)
const saveLabel = computed(() => updatesRunningVoice.value ? '保存并更新本句配音'
  : props.lineId != null && lineExecution.value?.active ? '仅保存本句设置' : `保存${scopeLabel.value}参数`)
const anyPathLocked = computed(() => fields.value.some(field => pathLocked(field.path)))
let request: AbortController | null = null
let disposed = false
let externalRefreshQueued = false
const same = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b)
const dirty = computed(() => !!saved.value && (JSON.stringify(draft.value) !== saved.value || resetKeys.value.size>0 || Object.keys(edited.value).length>0))
const changed = (path: string) => !resetKeys.value.has(path) && (!!overrides.value[path] || !!edited.value[path] || !same(draft.value[path], defaults.value[path]))
const hasOverrides = computed(() => fields.value.some(f => changed(f.path)))
function fieldSource(path: string): ConfigurationSource | undefined {
  if (changed(path)) return { kind: props.lineId == null ? 'project' : 'line' }
  return resetKeys.value.has(path) ? parentSources.value[path] : sources.value[path]
}
function fieldResetLabel(path: string) {
  if (path === INDEX_TTS_PRONUNCIATION_PATH) return '恢复跟随译文'
  return props.lineId != null ? '恢复项目设置' : parentSources.value[path]?.frozen ? '恢复项目基础值' : '恢复服务设置'
}
function fieldResetDisabled(path: string) {
  return !!locked.value || pathLocked(path) || !changed(path)
    || currentIndexTtsReferenceLine(path, draft.value, props.lineId) != null
}
function fieldResetHelp(path: string) {
  if (path === INDEX_TTS_PRONUNCIATION_PATH) return '恢复为当前译文，保存后继续跟随字幕修改。'
  if (currentIndexTtsReferenceLine(path, draft.value, props.lineId) != null) return '本句原声自动使用当前句子，无需单独恢复；切换为指定句原声时沿用此句。'
  return `${fieldResetLabel(path)} · ${sourcePresentation(parentSources.value[path]).title}，保存后生效。`
}
// 继承时直接显示当前译文，不把显示值写成单句覆盖；编辑中的空串保留，避免删除时跳回原文。
function fieldValue(path: string) {
  const currentReference = currentIndexTtsReferenceLine(path, draft.value, props.lineId)
  if (currentReference != null) return currentReference
  if (props.lineId != null && path === INDEX_TTS_PRONUNCIATION_PATH && !draft.value[path] && !edited.value[path]) return lineText.value
  return draft.value[path]
}
const readingDiffers = computed(() => !!lineText.value && fieldValue(INDEX_TTS_PRONUNCIATION_PATH) !== lineText.value)
const groups = computed(() => {
  const result = new Map<string, any[]>()
  const visible: any[] = []
  const useLlm = Boolean(draft.value['split.use_llm'] ?? true)
  for (const field of fields.value) {
    if (field.path === 'split.target_word_count' && !useLlm) continue
    if (field.path === 'split.max_split_length' && useLlm) continue
    if (props.stepKey === 'tts') {
      if ((field.depends_on ?? []).some(([key, value]: [string, any]) => key === 'tts.backend' && draft.value[key] !== value)) continue
    }
    visible.push(field)
    const group = field.group || (props.stepKey !== 'tts' ? '' :
      (field.path.endsWith('.url') || field.path.endsWith('.project_dir') || field.path === 'tts.backend' ? '服务连接' :
        field.path.startsWith('runner.') ? '运行与提示' : '音色与情感'))
    if (!result.has(group)) result.set(group, [])
    result.get(group)!.push(field)
  }
  if (props.stepKey === 'tts') return indexTtsParameterGroups(visible)
  // 旧 schema 可能把声音和字幕放在同一组；按声音作用范围分开，保留其他字段和显式栏目名称。
  if (props.stepKey === 'assemble' && !props.sectionTitles?.assemble) {
    const audio=visible.filter(field=>isAudioSetting(field.path))
    const others=[...result.entries()].flatMap(([name, fields]) => textStyleFieldGroups(fields.filter(field=>!isAudioSetting(field.path)),
      fields.some(field=>field.path.startsWith('assemble.subtitle_'))?'assemble.subtitle_':'',draft.value,name)).filter(group=>group.fields.length)
    return [...audioSettingGroups(audio),...others]
  }
  return [...result.entries()].flatMap(([name, fields]) => textStyleFieldGroups(fields,
    fields.some(f=>f.path.startsWith('assemble.subtitle_')) ? 'assemble.subtitle_' : '', draft.value, name))
})
const presetDisabled = computed(() => locked.value || pathLocked('tts')
  || !!indexTtsParameterGroups(fields.value).find(group => group.name === '预设参数')?.fields.some((field: any) => pathLocked(field.path)))
function applyPreset(values: Record<string, any>) {
  for (const field of fields.value) if (field.path in values) editField(field.path,values[field.path],false)
}
function hasTextColors(group: { fields: any[] }) {
  return ['assemble.subtitle_color', 'assemble.subtitle_outline_color'].every(path => group.fields.some(field => field.path === path))
}
function applyTextColors(id: string, source = false) {
  if (locked.value) return
  const patch = textColorPatch(id, source ? 'assemble.subtitle_source_' : 'assemble.subtitle_')
  if (patch && Object.keys(patch).every(path => fields.value.some(field => field.path === path))) applyPreset(patch)
}
function enableSourceStyle() {
  const path = 'assemble.subtitle_source_style_enabled'
  if (!locked.value && fields.value.some(field => field.path === path)) editField(path, true)
}
watch(draft, value => emit('draft-change', { ...value }), { deep: true })
watch(dirty, value => emit('dirty-change', value), { immediate: true })
watch(pending, value => emit('busy-change', value), { flush: 'sync' })
function disabledField(field: any) {
  if (locked.value) return true
  if (pathLocked(field.path)) return true
  if (indexTtsFieldInactiveReason(field.path, draft.value)) return true
  if (field.path === 'subtitles.source_first' && !['all', 'bilingual'].includes(draft.value['subtitles.mode'])
      && draft.value['assemble.subtitle_track'] !== 'bilingual') return true
  const splitLength = ['split.target_word_count', 'split.max_split_length'].includes(field.path)
  return (field.depends_on ?? []).some(([path, value]: [string, any]) =>
    !parameterDependencyMatches(splitLength && path === 'split.use_llm' ? Boolean(draft.value[path] ?? true) : draft.value[path], value))
}
function fieldHelp(field: any) { return parameterHelp(field, encodingFieldHelp({...field,help:audioFieldHelp({...field,help:indexTtsFieldHelp(field.path, field.help, draft.value)},draft.value,'configuration')},draft.value,'configuration')) }

async function load(preserveDraft = false) {
  request?.abort()
  request = new AbortController()
  const current = request
  loading.value = true
  error.value = ''
  try {
    const response = await fetch(endpoint.value, { cache: 'no-store', signal: current.signal })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '读取项目参数失败')
    if (disposed || current !== request) return
    if (preserveDraft === true && (dirty.value || pending.value)) { externalRefreshQueued = true; return }
    const schemas = [...new Set([props.stepKey, ...(props.additionalStepKeys ?? [])])]
      .map(key => ({ key, schema: data.steps?.[key] }))
    fields.value = schemas.flatMap(({ key, schema }) => (schema?.fields ?? []).map((field: any) => ({
      ...field, group: props.sectionTitles?.[key] ?? field.group,
    }))).filter((field: any) => (!props.paths || props.paths.includes(field.path)) && !props.excludePaths?.includes(field.path))
    const values = Object.assign({}, ...schemas.map(({ schema }) => schema?.values ?? {}))
    defaults.value = data.defaults ?? {}
    overrides.value = data.overridden ?? {}
    sources.value = data.sources ?? {}; parentSources.value = data.parent_sources ?? {}
    lineExecution.value = data.execution ?? null
    lineText.value = data.line_text ?? ''
    revision.value = data.revision ?? data.execution?.revision; fingerprint.value = data.fingerprint
    edited.value = {}; resetKeys.value = new Set()
    draft.value = { ...data.context, ...Object.fromEntries(fields.value.map(field => [field.path,
      values[field.path] ?? defaults.value[field.path] ?? (field.type === 'bool' ? false : '')])) }
    saved.value = JSON.stringify(draft.value)
    emit('fields-change', fields.value.map(field => field.path))
  } catch (err: any) {
    if (!current.signal.aborted) error.value = err.message || '读取项目参数失败'
  } finally {
    if (current === request) loading.value = false
  }
}
// 同页其他工作区保存后就地回读；草稿和正在提交的并发版本保留，清空草稿后再更新。
function refreshSavedParameters() {
  if (disposed) return
  externalRefreshQueued = true
  if (dirty.value || pending.value) return
  externalRefreshQueued = false
  void load(true)
}
async function save(execution: 'auto' | 'save' | 'retry' = 'auto') {
  if (locked.value) return
  saving.value = true
  const key = endpoint.value
  try {
    const updates: Record<string, any> = {}, reset: string[] = []
    for (const field of fields.value) {
      if (resetKeys.value.has(field.path)) reset.push(field.path)
      else if (edited.value[field.path] || !same(draft.value[field.path], JSON.parse(saved.value)[field.path])) updates[field.path] = draft.value[field.path]
    }
    if (props.lineId == null && editability.observing.value
      && !await editability.canEditPaths([...Object.keys(updates), ...reset])) {
      message.info(editability.reason.value || '这些设置正在使用，请在当前步骤结束后保存'); return
    }
    if (disposed || key !== endpoint.value) return
    const response = await fetch(key, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ updates, reset, revision: revision.value, fingerprint: fingerprint.value,
        ...(props.lineId == null ? {} : { execution: updatesRunningVoice.value ? execution : 'save' }) }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '参数保存失败')
    if (disposed || key !== endpoint.value) return
    // 保存回执到达即通知工作区刷新；表单回读与状态刷新并行，避免慢回读拖延配音提示。
    emit('saved')
    message.success(data.execution?.message || `已保存${scopeLabel.value}参数`)
    await load()
  } catch (err: any) {
    if (!disposed) message.error(err.message || '参数保存失败')
  } finally { saving.value = false }
}
function resetAll() {
  for (const field of fields.value) inheritField(field.path)
}
function inheritField(path:string) { if (locked.value||pathLocked(path)) return; draft.value[path]=defaults.value[path];resetKeys.value.add(path);delete edited.value[path] }
function editField(path:string,value:any,carryReference=true) {
  if (locked.value||pathLocked(path)) return
  const carry = carryReference ? indexTtsReferenceModeCarry(path,value,draft.value,props.lineId) : undefined
  draft.value[path]=value;edited.value[path]=true;resetKeys.value.delete(path)
  if (carry && draft.value[carry.path] !== carry.value) {
    draft.value[carry.path]=carry.value;edited.value[carry.path]=true;resetKeys.value.delete(carry.path)
  }
}
function discard() { if (saved.value && !pending.value) {draft.value = JSON.parse(saved.value);edited.value={};resetKeys.value=new Set()} }
watch(() => [props.uid, props.stepKey, props.lineId], () => { externalRefreshQueued = false; void load() }, { immediate: true })
watch(() => props.refreshToken, refreshSavedParameters)
watch([dirty, pending], () => { if (externalRefreshQueued && !dirty.value && !pending.value) refreshSavedParameters() })
// 更新运行回执不覆盖表单草稿或保存所用的并发版本号。
watch(() => props.executionState, value => { if (props.lineId != null && value) lineExecution.value = value }, { deep: true })
onUnmounted(() => { disposed = true; request?.abort(); emit('busy-change', false) })
</script>

<template>
  <n-spin :show="loading">
    <ConfigurationLockNotice v-if="editability.locked.value" :reason="editability.reason.value"
                             :refresh="editability.refresh" />
    <n-alert v-else-if="anyPathLocked" type="info" :show-icon="false">
      当前步骤相关设置已在运行中冻结；其他步骤和排队任务仍可编辑。
    </n-alert>
    <n-alert v-if="error" type="error" :show-icon="false">
      {{ error }} <n-button size="tiny" @click="load()">重试</n-button>
    </n-alert>
    <n-alert v-if="lineId != null && lineExecution?.active" type="info" :show-icon="false" class="line-execution-note">
      <template v-if="lineExecution.accepting && lineExecution.kind === 'generation'">当前请求保持原配置。保存后，本句待执行的请求使用新设置，仍生成到已确认的配音槽 {{ lineExecution.target_take }}；如已开始，将在当前请求结束后再次生成。</template>
      <template v-else>{{ lineExecution.accepting ? '当前请求保持原配置。保存后，未执行句使用新配置；本句如已开始，将在当前句结束后重新合成，成功后覆盖当前槽。需要保留旧声音对比时，请仅保存设置，再选择新槽生成。' : '正在完成排布或收尾；现在可以保存本句设置，之后再运行配音。' }}</template>
      <span v-if="lineExecution.message">{{ lineExecution.message }}</span>
      <n-button v-if="canRetryVoice" size="small" secondary @click="save('retry')">{{ lineExecution.kind === 'generation' ? '重试本句配音' : '重试本句更新' }}</n-button>
    </n-alert>
    <n-form v-if="!error && fields.length" label-placement="top" size="small" :show-feedback="false">
      <section v-for="group in groups" :key="group.name" class="parameter-group" :class="{'tts-parameter-group':stepKey==='tts'&&draft['tts.backend']==='indextts25'}" :aria-label="group.name">
      <div v-if="group.name" class="parameter-group-heading">
        <h3>{{ group.name }}</h3>
      </div>
      <DubbingPresets v-if="lineId == null && stepKey === 'tts' && draft['tts.backend'] === 'indextts25' && group.name === '预设参数'" :draft="draft" :fields="fields"
                      :disabled="presetDisabled" :scope-label="scopeLabel+'参数'" @load="applyPreset" @busy-change="presetBusy=$event" />
      <TextColorPresets v-if="hasTextColors(group)" kind="字幕" class="subtitle-palettes"
        :color="draft['assemble.subtitle_color']" :outline-color="draft['assemble.subtitle_outline_color']" :outline="draft['assemble.subtitle_outline']"
        :source-enabled="!!draft['assemble.subtitle_source_style_enabled']" :source-color="draft['assemble.subtitle_source_color']" :source-outline-color="draft['assemble.subtitle_source_outline_color']" :source-outline="draft['assemble.subtitle_source_outline']"
        :disabled="locked||pathLocked('assemble')" @select="applyTextColors" @enable-source="enableSourceStyle" />
      <div class="parameter-grid df-settings-grid">
        <n-form-item v-for="field in group.fields" :key="field.path" class="df-setting" :class="{ 'wide-field': field.type === 'vector' || field.path === INDEX_TTS_PRONUNCIATION_PATH }">
          <template #label>
            <ParameterHelp :help="fieldHelp(field)" :label="field.label"><ParameterLabel :field="field">
              <span v-if="currentIndexTtsReferenceLine(field.path, draft, lineId) != null" class="reference-follow-label" title="本句原声自动使用当前句子；切换为指定句原声时沿用此句">跟随本句</span>
              <ConfigurationSourceBadge v-else :source="fieldSource(field.path)" :inherited="!changed(field.path)" />
              <ParameterResetButton :label="field.label" :action="fieldResetLabel(field.path)" :help="fieldResetHelp(field.path)"
                                    :disabled="fieldResetDisabled(field.path)" @reset="inheritField(field.path)" />
            </ParameterLabel></ParameterHelp>
          </template>
          <div class="parameter-field-body">
          <div class="parameter-control">
            <div class="control-tip"><ParameterHelp :help="fieldHelp(field)" :label="field.label" :show-icon="false">
              <ParamControl :field="field" :values="draft" :project-uid="uid" :reference-follows-current="currentIndexTtsReferenceLine(field.path, draft, lineId) != null" :model-value="fieldValue(field.path)" :disabled="disabledField(field)" @update:model-value="editField(field.path,$event)" />
            </ParameterHelp></div>
          </div>
          <small v-if="indexTtsFieldInactiveReason(field.path, draft)" class="field-inactive" :title="indexTtsFieldInactiveReason(field.path, draft)">{{ indexTtsFieldInactiveSummary(field.path, draft) }}</small>
          <slot name="field-reference" :field="field" :value="draft[field.path]" />
          </div>
        </n-form-item>
      </div>
      <div v-if="lineId != null && group.name === '朗读内容' && readingDiffers" class="pronunciation-guide pronunciation-source">
        <span>字幕译文</span><p>{{ lineText }}</p>
      </div>
      </section>
      <slot name="actions" :save="save" :save-label="saveLabel" :updates-running-voice="updatesRunningVoice" :saving="saving" :loading="loading" :locked="locked" :dirty="dirty" :has-overrides="hasOverrides" :reset-all="resetAll" :discard="discard">
        <n-space align="center" class="parameter-footer">
          <n-button type="primary" size="small" :loading="saving" :disabled="!dirty || locked" @click="save()">{{ saveLabel }}</n-button>
          <n-button v-if="updatesRunningVoice" size="small" quaternary :disabled="!dirty || locked" @click="save('save')">仅保存设置</n-button>
          <n-button v-if="hasOverrides" size="small" quaternary :disabled="locked" @click="resetAll">{{ resetLabel }}（待保存）</n-button>
          <n-text v-if="dirty" type="warning" style="font-size: 12px">有未保存的修改</n-text>
        </n-space>
      </slot>
      <n-text depth="3" class="scope-note">{{ lineId == null ? '制作基础值保留，变化需主动应用；连接、代理和超时等服务设置用于之后的调用，项目单独设置优先。保存不自动运行。' : updatesRunningVoice ? '本句未覆盖的参数跟随本次运行设置；连续保存会合并为最新配置，不中断当前请求。' : '本句未覆盖的参数跟随项目设置；保存不启动新任务。' }}</n-text>
    </n-form>
  </n-spin>
</template>

<style scoped>
.parameter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr)); gap: 14px 24px; }
.parameter-grid > * { min-width: 0; }
.subtitle-palettes { margin-bottom:16px; }
.parameter-group + .parameter-group { margin-top: 24px; border-top: 1px solid rgba(128,128,128,.16); padding-top: 16px; }
.parameter-group-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:18px; }
.parameter-group h3 { font-size: 14px; font-weight: 600; margin:0; color: var(--df-accent); }
.tts-parameter-group { padding:20px; border:1px solid var(--df-border); border-radius:12px; background:var(--df-panel); }
.tts-parameter-group + .tts-parameter-group { border-top:1px solid var(--df-border); padding-top:20px; }
.tts-parameter-group .parameter-grid { margin-top:18px; }
.wide-field { grid-column: 1 / -1; }
.control-tip { flex: 1; min-width: 0; }
.parameter-grid :deep(.n-form-item-label__text) { width: 100%; }
.parameter-control { display: flex; align-items: center; gap: 6px; width: 100%; min-width: 0; }
.parameter-field-body { width: 100%; min-width: 0; }
.field-inactive { display: block; margin-top: 6px; color: var(--df-muted); font-size: 11px; line-height: 1.6; }
.parameter-control :deep(.n-input-number), .parameter-control :deep(.n-input), .parameter-control :deep(.n-select) { max-width: 100%; min-width: 0; }
.parameter-footer { margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--df-border); }
.scope-note { display: block; margin-top: 10px; font-size: 12px; }
.reference-follow-label { color:var(--df-muted); font-size:11px; }
.line-execution-note { margin-bottom: 16px; font-size: 12px; }
.line-execution-note span { display: block; margin-top: 6px; }
.pronunciation-guide { font-size:12px; line-height:1.7; color:var(--df-muted); }
.pronunciation-source { margin-bottom:12px; padding:10px 12px; border-radius:8px; background:rgba(128,128,128,.06); }
.pronunciation-source > span { font-size:11px; }
.pronunciation-source p { margin:4px 0 0; color:var(--df-text); white-space:pre-wrap; overflow-wrap:anywhere; }
</style>
