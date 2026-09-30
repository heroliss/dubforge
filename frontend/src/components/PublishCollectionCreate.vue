<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { NAlert, NButton, NInput, NModal, NSelect, useDialog } from 'naive-ui'

const props = defineProps<{ accountId: string; accountName?: string; coverId?: string; contextKey: string; seasonId?: number | null; seasons?: any[]; disabled?: boolean }>()
const emit = defineEmits<{ created: [value: any]; refresh: [] }>()
const dialog = useDialog()
const show = ref(false), busy = ref(false), error = ref(''), title = ref(''), description = ref(''), cover = ref('')
const target = ref<any>({}), pending = ref<any[]>([]), recoverySeason = ref<number | null>(null)
let disposed = false, generation = 0, requestId = '', previousBody = ''
const recoveryOptions = computed(() => props.accountId === target.value.account_id
  ? (props.seasons || []).filter(s => s.title === pending.value[0]?.title).map(s => ({ label: `${s.title} · ${s.id}`, value: s.id })) : [])
const valid = computed(() => !!title.value.trim() && [...title.value.trim()].length <= 50 && [...description.value].length <= 500 && !!cover.value)
async function api(path: string, method = 'GET', body?: any) {
  const response = await fetch(`/api/publishing${path}`, {method,
    headers: body === undefined || body instanceof FormData ? undefined : {'Content-Type':'application/json'},
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body)})
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Object.assign(new Error(data.message || '合集操作失败，请重试'), {code:data.code})
  return data
}
async function checkPending() {
  const rows = await api(`/accounts/${target.value.account_id}/collection-requests`)
  if (!disposed) pending.value = rows
}
async function open() {
  if (props.disabled || !props.accountId || busy.value) return
  generation++; show.value = true; title.value = ''; description.value = ''; cover.value = props.coverId || ''
  requestId = ''; previousBody = ''; error.value = ''; pending.value = []; recoverySeason.value = null
  target.value = {account_id:props.accountId, account_name:props.accountName || props.accountId,
    context_key:props.contextKey, previous_season:props.seasonId ?? null}
  busy.value = true
  try { await checkPending(); if (pending.value.length) emit('refresh') }
  catch (e: any) { if (!disposed) error.value = e.message }
  finally { if (!disposed) busy.value = false }
}
async function uploadCover(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file || busy.value) return
  busy.value = true; error.value = ''
  try {
    const data = new FormData(); data.append('file',file)
    const result = await api('/covers','POST',data)
    if (!disposed) cover.value = result.cover_id
  } catch(e:any) { if (!disposed) error.value = e.message }
  finally { if (!disposed) busy.value = false; input.value = '' }
}
async function create() {
  if (busy.value || props.disabled || !valid.value || pending.value.length) return
  const ticket = generation, context = {...target.value}
  const payload = {title:title.value.trim(),description:description.value.trim(),cover_id:cover.value}
  const signature = JSON.stringify(payload)
  if (signature !== previousBody) { requestId = crypto.randomUUID().replaceAll('-',''); previousBody = signature }
  busy.value = true; error.value = ''
  try {
    // 开窗和选图都只写本地；只有这个显式确认按钮才触发封面上传与远端创建。
    const result = await api(`/accounts/${context.account_id}/collections`,'POST',{...payload,request_id:requestId,confirmed:true})
    if (!disposed && ticket === generation) {
      emit('created',{...context,season_id:result.season_id,title:result.title}); show.value = false
    }
  } catch(e:any) {
    if (!disposed && ticket === generation) {
      error.value = e.message
      if (e.code === 'PUBLISH_COLLECTION_UNKNOWN') {
        // 先禁用重发，再读取持久化记录；即使核对请求失败也不能盲重试。
        pending.value = [{id:requestId,title:payload.title,busy:false}]
        try { await checkPending() } catch { /* 保留禁止重发状态。 */ }
        emit('refresh')
      }
    }
  } finally { if (!disposed && ticket === generation) busy.value = false }
}
function confirmResolution(found: boolean) {
  const row = pending.value[0]
  if (!row || busy.value || row.busy || (found && !recoverySeason.value)) return
  const seasonId = found ? recoverySeason.value : null, ticket = generation, context = {...target.value}
  dialog.warning({title:'确认核对结果', content:found
    ? '将所选合集关联为本次创建结果，不会再次创建。请确认这是刚才创建的同名合集。'
    : '请先在B站确认该合集确实没有创建成功。确认后仅解除重试保护，不会自动创建。',
    positiveText:found ? '确认关联' : '已核对未创建',negativeText:'取消',
    onPositiveClick:async () => {
      if (busy.value || disposed || ticket !== generation) return
      busy.value = true; error.value = ''
      try {
        await api(`/accounts/${context.account_id}/collection-requests/${row.id}/resolve`,'POST',{confirmed:true,season_id:seasonId})
        if (!disposed && ticket === generation) {
          pending.value = []
          if (found) { emit('created',{...context,season_id:seasonId,title:row.title});show.value=false }
        }
      } catch(e:any) { if (!disposed && ticket === generation) error.value = e.message }
      finally { if (!disposed && ticket === generation) busy.value = false }
    }})
}
async function recheck() {
  if (busy.value) return
  busy.value = true
  try { await checkPending(); emit('refresh') }
  catch(e:any) { if (!disposed) error.value = e.message }
  finally { if (!disposed) busy.value = false }
}
onBeforeUnmount(() => { disposed = true; generation++ })
</script>

<template>
  <n-button size="small" secondary :disabled="disabled || !accountId" @click="open">新建合集</n-button>
  <n-modal v-model:show="show" preset="card" title="新建B站合集" style="width:min(520px, calc(100vw - 32px))" :mask-closable="!busy" :close-on-esc="!busy" :closable="!busy">
    <div class="collection-create">
      <p class="account">创建到：{{ target.account_name }} · UID {{ target.account_id }}</p>
      <n-alert v-if="error" type="warning">{{ error }}</n-alert>
      <template v-if="pending.length">
        <n-alert type="warning">「{{ pending[0].title }}」的创建结果尚未确认。请核对B站合集列表，不要重复创建。</n-alert>
        <a href="https://member.bilibili.com/platform/home" target="_blank" rel="noopener noreferrer">打开B站创作中心核对 ↗</a>
        <n-select v-model:value="recoverySeason" :options="recoveryOptions" placeholder="选择刚才创建的同名合集" :disabled="busy" />
        <n-button :disabled="busy || pending[0].busy || !recoverySeason" @click="confirmResolution(true)">已创建，关联所选合集</n-button>
        <n-button :disabled="busy || pending[0].busy" @click="confirmResolution(false)">已在B站核对未创建，允许重试</n-button>
        <n-button :loading="busy" :disabled="busy" @click="recheck">重新核对状态与列表</n-button>
      </template>
      <template v-else>
        <label>合集名称<n-input v-model:value="title" :disabled="busy" maxlength="50" show-count :input-props="{'aria-label':'合集名称'}" placeholder="例如：越南旅行" /></label>
        <label>简介（可选）<n-input v-model:value="description" type="textarea" :disabled="busy" maxlength="500" show-count :input-props="{'aria-label':'合集简介'}" /></label>
        <div class="collection-cover"><img v-if="cover" :src="`/api/publishing/covers/${cover}`" alt="新合集封面" /><div><span>{{ cover && cover === coverId ? '已使用当前稿件封面' : '合集封面' }}</span><input type="file" aria-label="选择合集封面" accept="image/png,image/jpeg,image/webp" :disabled="busy" @change="uploadCover" /></div></div>
        <p class="scope">确认后会上传封面并创建空合集，可能需要B站审核。不会上传视频或投稿。</p>
        <div class="actions"><n-button :disabled="busy" @click="show=false">取消</n-button><n-button type="primary" :disabled="busy || disabled || !valid" :loading="busy" @click="create">确认在B站创建</n-button></div>
      </template>
    </div>
  </n-modal>
</template>

<style scoped>
.collection-create { display:grid; gap:16px; }.account,.scope { margin:0; color:var(--df-muted); font-size:12px; line-height:1.7; }
label { display:grid; gap:8px; font-size:13px; }.collection-cover { display:flex; align-items:center; gap:14px; }.collection-cover img { width:128px; max-height:90px; object-fit:cover; border-radius:6px; }.collection-cover>div { min-width:0; display:grid; gap:10px; font-size:12px; }.collection-cover input { max-width:100%; font-size:12px; }.actions { display:flex; justify-content:flex-end; gap:10px; }
</style>
