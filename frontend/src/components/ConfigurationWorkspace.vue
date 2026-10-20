<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NInput, useDialog } from 'naive-ui'
import ConfigurationForm from './ConfigurationForm.vue'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import FilePathLink from './FilePathLink.vue'
import { SOURCE_STYLES } from '../utils/configurationSource'
import { checkConfigurationEditability, useConfigurationEditability } from '../utils/configurationEditability'

const props = defineProps<{ scope: string; stage?: string }>()
const editability = useConfigurationEditability(() => props.scope)
const emit = defineEmits<{
  select: [scope: string]
  'stage-change': [stage: string]
  'dirty-change': [value: boolean]
  'busy-change': [value: boolean]
}>()
const dialog = useDialog()
const rows = ref<any[]>([]), name = ref(''), error = ref('')
const busy = ref(false), editorBusy = ref(false), dirty = ref(false)
const locked = computed(() => busy.value || editorBusy.value)
// 模板归档/恢复是范围管理操作；设置表单本身按活动步骤逐字段锁定。
const managementLocked = computed(() => locked.value || editability.checking.value)
const managementLockedFor = (row: any) => managementLocked.value || (row.key === props.scope
  && (editability.resourceLocked.value || editability.runningSteps.value.length > 0))
const activeRows = computed(() => rows.value.filter(row => !row.archived))
const archivedRows = computed(() => rows.value.filter(row => row.archived))
const archivedExpanded = ref(false), trashExpanded = ref(false)
const trashRows = ref<any[]>([]), trashLoading = ref(false), trashLoaded = ref(false)
const trashError = ref(''), trashNotice = ref('')
const trashNoticeWarning = ref(false)
let disposed = false, listRevision = 0, trashRevision = 0
watch(dirty, value => emit('dirty-change', value), { flush: 'sync' })
watch(locked, value => emit('busy-change', value), { flush: 'sync' })
watch(() => props.scope, () => { dirty.value = false; editorBusy.value = false }, { flush: 'sync' })
watch([() => props.scope, archivedRows], () => {
  if (archivedRows.value.some(row => row.key === props.scope)) archivedExpanded.value = true
}, { immediate: true, flush: 'sync' })

async function api(path: string, body?: any, method?: string) {
  const r = await fetch('/api/configuration' + path, {
    method: method || (body ? 'POST' : 'GET'), headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).catch(() => { throw new Error('无法连接 DubForge，请稍后重试。') })
  const data = await r.json().catch(() => { throw new Error('模板服务返回异常，请刷新后重试。') })
  if (!r.ok) throw new Error(data.message || '模板操作失败')
  return data
}
async function refresh() {
  const ticket = ++listRevision
  try {
    const data = await api('/templates')
    if (!disposed && ticket === listRevision) { rows.value = data; error.value = '' }
  } catch (e: any) { if (!disposed && ticket === listRevision) error.value = e.message }
}
function choose(scope: string) { if (!locked.value && scope !== props.scope) emit('select', scope) }
async function create() {
  if (disposed || locked.value || dirty.value || !name.value.trim()) return
  const initialScope = props.scope
  busy.value = true; error.value = ''
  let key = ''
  try {
    const data = await api('/templates', { name: name.value.trim() })
    if (disposed) return
    key = data.key; name.value = ''
    await refresh()
  } catch (e: any) { if (!disposed) error.value = e.message }
  finally { if (!disposed) busy.value = false }
  // 必须等写入完成后再导航，避免路由保存保护拦截新模板。
  if (!disposed && key && props.scope === initialScope) emit('select', key)
}
function archive(row: any) {
  if (disposed || managementLockedFor(row) || dirty.value) return
  dialog.warning({
    title: row.archived ? '恢复系列模板？' : '归档系列模板？',
    content: '已绑定项目继续继承原设置；归档后不能被新项目选择。不会删除B站合集或视频。',
    positiveText: '确认', negativeText: '取消',
    onPositiveClick: async () => {
      if (disposed || managementLocked.value || dirty.value) return false
      const initialScope = props.scope
      busy.value = true; error.value = ''; let completed = false
      try {
        if (editability.observing.value && !await checkConfigurationEditability(row.key)) { error.value = '模板影响的项目正在运行，或未能核对运行状态；暂不能归档或恢复。'; return false }
        if (disposed || dirty.value) return false
        await api(`/${encodeURIComponent(row.key)}/archive`, { revision: row.revision, archived: !row.archived })
        if (disposed) return false
        completed = true; await refresh()
      } catch (e: any) { if (!disposed) error.value = e.message }
      finally { if (!disposed) busy.value = false }
      if (!disposed && completed && props.scope === initialScope && props.scope === row.key) emit('select', 'global')
      return completed || false
    },
  })
}
function deleteReason(row: any) {
  return Number(row.project_count) > 0 ? `仍被${row.project_count}个项目使用，请先解除绑定或删除这些项目（包括已归档项目）。` : ''
}
function remove(row: any) {
  if (disposed || !row.archived || deleteReason(row) || locked.value || dirty.value) return
  dialog.warning({
    title: `删除模板“${row.name}”？`,
    content: '会从模板列表删除，设置保留在工作目录的 trash/templates 本地恢复备份中，可在“找回已删除模板”中恢复。不会进入 Windows 回收站，也不会删除项目、视频或B站合集。',
    positiveText: '删除并保留备份', negativeText: '取消',
    onPositiveClick: async () => {
      if (disposed || locked.value || dirty.value) return false
      const latest = rows.value.find(item => item.key === row.key)
      if (!latest || !latest.archived || latest.revision !== row.revision || deleteReason(latest)) {
        error.value = '模板状态或绑定项目已变化，请刷新列表后再删除。'; return false
      }
      const initialScope = props.scope
      busy.value = true; error.value = ''; ++listRevision; ++trashRevision; trashLoading.value = false
      let completed = false
      try {
        await api(`/${encodeURIComponent(row.key)}`, { revision: row.revision, confirmed: true }, 'DELETE')
        if (disposed) return false
        completed = true; rows.value = rows.value.filter(item => item.key !== row.key)
        await refresh()
        if (trashLoaded.value || trashExpanded.value) await refreshTrash()
      } catch (e: any) { if (!disposed) error.value = e.message }
      finally { if (!disposed) busy.value = false }
      if (!disposed && completed && props.scope === initialScope && props.scope === row.key) emit('select', 'global')
      return completed || false
    },
  })
}
async function refreshTrash() {
  const ticket = ++trashRevision
  trashLoading.value = true; trashError.value = ''
  try {
    const data = await api('/templates/trash')
    if (!disposed && ticket === trashRevision) { trashRows.value = data; trashLoaded.value = true }
  } catch (e: any) { if (!disposed && ticket === trashRevision) trashError.value = e.message }
  finally { if (!disposed && ticket === trashRevision) trashLoading.value = false }
}
function toggleArchived(event: Event) { archivedExpanded.value = (event.currentTarget as HTMLDetailsElement).open }
function toggleTrash(event: Event) {
  trashExpanded.value = (event.currentTarget as HTMLDetailsElement).open
  if (trashExpanded.value && !trashLoaded.value && !trashLoading.value) void refreshTrash()
}
function deletedAt(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
async function restore(row: any) {
  if (disposed || locked.value || dirty.value || row.restorable === false) return
  busy.value = true; trashError.value = ''; trashNotice.value = ''; trashNoticeWarning.value = false; ++listRevision; ++trashRevision; trashLoading.value = false
  try {
    const data = await api(`/templates/trash/${encodeURIComponent(row.backup_id)}/restore`, { confirmed: true })
    if (disposed) return
    // 恢复回执已经成立；辅助重读失败也不能继续把旧备份显示为可恢复。
    trashRows.value = trashRows.value.filter(item => item.backup_id !== row.backup_id)
    trashNoticeWarning.value = !!data.warning
    trashNotice.value = data.warning || `已找回“${data.name || row.name}”，保留在已归档模板中；恢复使用前请先检查设置。`
    archivedExpanded.value = true
    await refresh(); await refreshTrash()
  } catch (e: any) { if (!disposed) trashError.value = e.message }
  finally { if (!disposed) busy.value = false }
}
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; ++listRevision; ++trashRevision })
</script>

<template>
  <div class="configuration-workspace">
    <p class="inheritance-line"><ConfigurationSourceBadge :source="{kind:'global'}"/><span>→</span><ConfigurationSourceBadge :source="{kind:'series'}"/><span>→</span><ConfigurationSourceBadge :source="{kind:'project'}"/><small>新项目保存基础值；已有项目主动应用更新</small></p>
    <n-alert v-if="error" type="error">{{ error }} <n-button text :disabled="locked" @click="refresh">刷新列表</n-button></n-alert>
    <div class="templates-grid">
      <aside aria-label="配置范围">
        <button class="scope-button" :class="{ selected: scope === 'global' }" :aria-pressed="scope === 'global'" :disabled="locked" @click="choose('global')"><strong :style="{color:SOURCE_STYLES.global.color}">全局默认</strong><small>新项目的起点</small></button>
        <div class="series-heading" :style="{color:SOURCE_STYLES.series.color}">系列模板 <span>{{ activeRows.length }}</span></div>
      <div v-for="row in activeRows" :key="row.key" class="template-item">
          <button class="scope-button" :class="{ selected: scope === row.key }" :aria-pressed="scope === row.key" :disabled="locked" @click="choose(row.key)"><strong :style="{color:SOURCE_STYLES.series.color}">{{ row.name }}</strong><small>{{ row.project_count }} 个项目</small></button>
          <n-button text size="tiny" :disabled="dirty || managementLockedFor(row)" :title="scope === row.key ? (editability.reason.value || editability.resourceReason.value) : undefined" @click="archive(row)">归档</n-button>
        </div>
        <div class="new-template"><n-input v-model:value="name" placeholder="新模板名称" :input-props="{ 'aria-label': '新模板名称' }" maxlength="80" :disabled="locked || dirty" @keydown.enter="create"/><n-button :disabled="!name.trim() || locked || dirty" :loading="busy" @click="create">新建</n-button></div>
        <details class="secondary-templates" :open="archivedExpanded" @toggle="toggleArchived">
          <summary>已归档模板 <span>· {{ archivedRows.length }}</span></summary>
          <div v-if="archivedExpanded" class="secondary-content">
            <p v-if="!archivedRows.length" class="secondary-hint">暂无已归档模板。</p>
            <div v-for="row in archivedRows" :key="row.key" class="archived-template">
              <button class="scope-button" :class="{ selected: scope === row.key }" :aria-pressed="scope === row.key" :disabled="locked" @click="choose(row.key)"><strong :style="{color:SOURCE_STYLES.series.color}">{{ row.name }}</strong><small>{{ row.project_count }} 个项目 · 只读</small></button>
              <div class="archived-actions"><n-button text size="tiny" :disabled="dirty || managementLockedFor(row)" @click="archive(row)">恢复</n-button><n-button text size="tiny" :disabled="dirty || locked || !!deleteReason(row)" :title="deleteReason(row) || '删除后保留本地恢复备份'" @click="remove(row)">删除</n-button></div>
              <p v-if="deleteReason(row)" class="secondary-hint binding-hint">{{ deleteReason(row) }}</p>
            </div>
          </div>
        </details>
        <details class="secondary-templates template-trash" :open="trashExpanded" @toggle="toggleTrash">
          <summary>找回已删除模板</summary>
          <div v-if="trashExpanded" class="secondary-content">
            <p class="secondary-hint">从本地备份找回，模板仍保持归档，不会重新绑定项目。备份在工作目录的 trash/templates 中，不在 Windows 回收站。</p>
            <n-alert v-if="trashNotice" :type="trashNoticeWarning ? 'warning' : 'success'" :show-icon="false">{{ trashNotice }}</n-alert>
            <n-alert v-if="trashError" type="error" :show-icon="false">{{ trashError }}</n-alert>
            <n-button text size="tiny" :loading="trashLoading" :disabled="locked || trashLoading" @click="refreshTrash">{{ trashError ? '重试读取备份' : '刷新备份列表' }}</n-button>
            <p v-if="trashLoading" class="secondary-hint" role="status">正在读取备份…</p>
            <p v-else-if="trashLoaded && !trashRows.length" class="secondary-hint">暂无已删除模板的备份。</p>
            <div v-for="row in trashRows" :key="row.backup_file || row.backup_id" class="trash-item">
              <strong>{{ row.name }}</strong><small v-if="row.deleted_at">删除于 {{ deletedAt(row.deleted_at) }}</small>
              <p v-if="row.error || row.reason" class="secondary-hint backup-reason">{{ row.error || row.reason }}</p>
              <FilePathLink v-if="row.backup_path" :path="row.backup_path" label="模板备份文件">定位备份文件</FilePathLink>
              <n-button text size="tiny" :disabled="dirty || locked || row.restorable === false" :title="row.restorable === false ? row.reason || row.error || '备份暂不能找回，请检查模板和项目绑定。' : undefined" @click="restore(row)">{{ row.restorable === false ? '暂不能找回' : '找回模板' }}</n-button>
            </div>
          </div>
        </details>
      </aside>
      <section class="editor" aria-label="配置编辑">
        <ConfigurationForm :key="scope" :scope="scope" :stage="stage" @stage-change="emit('stage-change',$event)" @dirty-change="dirty = $event" @busy-change="editorBusy = $event" @saved="refresh" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.inheritance-line{font-size:13px;color:var(--df-text);display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 24px}.inheritance-line span,.inheritance-line small{color:var(--df-muted)}.inheritance-line small{font-size:12px;margin-left:auto}
.templates-grid{display:grid;grid-template-columns:220px minmax(0,1fr);gap:28px}aside{border-right:1px solid var(--df-border);padding-right:20px;min-width:0}.editor{min-width:0}
.scope-button{display:block;width:100%;min-width:0;text-align:left;border:1px solid transparent;background:transparent;border-radius:8px;padding:12px;color:var(--df-text);cursor:pointer}.scope-button:hover:not(:disabled){background:var(--df-accent-soft)}.scope-button.selected{background:var(--df-accent-soft);border-color:var(--df-border)}.scope-button:disabled{cursor:wait}.scope-button strong,.scope-button small{display:block}.scope-button strong{font-size:13px;font-weight:500;overflow-wrap:anywhere}.scope-button small{font-size:11px;color:var(--df-muted);margin-top:5px}
.series-heading{display:flex;justify-content:space-between;color:var(--df-muted);font-size:11px;margin:24px 12px 8px}.new-template{display:flex;gap:6px;margin:16px 0}.new-template :deep(.n-input){min-width:0}.template-item{display:flex;align-items:center;gap:6px}.template-item>.n-button{flex-shrink:0}
.secondary-templates{margin-top:8px;color:var(--df-muted);font-size:12px}.secondary-templates>summary{padding:8px 10px;cursor:pointer;user-select:none;border-radius:6px}.secondary-templates>summary:hover{background:var(--df-accent-soft)}.secondary-templates>summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:2px}.secondary-content{padding:4px 0 8px}.secondary-hint{font-size:11px;line-height:1.65;color:var(--df-muted);margin:8px 10px;overflow-wrap:anywhere}.archived-template{padding:4px 0;border-top:1px solid var(--df-border)}.archived-template:first-child{border-top:0}.archived-actions{display:flex;gap:16px;padding:0 12px 4px}.binding-hint{margin-top:0}.template-trash{margin-top:2px}.secondary-content :deep(.n-alert){margin:8px 0;font-size:12px}.secondary-content>.n-button{margin-left:10px}.trash-item{padding:10px;border-top:1px solid var(--df-border)}.trash-item strong,.trash-item small{display:block;overflow-wrap:anywhere}.trash-item strong{color:var(--df-text);font-size:12px;font-weight:500}.trash-item small{font-size:10px;margin:4px 0 8px}
.backup-reason{margin:6px 0}.trash-item :deep(.file-path-link){display:block;font-size:11px;margin:6px 0}.trash-item>.n-button{margin-top:4px}
@media(max-width:1000px){.templates-grid{grid-template-columns:1fr;gap:20px}aside{border-right:0;border-bottom:1px solid var(--df-border);padding:0 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.scope-button,.template-item{width:auto;max-width:240px}.template-item .scope-button{flex:1}.series-heading{margin:0 8px}.series-heading span{display:none}.new-template{width:240px;margin:0}.inheritance-line{margin-bottom:16px}}
@media(max-width:1000px){.secondary-templates{width:100%;margin-top:4px}.archived-template .scope-button{width:100%;max-width:none}.secondary-content{max-width:420px}}
</style>
