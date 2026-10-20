<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NQrCode, useMessage } from 'naive-ui'
import BiliStudio from './BiliStudio.vue'
import { LOGIN_STATUS } from '../utils/publishing'
import { publishApi } from '../utils/publishApi'

const props = withDefaults(defineProps<{ active?: boolean; defaultAccount?: string; defaultProject?: string; defaultBvid?: string; manageStudio?: boolean }>(), { active: true })
const emit = defineEmits<{ guard: [value: boolean]; 'busy-change': [value: boolean]; 'dirty-change': [value: boolean] }>()
const message = useMessage()
const catalog = ref<any>({ accounts: [] }), catalogLoading = ref(false), catalogReady = ref(false), catalogError = ref('')
const busy = ref(false), error = ref(''), loginId = ref(''), loginState = ref<any>(null)
const tab = ref(props.manageStudio ? 'studio' : 'accounts'), studioAccount = ref(props.defaultAccount || '')
const projects = ref<any[]>([]), projectsLoading = ref(false), projectsError = ref('')
const studioGuard = ref(false), studioBusy = ref(false), studioDirty = ref(false)
type AccountCheck = { status: 'checking' | 'verified' | 'expired' | 'failed'; name?: string; checked_at?: string; error?: string; code?: string }
const accountChecks = ref<Record<string, AccountCheck>>({}), checkingAccount = ref('')
let disposed = false, catalogRevision = 0, accountCheckRevision = 0, accountDirectoryRevision = 0
let timer: ReturnType<typeof setTimeout> | undefined
const guarded = computed(() => busy.value || studioGuard.value)
const fieldLabels: Record<string, string> = {
  title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态', declaration_id: '创作声明',
  part_order: '分P顺序', collection: '合集归属', append_part: '追加分P', copyright: '版权', source: '转载来源',
  tid: '分区', visibility: '可见性', watermark: '水印', publish_at: '定时发布', topic: '活动/话题',
  selected_comments: '精选评论', close_comments: '关闭评论', close_danmaku: '关闭弹幕', subtitles: '字幕',
  cover: '封面', dolby: '杜比', lossless_music: 'Hi-Res', commercial_promotion: '商业推广', chapters: '章节',
}
const writeLabels: Record<string, string> = {
  remote_edit: '已有稿件可预览后修改', submission_only: '仅新投稿可靠', read_only: '可对账但当前不写', unsupported: '未接入可靠契约',
}
const capabilityGroups = computed(() => {
  const fields = catalog.value.studio_capabilities?.field_capabilities || {}
  const order = ['remote_edit', 'submission_only', 'read_only', 'unsupported']
  return order.map(write => ({
    write,
    label: writeLabels[write],
    rows: Object.entries(fields).filter(([, item]: any) => item.write === write)
      .map(([key, item]: any) => ({ label: fieldLabels[key] || key, reason: item.reason, readScope: item.read_scope })),
  })).filter(group => group.rows.length)
})
watch(guarded, value => emit('guard', value), { immediate: true, flush: 'sync' })
watch(() => busy.value || studioBusy.value, value => emit('busy-change', value), { immediate: true, flush: 'sync' })
watch(studioDirty, value => emit('dirty-change', value), { immediate: true, flush: 'sync' })
const api = (path: string, method = 'GET', body?: any) => publishApi(path, body, method)
async function run(action: () => Promise<any>) {
  if (busy.value || disposed) return
  busy.value = true; error.value = ''
  try { return await action() }
  catch (e: any) { if (!disposed) { error.value = e.message; message.error(e.message) } }
  finally { if (!disposed) busy.value = false }
}
function invalidateAccountChecks(clearAll = false) {
  accountCheckRevision++
  if (clearAll) accountChecks.value = {}
  else if (checkingAccount.value) delete accountChecks.value[checkingAccount.value]
  checkingAccount.value = ''
}
function applyCatalog(data: any) {
  if (!Array.isArray(data?.accounts)) throw new Error('账号列表格式无效，请重新读取')
  catalogRevision++; catalogLoading.value = false
  accountDirectoryRevision++; invalidateAccountChecks()
  const previousAccounts = new Map(catalog.value.accounts.map((account: any) => [account.id, account]))
  const nextAccounts = new Map(data.accounts.map((account: any) => [account.id, account]))
  // 目录刷新只保留同一身份和凭据版本当时的验证结果，不代表重新登录。
  accountChecks.value = Object.fromEntries(Object.entries(accountChecks.value).filter(([id, result]) => {
    const previous: any = previousAccounts.get(id), next: any = nextAccounts.get(id)
    return result.status !== 'checking' && previous && next && previous.name === next.name && previous.revision === next.revision
  }))
  catalog.value = data; catalogReady.value = true; catalogError.value = ''
}
async function refreshCatalog() {
  if (catalogLoading.value || disposed) return
  const revision = ++catalogRevision
  invalidateAccountChecks(); catalogLoading.value = true
  try {
    const data = await api('/catalog')
    if (!disposed && revision === catalogRevision) applyCatalog(data)
  } catch (e: any) {
    if (!disposed && revision === catalogRevision) catalogError.value = e.message || '账号资料读取失败'
  } finally { if (!disposed && revision === catalogRevision) catalogLoading.value = false }
}
function accountCheckLabel(id: string) {
  const result = accountChecks.value[id]
  if (result?.status === 'checking') return '正在检查登录状态…'
  if (result?.status === 'verified') return `最近验证成功 · ${new Date(result.checked_at!).toLocaleString('zh-CN', { hour12: false })}（当时有效）`
  if (result?.status === 'expired') return '登录已失效，请重新登录'
  if (result?.status === 'failed') return '状态读取失败，登录是否有效尚未确认'
  return '尚未检查登录状态'
}
async function checkAccount(id: string) {
  if (disposed || !props.active || busy.value || tab.value !== 'accounts' || catalogLoading.value || !catalogReady.value || !catalog.value.worker_ready || loginId.value || checkingAccount.value === id
      || !catalog.value.accounts.some((account: any) => account.id === id)) return
  invalidateAccountChecks()
  const revision = accountCheckRevision, directoryRevision = accountDirectoryRevision
  checkingAccount.value = id; accountChecks.value[id] = { status: 'checking' }
  const applicable = () => !disposed && props.active && tab.value === 'accounts' && revision === accountCheckRevision && directoryRevision === accountDirectoryRevision
    && catalog.value.accounts.some((account: any) => account.id === id)
  try {
    const result = await api(`/accounts/${encodeURIComponent(id)}/check`, 'POST', {})
    if (!applicable()) return
    if (result.status !== 'verified' || typeof result.id !== 'string' || typeof result.name !== 'string' || !result.name.trim()
        || typeof result.checked_at !== 'string' || !Number.isFinite(Date.parse(result.checked_at))) {
      throw Object.assign(new Error('账号检查响应无效，请稍后重试'), { code: 'PUBLISH_ACCOUNT_RESPONSE' })
    }
    if (result.id !== id) throw Object.assign(new Error('校验返回的账号与所选账号不一致，请重新登录核对'), { code: 'PUBLISH_ACCOUNT_MISMATCH' })
    accountChecks.value[id] = { status: 'verified', name: result.name, checked_at: result.checked_at }
  } catch (e: any) {
    if (applicable()) accountChecks.value[id] = { status: e.code === 'PUBLISH_LOGIN_EXPIRED' ? 'expired' : 'failed', error: e.message || '账号检查失败，请稍后重试', code: e.code }
  } finally { if (applicable()) checkingAccount.value = '' }
}
watch(tab, () => invalidateAccountChecks(), { flush: 'sync' })
watch(() => props.active, () => invalidateAccountChecks(), { flush: 'sync' })
async function login() {
  if (!props.active || loginId.value || studioGuard.value || catalogLoading.value || !catalogReady.value || !catalog.value.worker_ready) return
  await run(async () => {
    invalidateAccountChecks(true)
    const state = await api('/login', 'POST', {})
    if (disposed) return
    loginId.value = state.id; loginState.value = { status: 'generating' }
  })
}
async function cancelLogin() {
  if (!loginId.value) return
  const id = loginId.value
  await run(async () => {
    const state = await api(`/login/${id}/cancel`, 'POST', {})
    if (disposed || loginId.value !== id) return
    loginId.value = ''; loginState.value = state
    if (state.status === 'connected') {
      const acceptedState = loginState.value
      const accounts = await api('/catalog')
      if (disposed || loginState.value !== acceptedState) return
      applyCatalog(accounts); message.success('账号已连接，无需重新扫码')
    }
  })
}
async function pollLogin() {
  const id = loginId.value
  if (!id) return
  let state
  try { state = await api(`/login/${id}`) }
  catch (e: any) {
    if (disposed || loginId.value !== id) return
    if (e.code === 'PUBLISH_LOGIN_NOT_FOUND') {
      loginId.value = ''; loginState.value = { status: 'expired', error: e.message, code: e.code }; return
    }
    throw e
  }
  // 已取消或重新获取二维码时，旧状态请求不能恢复旧登录。
  if (disposed || loginId.value !== id) return
  loginState.value = state
  if (state.status === 'connected') {
    const accounts = await api('/catalog')
    if (disposed || loginId.value !== id) return
    applyCatalog(accounts); loginId.value = ''; message.success('B站账号已连接')
  } else if (['failed', 'expired', 'cancelled'].includes(state.status)) loginId.value = ''
}
async function loadProjects() {
  if (projectsLoading.value || disposed) return
  projectsLoading.value = true; projectsError.value = ''
  try {
    const response = await fetch('/api/projects')
    if (!response.ok) throw new Error('关联项目读取失败，请重试')
    const data = await response.json()
    if (!Array.isArray(data)) throw new Error('关联项目列表格式无效，请重试')
    if (!disposed) projects.value = data
  } catch (e: any) { if (!disposed) projectsError.value = e.message }
  finally { if (!disposed) projectsLoading.value = false }
}
function switchWorkspace(value: string) {
  if (!['accounts', 'studio'].includes(value) || value === tab.value) return
  if (guarded.value) { message.warning('请先完成请求、提交修改或放弃本地编辑，再切换账号区域'); return }
  invalidateAccountChecks()
  tab.value = value
  if (value === 'studio') void loadProjects()
}
function manageAccount(id: string) {
  if (guarded.value || loginId.value || !catalog.value.accounts.some((account: any) => account.id === id)) return
  studioAccount.value = id; switchWorkspace('studio')
}
async function studioRefresh() { await refreshCatalog() }
async function poll() {
  try { if (props.active && !busy.value && loginId.value) await pollLogin() }
  catch (e: any) { if (!disposed) error.value = e.message }
  finally { if (!disposed) timer = setTimeout(poll, 2000) }
}
onMounted(async () => {
  await refreshCatalog()
  if (!disposed && tab.value === 'studio') await loadProjects()
  if (!disposed) timer = setTimeout(poll, 2000)
})
onBeforeUnmount(() => {
  disposed = true; catalogRevision++; invalidateAccountChecks(); clearTimeout(timer)
  emit('guard', false); emit('busy-change', false); emit('dirty-change', false)
})
</script>

<template>
  <section class="bili-account-manager" aria-label="B站账号管理">
    <n-alert v-if="catalogError || error" type="error" class="notice">{{ catalogError || error }}</n-alert>
    <n-alert v-if="catalogReady && catalog.worker_ready === false" type="warning" class="notice">发布组件尚未就绪。请在 workers/bilibili 目录运行 npm ci --ignore-scripts。</n-alert>
    <n-card v-show="tab === 'accounts'" title="已保存的B站账号" size="small">
      <p v-if="catalogLoading || !catalogReady && !catalogError" class="muted" role="status">正在读取已保存账号…</p>
      <p v-else-if="catalogReady && !catalogError && !catalog.accounts.length" class="muted">尚未保存B站账号，请扫码连接。</p>
      <div v-for="account in catalog.accounts" :key="account.id" class="account-row">
        <div class="account-identity"><strong>{{ account.name }}</strong><span>UID {{ account.id }} · 已保存</span>
          <p class="account-check-status" :class="{ verified: accountChecks[account.id]?.status === 'verified', warning: ['expired', 'failed'].includes(accountChecks[account.id]?.status || '') }" role="status" :data-account-check="accountChecks[account.id]?.status || 'unverified'">{{ accountCheckLabel(account.id) }}</p>
          <small v-if="accountChecks[account.id]?.error" class="muted">{{ accountChecks[account.id].error }}<span v-if="accountChecks[account.id].code"> · {{ accountChecks[account.id].code }}</span></small>
        </div>
        <div class="account-actions"><n-button size="small" secondary :loading="checkingAccount === account.id" :disabled="busy || catalogLoading || !catalogReady || !catalog.worker_ready || !!loginId || checkingAccount === account.id" @click="checkAccount(account.id)">检查登录状态</n-button>
          <n-button size="small" quaternary :disabled="busy || catalogLoading || !catalog.worker_ready || !!loginId" @click="manageAccount(account.id)">管理已有稿件</n-button></div>
      </div>
      <p v-if="catalog.accounts.length" class="muted">这里显示已保存的账号资料；登录是否仍有效，以平台校验结果为准。检查仅验证登录，不读取合集或发布视频。</p>
      <p class="muted">登录凭据由 Windows 当前用户加密保存，不进入项目文件或 AI 请求。</p>
      <div class="account-actions"><n-button type="primary" :disabled="busy || !!loginId || catalogLoading || !catalogReady || !catalog.worker_ready" @click="login">{{ catalog.accounts.length ? '添加账号 / 重新登录' : '扫码连接账号' }}</n-button><n-button secondary :loading="catalogLoading" :disabled="catalogLoading || busy" @click="refreshCatalog">重新读取账号</n-button></div>
      <div v-if="loginState" class="qr"><div v-if="loginState.url" class="qr-frame"><n-qr-code :value="loginState.url" :size="180" :padding="0" type="svg" color="#111111" background-color="#ffffff" /></div><p role="status" aria-live="polite">{{ loginState.error || LOGIN_STATUS[loginState.status] || '正在读取登录状态…' }}</p><small v-if="loginState.code" class="muted">{{ loginState.code }}</small><n-button v-if="loginId" :disabled="busy" @click="cancelLogin">取消登录</n-button></div>
    </n-card>
    <div v-if="tab === 'studio'" class="studio-workspace">
      <div class="studio-heading"><div><h2>管理已有稿件</h2><p class="muted">查看账号下的原BV，编辑文案、合集或分P；无需关联本地项目。</p></div><n-button secondary :disabled="guarded" @click="switchWorkspace('accounts')">返回账号列表</n-button></div>
      <n-alert type="info" :show-icon="false">已有BV替换视频请到B站创作中心处理。这里的修改需先预览并确认；不会新建另一条投稿。<a href="https://member.bilibili.com/platform/home" target="_blank" rel="noopener noreferrer">打开B站创作中心 ↗</a></n-alert>
      <details v-if="capabilityGroups.length" class="studio-capabilities"><summary>远端同步能力与限制</summary><p class="muted">这里是可验证的远端对账与有限更新，不是 B站官网全部设置的镜像。每次修改都会先预览，写入后再读取核对；未接入项不会静默覆盖。</p><div v-for="group in capabilityGroups" :key="group.write" class="capability-group"><strong>{{ group.label }}</strong><ul><li v-for="row in group.rows" :key="row.label"><span>{{ row.label }}</span>：{{ row.reason }}<small v-if="row.readScope === 'guard_only'">（当前值仅用于校验，未作为可编辑资料展示）</small><small v-else-if="row.readScope === 'none'">（当前未读取）</small></li></ul></div></details>
      <n-alert v-if="projectsError" type="warning" class="notice">{{ projectsError }} <n-button text :loading="projectsLoading" @click="loadProjects">重新读取关联项目</n-button></n-alert>
      <BiliStudio :accounts="catalog.accounts" :projects="projects" :default-account="studioAccount" :default-project="defaultProject" :default-bvid="defaultBvid" @guard="studioGuard = $event" @busy-change="studioBusy = $event" @dirty-change="studioDirty = $event" @refresh="studioRefresh" />
    </div>
  </section>
</template>

<style scoped>
.bili-account-manager{max-width:1120px}.notice{margin-bottom:16px}.account-row{display:flex;align-items:center;gap:20px;padding:18px 0;border-bottom:1px solid var(--df-border);flex-wrap:wrap}.account-identity{display:grid;gap:4px;flex:1 1 260px;min-width:0;overflow-wrap:anywhere}.account-identity>span,.muted{font-size:12px;color:var(--df-muted);line-height:1.8}.account-check-status{font-size:12px;margin:3px 0;color:var(--df-muted)}.account-check-status.verified{color:var(--df-accent)}.account-check-status.warning{color:var(--df-warning,#efbd70)}.account-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.qr{display:flex;flex-direction:column;align-items:flex-start;gap:12px;padding:24px 0}.qr p{margin:0;max-width:580px}.qr-frame{display:inline-flex;flex:none;padding:16px;border-radius:10px;background:#fff;line-height:0}.qr-frame :deep(.n-qr-code){box-sizing:content-box;flex:none}.qr-frame :deep(svg){display:block}.studio-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.studio-heading h2{font-size:18px;margin:0}.studio-heading p{margin:8px 0 0}.studio-workspace a{margin-left:8px}.studio-capabilities{margin:12px 0 18px;padding:10px 12px;border:1px solid var(--df-border);border-radius:8px}.studio-capabilities summary{cursor:pointer;font-size:12px}.studio-capabilities>p{margin:8px 0}.capability-group{margin-top:10px}.capability-group>strong{font-size:12px}.capability-group ul{margin:5px 0 0 18px;padding:0;color:var(--df-muted);font-size:11px;line-height:1.8}.capability-group small{color:var(--df-muted);margin-left:4px}@media(max-width:720px){.studio-heading{align-items:flex-start;flex-direction:column}.account-actions{width:100%}}
</style>
