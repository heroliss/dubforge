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
import DubbingRunOptions from './DubbingRunOptions.vue'
import TtsServiceControl from './TtsServiceControl.vue'
import { publicationChapterScope } from '../utils/publicationChapters'
import PublicationTargetSummary from './PublicationTargetSummary.vue'
import WorkflowUpdateSummary from './WorkflowUpdateSummary.vue'
import {updateStages,updateNeedsReconcile} from '../utils/publicationUpdate'
import { publicationVisibility } from '../utils/publishing'
import { dubbingRunPolicy, dubbingPolicyError, type DubbingRunPolicy } from '../utils/dubbingRunPolicy'
import type { ProjectRunFeedback } from '../utils/projectRunFeedback'

const props = withDefaults(defineProps<{ projects: { uid: string; title: string }[]; kind?: 'auto' | 'chain'; label?: string; scopeLabel?: string; disabled?: boolean; hiddenTrigger?: boolean }>(),
  { kind: 'auto', label: '', scopeLabel: '', disabled: false, hiddenTrigger: false })
const emit = defineEmits<{ resumed: []; runFeedback: [feedback: ProjectRunFeedback] }>()
const show = ref(false), busy = ref(false), checking = ref(false), error = ref('')
const items = ref<any[]>([]), results = ref<any[]>([]), checkedCount = ref(0), scopeName = ref('')
const scopeKey = computed(() => JSON.stringify([props.kind, [...new Set(props.projects.map(p => p.uid))].sort()]))
const inspectedScope = ref('')
const dubbingOverrides = ref<Record<string,DubbingRunPolicy>>({})
const eligible = (item: any) => !!item.plan && !item.blockers.length && !item.submitted
const ready = computed(() => items.value.filter(item => eligible(item) && item.selected))
const canConfirm = computed(() => !busy.value && !props.disabled && inspectedScope.value === scopeKey.value && ready.value.length > 0)
const endpoint=(item:any)=>item.plan?.recipe?.delivery_mode || (item.plan?.recipe?.mode==='update'?'publish':item.plan?.recipe?.mode)
const publicationCount = computed(() => ready.value.filter(item => item.kind === 'workflow' && endpoint(item) === 'publish').length)
const uploadCount = computed(() => ready.value.filter(item => item.kind === 'workflow' && endpoint(item) === 'upload').length)
const updateCount = computed(() => ready.value.filter(item => item.kind === 'workflow' && item.plan.recipe.mode === 'update').length)
const buttonLabel = computed(() => props.label || `检查${props.kind === 'chain' ? '制作' : ''}恢复方案（${props.projects.length} 个项目）…`)
const singleReconcile=computed(()=>ready.value.length===1 && ready.value[0].plan.recipe?.mode==='update' && updateNeedsReconcile(ready.value[0].plan.publication_update))
const deliveryOnly=(item:any)=>item.plan?.recipe?.mode==='update' && !!item.plan?.publication_update
const onlyDelivery=computed(()=>items.value.length>0 && items.value.every(item=>deliveryOnly(item)))
const pendingDelivery=(item:any)=>updateStages(item.plan?.publication_update).filter(stage=>!['completed','success','skipped'].includes(stage.status))
const drawerTitle = computed(() => props.kind === 'chain' ? '检查制作恢复方案' : '检查任务恢复方案')
let disposed = false, generation = 0, readAbort: AbortController | undefined
const uncertainRequests = new Map<string, { signature: string; id: string }>()
const requestSignature = (item: any) => JSON.stringify([item.kind, item.run?.id, item.plan?.fingerprint, item.plan?.recovery_fingerprint])
const recoveryOf = (plan: any) => plan?.recovery || (plan?.rows ? plan : null)
const TTS_SERVICE_BLOCKER = '配音服务未就绪，请先在配音页启动服务'
const hasTtsServiceBlocker = (item: any) => !item.archived && !item.submitted && item.blockers.includes(TTS_SERVICE_BLOCKER)

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

async function readProject(project: { uid: string; title: string }, signal: AbortSignal) {
  const item: any = { ...project, archived: false, kind: '', run: null, plan: null, policy: dubbingOverrides.value[project.uid], blockers: [], selected: false, requestId: '', submitted: false, result: '', note: '', link: '', serviceReadyChecked: false, serviceRefreshPending: false }
  try {
    const [detail, status] = await Promise.all([api(project.uid, '', undefined, signal), api(project.uid, '/workflow/status', undefined, signal)])
    if (signal.aborted) return item
    const run = status.run, chain = parseChain(detail.chain ?? detail.params?._chain)
    item.archived = !!detail.archived
    const running = (detail.steps || []).some((step: any) => ['running', 'queued'].includes(step.status))
    if (detail.archived) {
      item.blockers = ['项目已归档，请先恢复项目后再继续处理。']; item.link = 'project'
    } else if (running || ['running','pausing','stopping'].includes(detail.release_batch?.status) || chain?.state === 'active' || WORKFLOW_OPEN.includes(run?.status)) {
      item.note = '项目正在执行或等待平台审核，请先查看当前进度。'; item.link = 'project'
    } else if (props.kind !== 'chain' && run && ['paused', 'failed', 'attention'].includes(run.status) && (!run.publication_id || run.mode==='update')) {
      item.kind = 'workflow'; item.run = run
      item.plan = await api(project.uid, '/workflow/plan', {resume_id:run.id}, signal)
    } else if (chain?.state === 'held') {
      item.kind = 'chain'; item.plan = await api(project.uid, '/chain/plan', item.policy ? {dubbing:item.policy}: {}, signal)
    } else if (run?.publication_id || status.existing_publication_id || detail.publication?.id) {
      item.note = '已有发布稿件，请核对并继续现有稿件。'; item.link = 'publish'
    } else {
      item.note = '没有保留的恢复计划。需要制作时，请在项目总览检查并运行完整流程。'; item.link = 'project'
    }
    if (item.plan) {
      item.policy = dubbingRunPolicy(item.plan.dubbing?.policy || item.plan.recipe?.dubbing || item.policy)
      item.blockers = [...(item.plan.blockers || [])]
      if (!item.plan.fingerprint || !recoveryOf(item.plan) || (item.kind === 'workflow' && !item.plan.recovery_fingerprint)) item.blockers.push('恢复方案不完整，请更新服务后重新检查。')
      item.selected = !item.blockers.length
      const uncertain = uncertainRequests.get(item.uid)
      item.requestId = uncertain?.signature === requestSignature(item) ? uncertain.id : crypto.randomUUID()
    }
  } catch (failure: any) {
    if (!signal.aborted) item.blockers = [failure?.name === 'TimeoutError' ? '检查超时，请重新检查此项目。' : friendlyError(failure)]
  }
  return item
}

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
      const item = await readProject(project, readAbort.signal)
      if (disposed || ticket !== generation) return
      items.value.push(item); checkedCount.value++
    }
    if (!disposed && ticket === generation) inspectedScope.value = signature
  } finally {
    if (ticket === generation) { busy.value = false; checking.value = false; await refreshServicePlans() }
  }
}

async function onTtsServiceStatus(item: any, status: { online: boolean }) {
  if (disposed || !show.value || props.disabled || !items.value.includes(item) || !hasTtsServiceBlocker(item)) return
  // 每次连接轮询都会上报在线；每次离线到在线只重检一次，避免服务与预检状态不一致时循环。
  if (!status.online) { item.serviceReadyChecked = false; item.serviceRefreshPending = false; return }
  if (item.serviceReadyChecked || (!checking.value && inspectedScope.value !== scopeKey.value)) return
  item.serviceReadyChecked = true; item.serviceRefreshPending = true
  await refreshServicePlans()
}

async function refreshServicePlans() {
  if (disposed || !show.value || busy.value || props.disabled || inspectedScope.value !== scopeKey.value) return
  const targets = items.value.filter(item => item.serviceRefreshPending && hasTtsServiceBlocker(item))
  if (!targets.length) return
  const ticket = ++generation, signature = scopeKey.value
  readAbort?.abort(); readAbort = new AbortController()
  busy.value = true; checking.value = true
  try {
    for (const item of targets) {
      if (disposed || ticket !== generation) return
      item.serviceRefreshPending = false
      // 只更新受影响的项目；其他项目的取消勾选、已提交回执及未知结果 UUID 都保留。
      if (item.policy) dubbingOverrides.value[item.uid] = item.policy
      const fresh = await readProject({ uid: item.uid, title: item.title }, readAbort.signal)
      if (disposed || ticket !== generation || signature !== scopeKey.value) return
      Object.assign(item, fresh, { serviceReadyChecked: true, serviceRefreshPending: false })
    }
  } finally {
    if (ticket === generation) { busy.value = false; checking.value = false; await refreshServicePlans() }
  }
}
async function open() { if (!busy.value && !props.disabled && props.projects.length) { dubbingOverrides.value={};show.value = true; await inspect() } }
function onDubbingChange(item:any, value:DubbingRunPolicy) {
  if(busy.value || props.disabled || item.kind==='workflow')return
  dubbingOverrides.value={...dubbingOverrides.value,[item.uid]:value};item.policy=value;item.plan=null;item.selected=false;inspectedScope.value=''
  const failure=dubbingPolicyError(value)
  if(failure){item.blockers=[failure];return}
  void inspect()
}
function close() {
  if (busy.value && !checking.value) return
  generation++; readAbort?.abort(); checking.value = false; busy.value = false; show.value = false; inspectedScope.value = ''
}
function select(item: any, selected: boolean) { if (!busy.value && eligible(item)) item.selected = selected }
async function resumeAll() {
  if (!canConfirm.value) return
  const ticket = generation, targets = ready.value.map(item => ({ item, plan: JSON.parse(JSON.stringify(item.plan)), requestId: item.requestId }))
  busy.value = true; error.value = ''; results.value = []
  try {
    for (const { item, plan, requestId } of targets) {
      if (disposed || ticket !== generation) break
      const feedback = (stage: ProjectRunFeedback['stage'], message: string) => {
        item.feedback = stage
        emit('runFeedback', { id: requestId, projectUids: [item.uid], groupIds: [], workflowId: item.run?.id, stage, message })
      }
      item.result = '正在核对当前执行状态…'; feedback('checking', item.result)
      try {
        const detail = await api(item.uid, '')
        if (disposed || ticket !== generation) break
        if (detail.archived) {
          item.archived = true
          item.plan = null; item.selected = false; item.blockers = ['项目已归档，请先恢复项目后再继续处理。']
          item.result = '未恢复：项目已归档，请先恢复项目。'
          feedback('blocked', item.result)
          results.value.push({ uid: item.uid, title: item.title, error: item.result }); continue
        }
        const body = item.kind === 'workflow'
          ? { recipe: plan.recipe, fingerprint: plan.fingerprint, recovery_fingerprint: plan.recovery_fingerprint, confirmed: true, request_id: requestId }
          : { confirmed: true, fingerprint: plan.fingerprint, request_id: requestId,
            ...(plan.dubbing?.policy ? {dubbing:dubbingRunPolicy(plan.dubbing.policy)}: {}) }
        item.result = '正在提交恢复请求…'; feedback('submitting', item.result)
        const receipt = await api(item.uid, item.kind === 'workflow' ? `/workflow/${item.run.id}/resume` : '/chain/resume', body)
        item.submitted = true; item.selected = false
        uncertainRequests.delete(item.uid)
        if (item.kind === 'workflow') item.run = { ...item.run, ...receipt }
        item.result = item.kind === 'workflow' ? `恢复请求已处理 · 当前：${workflowRunStage(receipt).text}。再次重试前请重新检查。`
          : receipt.reused ? '该恢复请求已处理，请查看项目当前状态。再次重试前请重新检查。' : '已确认恢复制作，请查看项目当前状态。'
        if (['failed', 'attention', 'paused', 'stopped', 'cancelled'].includes(receipt.status)) {
          const reason = typeof receipt.error === 'string' ? receipt.error : receipt.error?.message
          item.result = `恢复未继续：${reason || receipt.phase || '任务已中断，请重新检查当前状态。'}`
          feedback('blocked', item.result)
        } else if (['completed', 'ready', 'done'].includes(receipt.status)) feedback('clear', '')
        else feedback('accepted', '恢复请求已接收，正在等待执行…')
        // 每项回执立即通知调用者，不等其余项目的慢检查结束才更新进度。
        emit('resumed')
        results.value.push({ uid: item.uid, title: item.title, error: '' })
      } catch (failure: any) {
        item.result = friendlyError(failure)
        const definitive = Number(failure.status) >= 400 && Number(failure.status) < 500
        if (definitive) uncertainRequests.delete(item.uid)
        else uncertainRequests.set(item.uid, { signature: requestSignature(item), id: requestId })
        feedback(definitive ? 'blocked' : 'unknown', definitive ? item.result : '恢复回执未确认，请核对原任务状态；重试会沿用本次请求。')
        if (failure.status === 409 || ['WORKFLOW_PLAN_CHANGED', 'WORKFLOW_CONFLICT', 'CHAIN_PLAN_CHANGED', 'PIPELINE_BUSY', 'REQUEST_CONFLICT'].includes(failure.code)) {
          item.plan = null; item.selected = false; item.blockers = [item.result + ' 请重新检查后确认。']
        }
        results.value.push({ uid: item.uid, title: item.title, error: item.result })
      }
    }
  } finally { if (!disposed) { busy.value = false; await refreshServicePlans() } }
}
onUnmounted(() => { disposed = true; generation++; readAbort?.abort() })
defineExpose({ open, close })
</script>
<template>
  <n-button v-if="!hiddenTrigger" size="small" secondary :disabled="disabled || !projects.length || busy" @click="open">{{ buttonLabel }}</n-button>
  <n-drawer :show="show" width="min(800px,100vw)" :mask-closable="!busy || checking" :close-on-esc="!busy || checking" @update:show="value => { if (!value) close() }">
    <n-drawer-content :title="drawerTitle" :closable="!busy || checking">
      <div class="batch-form" data-testid="resume-preview" :aria-busy="checking">
        <p class="scope-name">{{ scopeName }}</p><p v-if="onlyDelivery" class="muted">制作阶段已完成，本次仅核对并继续已确认原稿更新的剩余交付；不重新制作。检查不会启动任务。</p><p v-else class="muted">先核对当前输入与已有产物：需更新的步骤可能早于上次失败点，未变化的结果会复用。检查不会启动任务。</p>
        <n-alert v-if="error" type="warning">{{ error }}</n-alert>
        <n-spin v-if="checking" size="small" :description="`正在检查 ${checkedCount}/${projects.length} 个项目…`" />
        <p v-if="!checking && !items.length" class="muted">暂无恢复方案，请重新检查。</p>
        <article v-for="item in items" :key="item.uid" class="batch-row" :data-project="item.uid">
          <div class="row-head"><n-checkbox :checked="item.selected" :disabled="busy || !eligible(item)" :aria-label="`选择恢复「${item.title}」`" @update:checked="value => select(item, value)">{{ item.title }}</n-checkbox><small v-if="item.run">{{ item.feedback==='checking' ? '核对中' : item.feedback==='submitting' ? '正在提交' : item.feedback==='unknown' ? '回执待核对' : workflowRunStage(item.run).text }}</small></div>
          <p v-if="item.note" class="muted">{{ item.note }}</p>
          <RouterLink v-if="item.link === 'publish'" :to="`/projects/${item.uid}/publish`">查看并继续现有稿件 →</RouterLink>
          <RouterLink v-else-if="item.link === 'project'" :to="`/projects/${item.uid}#automatic-workflow`">{{ item.archived ? '查看项目 / 恢复项目 →' : '查看项目 / 检查并运行完整流程 →' }}</RouterLink>
          <DubbingRunOptions v-if="item.policy && !item.submitted && !deliveryOnly(item)" :readonly="item.kind==='workflow'" :uid="item.uid" :model-value="item.policy" :summary="item.plan?.dubbing" :disabled="busy || disabled" @update:model-value="value => onDubbingChange(item,value)" />
          <p v-if="item.plan?.dubbing && !deliveryOnly(item)" class="muted">恢复继续使用保留的配音策略与目标槽，已完成句子不重复生成。制作链调整方案会重新检查；项目任务沿用原来确认的策略。</p>
          <RecoveryPlan v-if="item.plan && recoveryOf(item.plan) && !deliveryOnly(item)" :recovery="recoveryOf(item.plan)" :pending-steps="item.plan.pending_production_steps" />
          <ul v-if="item.plan?.effects?.length && !deliveryOnly(item)" class="effects" aria-label="恢复制作说明"><li v-for="effect in item.plan.effects" :key="effect">{{ effect }}</li></ul>
          <div v-if="item.plan" class="execution-scope">
            <template v-if="item.kind === 'workflow'">
              <strong>本次执行到：{{ workflowModeLabel(endpoint(item)) }}</strong>
              <p v-for="version in item.plan.version_specs || []" :key="version.id">制作与复用：{{ version.name }} · {{ versionSummary(version) }}</p>
              <p v-if="!['produce','update'].includes(item.plan.recipe.mode)">发布分P：{{ (item.plan.recipe.version_ids || []).map((id:string,index:number)=>`P${index+1} ${item.plan.recipe.part_titles?.[id] || item.plan.version_specs?.find((version:any)=>version.id===id)?.name || id}`).join('、') }}</p>
              <WorkflowUpdateSummary v-if="item.plan.recipe.mode==='update' && item.plan.update_plan" :plan="item.plan.update_plan" :replace-videos="item.plan.recipe.update?.replace_videos!==false" :sync-fields="item.plan.recipe.update?.sync_fields" :delivery-mode="item.plan.recipe.delivery_mode" readonly :delivery-only="deliveryOnly(item)" />
              <section v-if="deliveryOnly(item)" class="delivery-scope"><strong>{{ updateNeedsReconcile(item.plan.publication_update) ? '先只读核对平台结果，再继续未完成步骤' : '仅继续未完成的原稿交付，不重新合成' }}</strong><ul><li v-for="stage in pendingDelivery(item)" :key="stage.key">{{ stage.label }} · {{ stage.text }}</li></ul></section>
              <PublicationTargetSummary v-if="item.plan.recipe.mode!=='produce' && item.plan.recipe.mode!=='update' && item.plan.settings" :plan="item.plan" />
              <p v-if="item.plan.recipe.mode === 'publish'">{{ publicationVisibility(item.plan.settings?.visibility) }} · {{ item.plan.settings?.publish_at ? new Date(item.plan.settings.publish_at*1000).toLocaleString('zh-CN')+' 定时发布' : '完成后上传并投稿，不再二次询问' }}</p>
              <p v-if="item.plan.recipe.mode === 'publish'">{{ publicationChapterScope(item.plan) }}</p>
              <p v-else-if="endpoint(item)==='upload'">上传后等待手动确认投稿，不自动提交。</p>
              <p v-else-if="item.plan.recipe.mode !== 'update'">{{ item.plan.recipe.mode === 'draft' ? '制作后准备本地草稿，不上传、不投稿。' : '仅制作成片，不上传、不投稿。' }}</p>
            </template>
            <template v-else><strong>本次仅恢复制作步骤</strong><p>按所列步骤更新或补齐，不上传、不投稿。</p><p v-for="version in item.plan.version_specs || []" :key="version.id">{{ version.name }} · {{ versionSummary(version) }}</p></template>
          </div>
          <n-alert v-if="item.blockers.length" type="warning">
            <ul class="resume-blockers"><li v-for="blocker in item.blockers" :key="blocker">{{ blocker === TTS_SERVICE_BLOCKER ? '配音服务未连接，可在此启动；连接后会自动重新检查恢复方案。' : blocker }}</li></ul>
            <div v-if="hasTtsServiceBlocker(item)" class="resume-service" data-testid="resume-tts-service">
              <TtsServiceControl :uid="item.uid" :disabled="busy || disabled || inspectedScope !== scopeKey" :revision="0" @status="status => onTtsServiceStatus(item, status)" />
              <RouterLink :to="`/projects/${item.uid}/steps/tts`">打开字幕与配音页查看设置 →</RouterLink>
              <p class="muted">启动服务只准备配音环境，仍需点击下方「确认恢复」才会继续制作。</p>
            </div>
          </n-alert><p v-if="item.result" role="status" class="result">{{ item.result }} <RouterLink :to="`/projects/${item.uid}`">查看项目 →</RouterLink></p>
        </article>
      </div>
      <template #footer>
        <PublicationRightsHint v-if="publicationCount || uploadCount" /><p v-if="updateCount" class="publish-notice">所选 {{ updateCount }} 个项目沿用原BV与原交付终点，复用已完成结果，仅补做未完成步骤。</p><p v-if="uploadCount" class="publish-notice">所选 {{ uploadCount }} 个项目仅上传，完成后等待你确认投稿。</p><p v-if="publicationCount" class="publish-notice">所选 {{ publicationCount }} 个项目会在制作后自动上传并投稿。</p>
        <div class="batch-actions"><n-button :disabled="busy && !checking" @click="close">{{ checking ? '取消检查' : '关闭' }}</n-button><n-button :disabled="busy || disabled" @click="inspect">重新检查</n-button><n-button type="primary" :loading="busy && !checking" :disabled="!canConfirm" @click="resumeAll">{{ singleReconcile ? '确认核对原稿更新结果' : `确认恢复 ${ready.length} 个项目` }}</n-button></div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.batch-form { display:grid; gap:16px; padding:20px 0; }.batch-form p { font-size:12px; line-height:1.7; }.scope-name { margin:0; font-weight:600; }.muted { color:var(--df-muted); }
.batch-row { display:grid; gap:12px; border-top:1px solid var(--df-border); padding:16px 0; }.batch-row p { margin:0; }.row-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }.row-head small { color:var(--df-muted); }.batch-row a { font-size:12px; }
.execution-scope { display:grid; gap:8px; font-size:12px; }.result { color:var(--df-text); }.publish-notice { font-size:12px; color:var(--df-warning); }.batch-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:12px; margin-top:12px; }
.resume-blockers { margin:0; padding-left:18px; display:grid; gap:6px; }
.resume-service { display:grid; gap:8px; margin-top:12px; padding-top:12px; border-top:1px solid var(--df-border); }
.resume-service :deep(.service-control) { margin:0; padding:0; border-bottom:0; }
</style>
