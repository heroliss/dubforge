<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NSelect, NSwitch, NTag } from 'naive-ui'
import { useLlmTrace } from '../composables/useLlmTrace'
import { conversationListUrl, conversationStatus, LLM_OPERATION_OPTIONS } from '../utils/llmDialog'
import LlmConversationPanel from './LlmConversationPanel.vue'
const props = defineProps<{ show: boolean; projectUid?: string; operation?: string; ownerId?: string }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()
const projectOnly = ref(!!props.projectUid), operation = ref(props.operation || ''), items = ref<any[]>([]), selected = ref('')
const ownerOnly = ref(!!props.ownerId)
const listError = ref(''), listWarning = ref(''), listLoading = ref(false)
const moreRecords = ref(false), nextCursor = ref('')
let disposed = false, mounted = false, generation = 0, controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
const filter = computed(() => ({ projectUid: projectOnly.value ? props.projectUid : '', operation: operation.value, ownerId: ownerOnly.value ? props.ownerId : '' }))
const trace = useLlmTrace(selected, () => props.show)
const current = computed(() => items.value.find(row => row.id === selected.value))
function date(value: unknown) {
  const parsed = typeof value === 'number' ? new Date(value * 1000) : new Date(String(value || ''))
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString('zh-CN', { hour12: false }) : ''
}
function stop() { generation++; clearTimeout(timer); controller?.abort(); listLoading.value = false }
async function load(older = false) {
  if (disposed || !props.show || listLoading.value) return
  if (typeof document !== 'undefined' && document.hidden) { if (mounted) timer = setTimeout(() => load(), 2000); return }
  const ticket = generation
  listLoading.value = true; clearTimeout(timer); controller = new AbortController()
  try {
    const url = conversationListUrl(filter.value) + (older === true && nextCursor.value ? `&before=${encodeURIComponent(nextCursor.value)}` : '')
    const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]) })
    const value = await response.json()
    if (disposed || !props.show || ticket !== generation) return
    if (!response.ok) throw new Error(value.message || '对话记录列表读取失败')
    const incoming = Array.isArray(value.items) ? value.items : []
    items.value = older === true ? [...items.value, ...incoming.filter((row:any) => !items.value.some(old => old.id === row.id))]
      : [...incoming, ...items.value.filter(old => !incoming.some((row:any) => row.id === old.id))]
    if (older === true || !nextCursor.value) { moreRecords.value = !!value.more; nextCursor.value = String(value.next_cursor || '') }
    listError.value = ''; listWarning.value = String(value.warning || '')
    if (!items.value.some(row => row.id === selected.value)) selected.value = items.value[0]?.id || ''
  } catch (reason: any) {
    if (!disposed && ticket === generation && reason.name !== 'AbortError') listError.value = reason.name === 'TimeoutError' ? '对话列表读取超时，可刷新重试。' : reason.message || '对话列表读取失败'
  } finally {
    if (!disposed && ticket === generation) { listLoading.value = false; if (mounted && props.show) timer = setTimeout(() => load(), 2000) }
  }
}
watch(() => [props.show, props.projectUid, props.operation, props.ownerId], () => {
  stop(); items.value = []; selected.value = ''; listError.value = ''; listWarning.value = ''
  moreRecords.value = false; nextCursor.value = ''
  projectOnly.value = !!props.projectUid; operation.value = props.operation || ''
  ownerOnly.value = !!props.ownerId
  if (props.show) void load()
}, { flush: 'sync' })
watch(filter, () => { stop(); selected.value = ''; items.value = []; moreRecords.value = false; nextCursor.value = ''; if (props.show) void load() })
onMounted(() => { mounted = true; if (props.show) void load() })
onBeforeUnmount(() => { disposed = true; stop() })
</script>

<template>
  <n-drawer :show="show" width="min(1080px,100vw)" placement="right" @update:show="value => emit('update:show', value)">
    <n-drawer-content title="LLM对话记录" closable>
      <div class="trace-toolbar"><n-select v-model:value="operation" :options="LLM_OPERATION_OPTIONS" size="small" aria-label="LLM任务筛选" /><label v-if="projectUid"><n-switch v-model:value="projectOnly" size="small" />只看当前项目</label><label v-if="ownerId"><n-switch v-model:value="ownerOnly" size="small" />只看当前稿件或任务</label><n-button size="small" quaternary :loading="listLoading" @click="load()">刷新记录</n-button></div>
      <p class="trace-scope">{{ ownerOnly && ownerId ? '当前稿件或任务的模型请求' : projectOnly && projectUid ? '当前项目的模型请求' : '全部项目与全局模型请求' }}。这里只读记录，不会重新发送模型请求或修改任务。</p>
      <n-alert v-if="listError" type="warning" :show-icon="false">{{ listError }}</n-alert>
      <p v-if="listWarning" class="trace-scope">{{ listWarning }}</p>
      <div class="trace-workspace">
        <aside class="trace-list" aria-label="LLM对话列表"><p v-if="!items.length" class="trace-empty">{{ listLoading ? '正在读取…' : '暂无匹配的对话。启用记录之前的历史无法恢复；下一次调用后会在这里显示。' }}</p><button v-for="item in items" :key="item.id" type="button" :aria-pressed="selected === item.id" @click="selected=item.id"><strong>{{ item.label || LLM_OPERATION_OPTIONS.find(option => option.value === item.operation)?.label || '模型请求' }}</strong><span>{{ item.project_title || (item.project_uid ? '项目请求' : '全局请求') }}</span><small>{{ date(item.started_at) }} · {{ conversationStatus(item.status) }}</small><small v-if="item.model">{{ item.model }}</small></button><n-button v-if="moreRecords" size="small" quaternary :loading="listLoading" @click="load(true)">载入更早记录</n-button></aside>
        <section class="trace-detail"><template v-if="selected"><header class="trace-detail-heading"><strong>{{ current?.label || '对话过程' }}</strong><n-tag size="small" :bordered="false" :type="trace.active.value ? 'info' : 'default'">{{ conversationStatus(trace.status.value || current?.status) }}</n-tag><n-button size="small" quaternary :loading="trace.loading.value" @click="trace.load">刷新对话</n-button></header><n-alert v-if="trace.error.value" type="warning" :show-icon="false">{{ trace.error.value }}</n-alert><p v-if="trace.warning.value || current?.warning" class="trace-scope">{{ trace.warning.value || current?.warning }}</p><LlmConversationPanel :events="trace.events.value" :active="trace.active.value" /></template><p v-else class="trace-empty">从左侧选择一条对话，查看实际输入、回复、返回的思考以及纠错过程。</p></section>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.trace-toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.trace-toolbar :deep(.n-select){width:220px}.trace-toolbar label{display:flex;align-items:center;gap:8px;font-size:12px}.trace-toolbar>.n-button{margin-left:auto}.trace-scope,.trace-empty{font-size:11px;color:var(--df-muted);line-height:1.8}.trace-workspace{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px;margin-top:16px;min-width:0}.trace-list{max-height:calc(100dvh - 195px);overflow:auto;border-right:1px solid var(--df-border);padding-right:12px}.trace-list button{display:grid;gap:6px;text-align:left;background:transparent;border:1px solid var(--df-border);border-radius:8px;color:var(--df-text);padding:12px;width:100%;margin-bottom:9px;cursor:pointer;overflow-wrap:anywhere}.trace-list button[aria-pressed=true]{border-color:var(--df-accent);background:var(--df-accent-soft)}.trace-list strong{font-size:12px}.trace-list span,.trace-list small{font-size:10px;color:var(--df-muted)}.trace-detail{min-width:0}.trace-detail-heading{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}.trace-detail-heading>.n-button{margin-left:auto}.trace-detail :deep(.conversation-history){max-height:calc(100dvh - 275px)}@media(max-width:760px){.trace-workspace{grid-template-columns:1fr}.trace-list{max-height:180px;border-right:0;border-bottom:1px solid var(--df-border);padding-right:0}.trace-detail :deep(.conversation-history){max-height:calc(100dvh - 480px)}}
</style>
