<script setup lang="ts">
// 所有恢复入口先核对实际起点；检查不启动任务，确认使用同一份冻结方案。
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NSpin } from 'naive-ui'
import { friendlyError } from '../utils/http'
import { parseChain } from '../utils/chainStatus'
import { WORKFLOW_OPEN, workflowModeLabel, workflowRunStage } from '../utils/workflow'
import { versionSummary } from '../utils/assemblyVersions'
import RecoveryPlan from './RecoveryPlan.vue'
import PublicationRightsHint from './PublicationRightsHint.vue'

const props = withDefaults(defineProps<{ projects: { uid: string; title: string }[]; kind?: 'auto' | 'chain'; label?: string; scopeLabel?: string; disabled?: boolean }>(),
  { kind: 'auto', label: '', scopeLabel: '', disabled: false })
const emit = defineEmits<{ resumed: [] }>()
const show = ref(false), busy = ref(false), checking = ref(false), error = ref('')
const items = ref<any[]>([]), results = ref<any[]>([]), checkedCount = ref(0), scopeName = ref('')
const scopeKey = computed(() => JSON.stringify([props.kind, [...new Set(props.projects.map(p => p.uid))].sort()]))
const inspectedScope = ref('')
const eligible = (item: any) => !!item.plan && !item.blockers.length && !item.submitted
const ready = computed(() => items.value.filter(item => eligible(item) && item.selected))
const canConfirm = computed(() => !busy.value && !props.disabled && inspectedScope.value === scopeKey.value && ready.value.length > 0)
const publicationCount = computed(() => ready.value.filter(item => item.kind === 'workflow' && item.plan.recipe.mode === 'publish').length)
const buttonLabel = computed(() => props.label || `检查恢复方案（${props.projects.length} 个项目）…`)
let disposed = false, generation = 0, readAbort: AbortController | undefined
const recoveryOf = (plan: any) => plan?.recovery || (plan?.rows ? plan : null)

async function api(uid: string, path: string, body?: any, signal?: AbortSignal) {
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}${path}`, {
    method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000),
  })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.message || '读取失败'), { code: data.code, status: response.status })
  return data
}
function invalidateScope() {
  generation++; readAbort?.abort(); inspectedScope.value = ''
  if (checking.value) { busy.value = false; checking.value = false }
  if (show.value) error.value = '项目范围已变化，请重新检查恢复方案。'
}
watch(scopeKey, invalidateScope)
watch(() => props.disabled, value => { if (value && checking.value) invalidateScope() })

async function inspect() {
  if (busy.value || props.disabled || !props.projects.length) return
  const ticket = ++generation, signature = scopeKey.value
  const targets = [...new Map(props.projects.map(project => [project.uid, { ...project }])).values()]
  readAbort?.abort(); readAbort = new AbortController()
  busy.value = true; checking.value = true; error.value = ''; items.value = []; results.value = []; checkedCount.value = 0
  inspectedScope.value = ''; scopeName.value = props.scopeLabel || `${targets.length === 1 ? '本项目' : '本次范围'} · ${targets.length} 个项目`
  try {
    for (const project of targets) {
      if (disposed || ticket !== generation) return
      const item: any = { ...project, kind: '', run: null, plan: null, blockers: [], selected: false, requestId: '', submitted: false, result: '', note: '', link: '' }
      try {
        const [detail, status] = await Promise.all([api(project.uid, '', undefined, readAbort.signal), api(project.uid, '/workflow/status', undefined, readAbort.signal)])
        if (disposed || ticket !== generation) return
        const run = status.run, chain = parseChain(detail.chain ?? detail.params?._chain)
        const running = (detail.steps || []).some((step: any) => ['running', 'queued'].includes(step.status))
        if (running || chain?.state === 'active' || WORKFLOW_OPEN.includes(run?.status)) {
          item.note = '项目正在执行或等待平台审核，请先查看当前进度。'; item.link = 'project'
        } else if (props.kind !== 'chain' && run && ['paused', 'failed', 'attention'].includes(run.status) && !run.publication_id) {
          item.kind = 'workflow'; item.run = run
          item.plan = await api(project.uid, '/workflow/plan', { mode: run.mode, resume_id: run.id }, readAbort.signal)
        } else if (chain?.state === 'held') {
          item.kind = 'chain'; item.plan = await api(project.uid, '/chain/plan', {}, readAbort.signal)
        } else if (run?.publication_id || status.existing_publication_id || detail.publication?.id) {
          item.note = '已有发布稿件，请核对并继续现有稿件。'; item.link = 'publish'
        } else {
          item.note = '没有保留的恢复计划。需要制作时，请在项目总览检查并运行完整流程。'; item.link = 'project'
        }
        if (item.plan) {
          item.blockers = [...(item.plan.blockers || [])]
          if (!item.plan.fingerprint || !recoveryOf(item.plan) || (item.kind === 'workflow' && !item.plan.recovery_fingerprint)) item.blockers.push('恢复方案不完整，请更新服务后重新检查。')
          item.selected = !item.blockers.length; item.requestId = crypto.randomUUID()
        }
      } catch (failure: any) {
        if (disposed || ticket !== generation || readAbort.signal.aborted) return
        item.blockers = [failure?.name === 'TimeoutError' ? '检查超时，请重新检查此项目。' : friendlyError(failure)]
      }
      if (disposed || ticket !== generation) return
      items.value.push(item); checkedCount.value++
    }
    if (!disposed && ticket === generation) inspectedScope.value = signature
  } finally { if (ticket === generation) { busy.value = false; checking.value = false } }
}
async function open() { if (!busy.value && !props.disabled && props.projects.length) { show.value = true; await inspect() } }
function close() {
  if (busy.value && !checking.value) return
  generation++; readAbort?.abort(); checking.value = false; busy.value = false; show.value = false; inspectedScope.value = ''
}
function select(item: any, selected: boolean) { if (!busy.value && eligible(item)) item.selected = selected }
async function resumeAll() {
  if (!canConfirm.value) return
  const ticket = generation, targets = ready.value.map(item => ({ item, plan: JSON.parse(JSON.stringify(item.plan)), requestId: item.requestId }))
  busy.value = true; error.value = ''; results.value = []; let succeeded = 0
  try {
    for (const { item, plan, requestId } of targets) {
      if (disposed || ticket !== generation) break
      item.result = '正在交给后台…'
      try {
        const body = item.kind === 'workflow'
          ? { recipe: plan.recipe, fingerprint: plan.fingerprint, recovery_fingerprint: plan.recovery_fingerprint, confirmed: true, request_id: requestId }
          : { confirmed: true, fingerprint: plan.fingerprint, request_id: requestId }
        const receipt = await api(item.uid, item.kind === 'workflow' ? `/workflow/${item.run.id}/resume` : '/chain/resume', body)
        item.submitted = true; item.selected = false
        item.result = item.kind === 'workflow' ? `恢复请求已处理 · 当前：${workflowRunStage(receipt).text}。再次重试前请重新检查。`
          : receipt.reused ? '该恢复请求已处理，请查看项目当前状态。再次重试前请重新检查。' : '已确认恢复制作，请查看项目当前状态。'
        succeeded++
        results.value.push({ uid: item.uid, title: item.title, error: '' })
      } catch (failure: any) {
        item.result = friendlyError(failure)
        if (failure.status === 409 || ['WORKFLOW_PLAN_CHANGED', 'WORKFLOW_CONFLICT', 'CHAIN_PLAN_CHANGED', 'PIPELINE_BUSY', 'REQUEST_CONFLICT'].includes(failure.code)) {
          item.plan = null; item.selected = false; item.blockers = [item.result + ' 请重新检查后确认。']
        }
        results.value.push({ uid: item.uid, title: item.title, error: item.result })
      }
    }
    if (succeeded && !disposed) emit('resumed')
  } finally { if (!disposed) busy.value = false }
}
onUnmounted(() => { disposed = true; generation++; readAbort?.abort() })
</script>
<template>
  <n-button size="small" secondary :disabled="disabled || !projects.length || busy" @click="open">{{ buttonLabel }}</n-button>
  <n-drawer :show="show" width="min(800px,100vw)" :mask-closable="!busy || checking" :close-on-esc="!busy || checking" @update:show="value => { if (!value) close() }">
    <n-drawer-content title="检查恢复方案" :closable="!busy || checking">
      <div class="batch-form" data-testid="resume-preview" :aria-busy="checking">
        <p class="scope-name">{{ scopeName }}</p><p class="muted">先核对当前输入与已有产物：需更新的步骤可能早于上次失败点，未变化的结果会复用。检查不会启动任务。</p>
        <n-alert v-if="error" type="warning">{{ error }}</n-alert>
        <n-spin v-if="checking" size="small" :description="`正在检查 ${checkedCount}/${projects.length} 个项目…`" />
        <p v-if="!checking && !items.length" class="muted">暂无恢复方案，请重新检查。</p>
        <article v-for="item in items" :key="item.uid" class="batch-row" :data-project="item.uid">
          <div class="row-head"><n-checkbox :checked="item.selected" :disabled="busy || !eligible(item)" :aria-label="`选择恢复「${item.title}」`" @update:checked="value => select(item, value)">{{ item.title }}</n-checkbox><small v-if="item.run">{{ workflowRunStage(item.run).text }}</small></div>
          <p v-if="item.note" class="muted">{{ item.note }}</p>
          <RouterLink v-if="item.link === 'publish'" :to="`/projects/${item.uid}/publish`">查看并继续现有稿件 →</RouterLink>
          <RouterLink v-else-if="item.link === 'project'" :to="`/projects/${item.uid}#automatic-workflow`">查看项目 / 检查并运行完整流程 →</RouterLink>
          <RecoveryPlan v-if="item.plan && recoveryOf(item.plan)" :recovery="recoveryOf(item.plan)" />
          <ul v-if="item.plan?.effects?.length" class="effects" aria-label="恢复制作说明"><li v-for="effect in item.plan.effects" :key="effect">{{ effect }}</li></ul>
          <div v-if="item.plan" class="execution-scope">
            <template v-if="item.kind === 'workflow'">
              <strong>本次执行到：{{ workflowModeLabel(item.plan.recipe.mode) }}</strong>
              <p v-for="(version,index) in item.plan.version_specs || []" :key="version.id">{{ item.plan.recipe.mode === 'produce' ? '版本 ' : 'P' }}{{ index + 1 }} · {{ item.plan.recipe.part_titles?.[version.id] || version.name }} · {{ versionSummary(version) }}</p>
              <p v-if="item.plan.account_name">账号：{{ item.plan.account_name }} · {{ item.plan.settings?.season_id ? '合集 ID '+item.plan.settings.season_id : '不加入合集' }}</p>
              <p v-if="item.plan.recipe.mode === 'publish'">{{ item.plan.settings?.visibility === 'self' ? '仅自己可见' : '公开可见' }} · {{ item.plan.settings?.publish_at ? new Date(item.plan.settings.publish_at*1000).toLocaleString('zh-CN')+' 定时发布' : '完成后上传并投稿，不再二次询问' }}</p>
              <p v-else>{{ item.plan.recipe.mode === 'draft' ? '制作后准备本地草稿，不上传、不投稿。' : '仅制作成片，不上传、不投稿。' }}</p>
            </template>
            <template v-else><strong>本次仅恢复制作步骤</strong><p>按所列步骤更新或补齐，不上传、不投稿。</p><p v-for="version in item.plan.version_specs || []" :key="version.id">{{ version.name }} · {{ versionSummary(version) }}</p></template>
          </div>
          <n-alert v-if="item.blockers.length" type="warning">{{ item.blockers.join('；') }}</n-alert><p v-if="item.result" role="status" class="result">{{ item.result }} <RouterLink :to="`/projects/${item.uid}`">查看项目 →</RouterLink></p>
        </article>
      </div>
      <template #footer>
        <PublicationRightsHint v-if="publicationCount" /><p v-if="publicationCount" class="publish-notice">所选 {{ publicationCount }} 个项目会在制作后自动上传并投稿。</p>
        <div class="batch-actions"><n-button :disabled="busy && !checking" @click="close">{{ checking ? '取消检查' : '关闭' }}</n-button><n-button :disabled="busy || disabled" @click="inspect">重新检查</n-button><n-button type="primary" :loading="busy && !checking" :disabled="!canConfirm" @click="resumeAll">确认恢复 {{ ready.length }} 个项目</n-button></div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.batch-form { display:grid; gap:16px; padding:20px 0; }.batch-form p { font-size:12px; line-height:1.7; }.scope-name { margin:0; font-weight:600; }.muted { color:var(--df-muted); }
.batch-row { display:grid; gap:12px; border-top:1px solid var(--df-border); padding:16px 0; }.batch-row p { margin:0; }.row-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }.row-head small { color:var(--df-muted); }.batch-row a { font-size:12px; }
.execution-scope { display:grid; gap:8px; font-size:12px; }.result { color:var(--df-text); }.publish-notice { font-size:12px; color:var(--df-warning); }.batch-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:12px; margin-top:12px; }
</style>
