<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NInput, NSelect, NSpin, NTag } from 'naive-ui'
import PublicationProgress from '../components/PublicationProgress.vue'
import PublicationRecordLifecycle from '../components/PublicationRecordLifecycle.vue'
import { PUBLICATION_STATUS, publicationStage } from '../utils/publishing'
import { friendlyError } from '../utils/http'

const records = ref<any[]>([]), projects = ref<any[]>([]), accounts = ref<any[]>([])
const loading = ref(false), ready = ref(false), projectsReady = ref(false)
const error = ref(''), contextError = ref(''), updatedAt = ref('')
const route = useRoute()
const search = ref(''), projectFilter = ref(typeof route.query.project==='string' ? route.query.project : 'all'), statusFilter = ref('all'), lifecycleFilter=ref('active'), managing=ref(false)
let revision = 0, disposed = false, controller: AbortController | undefined
const projectMap = computed(() => new Map(projects.value.map(project => [project.uid, project])))
const accountMap = computed(() => new Map(accounts.value.map(account => [account.id, account.name])))
function recordTitle(row: any) {
  return row.snapshot?.settings?.title || row.draft?.overrides?.title || row.draft?.content?.title || '未命名稿件'
}
function projectLabel(row: any) {
  const project = projectMap.value.get(row.project_uid)
  if (project) return `${project.title || project.uid}${project.archived ? ' · 已归档' : ''}`
  return projectsReady.value ? '来源项目已删除 · 记录保留' : `来源项目 ${row.project_uid || '未关联'}`
}
function recordTarget(row: any) {
  return projectMap.value.has(row.project_uid) && (!row.lifecycle || row.lifecycle==='active')
    ? { path: `/projects/${encodeURIComponent(row.project_uid)}/publish`, query: { publication: row.id } }
    : `/publications/${encodeURIComponent(row.id)}`
}
function accountLabel(row: any) {
  return row.snapshot?.account_name || accountMap.value.get(row.draft?.account_id) || (row.draft?.account_id ? `UID ${row.draft.account_id}` : '尚未选择账号')
}
function dateLabel(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleString('zh-CN')
}
const projectOptions = computed(() => [
  { label: '全部项目', value: 'all' },
  ...projects.value.filter(project => project.uid===projectFilter.value || records.value.some(row => row.project_uid === project.uid))
    .map(project => ({ label: `${project.title || project.uid}${project.archived ? ' · 已归档' : ''}`, value: project.uid })),
  ...(projectsReady.value && records.value.some(row => !projectMap.value.has(row.project_uid)) ? [{ label: '项目已删除的记录', value: 'missing' }] : []),
])
const statusOptions = computed(() => [{ label: '全部发布状态', value: 'all' },
  ...[...new Set(records.value.map(row => row.status))].map(status => ({ label: PUBLICATION_STATUS[status] || status, value: status })),
])
const filteredRecords = computed(() => {
  const needle = search.value.trim().toLowerCase()
  return records.value.filter(row => (statusFilter.value === 'all' || row.status === statusFilter.value)
    && (projectFilter.value === 'all' || (projectFilter.value === 'missing' ? projectsReady.value && !projectMap.value.has(row.project_uid) : row.project_uid === projectFilter.value))
    && (!needle || [recordTitle(row), projectLabel(row), accountLabel(row), row.receipt?.bvid || ''].join(' ').toLowerCase().includes(needle)))
})
const filtered = computed(() => !!search.value.trim() || projectFilter.value !== 'all' || statusFilter.value !== 'all')
function clearFilters() { search.value = ''; projectFilter.value = 'all'; statusFilter.value = 'all' }
async function read(path: string, signal: AbortSignal) {
  const response = await fetch(path, { cache: 'no-store', signal })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.message || '读取发布记录失败，请重试')
  return data
}
async function refresh() {
  if (managing.value) return
  const ticket = ++revision
  controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''; contextError.value = ''
  const results = await Promise.allSettled([
    read('/api/publishing/publications'+(lifecycleFilter.value==='active' ? '' : '?lifecycle='+encodeURIComponent(lifecycleFilter.value)), controller.signal),
    read('/api/projects?archive=all', controller.signal),
    read('/api/publishing/catalog', controller.signal),
  ])
  if (disposed || ticket !== revision) return
  const [publications, sources, catalog] = results
  if (publications.status === 'fulfilled' && Array.isArray(publications.value)) {
    records.value = publications.value; ready.value = true; updatedAt.value = new Date().toLocaleTimeString('zh-CN')
  } else error.value = friendlyError(publications.status === 'rejected' ? publications.reason : null, '发布记录格式无效，请重新读取')
  const contextErrors: string[] = []
  if (sources.status === 'fulfilled' && Array.isArray(sources.value)) { projects.value = sources.value; projectsReady.value = true }
  else { projectsReady.value = false; contextErrors.push('项目信息暂未读取成功；仍可打开保留的发布记录。') }
  if (catalog.status === 'fulfilled' && Array.isArray(catalog.value?.accounts)) accounts.value = catalog.value.accounts
  else contextErrors.push('账号名称暂未读取成功；已有稿件回执仍保留。')
  contextError.value = contextErrors.join(' '); loading.value = false
}
async function lifecycleChanged() { managing.value=false;await refresh() }
async function setLifecycle(value:string) { if (loading.value || managing.value) return;lifecycleFilter.value=value;await refresh() }
onMounted(() => void refresh())
onBeforeUnmount(() => { disposed = true; revision++; controller?.abort() })
</script>

<template>
  <div class="workspace-page publication-records-page">
    <header class="records-heading">
      <div><span class="page-kicker">项目空间 / 发布记录</span><h1>全部发布记录</h1><p>管理草稿、上传进度与投稿回执。未上传草稿可回收，已有上传或BV的记录可归档；平台稿件与防重复回执始终保留。</p></div>
      <div class="heading-actions"><RouterLink to="/">返回项目空间 →</RouterLink><n-button secondary :loading="loading" :disabled="loading || managing" @click="refresh">刷新记录</n-button></div>
    </header>
    <nav class="records-lifecycle-tabs" aria-label="记录存放位置"><n-button v-for="location in [{value:'active',label:'常用记录'},{value:'archived',label:'已归档'},{value:'deleted',label:'回收的草稿'}]" :key="location.value" :type="lifecycleFilter===location.value ? 'primary':'default'" :secondary="lifecycleFilter===location.value" :quaternary="lifecycleFilter!==location.value" :disabled="loading || managing" @click="setLifecycle(location.value)">{{ location.label }}</n-button></nav>
    <div class="records-filters">
      <n-input v-model:value="search" clearable placeholder="搜索稿件、项目、账号或BV号" aria-label="搜索发布记录" />
      <n-select v-model:value="projectFilter" :options="projectOptions" aria-label="筛选来源项目" />
      <n-select v-model:value="statusFilter" :options="statusOptions" aria-label="筛选发布状态" />
      <n-button v-if="filtered" quaternary @click="clearFilters">清除筛选</n-button>
    </div>
    <p class="records-summary">显示 {{ filteredRecords.length }} / {{ records.length }} 条记录<span v-if="updatedAt"> · {{ updatedAt }} 读取本地状态</span><span v-if="records.length >= 200"> · 当前列表为最近200条</span></p>
    <p v-if="lifecycleFilter!=='active'" class="lifecycle-hint">{{ lifecycleFilter==='deleted' ? '草稿仍保存在本地数据库中，可恢复；这里不是系统回收站。' : '归档只隐藏本地记录，不删除B站稿件，也不会解除防重复投稿保护。' }}恢复后才能编辑或继续发布。</p>
    <n-alert v-if="error" type="error" class="records-notice">{{ error }}<span v-if="ready">；下面保留上次读取结果。</span></n-alert>
    <n-alert v-if="contextError" type="warning" class="records-notice">{{ contextError }}</n-alert>
    <n-spin :show="loading && !ready">
      <div v-if="filteredRecords.length" class="records-list">
        <n-card v-for="row in filteredRecords" :key="row.id" size="small" class="record-card">
          <div class="record-heading"><div class="record-title"><RouterLink :to="recordTarget(row)">{{ recordTitle(row) }}</RouterLink><n-tag size="small" :type="publicationStage(row.status).tone">{{ PUBLICATION_STATUS[row.status] || row.status }}</n-tag></div><RouterLink class="record-action" :to="recordTarget(row)">{{ typeof recordTarget(row)==='object' ? '到项目查看 →' : '查看保留记录 →' }}</RouterLink></div>
          <p class="record-meta"><span>{{ projectLabel(row) }}</span><span>{{ accountLabel(row) }}</span><span>{{ (row.snapshot?.parts || row.draft?.parts || []).length }} 个分P</span><span>{{ dateLabel(row.updated_at) }}</span></p>
          <a v-if="row.receipt?.bvid" class="receipt-link" :href="`https://www.bilibili.com/video/${encodeURIComponent(row.receipt.bvid)}`" target="_blank" rel="noopener noreferrer">{{ row.receipt.bvid }} ↗</a>
          <p v-if="row.error" class="record-error">{{ row.error }}</p>
          <PublicationProgress :publication="row" compact />
          <div class="record-management"><PublicationRecordLifecycle :record="row" :disabled="loading || managing" @busy-change="managing=$event" @changed="lifecycleChanged" /></div>
        </n-card>
      </div>
      <n-empty v-else-if="!loading && !error" :description="filtered ? '没有符合筛选的发布记录' : '还没有发布记录'" class="records-empty"><template #extra><RouterLink v-if="!filtered" to="/">打开项目，准备发布 →</RouterLink><n-button v-else size="small" @click="clearFilters">清除筛选</n-button></template></n-empty>
    </n-spin>
  </div>
</template>

<style scoped>
.records-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
h1 { margin: 6px 0 8px; font-size: 26px; font-weight: 650; }
.records-heading p, .records-summary { margin: 0; color: var(--df-muted); font-size: 12px; line-height: 1.8; }
.heading-actions { display: flex; align-items: center; gap: 18px; padding-top: 8px; flex-shrink: 0; }
a { color: var(--df-accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.heading-actions a, .record-action, .receipt-link { font-size: 12px; }
.records-filters { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.records-lifecycle-tabs{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--df-border);padding-bottom:12px}.record-management{margin-top:12px;display:flex;justify-content:flex-end}.lifecycle-hint{font-size:12px;line-height:1.8;color:var(--df-muted);margin:-6px 0 16px}
.records-filters > .n-input { flex: 1; min-width: 200px; }
.records-filters > .n-select { width: 210px; }
.records-summary { margin: 12px 0 18px; }
.records-notice { margin-bottom: 16px; }
.records-list { display: flex; flex-direction: column; gap: 12px; }
.record-heading, .record-title { display: flex; align-items: center; gap: 12px; }
.record-heading { justify-content: space-between; }
.record-title { flex-wrap: wrap; min-width: 0; }
.record-title > a { color: var(--df-text); font-size: 15px; font-weight: 600; overflow-wrap: anywhere; }
.record-action { white-space: nowrap; }
.record-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 12px 0 4px; color: var(--df-muted); font-size: 12px; }
.record-error { color: var(--df-danger, #ef9299); font-size: 12px; margin: 10px 0 0; overflow-wrap: anywhere; }
.records-empty { padding: 70px 0; }
@media (max-width: 720px) {
  .records-heading { flex-direction: column; gap: 12px; }
  .heading-actions { width: 100%; padding-top: 0; justify-content: space-between; }
  .records-filters > .n-select { flex: 1; min-width: 160px; }
  .record-heading { align-items: flex-start; gap: 8px; }
}
</style>
