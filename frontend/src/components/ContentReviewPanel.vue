<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NCheckbox, NInput, NTag } from 'naive-ui'
import { normalizeContentReview, reviewTerms, reviewTime, type ContentReviewReport } from '../utils/contentReview'

const props = defineProps<{ uid: string; disabled?: boolean }>()
const emit = defineEmits<{
  (event: 'dirty-change', value: boolean): void
  (event: 'busy-change', value: boolean): void
  (event: 'saved', value: ContentReviewReport): void
}>()
const report = ref<ContentReviewReport | null>(null), error = ref(''), notice = ref('')
const conflict = ref(false)
const enabled = ref(true), muteAudio = ref(true), terms = ref(''), ranges = ref('')
const operation = ref<'read' | 'refresh' | 'save' | 'reset' | null>(null)
const baseline = ref('')
const draft = computed(() => ({ enabled: enabled.value, mute_audio: muteAudio.value, blocked_terms: reviewTerms(terms.value), blocked_ranges: ranges.value }))
const dirty = computed(() => !!report.value && JSON.stringify(draft.value) !== baseline.value)
const writable = computed(() => report.value?.revision !== null && !!report.value?.configuration_fingerprint)
const controlsDisabled = computed(() => !!props.disabled || !!operation.value || !report.value || !writable.value)
const mutating = computed(() => operation.value === 'save' || operation.value === 'reset' || operation.value === 'refresh')
let generation = 0, disposed = false, mounted = false, controller: AbortController | undefined

watch(dirty, value => emit('dirty-change', value), { flush: 'sync' })
watch(mutating, value => emit('busy-change', value), { flush: 'sync' })
function apply(value: ContentReviewReport) {
  report.value = value
  enabled.value = value.enabled; muteAudio.value = value.mute_audio
  terms.value = value.blocked_terms.join('\n'); ranges.value = value.blocked_ranges
  baseline.value = JSON.stringify(draft.value)
}
function discard() {
  if (!report.value || operation.value) return
  apply(report.value); error.value = ''; notice.value = '已放弃未保存的修改'
}
async function request(kind: Exclude<typeof operation.value, null>, body?: Record<string, unknown>, keepDraft = false) {
  if (disposed || operation.value || !props.uid) return
  const ticket = generation, target = props.uid
  operation.value = kind; error.value = ''; notice.value = ''; controller = new AbortController()
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(target)}/content-review`, {
      method: kind === 'read' ? 'GET' : kind === 'refresh' ? 'POST' : 'PUT', cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    })
    const value = await response.json().catch(() => ({}))
    if (disposed || ticket !== generation || props.uid !== target) return
    if (!response.ok) {
      conflict.value = response.status === 409 || value?.code === 'CONTENT_REVIEW_CONFLICT'
      throw new Error(value?.message || '内容审查请求失败，请稍后重试')
    }
    const parsed = normalizeContentReview(value)
    // 重读不能丢弃用户正在编辑的设置，先保存或放弃再刷新。
    if (kind === 'read' && dirty.value && !keepDraft) return
    const editing = keepDraft ? { enabled: enabled.value, muteAudio: muteAudio.value, terms: terms.value, ranges: ranges.value } : null
    apply(parsed)
    if (editing) {
      enabled.value = editing.enabled; muteAudio.value = editing.muteAudio
      terms.value = editing.terms; ranges.value = editing.ranges
      notice.value = '已读取最新设置并保留编辑内容。请核对后保存；保存将以当前面板内容覆盖本项目的审查设置。'
    }
    conflict.value = false
    if (kind === 'save' || kind === 'reset') {
      notice.value = kind === 'reset' ? '已恢复继承设置。请重新生成字幕和成片，再核对待发布文件。' : '审查设置已保存。请重新生成字幕和成片，再核对待发布文件。'
      emit('saved', parsed)
    } else if (kind === 'refresh') notice.value = '已重新执行本地文本筛查'
  } catch (e: any) {
    if (!disposed && ticket === generation && props.uid === target) error.value = e.name === 'TimeoutError' ? '内容审查请求超时，请刷新确认最新设置' : e.name === 'TypeError' ? '无法读取内容审查，请检查后端连接后重试' : e.message || '内容审查请求失败'
  } finally { if (!disposed && ticket === generation) operation.value = null }
}
async function load() { await request('read') }
async function rebase() { if (!props.disabled && conflict.value && dirty.value) await request('read', undefined, true) }
async function refresh() { if (!props.disabled && !dirty.value) await request('refresh') }
async function save() {
  if (controlsDisabled.value || !dirty.value) return
  await request('save', { ...draft.value, revision: report.value!.revision, configuration_fingerprint: report.value!.configuration_fingerprint })
}
async function reset() {
  if (controlsDisabled.value || dirty.value || report.value?.inherited !== false) return
  await request('reset', { reset: true, revision: report.value!.revision, configuration_fingerprint: report.value!.configuration_fingerprint })
}
function resetProject() {
  generation++; controller?.abort(); operation.value = null; report.value = null; baseline.value = ''
  enabled.value = true; muteAudio.value = true; terms.value = ''; ranges.value = ''; error.value = ''; notice.value = ''; conflict.value = false
  if (mounted) void load()
}
watch(() => props.uid, resetProject, { flush: 'sync' })
onMounted(() => { mounted = true; void load() })
onBeforeUnmount(() => { disposed = true; generation++; controller?.abort(); emit('dirty-change', false); emit('busy-change', false) })
</script>

<template>
  <n-card title="发布前内容审查" size="small" class="content-review-panel">
    <template #header-extra><n-tag v-if="report" size="small" :type="report.enabled ? 'warning' : 'default'">{{ report.enabled ? '自动屏蔽已启用' : '自动屏蔽已关闭' }}</n-tag></template>
    <p class="review-scope">文本筛查不判断画面，仍需核对原视频；不能保证审核通过。此处刷新只执行本地规则，不调用模型。</p>
    <n-alert v-if="error" type="warning" :show-icon="false" role="status">{{ error }}</n-alert>
    <n-alert v-if="notice" type="info" :show-icon="false" role="status">{{ notice }}</n-alert>
    <p v-if="!report" class="review-muted">{{ operation ? '正在读取项目内容审查…' : '尚未取得内容审查结果。' }}</p>
    <template v-if="report">
      <div class="review-summary"><strong>已检查 {{ report.summary.lines }} 句</strong><span>命中 {{ report.summary.blocked_lines }} 句</span><span>屏蔽评论 {{ report.summary.blocked_comments }} 条</span></div>
      <p class="review-muted">{{ report.origin || (report.inherited === true ? '当前继承上层设置' : report.inherited === false ? '当前使用本项目审查设置' : '配置来源暂未提供') }}</p>
      <n-alert v-for="(warning, index) in report.warnings" :key="index" type="warning" :show-icon="false">{{ warning }}</n-alert>
      <n-alert v-if="!writable" type="warning" :show-icon="false">当前报告缺少配置版本，暂不能保存；请刷新后再编辑。</n-alert>
      <div class="review-switches"><n-checkbox v-model:checked="enabled" :disabled="controlsDisabled">自动隐藏命中句的全部字幕并屏蔽命中评论</n-checkbox><n-checkbox v-model:checked="muteAudio" :disabled="controlsDisabled">同时静音命中时段的完整混音</n-checkbox></div>
      <p class="review-muted">屏蔽覆盖原文与译文整句；开启静音后，原声版与配音版的最终混音均处理。关闭静音仍可能保留原片或配音中的相关声音。</p>
      <div class="review-inputs">
        <label>自定义屏蔽词 · 每行一个<n-input v-model:value="terms" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :disabled="controlsDisabled" placeholder="输入需要屏蔽的词，每行一个" aria-label="自定义屏蔽词" /></label>
        <label>人工屏蔽时段 · 每行一段<n-input v-model:value="ranges" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :disabled="controlsDisabled" placeholder="0-12.5:需核对的画面或台词" aria-label="人工屏蔽时段" /><small>格式：开始秒-结束秒:原因，按原视频时间轴填写；例如 60.5-72:平台指出的片段。时段内全部字幕隐藏，启用静音时完整混音静音；画面需另行核对与处理。</small></label>
      </div>
      <p class="review-muted">保存作用于本项目。请重新生成字幕和成片后检查效果；已有成片、稿件已选文件和已上传文件不会自动替换。</p>
      <n-alert v-if="dirty" type="warning" :show-icon="false">审查设置有未保存的修改，请保存或放弃后再生成成片或上传。</n-alert>
      <div class="review-actions"><n-button type="primary" size="small" :disabled="controlsDisabled || !dirty" :loading="operation === 'save'" @click="save">保存审查设置</n-button><n-button v-if="conflict && dirty" size="small" :disabled="!!disabled || !!operation" @click="rebase">读取最新设置并保留修改</n-button><n-button v-if="dirty" size="small" :disabled="!!operation" @click="discard">放弃修改</n-button><n-button v-if="report.inherited === false" size="small" :disabled="controlsDisabled || dirty" :loading="operation === 'reset'" @click="reset">恢复继承</n-button></div>
      <details v-if="report.findings.length" class="review-findings" open><summary>字幕与配音命中明细 · {{ report.findings.length }} 条</summary><div class="review-list"><article v-for="(finding, index) in report.findings" :key="index" class="review-finding"><header><strong>{{ reviewTime(finding.start) }} – {{ reviewTime(finding.end) }}</strong><span v-if="finding.ids.length">句 {{ finding.ids.join('、') }}</span></header><p><b>原因</b>{{ finding.reason }}<small v-if="finding.rule"> · {{ finding.rule }}</small></p><p><b>原文</b>{{ finding.source_text || '（无原文）' }}</p><p><b>译文</b>{{ finding.text || '（无译文）' }}</p></article></div></details>
      <p v-else class="review-muted">当前整理稿未命中已配置的文本规则；原声识别稿与人工时段另行处理，仍需核对原视频和最终成片。</p>
      <details v-if="report.original_findings.length" class="review-findings"><summary>原声识别稿命中明细 · {{ report.original_findings.length }} 条</summary><div class="review-list"><article v-for="(finding, index) in report.original_findings" :key="index" class="review-finding"><header><strong>{{ reviewTime(finding.start) }} – {{ reviewTime(finding.end) }}</strong><span v-if="finding.ids.length">句 {{ finding.ids.join('、') }}</span></header><p><b>原因</b>{{ finding.reason }}<small v-if="finding.rule"> · {{ finding.rule }}</small></p><p><b>原文</b>{{ finding.source_text || '（无原文）' }}</p><p v-if="finding.text"><b>译文</b>{{ finding.text }}</p></article></div></details>
      <details v-if="report.comment_findings.length" class="review-findings"><summary>评论命中明细 · {{ report.comment_findings.length }} 条</summary><div class="review-list"><article v-for="(finding, index) in report.comment_findings" :key="index" class="review-finding"><header><strong>评论 {{ finding.ids.join('、') || index + 1 }}</strong></header><p><b>原因</b>{{ finding.reason }}<small v-if="finding.rule"> · {{ finding.rule }}</small></p><p><b>原文</b>{{ finding.source_text || '（无原文）' }}</p><p><b>译文</b>{{ finding.text || '（无译文）' }}</p></article></div></details>
      <p v-else-if="report.summary.blocked_comments" class="review-muted">命中的评论详情尚未提供，请刷新后核对。</p>
    </template>
    <div class="review-refresh"><n-button size="small" :disabled="!!disabled || !!operation || dirty" :loading="operation === 'refresh' || operation === 'read'" @click="refresh">刷新本地审查</n-button></div>
  </n-card>
</template>

<style scoped>
.content-review-panel{margin:18px 0}.review-scope,.review-muted{font-size:12px;line-height:1.75;color:var(--df-muted);margin:10px 0}.review-scope{margin-top:0}.review-summary,.review-switches,.review-actions,.review-finding header{display:flex;flex-wrap:wrap;align-items:center;gap:12px}.review-summary{font-size:13px;margin-top:16px}.review-switches{margin:16px 0 8px}.review-inputs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:14px}.review-inputs label{display:grid;gap:8px;align-content:start;font-size:12px}.review-inputs small,.review-finding small,.review-finding header span{font-size:11px;color:var(--df-muted);line-height:1.7}.review-actions{margin:14px 0}.review-findings{margin-top:18px}.review-findings summary{font-size:12px;cursor:pointer}.review-list{max-height:420px;overflow:auto;display:grid;gap:10px;margin-top:12px}.review-finding{border:1px solid var(--df-border);border-radius:6px;padding:12px;min-width:0}.review-finding header{font-size:12px}.review-finding p{font-size:12px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.review-finding b{display:inline-block;color:var(--df-muted);font-weight:400;margin-right:10px}.review-refresh{display:flex;justify-content:flex-end;margin-top:14px}.content-review-panel :deep(.n-alert){margin:10px 0}@media(max-width:800px){.review-inputs{grid-template-columns:1fr}}
</style>
