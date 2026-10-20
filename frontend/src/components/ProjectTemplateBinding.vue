<script setup lang="ts">
import { ref, watch, onUnmounted, computed, h } from 'vue'
import { RouterLink } from 'vue-router'
import { NButton, NSelect, NAlert, useDialog } from 'naive-ui'
import { checkConfigurationEditability, useConfigurationBatchEditability } from '../utils/configurationEditability'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import ConfigurationApplyPreview from './ConfigurationApplyPreview.vue'
import { useTemplateCatalogStore } from '../stores/templateCatalog'
import { useProjectListStore } from '../stores/projectList'

const props = defineProps<{ uids: string[]; compact?: boolean; summary?: any; disabled?: boolean }>(), emit = defineEmits<{ changed: [] }>()
const templateCatalog = props.compact ? useTemplateCatalogStore() : null
const listSnapshots = props.compact ? useProjectListStore() : null
const editability = useConfigurationBatchEditability(() => props.compact ? [] : props.uids.map(uid => 'project:' + uid))
const bindingLocked = computed(() => props.disabled || busy.value || loading.value || editability.locked.value)
const dialog = useDialog(), rows = ref<any[]>([]), current = ref<string | null>(null)
const busy = ref(false), loading = ref(false), error = ref(''), overrides = ref(0)
let generation = 0, operation = 0, disposed = false
const selectedTemplate = computed(() => props.compact ? props.summary?.known ? props.summary.template_id : null : current.value)
const catalogInContext = computed(() => !!templateCatalog && templateCatalog.context === listSnapshots?.context)
const templateOptions = computed(() => {
  const options = [{ label: '全局默认（不使用模板）', value: '' }, ...(props.compact
    ? catalogInContext.value ? templateCatalog!.rows : [] : rows.value)
    .filter(row => !row.archived || row.id === selectedTemplate.value)
    .map(row => ({ label: row.name + (row.archived ? ' · 已归档' : ''), value: row.id, disabled: row.archived }))]
  if (props.compact && selectedTemplate.value && !options.some(row => row.value === selectedTemplate.value)) {
    options.push({ label: props.summary?.template_name || '模板名称未载入', value: selectedTemplate.value,
      disabled: !!props.summary?.template_archived })
  }
  return options
})
async function prepareTemplates(shown: boolean) {
  if (props.compact && shown) await templateCatalog!.refresh(listSnapshots!.context)
}

async function api(path: string, body?: any) {
  const response = await fetch('/api/configuration' + path, {
    method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.message || '项目基础设置操作失败')
  return data
}
async function load() {
  const ticket = ++generation
  loading.value = true; error.value = ''
  try {
    if (props.compact) return
    const list = await api('/templates')
    if (disposed || ticket !== generation) return
    rows.value = list; current.value = null; overrides.value = 0
    if (props.uids.length === 1) {
      const data = await api('/project:' + encodeURIComponent(props.uids[0]))
      if (disposed || ticket !== generation) return
      current.value = data.parent?.slice(7) || ''
      overrides.value = Object.keys(data.overrides).length
    }
  } catch (err: any) {
    if (!disposed && ticket === generation) error.value = err.message
  } finally {
    if (ticket === generation) loading.value = false
  }
}
async function bind(value: string | null) {
  if (bindingLocked.value || !props.uids.length) return
  busy.value = true; error.value = ''
  const uids = [...props.uids], ticket = generation, operationId = ++operation, templateId = value || ''
  const context = listSnapshots?.context
  const stale = () => disposed || ticket !== generation || (props.compact && context !== listSnapshots?.context)
  let sent = false
  const close = () => { if (!disposed && operationId === operation) busy.value = false }
  try {
    if (!await checkTargets(uids)) { close(); return }
    const plan = await api('/binding-plan', { uids, template_id: templateId })
    if (stale()) { close(); return }
    dialog.warning({
      title: `应用最新${plan.name}设置？`,
      content: () => h(ConfigurationApplyPreview, { plan }),
      style: 'width: min(760px, calc(100vw - 32px));',
      positiveText: '确认应用设置', negativeText: '取消',
      onPositiveClick: async () => {
        if (sent) return false
        if (stale()) { close(); return false }
        sent = true
        try {
          if (!await checkTargets(uids)) return
          if (stale()) return
          await api('/bind', { uids, template_id: templateId, fingerprint: plan.fingerprint, confirmed: true })
          if (!stale()) { await load(); emit('changed') }
        } catch (err: any) {
          if (!stale()) error.value = err.message
        } finally { close() }
      },
      onNegativeClick: () => sent ? false : close(), onClose: () => sent ? false : close(),
      onMaskClick: () => { if (!sent) close() }, maskClosable: false, closeOnEsc: false,
    })
  } catch (err: any) {
    if (!stale()) error.value = err.message
    close()
  }
}
function applyLatest() {
  if (props.compact && props.summary?.known) return bind(props.summary.template_id || '')
  if (props.uids.length === 1 && current.value !== null) return bind(current.value)
}
async function checkTargets(uids: string[]) {
  if (!props.compact && !editability.observing.value) return true
  const results = await Promise.all(uids.map(uid => checkConfigurationEditability('project:' + uid)))
  if (results.every(Boolean)) return true
  error.value = '所选项目有任务正在执行，或未能核对运行状态；暂不能应用基础设置。'
  return false
}
watch(() => props.uids.join(','), () => { operation++; busy.value = false; void load() }, { immediate: true })
watch(() => props.compact ? listSnapshots!.context : '', () => { generation++; operation++; busy.value = false })
onUnmounted(() => { disposed = true; generation++; operation++ })
</script>

<template>
  <div class="binding" :class="{ compact }">
    <span>{{ compact ? '模板' : uids.length > 1 ? '批量应用基础设置' : '项目基础来源' }}</span>
    <n-select :value="selectedTemplate" :options="templateOptions"
      :disabled="bindingLocked || !uids.length" :loading="loading || compact && catalogInContext && templateCatalog?.loading" :placeholder="compact ? '选择模板…' : uids.length > 1 ? '选择要应用的来源…' : '读取基础来源…'"
      :aria-label="compact ? '项目模板' : '项目基础设置来源'" @update:value="bind" @update:show="prepareTemplates" />
    <n-button v-if="uids.length === 1" size="small" :disabled="bindingLocked || selectedTemplate === null || (compact ? summary?.template_archived : rows.some(row => row.id === current && row.archived))"
      :loading="busy" title="比较当前项目与来源的最新设置，确认后才应用；项目单独设置保持不变。" @click="applyLatest">应用最新设置…</n-button>
    <span v-if="uids.length === 1 && (!compact || summary?.known)" class="binding-overrides"><ConfigurationSourceBadge :source="{kind:'project'}"/><small>{{ compact ? summary?.override_count || 0 : overrides }} 项覆盖</small></span>
    <RouterLink v-if="!compact" :to="{path:'/settings',query:{section:'production',...(uids.length === 1 && current ? {template:current} : {})}}">{{ uids.length === 1 ? (current ? '编辑当前模板' : '编辑全局默认') : '管理模板' }}</RouterLink>
    <small v-if="!compact" class="binding-note">项目保留创建或上次应用时的基础值；修改全局或模板后，需主动应用才会更新本项目。</small>
  </div>
  <n-alert v-if="editability.locked.value" type="info">{{ editability.reason.value }}</n-alert>
  <n-alert v-if="error" type="error">{{ error }} <n-button text size="small" :disabled="bindingLocked" @click="load">重新读取</n-button></n-alert>
  <small v-if="compact && catalogInContext && templateCatalog?.error" class="template-read-error">{{ templateCatalog.error }}，可重新展开模板列表重试。</small>
</template>

<style scoped>
.binding{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:16px 0;font-size:12px}.binding :deep(.n-select){width:230px}.binding-overrides{display:inline-flex;align-items:center;gap:6px}.binding small{color:var(--df-muted)}.binding a{margin-left:auto}.binding-note{flex-basis:100%;line-height:1.65}
.binding.compact{margin:10px 0 0;gap:8px}.binding.compact :deep(.n-select){width:210px}.binding.compact>.n-button{font-size:12px}.template-read-error{display:block;color:var(--df-warning);font-size:12px;margin-top:6px}@media(max-width:680px){.binding.compact :deep(.n-select){min-width:150px;flex:1}.binding-overrides{flex-wrap:wrap}}
</style>
