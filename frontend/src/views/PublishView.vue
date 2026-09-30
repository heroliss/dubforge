<script setup lang="ts">
import FilePathLink from '../components/FilePathLink.vue'
import ProgressActivity from '../components/ProgressActivity.vue'
import PublicationRightsHint from '../components/PublicationRightsHint.vue'
import { publicationActivity } from '../utils/progressActivity'
import ConfigurationForm from '../components/ConfigurationForm.vue'
import { computed, h, onMounted, onBeforeUnmount, ref } from 'vue'
import { useRoute, onBeforeRouteLeave, RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NCheckbox, NInput, NSelect, NTag, NTabs, NTabPane, NQrCode, NProgress, useMessage, useDialog } from 'naive-ui'
import PublishSettingsForm from '../components/PublishSettingsForm.vue'
import PublishCopyAssistant from '../components/PublishCopyAssistant.vue'
import BiliStudio from '../components/BiliStudio.vue'
import { PUBLICATION_STATUS, PUBLISH_ACTIVE, LOGIN_STATUS, movePart, publicationScope, publicationAiNotice } from '../utils/publishing'
import { finalVideoLabel } from '../utils/preview'
import { fmtSize } from '../utils/format'
import { usePublishMetadata } from '../composables/usePublishMetadata'
import { refreshInBackground } from '../utils/backgroundRefresh'
import type { ConfigurationSource } from '../utils/configurationSource'

const route = useRoute(), message = useMessage(), dialog = useDialog()
const catalog = ref<any>({ accounts: [], templates: [], defaults: {} }), records = ref<any[]>([]), projects = ref<any[]>([])
const current = ref<any>(null), draft = ref<any>(null), source = ref<any>(null)
const tab = ref(route.params.uid && route.hash === '#project-rules' ? 'rules' : 'drafts'), busy = ref(false), error = ref(''), saved = ref(''), projectUid = ref(String(route.params.uid || ''))
const loginId = ref(''), loginState = ref<any>(null), receiptBV = ref('')
const confirming = ref(false)
const refreshingFiles = ref(false)
const studioGuard = ref(false)
const coverLoading = ref(false), coverError = ref(''), coverBroken = ref(false)
let coverGeneration = 0
let timer: ReturnType<typeof setTimeout> | undefined, disposed = false
let listRevision = 0
const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== saved.value)
const locked = computed(() => busy.value || current.value?.status !== 'draft')
const metadataLocked = computed(() => busy.value || !['draft', 'uploaded'].includes(current.value?.status))
const lockReason = computed(() => current.value?.status === 'unknown'
  ? '请先核对B站回执，暂不允许修改或复制，以免重复投稿。'
  : ['queued', 'uploading', 'paused', 'failed'].includes(current.value?.status)
    ? '上传清单已冻结；完成上传后可修改文案。若要更换文件或账号，请复制新草稿。'
    : '当前资料已锁定；已有投稿请到「B站稿件」修改，另一条投稿请复制新草稿。')
const resolved = computed(() => ({values:draft.value?{...current.value?.base_settings,...draft.value.overrides}:{}}))
const active = computed(() => PUBLISH_ACTIVE.includes(current.value?.status))
const hasAiDubbing = computed(() => current.value?.status === 'draft'
  ? !!draft.value?.parts.some((p:any) => source.value?.files.some((f:any) => f.id === p.artifact_id && f.has_dubbing))
  : !!current.value?.snapshot.parts?.some((p:any) => p.has_dubbing))
const accountOptions = computed(() => catalog.value.accounts.map((a: any) => ({ label: `${a.name} · ${a.id}`, value: a.id })))
const displayValues = computed(() => current.value?.status === 'draft' ? resolved.value.values : current.value?.status === 'uploaded' ? { ...(current.value.snapshot.base_settings || current.value.snapshot.settings), ...draft.value.overrides } : current.value?.snapshot.settings || {})
const displaySources = computed<Record<string, ConfigurationSource>>(() => {
  const row=current.value, snapshot=row?.snapshot
  const editable=row?.status==='draft'||row?.status==='uploaded'
  // 冻结稿件的来源不能用今天的项目设置倒推；未记录来源时如实显示为稿件快照。
  const recorded=row?.status==='draft'?row.base_sources:editable?snapshot?.base_sources:snapshot?.sources
  const result:Record<string,ConfigurationSource>=Object.fromEntries(Object.keys(displayValues.value).map(key=>[key,recorded?.[key]||{kind:row?.status==='draft'?'unknown':'snapshot'}]))
  if(editable)for(const key of Object.keys(draft.value?.overrides||{}))result[key]={kind:'publication'}
  return result
})
const clone = (value: any) => JSON.parse(JSON.stringify(value))
const metadataAccount = computed(() => tab.value === 'drafts' ? String(draft.value?.account_id || '') : '')
const collectionContext = computed(() => `publication:${current.value?.id || ''}`)
const metadataAccountName = computed(() => catalog.value.accounts.find((a: any) => a.id === metadataAccount.value)?.name || '')
const { metadata, loading: metadataLoading, error: metadataError, ready: metadataReady, refresh: refreshMetadata, refreshAfterMutation } =
  usePublishMetadata(metadataAccount, account => api(`/accounts/${encodeURIComponent(account)}/metadata`, 'POST', {}))

async function collectionCreated(result: any) {
  message.success(`已在B站创建合集「${result.title}」，尚未加入视频`)
  if (result.account_id !== metadataAccount.value || result.context_key !== collectionContext.value) return
  const target = draft.value
  const selected = () => displayValues.value.season_id
  await refreshAfterMutation()
  if (disposed || result.account_id !== metadataAccount.value || result.context_key !== collectionContext.value
      || target !== draft.value
      || (selected() ?? null) !== result.previous_season) return
  const season = metadata.value.seasons?.find((s: any) => s.id === result.season_id)
  if (!season) { message.warning(`合集已创建（ID ${result.season_id}），列表暂未返回；请稍后刷新，不要重复创建`); return }
  if (tab.value === 'drafts' && metadataLocked.value) return
  const overrides = target.overrides
  overrides.season_id = season.id
  overrides.section_id = season.sections?.length === 1 ? season.sections[0].id : null
}

async function api(path: string, method = 'GET', body?: any) {
  const response = await fetch(`/api/publishing${path}`, { method, headers: body === undefined || body instanceof FormData ? undefined : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body) })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Object.assign(new Error(data.message || '发布中心请求失败'), { code: data.code })
  return data
}
async function run(action: () => Promise<any>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { return await action() } catch (e: any) { error.value = e.message; message.error(e.message) }
  finally { busy.value = false }
}
async function refreshList() {
  const revision = ++listRevision
  const data = await api(`/publications${route.params.uid ? '?project_uid=' + encodeURIComponent(String(route.params.uid)) : ''}`)
  if (!disposed && revision === listRevision) records.value = data
}
function syncList() {
  refreshInBackground(refreshList, () => { if (!disposed) message.warning('操作已完成，但队列列表刷新失败，请稍后刷新') })
}
function apply(record: any) {
  // 写回执立即呈现；先前发出的列表请求不能用旧状态覆盖它。
  listRevision++
  records.value = records.value.some(r => r.id === record.id)
    ? records.value.map(r => r.id === record.id ? record : r) : [record, ...records.value]
  coverGeneration++; coverLoading.value = false; coverError.value = ''; coverBroken.value = false
  current.value = record; draft.value = clone(record.draft); saved.value = JSON.stringify(draft.value)
}
function canSwitch() { if (busy.value || confirming.value) return false; if (dirty.value || studioGuard.value) { message.warning('请先保存/放弃当前修改，并等待当前操作完成，再切换页面'); return false }; return true }
async function studioRefresh() { catalog.value=await api('/catalog');await refreshList();if(current.value && !dirty.value)apply(await api(`/publications/${current.value.id}`)) }
async function open(id: string) {
  if (!canSwitch()) return
  await run(async () => {
    const record = await api(`/publications/${id}`)
    apply(record); source.value = null; tab.value = 'drafts'
    try { source.value = await api(`/projects/${record.project_uid}/candidates`) }
    catch { error.value = '来源项目不可用；已保存的发布回执仍可查看。' }
  })
}
async function create() {
  if (!projectUid.value || !canSwitch()) return
  await run(async () => {
    const record = await api(`/projects/${projectUid.value}/drafts`, 'POST', {})
    source.value = await api(`/projects/${projectUid.value}/candidates`)
    apply(await api(`/publications/${record.id}`)); syncList()
  })
}
async function save() { if (coverLoading.value) return; await run(async () => { apply(await api(`/publications/${current.value.id}`, 'PUT', { ...draft.value, revision: current.value.revision })); syncList(); message.success('资料已保存，未触发投稿') }) }
function change(key: string, value: any) { draft.value.overrides[key] = value }
function inherit(key: string) { delete draft.value.overrides[key] }
function selectAccount(value: string) { draft.value.account_id = value || ''; draft.value.template_id = '' }
function addPart(file: any, selected: boolean) {
  if (selected) draft.value.parts.push({ artifact_id: file.id, title: file.is_source ? '原声原片' : file.version_name || finalVideoLabel(file.name).split(' · ').slice(0, 2).join(' · ') })
  else draft.value.parts = draft.value.parts.filter((p: any) => p.artifact_id !== file.id)
}
async function refreshFiles() {
  if (!current.value || refreshingFiles.value) return
  const id = current.value.id, uid = current.value.project_uid
  refreshingFiles.value = true
  try {
    const data = await api(`/projects/${uid}/candidates`)
    if (!disposed && current.value?.id === id) { source.value = data; message.success('已刷新文件状态，分P选择与未保存资料保持不变') }
  } catch (e: any) { if (!disposed && current.value?.id === id) message.error(e.message || '刷新失败，请重试') }
  finally { refreshingFiles.value = false }
}
async function cover(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  const data = new FormData(); data.append('file', file)
  await setCover(() => api('/covers', 'POST', data))
  input.value = ''
}
async function setCover(request: () => Promise<any>) {
  if (locked.value || coverLoading.value) return
  const target = draft.value, id = current.value.id, ticket = ++coverGeneration
  coverLoading.value = true; coverError.value = ''
  try {
    const result = await request()
    if (!disposed && ticket === coverGeneration && current.value?.id === id && draft.value === target) {
      target.cover_id = result.cover_id; coverBroken.value = false
    }
  } catch (e: any) {
    if (!disposed && ticket === coverGeneration) coverError.value = e.message || '封面设置失败，请重试'
  } finally { if (!disposed && ticket === coverGeneration) coverLoading.value = false }
}
async function importCover() {
  const uid = current.value?.project_uid
  if (uid) await setCover(() => api(`/projects/${uid}/platform-cover`, 'POST', {}))
}
async function upload(andSubmit = false) {
  if (confirming.value || busy.value || coverLoading.value || disposed) return
  if (dirty.value) { message.warning('请先保存草稿'); return }
  await run(async () => {
    const key = current.value.id
    const preview = await api(`/publications/${key}/plan`, 'POST', {})
    if (disposed || current.value?.id !== key) return
    confirming.value = true
    const aiNotice = publicationAiNotice(preview, metadata.value)
    let accepted = false
    const confirm = andSubmit || aiNotice ? dialog.warning : dialog.info
    confirm({ title: andSubmit ? '确认上传并投稿到B站' : '确认仅上传所选文件',
      content: (andSubmit ? `${publicationScope(preview.snapshot)}。标题「${preview.snapshot.settings.title}」。全部文件上传成功后将自动正式投稿，不再二次询问；定时发布与可见范围保持上述设置。关闭页面不停止后台任务。`
        : `只上传 ${preview.snapshot.parts.length} 个分P及封面至「${preview.snapshot.account_name}」，不会正式投稿。适合稍后投稿或追加到已有BV；上传后文字资料仍可修改。`) + (aiNotice ? `\n\n${aiNotice}` : ''),
      positiveText: andSubmit ? '确认上传并投稿' : '仅上传，不投稿', negativeText: '取消', onAfterLeave: () => { confirming.value = false },
      onPositiveClick: () => {
        if (accepted || busy.value || disposed || current.value?.id !== key) return
        if (dirty.value) { message.warning('资料已修改，请先保存并重新确认'); return false }
        accepted = true
        return run(async () => { apply(await api(`/publications/${key}/actions`, 'POST', {
          action: andSubmit ? 'publish' : 'upload', revision: preview.revision, fingerprint: preview.fingerprint,
          ...(andSubmit ? {confirmed:true} : {}),
          ...(aiNotice ? {acknowledge_ai_notice:true} : {}),
        })); syncList() })
      } })
  })
}
async function submit() {
  if (confirming.value || busy.value || dirty.value || disposed) return
  await run(async () => {
    const key = current.value.id
    const preview = await api(`/publications/${key}/plan`, 'POST', {})
    if (disposed || current.value?.id !== key) return
    const aiNotice = publicationAiNotice(preview, metadata.value)
    confirming.value = true
    let accepted = false
    dialog.warning({ title: '确认正式投稿到B站', content: publicationScope(preview.snapshot) + (aiNotice ? `\n\n${aiNotice}` : ''), positiveText: '确认投稿', negativeText: '取消',
      onAfterLeave: () => { confirming.value = false },
      onPositiveClick: () => {
        if (accepted || busy.value || disposed || current.value?.id !== key) return
        if (dirty.value) { message.warning('资料已修改，请先保存并重新确认'); return false }
        accepted = true
        return run(async () => { apply(await api(`/publications/${key}/actions`, 'POST', {
          action:'submit', revision:preview.revision, fingerprint:preview.fingerprint, confirmed:true,
          ...(aiNotice ? {acknowledge_ai_notice:true} : {}),
        })); syncList() })
      } })
  })
}
async function action(name: string) { await run(async () => { apply(await api(`/publications/${current.value.id}/actions`, 'POST', { action: name, revision: current.value.revision })); syncList() }) }
async function pause() { await run(async () => { await api(`/publications/${current.value.id}/pause`, 'POST', {}); message.info('正在停止上传，不会删除B站稿件') }) }
async function copy() { await run(async () => { const row = await api(`/publications/${current.value.id}/copy`, 'POST', {}); apply(await api(`/publications/${row.id}`)); syncList() }) }
async function attachReceipt() { await run(async () => { apply(await api(`/publications/${current.value.id}/receipt`, 'POST', { bvid: receiptBV.value })); syncList() }) }
async function login() { await run(async () => { loginId.value = (await api('/login', 'POST', {})).id; loginState.value = { status: 'generating' } }) }
async function cancelLogin() { await run(async () => {
  const state = await api(`/login/${loginId.value}/cancel`, 'POST', {})
  loginId.value = ''; loginState.value = state
  if (state.status === 'connected') { catalog.value = await api('/catalog'); message.success('账号已连接，无需重新扫码') }
}) }

async function poll() {
  try {
    if (!busy.value) {
      if (current.value && active.value && !dirty.value) {
        const key = current.value.id
        const row = await api(`/publications/${key}`)
        if (!disposed && !busy.value && !dirty.value && current.value?.id === key) apply(row)
      }
      if (records.value.some(r => PUBLISH_ACTIVE.includes(r.status))) await refreshList()
      if (loginId.value) {
        await pollLogin()
      }
    }
  } catch (e: any) { error.value = e.message }
  finally { if (!disposed) timer = setTimeout(poll, 2000) }
}
async function pollLogin() {
  const id = loginId.value
  if (!id) return
  let state
  try { state = await api(`/login/${id}`) }
  catch (e: any) {
    if (disposed || loginId.value !== id) return
    if (e.code === 'PUBLISH_LOGIN_NOT_FOUND') {
      loginId.value = ''; loginState.value = {status:'expired',error:e.message,code:e.code}; return
    }
    throw e
  }
  // 取消或重新获取后，旧请求不得复活旧二维码或覆盖新登录状态。
  if (disposed || loginId.value !== id) return
  loginState.value = state
  if (state.status === 'connected') {
    const accounts = await api('/catalog')
    if (disposed || loginId.value !== id) return
    catalog.value = accounts; loginId.value = ''; message.success('B站账号已连接')
    // 重新登录可能恢复先前失败的选项请求；不改变用户选择的发布账号。
    if (metadataAccount.value) void refreshMetadata()
  } else if (['failed', 'expired', 'cancelled'].includes(state.status)) loginId.value = ''
}
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || studioGuard.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(async () => {
  window.addEventListener('beforeunload', beforeUnload)
  await run(async () => { catalog.value = await api('/catalog'); await refreshList(); const response = await fetch('/api/projects'); if (!response.ok) throw new Error('项目列表读取失败'); projects.value = await response.json() })
  if (!disposed && tab.value === 'drafts' && route.params.uid && records.value.length) await open(records.value[0].id)
  if (!disposed) timer = setTimeout(poll, 2000)
})
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); window.removeEventListener('beforeunload', beforeUnload) })
onBeforeRouteLeave(() => {
  if (!dirty.value && !studioGuard.value) return true
  return new Promise<boolean>(resolve => dialog.warning({ title: '尚有未保存的发布资料', content: '离开会丢弃当前修改，不会取消已经开始的上传。', positiveText: '放弃修改并离开', negativeText: '继续编辑', onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false), onClose: () => resolve(false), onMaskClick: () => resolve(false) }))
})
</script>

<template>
  <main class="publish-page">
    <header class="page-heading"><div><span class="eyebrow">{{ route.params.uid ? '08 / PUBLISH' : 'PUBLISH / BILIBILI' }}</span><h1>{{ route.params.uid ? '发布到B站' : '发布中心' }}</h1><p>一个稿件，多种配音版本；同类内容归入合集。</p></div><a href="https://member.bilibili.com/platform/upload/video/frame" target="_blank" rel="noopener noreferrer">打开B站创作中心 ↗</a></header>
    <n-alert v-if="error" type="error" class="notice">{{ error }}</n-alert>
    <n-alert v-if="!catalog.worker_ready" type="warning" class="notice">发布组件尚未就绪。请在 workers/bilibili 目录运行 npm ci --ignore-scripts；本地草稿仍可编辑。</n-alert>
    <n-tabs :value="tab" type="line" @update:value="value => { if (canSwitch()) tab = value }">
      <n-tab-pane v-if="route.params.uid" name="rules" tab="项目发布设置"><ConfigurationForm :scope="`project:${route.params.uid}`" :sections="['publication']" @dirty-change="studioGuard=$event" @saved="studioRefresh" /></n-tab-pane>
      <n-tab-pane name="drafts" tab="稿件与队列">
        <div class="publish-workspace">
          <aside class="publication-list"><div class="new-draft"><n-select v-model:value="projectUid" :options="projects.map(p => ({ label: p.title, value: p.uid }))" filterable placeholder="选择来源项目" :disabled="busy || !!route.params.uid" /><n-button :disabled="!projectUid || busy || dirty" secondary type="primary" @click="create">新建发布草稿</n-button></div>
            <div v-if="!records.length" class="empty">还没有发布记录。新建草稿不会上传视频。</div>
            <button v-for="item in records" :key="item.id" class="publication-item" :class="{ selected: current?.id === item.id }" :disabled="busy" @click="open(item.id)"><strong>{{ item.snapshot.settings?.title || item.draft.overrides?.title || item.draft.content?.title || '未命名稿件' }}</strong><span>{{ PUBLICATION_STATUS[item.status] || item.status }}</span><small>{{ item.draft.parts.length }} 个分P · {{ new Date(item.updated_at).toLocaleDateString() }}</small></button>
          </aside>
          <section v-if="current && draft" class="publication-editor">
            <div class="editor-heading"><div><n-tag size="small" :bordered="false">{{ PUBLICATION_STATUS[current.status] }}</n-tag><span v-if="dirty" class="dirty">未保存</span></div><RouterLink :to="`/projects/${current.project_uid}/steps/assemble`">返回成片 →</RouterLink></div>
            <n-alert v-if="current.error" type="warning" class="notice">{{ current.error }}</n-alert>
            <n-alert v-if="current.status === 'unknown'" type="warning" class="notice">投稿可能已成功。请先在B站核对，填写BV号关联回执，不要重复发布。<div class="receipt-form"><n-input v-model:value="receiptBV" placeholder="BV号" /><n-button :disabled="busy" @click="attachReceipt">核对并关联</n-button></div></n-alert>
            <n-card title="发布内容" size="small">
              <div class="two-columns"><label>发布账号<n-select :value="draft.account_id || null" :options="accountOptions" :disabled="locked" placeholder="先在账号页连接B站" @update:value="selectAccount" /></label><span class="muted">继承项目发布设置；此处修改仅用于当前稿件。</span></div>
              <div class="subheading"><strong>分P清单 · {{ draft.parts.length }} 个</strong><span>所有分P共用下方稿件设置</span><n-button size="tiny" quaternary :loading="refreshingFiles" :disabled="refreshingFiles" @click="refreshFiles">刷新文件列表</n-button></div>
              <div v-for="(part, index) in draft.parts" :key="part.artifact_id" class="part-row"><span class="part-number">P{{ index + 1 }}</span><n-input v-model:value="part.title" :disabled="locked" maxlength="80" :aria-label="`P${index + 1}名称`" /><n-button size="tiny" :disabled="locked || index === 0" :aria-label="`上移P${index + 1}`" @click="draft.parts = movePart(draft.parts, index, -1)">↑</n-button><n-button size="tiny" :disabled="locked || index === draft.parts.length - 1" :aria-label="`下移P${index + 1}`" @click="draft.parts = movePart(draft.parts, index, 1)">↓</n-button><n-button size="tiny" :disabled="locked" @click="draft.parts.splice(index, 1)">移除</n-button></div>
              <p v-if="source && draft.parts.some((p: any) => !source.files.some((f: any) => f.id === p.artifact_id && f.exists))" class="muted">部分已选分P的本地文件缺失。选择仍保留；还原文件后刷新，或移除该分P。已上传的远程文件不受影响。</p>
              <div v-if="!draft.parts.length" class="empty">尚未选择成片，不会自动勾选任何版本。</div>
              <details v-if="!locked" class="available-files" open><summary>从成片或原片选择</summary><div v-for="file in source?.files || []" :key="file.id" class="candidate"><n-checkbox :checked="draft.parts.some((p: any) => p.artifact_id === file.id)" :disabled="!file.exists" @update:checked="addPart(file, $event)"><span>{{ file.is_source ? '原声原片 · 不叠加字幕或评论' : file.version_name ? `${file.version_name} · ${file.version_summary}` : finalVideoLabel(file.name) }}</span></n-checkbox><small>{{ fmtSize(file.size) }} · {{ new Date(file.created_at).toLocaleString() }}{{ file.exists ? '' : ' · 文件不存在' }} · <FilePathLink v-if="file.path" :path="file.path" label="定位文件" /></small></div><p v-if="!source?.files.length" class="muted">还没有可发布的 MP4/MOV/MKV 原片或成片；试片不会进入发布清单。</p><RouterLink :to="`/projects/${projectUid}/steps/assemble`">修改成片版本效果 →</RouterLink></details>
            </n-card>
            <n-card title="稿件资料" size="small">
              <div class="cover-row"><div class="cover-preview"><img v-if="draft.cover_id" :src="`/api/publishing/covers/${draft.cover_id}`" alt="本稿件封面" /><img v-else-if="source?.thumbnail_preview && !coverBroken" :src="source.thumbnail_preview" alt="可选的平台封面" @error="coverBroken = true" /><div v-else class="cover-placeholder">选择封面</div><small v-if="!draft.cover_id && source?.thumbnail_preview && !coverBroken" class="muted">平台封面 · 尚未使用</small></div><div><label class="file-picker">上传封面<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="locked || coverLoading" @change="cover" /></label><n-button size="small" :loading="coverLoading" :disabled="locked || coverLoading || !source?.platform_cover_available" @click="importCover">使用平台封面</n-button><small v-if="coverError" role="alert" class="cover-error">{{ coverError }}</small></div></div>
              <div class="metadata-bar"><span>{{ metadataLoading ? '正在读取账号选项，其他资料可继续编辑' : '标题、封面及以下设置由整个稿件共用' }}</span><n-button size="small" :loading="metadataLoading" :disabled="busy || metadataLoading || !metadataAccount" @click="refreshMetadata()">刷新账号选项</n-button></div>
              <n-alert v-if="metadataError" type="warning" class="notice">{{ metadataError }}<span v-if="metadataReady">；暂显示上次读取的选项。</span></n-alert>
              <n-alert v-if="metadata.collection_error" type="warning" class="notice">合集读取失败，请重试；不会擅自改为不加入合集。</n-alert>
              <n-alert v-if="metadataLocked && !busy" type="info" class="notice">{{ lockReason }}</n-alert>
              <PublishCopyAssistant :publication-id="current.id" :revision="current.revision" :values="displayValues" :disabled="metadataLocked" @apply="values => Object.entries(values).forEach(([key, value]) => change(key, value))" />
              <PublishSettingsForm :values="displayValues" :source-published-date="source?.source_published_date" :sources="displaySources" :overrides="draft.overrides" :metadata="metadata" :metadata-loading="metadataLoading" :metadata-error="metadataError" :metadata-ready="metadataReady" :metadata-account="metadataAccount" :metadata-account-name="metadataAccountName" :context-key="collectionContext" :cover-id="draft.cover_id" :has-ai-dubbing="hasAiDubbing" :disabled="metadataLocked" @change="change" @inherit="inherit" @collection-created="collectionCreated" @refresh-metadata="refreshMetadata()" />
              <details class="api-scope"><summary>与B站网页设置的对应范围</summary><p>已接入基本文案、封面、分P、新版分区、稿件类型与声明、可见范围、定时发布、水印、互动开关和话题选择。合集在取得投稿回执后单独加入。</p><p>商业推广、付费推广、卡片、章节、平台外挂字幕、杜比／Hi-Res、空间隐藏尚未接入，可在B站补充。账号权限及最终校验以B站为准。</p></details>
            </n-card>
            <div class="publish-actions"><span v-if="active">{{ PUBLICATION_STATUS[current.status] }}<template v-if="current.snapshot.publish_after_upload && ['queued','uploading'].includes(current.status)"> · 完成后自动投稿</template></span><span v-else-if="current.status === 'draft'">保存仅在本地；投稿需确认</span><span v-else>使用已确认的资料和成片版本</span>
              <n-button v-if="['draft', 'uploaded'].includes(current.status)" :disabled="busy || coverLoading || !dirty" @click="save">保存资料</n-button>
              <template v-if="['draft', 'paused', 'failed'].includes(current.status)"><n-button :disabled="busy || confirming || coverLoading || dirty || !draft.parts.length || !catalog.worker_ready" title="上传后停留待投稿，适合稍后投稿或追加到已有BV" @click="upload(false)">{{ current.status === 'draft' ? '仅上传文件' : '仅继续上传' }}</n-button><n-button type="primary" :disabled="busy || confirming || coverLoading || dirty || !draft.parts.length || !catalog.worker_ready" @click="upload(true)">{{ current.status === 'draft' ? '上传并投稿' : '继续上传并投稿' }}</n-button></template>
              <n-button v-if="current.status === 'uploaded'" type="primary" :disabled="busy || dirty || confirming" @click="submit">确认投稿</n-button>
              <n-button v-if="current.status === 'uploaded'" :disabled="busy || dirty || confirming" @click="tab='studio'">改为追加到已有BV</n-button>
              <n-button v-if="['queued', 'uploading'].includes(current.status)" :disabled="busy" @click="pause">暂停上传</n-button>
              <n-button v-if="current.receipt.bvid" :disabled="busy || active" @click="action('check')">查询平台状态</n-button>
              <n-button v-if="current.status === 'collection_pending'" :disabled="busy" @click="action('collection')">重试加入合集</n-button>
              <n-button v-if="!['draft', 'unknown'].includes(current.status) && !active" :disabled="busy" @click="copy">复制为新草稿</n-button>
            </div>
            <PublicationRightsHint v-if="['draft', 'uploaded', 'paused', 'failed'].includes(current.status)" />
            <n-progress v-if="['uploading', 'paused', 'uploaded'].includes(current.status)" type="line" :percentage="Math.round(current.progress)" :show-indicator="true" />
            <ProgressActivity :activity="publicationActivity(current)" />
            <div v-if="current.receipt.bvid" class="receipt"><a :href="`https://www.bilibili.com/video/${current.receipt.bvid}`" target="_blank" rel="noopener noreferrer">{{ current.receipt.bvid }} ↗</a><span>{{ current.receipt.state_desc || '已取得投稿回执，上传完成不代表审核通过' }}</span><span v-if="current.receipt.collection_joined">已加入合集</span></div>
            <n-alert v-if="current.receipt.reject_reason" type="warning" class="notice">{{ current.receipt.reject_reason }}</n-alert>
            <details v-if="current.receipt.videos?.length" class="available-files"><summary>各分P审核 / 转码信息</summary><p v-for="(p,i) in current.receipt.videos" :key="p.cid">P{{ i+1 }} · {{ p.title }} · {{ p.state_desc || '平台未提供状态说明' }}<span v-if="p.xcode_state !== undefined && p.xcode_state !== null"> · 转码状态码 {{ p.xcode_state }}</span><br v-if="p.reject_reason" />{{ p.reject_reason }}</p></details>
          </section>
          <div v-else class="editor-empty"><h2>准备下一条发布</h2><p>选择项目，新建草稿。原声、不同配音版本可按顺序加入同一个稿件。</p><n-button secondary @click="tab = 'accounts'">连接B站账号</n-button></div>
        </div>
      </n-tab-pane>
      <n-tab-pane name="studio" tab="B站稿件">
        <BiliStudio v-if="tab==='studio'" :accounts="catalog.accounts" :projects="projects" :default-project="projectUid" @guard="studioGuard=$event" @refresh="studioRefresh" />
      </n-tab-pane>
      <n-tab-pane name="accounts" tab="账号">
        <n-card title="B站账号" class="settings-card"><div v-for="account in catalog.accounts" :key="account.id" class="account-row"><strong>{{ account.name }}</strong><span>UID {{ account.id }}</span></div><p class="muted">登录凭据由 Windows 当前用户加密保存，不进入项目文件或 AI 请求。</p><n-button type="primary" :disabled="busy || !!loginId || !catalog.worker_ready" @click="login">扫码连接 / 重新登录</n-button><div v-if="loginState" class="qr"><div v-if="loginState.url" class="qr-frame"><n-qr-code :value="loginState.url" :size="180" :padding="0" type="svg" color="#111111" background-color="#ffffff" /></div><p role="status" aria-live="polite">{{ loginState.error || LOGIN_STATUS[loginState.status] || '正在读取登录状态…' }}</p><small v-if="loginState.code" class="muted">{{ loginState.code }}</small><n-button v-if="loginId" :disabled="busy" @click="cancelLogin">取消登录</n-button></div></n-card>
      </n-tab-pane>
    </n-tabs>
  </main>
</template>

<style scoped>
.deleted-templates { margin-top:20px; color:var(--df-muted); font-size:12px; }.deleted-templates summary { cursor:pointer; padding:8px 0; }.delete-template { margin-right:auto; }
.cover-preview { display:grid; gap:6px; }.cover-error { color:var(--df-danger, #f08d8d); max-width:360px; }
.api-scope { margin-top:20px; color:var(--df-muted); font-size:11px; line-height:1.8; }.api-scope p { max-width:760px; }
.qr { display:flex; flex-direction:column; align-items:flex-start; gap:12px; } .qr p { margin:0; max-width:580px; }
.qr-frame { display:inline-flex; flex:none; padding:16px; border-radius:10px; background:#fff; line-height:0; }
.qr-frame :deep(.n-qr-code) { box-sizing:content-box; flex:none; }
.qr-frame :deep(svg) { display:block; }
.publish-page { max-width:1480px; margin:0 auto; padding:28px 32px 60px; }.page-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:22px; }.eyebrow { color:var(--df-accent); font-size:10px; letter-spacing:.18em; }h1 { margin:7px 0; font-size:26px; }.page-heading p,.muted { color:var(--df-muted); font-size:12px; }.page-heading a,.editor-heading a { font-size:12px; }.notice { margin:12px 0; }
.publish-workspace { display:grid; grid-template-columns:260px minmax(0,1fr); gap:24px; margin-top:12px; }.publication-list { border-right:1px solid var(--df-border); padding-right:20px; }.new-draft { display:grid; gap:10px; margin-bottom:20px; }.publication-item { width:100%; padding:14px; text-align:left; border:1px solid transparent; border-radius:8px; margin-bottom:8px; background:transparent; color:var(--df-text); cursor:pointer; }.publication-item strong { display:block; line-height:1.6; font-size:13px; overflow-wrap:anywhere; }.publication-item span,.publication-item small { display:block; color:var(--df-muted); font-size:11px; margin-top:6px; }.publication-item.selected { border-color:var(--df-border); background:var(--df-accent-soft); }.publication-item:hover { background:var(--df-accent-soft); }.publication-editor { min-width:0; }.publication-editor :deep(.n-card) { margin-bottom:18px; }.editor-heading { display:flex; justify-content:space-between; gap:12px; margin-bottom:16px; }.dirty { font-size:11px; color:#efbd70; margin-left:12px; }.two-columns { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }.two-columns label { display:grid; gap:8px; font-size:12px; }.subheading { margin:24px 0 12px; display:flex; justify-content:space-between; gap:12px; font-size:12px; }.subheading span,.metadata-bar span { color:var(--df-muted); font-size:11px; }.part-row { display:flex; align-items:center; gap:8px; margin-bottom:9px; }.part-number { color:var(--df-accent); font-size:12px; flex:0 0 25px; }.candidate { display:flex; flex-direction:column; gap:4px; padding:10px 0; border-bottom:1px solid var(--df-border); }.candidate small { font-size:10px; color:var(--df-muted); padding-left:26px; }.available-files { margin-top:18px; font-size:12px; }summary { cursor:pointer; color:var(--df-muted); }.cover-row { display:flex; gap:20px; align-items:center; margin-bottom:24px; }.cover-row img,.cover-placeholder { width:192px; aspect-ratio:16/10; object-fit:cover; border-radius:8px; background:var(--df-accent-soft); }.cover-placeholder { display:grid; place-items:center; color:var(--df-muted); }.cover-row>div:last-child { display:flex; flex-direction:column; gap:12px; }.file-picker { font-size:12px; display:grid; gap:8px; }.file-picker input { max-width:230px; font-size:11px; }.metadata-bar { display:flex; flex-wrap:wrap; justify-content:space-between; gap:10px; align-items:center; margin:16px 0; }.publish-actions { display:flex; justify-content:flex-end; flex-wrap:wrap; align-items:center; gap:10px; padding:16px 0; }.publish-actions>span { color:var(--df-muted); font-size:11px; margin-right:auto; }.receipt { display:flex; gap:16px; flex-wrap:wrap; margin-top:16px; font-size:12px; }.receipt span { color:var(--df-muted); }.receipt-form { display:flex; gap:10px; margin-top:12px; }.empty,.editor-empty { padding:24px; color:var(--df-muted); font-size:12px; line-height:1.8; }.editor-empty { padding:70px 30px; }.editor-empty h2 { color:var(--df-text); font-size:22px; }.settings-card { max-width:900px; }.account-row { display:flex; gap:18px; align-items:center; padding:14px 0; }.account-row span { color:var(--df-muted); font-size:12px; }.qr { padding:24px 0; }.templates-layout { display:grid; grid-template-columns:230px minmax(0,1fr); gap:24px; padding-top:12px; }.templates-layout aside>:deep(*) { margin-bottom:12px; }.profile-actions { display:flex; justify-content:flex-end; padding-top:24px; }
@media(max-width:1000px) { .publish-page { padding:20px; }.publish-workspace { grid-template-columns:210px minmax(0,1fr); gap:16px; }.cover-row { flex-wrap:wrap; } }
@media(max-width:760px) { .publish-workspace,.templates-layout,.two-columns { grid-template-columns:1fr; }.publication-list { border-right:0; padding-right:0; max-height:300px; overflow:auto; }.page-heading { align-items:flex-start; flex-direction:column; }.subheading { flex-direction:column; } }
</style>
