<script setup lang="ts">
// 新执行直接检查项目目标；仅在有中断任务时选择恢复原计划或使用当前设置。
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { NAlert, NButton, NDrawer, NDrawerContent, NSpin } from 'naive-ui'
import { friendlyError } from '../utils/http'
import { parseChain } from '../utils/chainStatus'
import { WORKFLOW_OPEN, workflowModeLabel } from '../utils/workflow'
import { releaseBatchLockMessage } from '../utils/releaseBatch'
import BatchWorkflowResume from './BatchWorkflowResume.vue'
import BatchWorkflow from './BatchWorkflow.vue'
import type { ProjectRunFeedback } from '../utils/projectRunFeedback'

type Target = { uid: string; title: string }
type Row = Target & { run: any; chain: any; publication: string; remotePublication?:boolean; batch: any; blocked: string; error: string }
type Intent = 'resume' | 'chain' | 'workflow'
const props = withDefaults(defineProps<{ projects?: Target[]; label?: string; scopeLabel?: string;
  disabled?: boolean; hiddenTrigger?: boolean; allowWorkflow?: boolean; workflowHandler?: boolean }>(),
  { projects: () => [], label: '运行项目…', scopeLabel: '', disabled: false, hiddenTrigger: false, allowWorkflow: true, workflowHandler: false })
const emit = defineEmits<{ started: []; workflow: [resumeId: string, preferredMode?: string]; runFeedback: [feedback: ProjectRunFeedback] }>()
const router=useRouter()
const show = ref(false), checking = ref(false), error = ref(''), scopeName = ref('')
const targets = ref<Target[]>([]), rows = ref<Row[]>([]), intent = ref<Intent>('workflow')
const currentTarget = ref('')
const checkedCount = computed(() => rows.value.length)
const recovery = ref<InstanceType<typeof BatchWorkflowResume> | null>(null)
const fullWorkflow = ref<InstanceType<typeof BatchWorkflow> | null>(null)
const recoveryKind = ref<'auto' | 'chain'>('auto')
const savedWorkflow = (row: Row) => !row.blocked && !row.error && row.run
  && ['paused', 'failed', 'attention'].includes(row.run.status) && (!row.run.publication_id || row.run.mode==='update')
const savedChain = (row: Row) => !row.blocked && !row.error && row.chain?.state === 'held'
const resumable = computed(() => rows.value.filter(row => savedWorkflow(row) || savedChain(row)))
const separateChain = computed(() => rows.value.some(row => savedWorkflow(row) && savedChain(row)))
const hasSavedChain = computed(() => rows.value.some(row => savedChain(row)))
const eligible = computed(() => rows.value.some(row => !row.blocked && !row.error))
const canPreview = computed(() => show.value && !checking.value && !props.disabled && eligible.value
  && (intent.value === 'resume' ? !!resumable.value.length : intent.value === 'chain' ? separateChain.value
    : intent.value !== 'workflow' || props.allowWorkflow))
const isWorkflowResume = computed(() => targets.value.length === 1 && rows.value[0] && savedWorkflow(rows.value[0]))
const deliveryResume=computed(()=>isWorkflowResume.value && rows.value[0].run.mode==='update' && !!rows.value[0].run.update_id)
const resumeEnd = computed(() => {
  if(deliveryResume.value)return '原稿更新交付（保留原BV）'
  if (targets.value.length === 1 && rows.value[0]) return savedWorkflow(rows.value[0])
    ? workflowModeLabel(rows.value[0].run.delivery_mode || rows.value[0].run.mode) : '仅制作成片'
  return '按每个项目保留的计划执行，下一步逐项核对终点'
})
let disposed = false, generation = 0, readAbort: AbortController | undefined
async function read(uid: string, suffix: string, signal: AbortSignal) {
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}${suffix}`, { cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(60000)]) })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.message || '读取任务状态失败'), { code: data.code, status: response.status })
  return data
}
async function open(input = props.projects, scope = props.scopeLabel) {
  if (disposed || props.disabled) return
  const next = [...new Map(input.filter(target => target?.uid).map(target =>
    [target.uid, { uid: target.uid, title: target.title || target.uid }])).values()]
  if (!next.length) return
  const ticket = ++generation
  readAbort?.abort(); const controller = new AbortController(); readAbort = controller
  targets.value = next; rows.value = []; scopeName.value = scope || (next.length === 1 ? next[0].title : `本次 ${next.length} 个项目`)
  show.value = true; checking.value = true; currentTarget.value = ''; error.value = ''; intent.value = 'workflow'
  try {
    for (const target of next) {
      if (disposed || ticket !== generation || controller.signal.aborted) return
      currentTarget.value = target.title
      const row: Row = { ...target, run: null, chain: null, publication: '', batch: null, blocked: '', error: '' }
      try {
        const [detail, status] = await Promise.all([read(target.uid, '', controller.signal), read(target.uid, '/workflow/status', controller.signal)])
        if (disposed || ticket !== generation) return
        row.title = detail.title || target.title; row.run = status.run
        row.batch = detail.release_batch || null
        row.chain = parseChain(detail.chain ?? detail.params?._chain)
        row.publication = row.run?.publication_id || status.existing_publication_id || detail.publication?.id || ''
        row.remotePublication = status.has_remote_publication ?? !!row.publication
        row.blocked = detail.archived ? '项目已归档，请先恢复项目。' : detail.dir_exists === false ? '项目目录缺失，请先核对文件位置。'
          : ['running','pausing','stopping'].includes(row.batch?.status) ? releaseBatchLockMessage(row.batch)
          : detail.running_step || (detail.steps || []).some((step: any) => ['running', 'queued', 'stopping'].includes(step.status))
            || row.chain?.state === 'active' || WORKFLOW_OPEN.includes(row.run?.status)
            || ['queued', 'preparing', 'uploading', 'submitting', 'joining'].includes(detail.publication?.status)
            ? '项目正在执行或等待审核，请先查看当前进度。' : ''
      } catch (failure: any) {
        if (disposed || ticket !== generation || controller.signal.aborted) return
        row.error = friendlyError(failure, '无法读取任务状态，请重新读取。')
      }
      if (disposed || ticket !== generation) return
      rows.value.push(row)
    }
    if (ticket === generation) intent.value = resumable.value.length ? 'resume' : 'workflow'
  } finally { if (!disposed && ticket === generation) { checking.value = false; currentTarget.value = '' } }
  if (!disposed && ticket === generation && !resumable.value.length && rows.value.every(row => !row.blocked && !row.error)) await preview()
}
function close() {
  generation++; readAbort?.abort(); show.value = false; checking.value = false; currentTarget.value = ''
}
async function preview() {
  if (!canPreview.value) return
  const selected = intent.value, oldRun = rows.value[0]?.run?.id || '', ticket = generation
  const scope = scopeName.value, chosen = targets.value.map(target => ({...target}))
  show.value = false
  await nextTick()
  if (disposed || props.disabled || ticket !== generation) return
  if (selected === 'workflow') {
    if (props.workflowHandler) emit('workflow', '')
    else if(chosen.length===1)await router.push({path:`/projects/${chosen[0].uid}`,query:{run:'workflow'}})
    else await fullWorkflow.value?.open()
  } else if (selected === 'resume' && props.workflowHandler && isWorkflowResume.value) emit('workflow', oldRun)
  else if (selected === 'resume' || selected === 'chain') {
    recoveryKind.value = selected === 'chain' ? 'chain' : 'auto'
    await nextTick(); if (!disposed && ticket === generation && !props.disabled) await recovery.value?.open()
  }
}
watch(() => props.disabled, value => { if (value && show.value) close() })
onUnmounted(() => { disposed = true; generation++; readAbort?.abort() })
defineExpose({ open, close })
</script>
<template>
  <n-button v-if="!hiddenTrigger" size="small" :disabled="disabled || !projects.length" @click="open()">{{ label }}</n-button>
  <n-drawer :show="show" width="min(680px,100vw)" @update:show="value => { if (!value) close() }">
    <n-drawer-content :title="checking?'正在检查项目':'继续上次任务或开始新执行'" closable>
      <div class="run-choice" data-testid="project-run-choice">
        <p class="scope-name">{{ scopeName }}</p>
        <p class="muted">继续任务沿用原计划；新的执行使用各步骤当前保存的设置。查看方案不会启动任务。</p>
        <div v-if="checking" class="inspection-progress" role="status" aria-live="polite" aria-atomic="true">
          <n-spin size="small" /><span>已检查 {{ checkedCount }} / {{ targets.length }} 个项目<small v-if="currentTarget">正在读取：{{ currentTarget }}</small></span>
        </div>
        <n-alert v-if="error" type="warning">{{ error }}</n-alert>
        <div v-if="!checking && resumable.length" class="intent-options" role="radiogroup" aria-label="本次运行方式">
          <button v-if="resumable.length" type="button" role="radio" :aria-checked="intent==='resume'" :disabled="disabled" :class="{chosen:intent==='resume'}" @click="intent='resume'">
            <strong>继续上次任务 <span>推荐</span></strong><small>本次执行到：{{ resumeEnd }}</small><small v-if="deliveryResume">仅继续已确认原稿更新的剩余交付，复用成片和上传，不重新制作。结果未知时先核对平台。</small><small v-else>重新检查当前输入，复用有效结果；需更新时可能从更早步骤开始。</small>
          </button>
          <button v-if="separateChain" type="button" role="radio" :aria-checked="intent==='chain'" :disabled="disabled" :class="{chosen:intent==='chain'}" @click="intent='chain'">
            <strong>继续保留的制作计划</strong><small>另有挂起的制作计划，只制作成片，不执行上次完整流程的发布终点。</small>
          </button>
          <button type="button" role="radio" :aria-checked="intent==='workflow'" :disabled="disabled" :class="{chosen:intent==='workflow'}" @click="intent='workflow'">
            <strong>按当前设置开始新执行</strong><small>根据合成成片页的目标版本和发布到B站页的交付设置检查执行方案。</small><small v-if="hasSavedChain">新的执行会替换挂起的制作计划。</small><small v-if="targets.length>1">逐项核对各项目终点；固定发布顺序可使用项目列表的「顺序发布」。</small>
          </button>
        </div>
        <div v-if="rows.length" class="task-records" aria-label="已检查项目">
          <div v-for="row in rows" :key="row.uid" class="task-record" :data-project="row.uid">
            <strong>{{ row.title }}</strong>
            <p v-if="row.blocked || row.error" class="warning">{{ row.blocked || row.error }}</p>
            <p v-else-if="savedWorkflow(row)">上次完整流程已中断 · 终点：{{ workflowModeLabel(row.run.delivery_mode || row.run.mode) }}<span v-if="row.run.phase"> · 停在：{{ row.run.phase }}</span></p>
            <p v-else-if="savedChain(row)">保留了挂起的制作计划 · 仅制作成片</p>
            <p v-else class="muted">{{ row.publication ? '已有发布记录，可在发布页继续处理。' : '按当前项目设置检查目标版本与交付终点。' }}</p>
            <RouterLink :to="`/projects/${row.uid}${row.publication ? '/publish' : ''}`">{{ row.publication ? '查看现有稿件 →' : '查看项目总览 →' }}</RouterLink>
            <RouterLink v-if="row.batch?.id" :to="`/#release-batch-${row.batch.id}`">查看顺序发布批次 →</RouterLink>
          </div>
        </div>
      </div>
      <template #footer><div class="choice-actions"><n-button @click="close">{{ checking ? '取消读取' : '取消' }}</n-button><n-button :disabled="checking" @click="open(targets,scopeName)">重新读取</n-button><n-button type="primary" :disabled="!canPreview" @click="preview">查看执行方案 →</n-button></div></template>
    </n-drawer-content>
  </n-drawer>
  <BatchWorkflowResume ref="recovery" :projects="targets" :scope-label="scopeName" :kind="recoveryKind" hidden-trigger :disabled="disabled" @resumed="emit('started')" @run-feedback="feedback => emit('runFeedback',feedback)" />
  <BatchWorkflow v-if="!workflowHandler" ref="fullWorkflow" :uids="targets.map(target=>target.uid)" hidden-trigger @started="emit('started')" />
</template>
<style scoped>
.run-choice { display:grid; gap:16px; padding:18px 0; }.run-choice p { margin:0; font-size:12px; line-height:1.8; }.scope-name { font-weight:600; }.muted { color:var(--df-muted); }
.inspection-progress{display:flex;align-items:center;gap:12px;font-size:12px;line-height:1.7}.inspection-progress small{display:block;color:var(--df-muted);overflow-wrap:anywhere}
.intent-options { display:grid; gap:10px; }.intent-options>button { display:grid; gap:7px; padding:15px 16px; text-align:left; border:1px solid var(--df-border); border-radius:10px; background:var(--df-panel); color:var(--df-text); cursor:pointer; font:inherit; }.intent-options>button:hover:not(:disabled),.intent-options>button.chosen { border-color:var(--df-accent); background:color-mix(in srgb,var(--df-accent) 9%,var(--df-panel)); }.intent-options>button:disabled { opacity:.55; cursor:default; }.intent-options strong { font-size:13px; font-weight:600; }.intent-options strong span { margin-left:7px; color:var(--df-accent); font-size:11px; font-weight:400; }.intent-options small { font-size:12px; line-height:1.7; color:var(--df-muted); }
.task-records { display:grid; gap:12px; }.task-record { display:grid; gap:6px; border-top:1px solid var(--df-border); padding-top:12px; }.task-record strong,.task-record a { font-size:12px; }.task-record a { color:var(--df-accent); width:fit-content; }.warning { color:var(--df-warning,#dfb66f); }.choice-actions { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
</style>
