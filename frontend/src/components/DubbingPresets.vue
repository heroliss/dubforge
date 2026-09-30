<script setup lang="ts">
import FilePathLink from './FilePathLink.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NModal, NSelect, NText, useMessage } from 'naive-ui'
const props = withDefaults(defineProps<{ draft: Record<string, any>; disabled?: boolean; scopeLabel?: string }>(), { scopeLabel: '项目参数' })
const emit = defineEmits<{ (e: 'load', values: Record<string, any>): void; (e: 'busy-change', value: boolean): void }>()
const message = useMessage()
const entries = ref<any[]>([]), directories = ref<Record<string, string>>({})
const selected = ref<string | null>(null), error = ref(''), note = ref('')
const busy = ref(false), listing = ref(false), showSave = ref(false)
const name = ref(''), storage = ref('local'), overwrite = ref(false), saveError = ref('')
const projectDir = computed(() => props.draft['tts.indextts25.project_dir'] || '')
const options = computed(() => entries.value.map(p => ({ label: `${p.name} · ${p.storage === 'shared' ? 'IndexTTS' : 'DubForge'}`, value: JSON.stringify([p.storage, p.name]) })))
const destinations = computed(() => Object.keys(directories.value).map(key => ({ value: key, label: key === 'shared' ? 'IndexTTS 共享预设库' : 'DubForge 预设库' })))
let controller: AbortController | undefined, disposed = false
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
async function post(action: string, body: any) {
  const r = await fetch(`/api/workbench-presets/${action}`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, project_dir: projectDir.value }) })
  const data = await r.json()
  if (!r.ok) throw new Error(data.message || '预设操作失败')
  return data
}
async function load() {
  if (!selected.value || busy.value || props.disabled) return
  busy.value = true
  try {
    const [storage, name] = JSON.parse(selected.value)
    const data = await post('load', { storage, name })
    if (disposed) return
    emit('load', data.values)
    note.value = [`已载入「${name}」，保存${props.scopeLabel}后用于合成。`, ...data.warnings].join(' ')
  } catch (e: any) { if (!disposed) message.error(e.message) }
  finally { busy.value = false }
}
function openSave() {
  name.value = selected.value ? JSON.parse(selected.value)[1] : ''
  storage.value = directories.value.shared ? 'shared' : 'local'
  overwrite.value = false
  saveError.value = ''
  showSave.value = true
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
    selected.value = JSON.stringify([data.storage, data.name])
  } catch (e: any) { if (!disposed) saveError.value = e.message }
  finally { busy.value = false }
}
watch(projectDir, refresh, { immediate: true })
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
onUnmounted(() => { disposed = true; controller?.abort() })
</script>

<template>
  <div class="presets">
    <div class="preset-row">
      <n-select v-model:value="selected" :options="options" :loading="listing" filterable clearable
                placeholder="选择音色预设" :disabled="disabled || busy" aria-label="选择音色预设" />
      <n-button size="small" :disabled="!selected || disabled || busy" @click="load">载入到当前表单</n-button>
      <n-button size="small" :disabled="disabled || busy" @click="openSave">保存为预设</n-button>
      <n-button size="small" quaternary :disabled="listing || busy" @click="refresh">刷新</n-button>
    </div>
    <n-text depth="3" class="preset-note">载入只填入表单；保存{{ scopeLabel }}后生效。保存为预设不等于应用参数。</n-text>
    <n-text v-if="error" depth="3" class="preset-note">{{ error }}</n-text>
    <n-alert v-if="note" type="info" :show-icon="false" class="preset-note">{{ note }}</n-alert>
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
.save-fields { display: grid; gap: 14px; }
.save-fields label { display: grid; gap: 6px; }
.preset-path { overflow-wrap: anywhere; font-size: 12px; }
.save-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
