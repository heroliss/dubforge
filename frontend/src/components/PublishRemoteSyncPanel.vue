<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NTag } from 'naive-ui'
import { publishApi } from '../utils/publishApi'

/**
 * 发布步骤内的已有稿件同步入口。
 *
 * 这里故意只接入工作室适配器已经声明为 remote_edit 的字段，且分为
 * 读取 → 预览 → 确认 → 回读四步。这样 AI 候选可以直接成为同步目标，
 * 但不会因为打开发布记录或生成候选而意外写入 B 站。
 */
const props = defineProps<{
  accountId: string | number
  bvid: string
  localValues: Record<string, any>
  selectedFields?: string[]
  disabled?: boolean
}>()
const emit = defineEmits<{ 'busy-change': [value: boolean]; synced: [values: Record<string, any>] }>()

const fieldLabels: Record<string, string> = {
  // 发布步骤内的快捷同步只处理文案字段；版权、声明等平台审核字段仍在高级管理中显式选择。
  title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态',
}
const editableFields = Object.keys(fieldLabels)
const remote = ref<any>(null)
const preview = ref<any>(null)
const showPreview = ref(false)
const loading = ref(false)
const operation = ref('')
const error = ref('')
const result = ref('')
const resultType = ref<'success' | 'warning'>('success')
let generation = 0
let disposed = false
let commitStarted = false

const busy = computed(() => loading.value)
const selected = computed(() => [...new Set((props.selectedFields || []).filter(key => editableFields.includes(key)))])
const local = computed(() => {
  const values = props.localValues || {}
  return Object.fromEntries(selected.value.map(key => [key, key === 'tags'
    ? (Array.isArray(values[key]) ? values[key] : [])
    : (values[key] ?? '')]))
})
const remoteValues = computed(() => remote.value?.settings || {})
const differences = computed(() => selected.value.filter(key =>
  JSON.stringify(local.value[key]) !== JSON.stringify(remoteValues.value[key]))
    .map(key => ({ key, label: fieldLabels[key], before: remoteValues.value[key], after: local.value[key] })))
const previewDifferences = computed(() => {
  const before = preview.value?.preview?.before?.settings || {}
  const after = preview.value?.preview?.after?.settings || {}
  return selected.value.filter(key => JSON.stringify(before[key]) !== JSON.stringify(after[key]))
    .map(key => ({ key, label: fieldLabels[key], before: before[key], after: after[key] }))
})
const previewWarnings = computed(() => Array.isArray(preview.value?.preview?.warnings) ? preview.value.preview.warnings : [])
const hasTarget = computed(() => /^BV[0-9A-Za-z]{10}$/.test(String(props.bvid || '').trim()))
const canRead = computed(() => hasTarget.value && !!String(props.accountId || '').trim() && !props.disabled && !busy.value)
const canPreview = computed(() => !!remote.value && differences.value.length > 0 && !props.disabled && !busy.value)
const syncStep = computed(() => !selected.value.length ? 1 : !remote.value ? 2 : preview.value ? 4 : differences.value.length ? 3 : 4)

function format(value: any) {
  if (Array.isArray(value)) return value.join('、') || '（空）'
  if (value === null || value === undefined || value === '') return '（空）'
  return String(value)
}
function resetForTarget() {
  generation++
  loading.value = false
  operation.value = ''
  remote.value = null
  preview.value = null
  showPreview.value = false
  result.value = ''
  resultType.value = 'success'
  error.value = ''
  commitStarted = false
}
watch(() => `${props.accountId}:${props.bvid}`, resetForTarget)
watch(() => `${JSON.stringify(props.localValues)}:${JSON.stringify(props.selectedFields || [])}`, () => {
  // 本地文案变化后，之前的预览已经过期，不可继续确认。
  generation++
  loading.value = false
  operation.value = ''
  if (preview.value) { preview.value = null; showPreview.value = false }
  commitStarted = false
})
watch(busy, value => emit('busy-change', value), { immediate: true })

async function readRemote() {
  if (!canRead.value) return
  const ticket = ++generation
  loading.value = true; operation.value = '正在读取B站稿件资料…'; error.value = ''; result.value = ''
  try {
    const value = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/read/detail`, { bvid: props.bvid })
    if (disposed || ticket !== generation) return
    remote.value = value
  } catch (e: any) {
    if (!disposed && ticket === generation) error.value = e.message || '读取B站稿件失败，请重试'
  } finally { if (!disposed && ticket === generation) {loading.value = false;operation.value = ''} }
}

async function prepare() {
  if (!canPreview.value) return
  const ticket = generation
  loading.value = true; operation.value = '正在核对远端资料并生成同步计划…'; error.value = ''; result.value = ''
  try {
    const value = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/changes`, {
      kind: 'edit', bvid: props.bvid,
      values: Object.fromEntries(differences.value.map(item => [item.key, item.after])),
    })
    if (disposed || ticket !== generation) return
    preview.value = value
    showPreview.value = true
  } catch (e: any) {
    if (!disposed && ticket === generation) error.value = e.message || '生成同步预览失败，请重试'
  } finally { if (!disposed && ticket === generation) {loading.value = false;operation.value = ''} }
}

async function commit() {
  const item = preview.value
  if (!item || busy.value || commitStarted) return
  const ticket = generation
  commitStarted = true
  loading.value = true; operation.value = '正在提交已确认的稿件资料…'; error.value = ''
  try {
    const committed = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/changes/${encodeURIComponent(item.id)}/commit`, { confirmed: true })
    if (disposed || ticket !== generation) return
    showPreview.value = false; preview.value = null
    const accepted = committed?.status === 'done' && (committed?.result?.accepted === true || committed?.accepted === true)
    if (!accepted) {
      resultType.value = 'warning'
      result.value = '修改响应未确认完成，请在修改记录或B站官网核对；未自动重试。'
      return
    }
    resultType.value = 'warning'
    result.value = 'B站已接收修改，正在回读确认。'
    operation.value = '正在回读B站稿件，核对同步结果…'
    emit('synced', Object.fromEntries(selected.value.map(key => [key, local.value[key]])))
    try {
      const refreshed = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/read/detail`, { bvid: props.bvid })
      if (disposed || ticket !== generation) return
      remote.value = refreshed
      const expected = item?.preview?.after?.settings || {}
      const matches = selected.value.every(key => JSON.stringify(refreshed?.settings?.[key]) === JSON.stringify(expected[key]))
      if (matches) { resultType.value = 'success'; result.value = '已同步并完成回读核验。' }
      else { resultType.value = 'warning'; result.value = 'B站已接收修改，但详情尚未完全刷新；请稍后重新读取核对。' }
    } catch {
      if (!disposed && ticket === generation) { resultType.value = 'warning'; result.value = 'B站已接收修改，但回读暂时失败；请稍后重新读取核对。' }
    }
  } catch (e: any) {
    if (!disposed && ticket === generation) {
      showPreview.value = false; preview.value = null
      resultType.value = 'warning'
      error.value = e.message || '同步结果尚未确认，请先在修改记录中核对结果'
    }
  } finally {
    if (!disposed && ticket === generation) { loading.value = false; operation.value = ''; commitStarted = false }
  }
}

onBeforeUnmount(() => { disposed = true; generation++; commitStarted = true })
</script>

<template>
  <section v-if="hasTarget" class="remote-sync-panel" aria-label="同步B站稿件资料">
    <div class="remote-sync-heading">
      <div><strong>同步B站稿件资料</strong><small>在当前发布步骤内读取、预览并确认已有 BV 的文案修改</small></div>
      <n-button size="small" secondary :loading="loading" :disabled="!canRead" @click="readRemote">{{ remote ? '重新读取' : '读取B站资料' }}</n-button>
    </div>
    <p class="remote-sync-guide">页面中的差异用于调整同步目标；点击“生成最终同步计划”后，服务端会重新核对，弹窗只做最后确认。不会创建新的投稿。</p>
    <p v-if="loading && operation" class="remote-sync-guide" role="status" aria-live="polite">{{ operation }}</p>
    <ol class="remote-sync-steps" aria-label="B站文案同步流程">
      <li :class="{ current: syncStep === 1, done: syncStep > 1 }"><b>1</b><span>准备文案</span></li>
      <li :class="{ current: syncStep === 2, done: syncStep > 2 }"><b>2</b><span>读取B站</span></li>
      <li :class="{ current: syncStep === 3, done: syncStep > 3 }"><b>3</b><span>预览差异</span></li>
      <li :class="{ current: syncStep === 4 }"><b>4</b><span>确认同步</span></li>
    </ol>
    <n-alert v-if="!selected.length" type="info" :show-icon="false" class="remote-sync-alert">请先载入项目已保存文案，或在AI文案助手中应用候选字段。
    </n-alert>
    <n-alert v-if="error" type="warning" :show-icon="false" class="remote-sync-alert">{{ error }}<n-button v-if="remote" size="tiny" quaternary @click="readRemote">重试读取</n-button>
    </n-alert>
    <n-alert v-if="result" :type="resultType" :show-icon="false" class="remote-sync-alert">{{ result }}
    </n-alert>
    <template v-if="remote">
      <div class="remote-sync-meta"><n-tag size="small" :bordered="false">{{ remote.state_desc || '已读取' }}</n-tag><span>{{ props.bvid }}</span><span>{{ !selected.length ? '尚未选择同步字段' : differences.length ? `有 ${differences.length} 项待同步` : '所选字段与B站一致' }}</span></div>
      <div v-if="differences.length" class="remote-sync-diff">
        <div v-for="item in differences" :key="item.key" class="remote-sync-row"><strong>{{ item.label }}</strong><div><small>B站当前</small><p>{{ format(item.before) }}</p></div><div><small>准备同步</small><p>{{ format(item.after) }}</p></div></div>
      </div>
      <p v-else class="remote-sync-empty">没有需要同步的文案字段。重新生成候选或修改发布资料后可再次核对。</p>
      <n-button v-if="differences.length" type="primary" secondary :loading="loading" :disabled="!canPreview" @click="prepare">生成最终同步计划</n-button>
    </template>
    <n-modal v-model:show="showPreview" preset="card" title="确认同步B站稿件" style="width:min(760px,calc(100vw - 32px))" :mask-closable="!loading" :closable="!loading" :close-on-esc="!loading">
      <template v-if="preview"><p class="remote-sync-modal-note">服务端已生成最终同步计划。确认后只修改已有 {{ props.bvid }}，不会创建另一条投稿；平台可能重新审核简介。</p><div class="remote-sync-plan-meta"><span>计划 {{ preview.id }}</span><span>{{ previewDifferences.length }} 项字段</span></div><n-alert v-for="warning in previewWarnings" :key="warning" type="warning" :show-icon="false" class="remote-sync-plan-warning">{{ warning }}</n-alert><p v-if="!previewDifferences.length" class="remote-sync-empty">服务端预览未发现可同步字段，请返回并重新读取B站资料。</p><p v-else class="remote-sync-final-summary">将同步：{{ previewDifferences.map(item => item.label).join('、') }}</p><details v-if="previewDifferences.length" class="remote-sync-final-details"><summary>查看服务端最终差异</summary><div v-for="item in previewDifferences" :key="item.key" class="remote-sync-row modal-row"><strong>{{ item.label }}</strong><div><small>修改前</small><p>{{ format(item.before) }}</p></div><div><small>修改后</small><p>{{ format(item.after) }}</p></div></div></details><div class="remote-sync-actions"><n-button :disabled="loading" @click="showPreview=false">返回编辑</n-button><n-button type="primary" :loading="loading" :disabled="loading || !previewDifferences.length" @click="commit">确认同步到B站</n-button></div></template>
    </n-modal>
  </section>
</template>

<style scoped>
.remote-sync-panel{border-top:1px solid var(--df-border);margin-top:20px;padding-top:18px}.remote-sync-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.remote-sync-heading strong{display:block;font-size:13px}.remote-sync-heading small{display:block;margin-top:5px;color:var(--df-muted);font-size:11px}.remote-sync-guide,.remote-sync-empty,.remote-sync-modal-note{margin:9px 0;color:var(--df-muted);font-size:11px;line-height:1.7}.remote-sync-steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:14px 0;padding:0;list-style:none}.remote-sync-steps li{display:flex;align-items:center;gap:6px;min-width:0;padding:7px 8px;border:1px solid var(--df-border);border-radius:8px;color:var(--df-muted);font-size:11px}.remote-sync-steps b{display:grid;place-items:center;flex:0 0 19px;width:19px;height:19px;border:1px solid var(--df-border);border-radius:50%;font-size:10px}.remote-sync-steps li.current{color:var(--df-accent);border-color:var(--df-accent);background:var(--df-accent-soft)}.remote-sync-steps li.current b,.remote-sync-steps li.done b{border-color:var(--df-accent);background:var(--df-accent);color:var(--df-bg)}.remote-sync-steps li.done{color:var(--df-text)}.remote-sync-alert{margin:12px 0}.remote-sync-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:13px 0;color:var(--df-muted);font-size:11px}.remote-sync-diff{border:1px solid var(--df-border);border-radius:8px;margin:10px 0 14px;overflow:hidden}.remote-sync-row{display:grid;grid-template-columns:88px minmax(0,1fr) minmax(0,1fr);gap:12px;padding:12px;border-bottom:1px solid var(--df-border);font-size:12px}.remote-sync-row:last-child{border-bottom:0}.remote-sync-row>div{min-width:0}.remote-sync-row small{color:var(--df-muted);font-size:10px}.remote-sync-row p{margin:4px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6;max-height:180px;overflow:auto}.remote-sync-plan-meta{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;color:var(--df-muted);font-size:11px}.remote-sync-plan-meta span{padding:5px 8px;border:1px solid var(--df-border);border-radius:6px}.remote-sync-plan-warning{margin:10px 0}.remote-sync-final-summary{margin:10px 0;font-size:12px}.remote-sync-final-details{margin:10px 0;border-top:1px solid var(--df-border);padding-top:10px}.remote-sync-final-details summary{cursor:pointer;color:var(--df-muted);font-size:11px}.modal-row{margin:10px -4px 0}.remote-sync-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}@media(max-width:680px){.remote-sync-steps{grid-template-columns:repeat(2,minmax(0,1fr))}.remote-sync-row{grid-template-columns:1fr}.remote-sync-row strong{color:var(--df-accent)}}
</style>
