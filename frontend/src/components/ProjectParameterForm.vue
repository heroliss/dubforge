<script setup lang="ts">
// 所有步骤共用项目参数读写，避免试配页误改全局或各页保存语义不一致。
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NPopover, NSpace, NSpin, NText, NTooltip, useMessage } from 'naive-ui'
import ParamControl from './ParamControl.vue'
import DubbingPresets from './DubbingPresets.vue'
import TextColorPresets from './TextColorPresets.vue'
import { textColorPatch } from '../utils/textColorPresets'
import { textStyleFieldGroups } from '../utils/textStyleFields'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { sourcePresentation, type ConfigurationSource } from '../utils/configurationSource'

const props = defineProps<{ uid: string; stepKey: string; disabled?: boolean; lineId?: number; paths?: string[];
  additionalStepKeys?: string[]; sectionTitles?: Record<string, string>; excludePaths?: string[] }>()
const endpoint = computed(() => props.lineId == null ? `/api/projects/${props.uid}/step_params`
  : `/api/projects/${props.uid}/workbench/lines/${props.lineId}/params`)
const scopeLabel = computed(() => props.lineId == null ? '项目' : '本句')
const emit = defineEmits<{
  (e: 'draft-change', value: Record<string, any>): void
  (e: 'fields-change', value: string[]): void
  (e: 'dirty-change', value: boolean): void
  (e: 'saved'): void
}>()
const message = useMessage()
const fields = ref<any[]>([])
const draft = ref<Record<string, any>>({})
const defaults = ref<Record<string, any>>({})
const overrides = ref<Record<string, boolean>>({})
const sources = ref<Record<string, ConfigurationSource>>({}), parentSources = ref<Record<string, ConfigurationSource>>({})
const revision = ref<number>(), fingerprint = ref<string>()
const edited = ref<Record<string, boolean>>({})
const resetKeys = ref(new Set<string>())
const saved = ref('')
const loading = ref(false), saving = ref(false), error = ref('')
let request: AbortController | null = null
let disposed = false
const same = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b)
const dirty = computed(() => !!saved.value && (JSON.stringify(draft.value) !== saved.value || resetKeys.value.size>0 || Object.keys(edited.value).length>0))
const changed = (path: string) => !resetKeys.value.has(path) && (!!overrides.value[path] || !!edited.value[path] || !same(draft.value[path], defaults.value[path]))
const hasOverrides = computed(() => fields.value.some(f => changed(f.path)))
function fieldSource(path: string): ConfigurationSource | undefined {
  if (changed(path)) return { kind: props.lineId == null ? 'project' : 'line' }
  return resetKeys.value.has(path) ? parentSources.value[path] : sources.value[path]
}
const groups = computed(() => {
  const result = new Map<string, any[]>()
  for (const field of fields.value) {
    if (props.stepKey === 'tts') {
      if ((field.depends_on ?? []).some(([key, value]: [string, any]) => key === 'tts.backend' && draft.value[key] !== value)) continue
      if (['vector', 'text'].includes(field.type) && (field.depends_on ?? []).some(([key, value]: [string, any]) => draft.value[key] !== value)) continue
    }
    const group = field.group || (props.stepKey !== 'tts' ? '' :
      (field.path.endsWith('.url') || field.path.endsWith('.project_dir') || field.path === 'tts.backend' ? '服务连接' :
        field.path.startsWith('runner.') ? '运行与提示' : '音色与情感'))
    if (!result.has(group)) result.set(group, [])
    result.get(group)!.push(field)
  }
  return [...result.entries()].flatMap(([name, fields]) => textStyleFieldGroups(fields,
    fields.some(f=>f.path.startsWith('assemble.subtitle_')) ? 'assemble.subtitle_' : '', draft.value, name))
})
function applyPreset(values: Record<string, any>) {
  for (const field of fields.value) if (field.path in values) editField(field.path,values[field.path])
}
function hasTextColors(group: { fields: any[] }) {
  return ['assemble.subtitle_color', 'assemble.subtitle_outline_color'].every(path => group.fields.some(field => field.path === path))
}
function applyTextColors(id: string, source = false) {
  if (props.disabled || saving.value || loading.value) return
  const patch = textColorPatch(id, source ? 'assemble.subtitle_source_' : 'assemble.subtitle_')
  if (patch && Object.keys(patch).every(path => fields.value.some(field => field.path === path))) applyPreset(patch)
}
function enableSourceStyle() {
  const path = 'assemble.subtitle_source_style_enabled'
  if (!props.disabled && !saving.value && !loading.value && fields.value.some(field => field.path === path)) editField(path, true)
}
watch(draft, value => emit('draft-change', { ...value }), { deep: true })
watch(dirty, value => emit('dirty-change', value), { immediate: true })
function disabledField(field: any) {
  if (props.disabled || saving.value) return true
  if (field.path === 'subtitles.source_first' && !['all', 'bilingual'].includes(draft.value['subtitles.mode'])
      && draft.value['assemble.subtitle_track'] !== 'bilingual') return true
  return (field.depends_on ?? []).some(([path, value]: [string, any]) => !same(draft.value[path], value))
}

async function load() {
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
    const schemas = [...new Set([props.stepKey, ...(props.additionalStepKeys ?? [])])]
      .map(key => ({ key, schema: data.steps?.[key] }))
    fields.value = schemas.flatMap(({ key, schema }) => (schema?.fields ?? []).map((field: any) => ({
      ...field, group: props.sectionTitles?.[key] ?? field.group,
    }))).filter((field: any) => (!props.paths || props.paths.includes(field.path)) && !props.excludePaths?.includes(field.path))
    const values = Object.assign({}, ...schemas.map(({ schema }) => schema?.values ?? {}))
    defaults.value = data.defaults ?? {}
    overrides.value = data.overridden ?? {}
    sources.value = data.sources ?? {}; parentSources.value = data.parent_sources ?? {}
    revision.value = data.revision; fingerprint.value = data.fingerprint
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
async function save() {
  if (saving.value || props.disabled) return
  saving.value = true
  try {
    const updates: Record<string, any> = {}, reset: string[] = []
    for (const field of fields.value) {
      if (resetKeys.value.has(field.path)) reset.push(field.path)
      else if (edited.value[field.path] || !same(draft.value[field.path], JSON.parse(saved.value)[field.path])) updates[field.path] = draft.value[field.path]
    }
    const response = await fetch(endpoint.value, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ updates, reset, revision: revision.value, fingerprint: fingerprint.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '参数保存失败')
    if (disposed) return
    await load()
    message.success(`已保存${scopeLabel.value}参数`)
    emit('saved')
  } catch (err: any) {
    if (!disposed) message.error(err.message || '参数保存失败')
  } finally { saving.value = false }
}
function resetAll() {
  for (const field of fields.value) inheritField(field.path)
}
function inheritField(path:string) { draft.value[path]=defaults.value[path];resetKeys.value.add(path);delete edited.value[path] }
function editField(path:string,value:any) { draft.value[path]=value;edited.value[path]=true;resetKeys.value.delete(path) }
function discard() { if (saved.value && !saving.value) {draft.value = JSON.parse(saved.value);edited.value={};resetKeys.value=new Set()} }
watch(() => [props.uid, props.stepKey, props.lineId], load, { immediate: true })
onUnmounted(() => { disposed = true; request?.abort() })
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="error" type="error" :show-icon="false">
      {{ error }} <n-button size="tiny" @click="load">重试</n-button>
    </n-alert>
    <n-form v-else-if="fields.length" label-placement="top" size="small" :show-feedback="false">
      <DubbingPresets v-if="lineId == null && stepKey === 'tts' && draft['tts.backend'] === 'indextts25'" :draft="draft"
                      :disabled="disabled || saving" @load="applyPreset" />
      <section v-for="group in groups" :key="group.name" class="parameter-group">
      <h3 v-if="group.name">{{ group.name }}</h3>
      <TextColorPresets v-if="hasTextColors(group)" kind="字幕" class="subtitle-palettes"
        :color="draft['assemble.subtitle_color']" :outline-color="draft['assemble.subtitle_outline_color']" :outline="draft['assemble.subtitle_outline']"
        :source-enabled="!!draft['assemble.subtitle_source_style_enabled']" :source-color="draft['assemble.subtitle_source_color']" :source-outline-color="draft['assemble.subtitle_source_outline_color']" :source-outline="draft['assemble.subtitle_source_outline']"
        :disabled="disabled || saving || loading" @select="applyTextColors" @enable-source="enableSourceStyle" />
      <div class="parameter-grid">
        <n-form-item v-for="field in group.fields" :key="field.path" :class="{ 'wide-field': field.type === 'vector' }">
          <template #label>
            <n-popover :disabled="!field.help" trigger="click" placement="top-start" width="trigger" style="max-width: 420px; box-sizing: border-box">
            <template #trigger><span class="parameter-label" :title="field.help">
              {{ field.label }}
              <ConfigurationSourceBadge :source="fieldSource(field.path)" :inherited="!changed(field.path)" />
              <n-button v-if="field.help" size="tiny" quaternary circle :aria-label="`${field.label}说明`">?</n-button>
            </span></template>
            {{ field.help }}
            </n-popover>
          </template>
          <div class="parameter-control">
            <n-tooltip :disabled="!field.help" :delay="350" placement="top-start" width="trigger" style="max-width: 420px; box-sizing: border-box">
              <template #trigger><div class="control-tip"><ParamControl :field="field" :model-value="draft[field.path]" :disabled="disabledField(field)" @update:model-value="editField(field.path,$event)" /></div></template>
              {{ field.help }}
            </n-tooltip>
            <n-button v-if="changed(field.path)" size="tiny" quaternary :disabled="disabled || saving"
                      :aria-label="`${field.label}恢复继承`" :title="`恢复继承 · ${sourcePresentation(parentSources[field.path]).title}，保存后生效`" @click="inheritField(field.path)">↶</n-button>
          </div>
        </n-form-item>
      </div>
      </section>
      <slot name="actions" :save="save" :saving="saving" :loading="loading" :dirty="dirty" :has-overrides="hasOverrides" :reset-all="resetAll" :discard="discard">
        <n-space align="center" class="parameter-footer">
          <n-button type="primary" size="small" :loading="saving" :disabled="!dirty || disabled || loading" @click="save">保存{{ scopeLabel }}参数</n-button>
          <n-button v-if="hasOverrides" size="small" quaternary :disabled="disabled || saving" @click="resetAll">恢复继承（待保存）</n-button>
          <n-text v-if="dirty" type="warning" style="font-size: 12px">有未保存的修改</n-text>
        </n-space>
      </slot>
      <n-text depth="3" class="scope-note">{{ lineId == null ? '全局默认 → 系列模板 → 项目设置' : '本句未覆盖的参数跟随项目的最终设置' }} · 保存不自动运行</n-text>
    </n-form>
  </n-spin>
</template>

<style scoped>
.parameter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(250px, 100%), 1fr)); gap: 14px 24px; }
.parameter-grid > * { min-width: 0; }
.subtitle-palettes { margin-bottom:16px; }
.parameter-group + .parameter-group { margin-top: 24px; border-top: 1px solid rgba(128,128,128,.16); padding-top: 16px; }
.parameter-group h3 { font-size: 14px; font-weight: 600; margin: 0 0 18px; color: var(--df-accent); }
.wide-field { grid-column: 1 / -1; }
.control-tip { flex: 1; min-width: 0; }
.parameter-label { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; width: 100%; }
.parameter-grid :deep(.n-form-item-label__text) { width: 100%; }
.parameter-control { display: flex; align-items: center; gap: 6px; width: 100%; min-width: 0; }
.parameter-control :deep(.n-input-number), .parameter-control :deep(.n-input), .parameter-control :deep(.n-select) { max-width: 100%; min-width: 0; }
.parameter-footer { margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--df-border); }
.scope-note { display: block; margin-top: 10px; font-size: 12px; }
</style>
