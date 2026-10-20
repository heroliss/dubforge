<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { NAlert, NButton, NModal } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
const props = defineProps<{ record:any; disabled?:boolean }>()
const emit = defineEmits<{ changed:[record:any]; 'busy-change':[busy:boolean] }>()
type Action = 'archive' | 'delete' | 'restore'
const pending = ref<{action:Action; id:string; revision:number}|null>(null), busy = ref(false), error = ref('')
let disposed = false
const labels = { archive:'归档本地记录', delete:'回收草稿', restore:'恢复记录' }
const lifecycle = computed(()=>props.record?.lifecycle || (props.record?.deleted_at ? 'deleted' : props.record?.archived_at ? 'archived' : 'active'))
const actions = computed(()=>lifecycle.value==='active' ? ['archive','delete'] as Action[] : ['restore'] as Action[])
const notice = computed(()=>pending.value?.action==='delete'
  ? '将未上传的草稿放入本地回收区，可随时恢复。保留草稿资料、成片文件和项目，不删除B站稿件；这不是系统回收站。'
  : pending.value?.action==='archive'
    ? '从常用列表隐藏这条记录，可在「已归档」中恢复。平台稿件、BV号、上传清单与防重复投稿回执均保留。'
    : '恢复到常用发布记录列表；不会上传、重新投稿或修改B站稿件。')
function prepare(action:Action) {
  if (busy.value || props.disabled || !props.record?.lifecycle_actions?.[action]?.allowed) return
  error.value=''; pending.value={action,id:props.record.id,revision:props.record.revision}
}
function close() { if (!busy.value) pending.value=null }
async function confirm() {
  const intent=pending.value
  if (!intent || busy.value || props.disabled) return
  if (props.record.id!==intent.id || props.record.revision!==intent.revision || !props.record.lifecycle_actions?.[intent.action]?.allowed) {
    error.value='记录状态已变化，请重新读取后再操作。'; return
  }
  busy.value=true; emit('busy-change',true); error.value=''
  try {
    const row=await publishApi(`/publications/${encodeURIComponent(intent.id)}${intent.action==='delete' ? '' : '/'+intent.action}`,
      {revision:intent.revision,confirmed:true},intent.action==='delete' ? 'DELETE' : 'POST')
    if (disposed) return
    pending.value=null; emit('changed',row)
  } catch (e:any) {
    if (!disposed) error.value=e.message || '记录管理结果暂未确认，请刷新记录后核对。'
  } finally { if (!disposed) { busy.value=false;emit('busy-change',false) } }
}
onBeforeUnmount(()=>{disposed=true;emit('busy-change',false)})
</script>
<template>
  <div v-if="record?.lifecycle_actions" class="record-lifecycle" aria-label="本地记录管理">
    <n-button v-for="action in actions" :key="action" size="tiny" quaternary :disabled="disabled || busy || !record.lifecycle_actions[action]?.allowed" :title="record.lifecycle_actions[action]?.reason || labels[action]" @click="prepare(action)">{{ labels[action] }}</n-button>
    <n-modal :show="!!pending" preset="card" :title="pending ? labels[pending.action] : ''" style="width:min(520px,calc(100vw - 32px))" :mask-closable="!busy" :closable="!busy" :close-on-esc="!busy" @update:show="value => {if (!value) close()}">
      <p class="lifecycle-title">{{ record.snapshot?.settings?.title || record.draft?.overrides?.title || record.draft?.content?.title || '未命名稿件' }}</p>
      <p class="lifecycle-notice">{{ notice }}</p><n-alert v-if="error" type="error">{{ error }}</n-alert>
      <template #footer><div class="lifecycle-confirm"><n-button :disabled="busy" @click="close">取消</n-button><n-button type="primary" :loading="busy" :disabled="busy || disabled" @click="confirm">{{ pending ? labels[pending.action] : '确认' }}</n-button></div></template>
    </n-modal>
  </div>
</template>
<style scoped>
.record-lifecycle,.lifecycle-confirm{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.lifecycle-confirm{justify-content:flex-end}.lifecycle-title{font-size:14px;font-weight:600;overflow-wrap:anywhere}.lifecycle-notice{font-size:13px;line-height:1.8;color:var(--df-muted)}
</style>
