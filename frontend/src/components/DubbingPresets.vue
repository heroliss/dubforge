<script setup lang="ts">
import FilePathLink from './FilePathLink.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NModal, NSelect, NText, useMessage } from 'naive-ui'
import { presetComparison, presetValueLabel } from '../utils/dubbingPreset'
const props = withDefaults(defineProps<{ draft: Record<string, any>; fields?: any[]; disabled?: boolean; scopeLabel?: string }>(), { scopeLabel: '项目参数', fields: () => [] })
const emit = defineEmits<{ (e: 'load', values: Record<string, any>): void; (e: 'busy-change', value: boolean): void }>()
const message = useMessage()
const entries = ref<any[]>([]), directories = ref<Record<string, string>>({})
const selected = ref<string | null>(null), error = ref(''), note = ref('')
const busy = ref(false), listing = ref(false), showSave = ref(false)
const previewLoading = ref(false), previewError = ref('')
const candidate = ref<{ key: string; name: string; projectDir: string; values: Record<string, any>; warnings: string[] } | null>(null)
const loadedKey = ref<string | null>(null)
const loadedChanges = ref<any[]>([])
const name = ref(''), storage = ref('local'), overwrite = ref(false), saveError = ref('')
const projectDir = computed(() => props.draft['tts.indextts25.project_dir'] || '')
const options = computed(() => entries.value.map(p => ({ label: `${p.name} · ${p.storage === 'shared' ? 'IndexTTS' : 'DubForge'}`, value: JSON.stringify([p.storage, p.name]) })))
const destinations = computed(() => Object.keys(directories.value).map(key => ({ value: key, label: key === 'shared' ? 'IndexTTS 共享预设库' : 'DubForge 预设库' })))
let controller: AbortController | undefined, disposed = false
let previewController: AbortController | undefined, previewGeneration = 0
const comparison = computed(() => presetComparison(candidate.value?.values ?? {}, props.draft, props.fields))
const candidateReady = computed(() => !!candidate.value && candidate.value.key === selected.value
  && candidate.value.projectDir === projectDir.value && !previewLoading.value && !previewError.value)
const showingLoadedChanges = computed(() => loadedKey.value === candidate.value?.key && !comparison.value.changed.length)
const displayedChanges = computed(() => showingLoadedChanges.value ? loadedChanges.value : comparison.value.changed)
const displayedUnchanged = computed(() => showingLoadedChanges.value
  ? comparison.value.unchanged.filter(row => !loadedChanges.value.some(change => change.path === row.path))
  : comparison.value.unchanged)
const previewWarnings = computed(() => (candidate.value?.warnings ?? [])
  .filter(warning => !comparison.value.preserved.length || !warning.includes('已保留页面当前值')))
const valueLabel = (path: string, value: any) => presetValueLabel(path, value, props.fields)
async function refresh() {
  controller?.abort()
  const request = new AbortController()
  controller = request
  listing.value = true
  try {
    const r = await fetch(`/api/workbench-presets?project_dir=${encodeURIComponent(projectDir.value)}`, { signal: request.signal, cache: 'no-store' })
    const data = await r.json()
    if (!r.ok) throw new Error(data.message || '读取预设失败')
    if (disposed || controller !== request) return
    entries.value = data.presets
    directories.value = data.directories
    error.value = data.warning ? `共享预设库暂不可用：${data.warning}` : ''
    if (!options.value.some(p => p.value === selected.value)) selected.value = null
  } catch (e: any) {
    if (!request.signal.aborted && !disposed) error.value = e.message || '读取预设失败'
  } finally { if (controller === request) listing.value = false }
}
async function post(action: string, body: any, signal?: AbortSignal) {
  const r = await fetch(`/api/workbench-presets/${action}`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_dir: projectDir.value, ...body }), signal })
  const data = await r.json()
  if (!r.ok) throw new Error(data.message || '预设操作失败')
  return data
}
async function readPreview() {
  previewController?.abort()
  const ticket = ++previewGeneration, key = selected.value, directory = projectDir.value
  candidate.value = null; previewError.value = ''; loadedKey.value = null; loadedChanges.value = []; note.value = ''
  previewLoading.value = false
  if (!key || disposed) return
  const request = new AbortController(); previewController = request
  previewLoading.value = true
  try {
    const [storage, presetName] = JSON.parse(key)
    const data = await post('load', { storage, name: presetName, project_dir: directory }, request.signal)
    if (disposed || request.signal.aborted || ticket !== previewGeneration || selected.value !== key || projectDir.value !== directory) return
    if (!data.values || typeof data.values !== 'object' || Array.isArray(data.values)) throw new Error('预设参数格式无效，请刷新后重试')
    candidate.value = { key, name: presetName, projectDir: directory, values: data.values,
      warnings: Array.isArray(data.warnings) ? data.warnings : [] }
  } catch (e: any) {
    if (!disposed && !request.signal.aborted && ticket === previewGeneration) previewError.value = e.message || '读取预设失败'
  } finally { if (ticket === previewGeneration) previewLoading.value = false }
}
function load() {
  if (!candidateReady.value || busy.value || props.disabled) return
  const preview = candidate.value!
  const rows = [...comparison.value.changed, ...comparison.value.unchanged]
  if (!rows.length) return
  loadedChanges.value = comparison.value.changed.map(row => ({ ...row }))
  // 先完成只读预览，再同步填入草稿。不能在 busy-change 锁住父表单时发出 load。
  emit('load', Object.fromEntries(rows.map(row => [row.path, JSON.parse(JSON.stringify(row.after))])))
  loadedKey.value = preview.key
  note.value = `已载入「${preview.name}」到当前表单，保存${props.scopeLabel}后生效。`
}
function openSave() {
  name.value = selected.value ? JSON.parse(selected.value)[1] : ''
  storage.value = directories.value.shared ? 'shared' : 'local'
  overwrite.value = false
  saveError.value = ''
  showSave.value = true
}
async function refreshPresets() {
  await refresh()
  if (selected.value && !disposed) await readPreview()
}
async function save() {
  if (!name.value.trim() || busy.value || props.disabled) return
  busy.value = true
  saveError.value = ''
  try {
    const data = await post('save', { name: name.value.trim(), storage: storage.value,
      overwrite: overwrite.value, values: props.draft })
    if (disposed) return
    showSave.value = false
    message.success(`当前表单已保存为预设；${props.scopeLabel}保持不变`)
    await refresh()
    const key = JSON.stringify([data.storage, data.name])
    if (selected.value === key) await readPreview()
    else selected.value = key
  } catch (e: any) { if (!disposed) saveError.value = e.message }
  finally { busy.value = false }
}
watch(projectDir, refresh, { immediate: true })
watch([selected, projectDir], readPreview)
watch(() => comparison.value.changed.length, count => {
  if (count && loadedKey.value) { loadedKey.value = null; loadedChanges.value = []; note.value = '' }
})
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
onUnmounted(() => { disposed = true; previewGeneration++; controller?.abort(); previewController?.abort() })
</script>

<template>
  <div class="presets">
    <div class="preset-row">
      <n-select v-model:value="selected" :options="options" :loading="listing" filterable clearable
                placeholder="选择音色预设" :disabled="disabled || busy" aria-label="选择音色预设" />
      <n-button size="small" :disabled="!candidateReady || disabled || busy" :loading="previewLoading" @click="load">载入到当前表单</n-button>
      <n-button size="small" :disabled="disabled || busy" @click="openSave">保存为预设</n-button>
      <n-button size="small" quaternary :disabled="listing || busy" @click="refreshPresets">刷新</n-button>
    </div>
    <n-text depth="3" class="preset-note">选择预设查看变化 → 载入到当前表单 → 保存{{ scopeLabel }}。服务与运行设置不随预设改变。</n-text>
    <n-text v-if="error" depth="3" class="preset-note">{{ error }}</n-text>
    <n-alert v-if="previewError" type="error" :show-icon="false" class="preset-note">{{ previewError }} <n-button text size="small" :disabled="previewLoading" @click="readPreview">重新读取</n-button></n-alert>
    <p v-else-if="previewLoading" class="preset-note" role="status">正在读取预设参数，当前表单保持不变…</p>
    <div v-else-if="candidateReady" class="preset-preview" aria-label="预设设置变化">
      <div class="preview-heading"><strong>{{ candidate!.name }}</strong><span v-if="comparison.changed.length">将修改 {{ comparison.changed.length }} 项</span><span v-else>{{ loadedKey === candidate!.key ? `已载入 · 修改 ${loadedChanges.length} 项` : '预设值与当前表单一致' }}</span></div>
      <p v-if="note" class="preview-note" role="status">{{ note }}</p>
      <div v-if="displayedChanges.length" class="preview-table" role="table" aria-label="预设参数对比">
        <div class="preview-row preview-columns" role="row"><span role="columnheader">设置项</span><span role="columnheader">{{ showingLoadedChanges ? '载入前' : '当前值' }}</span><span role="columnheader">{{ showingLoadedChanges ? '载入值' : '预设值' }}</span></div>
        <div v-for="row in displayedChanges" :key="row.path" class="preview-row" role="row">
          <span role="cell">{{ row.label }}</span><span role="cell">{{ valueLabel(row.path, row.before) }}</span><span role="cell" class="preset-value">{{ valueLabel(row.path, row.after) }}</span>
        </div>
      </div>
      <details v-if="displayedUnchanged.length" class="preview-details"><summary>相同设置 · {{ displayedUnchanged.length }} 项</summary><div class="unchanged-list"><span v-for="row in displayedUnchanged" :key="row.path"><strong>{{ row.label }}</strong>{{ valueLabel(row.path, row.after) }}</span></div></details>
      <p v-if="comparison.preserved.length" class="preserved-note">保持当前值：{{ comparison.preserved.map(row => row.label).join('、') }}。</p>
      <p v-for="warning in previewWarnings" :key="warning" class="preview-warning">{{ warning }}</p>
    </div>
    <n-modal v-model:show="showSave" preset="card" title="保存配音预设" :style="{ width: '560px', maxWidth: 'calc(100vw - 32px)' }"
             :mask-closable="!busy" :closable="!busy">
      <div class="save-fields">
        <label>预设名称<n-input v-model:value="name" placeholder="例如：沉稳旁白" :disabled="busy" /></label>
        <label>保存位置<n-select v-model:value="storage" :options="destinations" :disabled="busy" /></label>
        <FilePathLink class="preset-path" :path="directories[storage]" />
        <n-text depth="3">包含当前表单的合成参数和参考音频副本。IndexTTS 可读取通用参数；语言、时长因子由 DubForge 扩展保留。视频原声参考规则仅能保存在 DubForge 预设库。</n-text>
        <n-checkbox v-model:checked="overwrite" :disabled="busy">覆盖同名预设</n-checkbox>
        <n-alert v-if="saveError" type="error" :show-icon="false">{{ saveError }}</n-alert>
        <div class="save-actions"><n-button :disabled="busy" @click="showSave = false">取消</n-button>
          <n-button type="primary" :loading="busy" :disabled="!name.trim() || disabled" @click="save">保存预设</n-button></div>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.presets { margin: 4px 0 20px; }
.preset-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.preset-row :deep(.n-select) { flex: 1 1 240px; min-width: 0; }
.preset-note { display: block; margin-top: 8px; font-size: 12px; }
.preset-preview { margin-top: 12px; padding: 14px; border: 1px solid var(--df-border, #5555); border-radius: 8px; background: var(--df-bg, transparent); font-size: 12px; }
.preview-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; }
.preview-heading strong { font-size: 13px; }.preview-heading span { color: var(--df-muted); }
.preview-table { margin-top: 10px; max-height: 310px; overflow: auto; }
.preview-row { display: grid; grid-template-columns: minmax(110px, .8fr) minmax(0, 1fr) minmax(0, 1fr); gap: 12px; padding: 9px 2px; border-bottom: 1px solid var(--df-border, #5553); }
.preview-row > span { min-width: 0; overflow-wrap: anywhere; white-space: pre-wrap; }
.preview-columns { position: sticky; top: 0; background: var(--df-bg, #20262f); color: var(--df-muted); }
.preset-value { color: var(--df-accent, #63b3ed); }
.preview-note, .preserved-note, .preview-warning { margin: 10px 0 0; line-height: 1.6; }.preview-warning { color: var(--df-muted); }
.preview-details { margin-top: 10px; }.preview-details summary { cursor: pointer; color: var(--df-muted); }
.unchanged-list { display: grid; gap: 6px; margin-top: 9px; }.unchanged-list > span { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 12px; overflow-wrap: anywhere; }.unchanged-list strong { font-weight: 400; }
@media (max-width: 600px) { .preview-row { gap: 8px; grid-template-columns: 96px minmax(0, 1fr) minmax(0, 1fr); }.preset-preview { padding: 10px; }.unchanged-list > span { grid-template-columns: 110px minmax(0, 1fr); } }
.save-fields { display: grid; gap: 14px; }
.save-fields label { display: grid; gap: 6px; }
.preset-path { overflow-wrap: anywhere; font-size: 12px; }
.save-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
