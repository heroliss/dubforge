<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NTag, useDialog } from 'naive-ui'
import ConfigurationForm from './ConfigurationForm.vue'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { SOURCE_STYLES } from '../utils/configurationSource'

const props = defineProps<{ scope: string }>()
const emit = defineEmits<{
  select: [scope: string]
  'dirty-change': [value: boolean]
  'busy-change': [value: boolean]
}>()
const dialog = useDialog()
const rows = ref<any[]>([]), name = ref(''), error = ref('')
const busy = ref(false), editorBusy = ref(false), dirty = ref(false)
const locked = computed(() => busy.value || editorBusy.value)
watch(dirty, value => emit('dirty-change', value), { flush: 'sync' })
watch(locked, value => emit('busy-change', value), { flush: 'sync' })
watch(() => props.scope, () => { dirty.value = false; editorBusy.value = false }, { flush: 'sync' })

async function api(path: string, body?: any) {
  const r = await fetch('/api/configuration' + path, {
    method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await r.json()
  if (!r.ok) throw new Error(data.message || '模板操作失败')
  return data
}
async function refresh() {
  try { rows.value = await api('/templates'); error.value = '' }
  catch (e: any) { error.value = e.message }
}
function choose(scope: string) { if (!locked.value && scope !== props.scope) emit('select', scope) }
async function create() {
  if (locked.value || dirty.value || !name.value.trim()) return
  busy.value = true; error.value = ''
  let key = ''
  try {
    const data = await api('/templates', { name: name.value.trim() })
    key = data.key; name.value = ''
    await refresh()
  } catch (e: any) { error.value = e.message }
  finally { busy.value = false }
  // 必须等写入完成后再导航，避免路由保存保护拦截新模板。
  if (key) emit('select', key)
}
function archive(row: any) {
  if (locked.value || dirty.value) return
  dialog.warning({
    title: row.archived ? '恢复系列模板？' : '归档系列模板？',
    content: '已绑定项目继续继承原设置；归档后不能被新项目选择。不会删除B站合集或视频。',
    positiveText: '确认', negativeText: '取消',
    onPositiveClick: async () => {
      if (locked.value || dirty.value) return false
      busy.value = true; error.value = ''; let completed = false
      try {
        await api(`/${encodeURIComponent(row.key)}/archive`, { revision: row.revision, archived: !row.archived })
        completed = true; await refresh()
      } catch (e: any) { error.value = e.message }
      finally { busy.value = false }
      if (completed && props.scope === row.key) emit('select', 'global')
    },
  })
}
onMounted(refresh)
</script>

<template>
  <div class="configuration-workspace">
    <p class="inheritance-line"><ConfigurationSourceBadge :source="{kind:'global'}"/><span>→</span><ConfigurationSourceBadge :source="{kind:'series'}"/><span>→</span><ConfigurationSourceBadge :source="{kind:'project'}"/><small>只覆盖需要不同的部分</small></p>
    <n-alert v-if="error" type="error">{{ error }} <n-button text :disabled="locked" @click="refresh">刷新列表</n-button></n-alert>
    <div class="templates-grid">
      <aside aria-label="配置范围">
        <button class="scope-button" :class="{ selected: scope === 'global' }" :aria-pressed="scope === 'global'" :disabled="locked" @click="choose('global')"><strong :style="{color:SOURCE_STYLES.global.color}">全局默认</strong><small>所有项目的起点</small></button>
        <div class="series-heading" :style="{color:SOURCE_STYLES.series.color}">系列模板 <span>{{ rows.filter(row => !row.archived).length }}</span></div>
        <div v-for="row in rows" :key="row.key" class="template-item">
          <button class="scope-button" :class="{ selected: scope === row.key }" :aria-pressed="scope === row.key" :disabled="locked" @click="choose(row.key)"><strong :style="{color:SOURCE_STYLES.series.color}">{{ row.name }}</strong><small>{{ row.project_count }} 个项目 <n-tag v-if="row.archived" size="tiny">已归档</n-tag></small></button>
          <n-button text size="tiny" :disabled="dirty || locked" @click="archive(row)">{{ row.archived ? '恢复' : '归档' }}</n-button>
        </div>
        <div class="new-template"><n-input v-model:value="name" placeholder="新模板名称" :input-props="{ 'aria-label': '新模板名称' }" maxlength="80" :disabled="locked || dirty" @keydown.enter="create"/><n-button :disabled="!name.trim() || locked || dirty" :loading="busy" @click="create">新建</n-button></div>
      </aside>
      <section class="editor" aria-label="配置编辑">
        <ConfigurationForm :key="scope" :scope="scope" @dirty-change="dirty = $event" @busy-change="editorBusy = $event" @saved="refresh" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.inheritance-line{font-size:13px;color:var(--df-text);display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 24px}.inheritance-line span,.inheritance-line small{color:var(--df-muted)}.inheritance-line small{font-size:12px;margin-left:auto}
.templates-grid{display:grid;grid-template-columns:220px minmax(0,1fr);gap:28px}aside{border-right:1px solid var(--df-border);padding-right:20px;min-width:0}.editor{min-width:0}
.scope-button{display:block;width:100%;min-width:0;text-align:left;border:1px solid transparent;background:transparent;border-radius:8px;padding:12px;color:var(--df-text);cursor:pointer}.scope-button:hover:not(:disabled){background:var(--df-accent-soft)}.scope-button.selected{background:var(--df-accent-soft);border-color:var(--df-border)}.scope-button:disabled{cursor:wait}.scope-button strong,.scope-button small{display:block}.scope-button strong{font-size:13px;font-weight:500;overflow-wrap:anywhere}.scope-button small{font-size:11px;color:var(--df-muted);margin-top:5px}
.series-heading{display:flex;justify-content:space-between;color:var(--df-muted);font-size:11px;margin:24px 12px 8px}.new-template{display:flex;gap:6px;margin:16px 0}.new-template :deep(.n-input){min-width:0}.template-item{display:flex;align-items:center;gap:6px}.template-item>.n-button{flex-shrink:0}
@media(max-width:1000px){.templates-grid{grid-template-columns:1fr;gap:20px}aside{border-right:0;border-bottom:1px solid var(--df-border);padding:0 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.scope-button,.template-item{width:auto;max-width:240px}.template-item .scope-button{flex:1}.series-heading{margin:0 8px}.series-heading span{display:none}.new-template{width:240px;margin:0}.inheritance-line{margin-bottom:16px}}
</style>
