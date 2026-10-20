<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NCheckbox, NInput, NRadio, NRadioGroup, NTag } from 'naive-ui'
import ContentReviewPreview from './ContentReviewPreview.vue'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { decisionLabel, findingEffect, normalizeContentReview, reviewAudioEffect, reviewDate, reviewTerms, reviewTime, type ContentFinding, type ContentReviewReport, type ReviewAudioMode, type ReviewDecisionAction } from '../utils/contentReview'

const props = defineProps<{ uid: string; disabled?: boolean }>()
const emit = defineEmits<{
  (event: 'dirty-change', value: boolean): void
  (event: 'busy-change', value: boolean): void
  (event: 'saved', value: ContentReviewReport): void
}>()
const report = ref<ContentReviewReport | null>(null), error = ref(''), notice = ref(''), conflict = ref(false)
const enabled = ref(true), muteAudio = ref(true), terms = ref(''), ranges = ref('')
const audioMode = ref<ReviewAudioMode>('voice_only')
const operation = ref<'read' | 'refresh' | 'save' | 'reset' | 'decision' | null>(null)
const baseline = ref(''), reasons = ref<Record<string, string>>({}), decisionKey = ref(''), decisionAction = ref<ReviewDecisionAction | ''>('')
const selected = ref<ContentFinding | null>(null), previewBusy = ref(false)
const editability = useConfigurationEditability(() => `project:${props.uid}`)
const isDisabled = computed(() => !!props.disabled || editability.locked.value
  || editability.isPathLocked('content_review'))
const draft = computed(() => ({ enabled: enabled.value, mute_audio: muteAudio.value, audio_mode: audioMode.value, blocked_terms: reviewTerms(terms.value), blocked_ranges: ranges.value }))
const dirty = computed(() => !!report.value && JSON.stringify(draft.value) !== baseline.value)
const writable = computed(() => !!report.value && report.value.revision !== null && !!report.value.configuration_fingerprint)
const controlsDisabled = computed(() => isDisabled.value || !!operation.value || previewBusy.value || conflict.value || !writable.value)
const decisionWritable = computed(() => !controlsDisabled.value && !!report.value?.enabled && !!report.value?.decision_revision && !report.value?.blocker)
const mutating = computed(() => previewBusy.value || operation.value === 'save' || operation.value === 'reset' || operation.value === 'refresh' || operation.value === 'decision')
const groups = computed(() => report.value ? [
  { id: 'current', title: '字幕与配音命中明细', rows: report.value.findings },
  { id: 'original', title: '原声识别稿命中明细', rows: report.value.original_findings },
  { id: 'comments', title: '评论命中明细', rows: report.value.comment_findings },
] : [])
const coverage = computed(() => report.value ? [
  { label: '原声识别稿', count: report.value.coverage.original, unit: '句' },
  { label: '当前整理原文', count: report.value.coverage.source, unit: '句' },
  { label: '当前译文', count: report.value.coverage.translated, unit: '句' },
  { label: '评论', count: report.value.coverage.comments, unit: '条' },
] : [])
const fieldNames: Record<string, string> = { source_text: '整理原文', text: '译文', source_audio: '原声文字', source_audio_text: '原声文字', text_zh: '评论译文', display_text: '上屏文字', original: '采集原文' }
const commentFieldNames: Record<string, string> = { text: '评论原文', source_text: '上屏原文', text_zh: '评论译文', display_text: '上屏文字', original: '采集原文', review_evidence: '保存的命中文字' }
function fieldLabel(field: string, group: string) {
  return group === 'original' ? '原始识别稿' : group === 'comments' ? commentFieldNames[field] || '其他评论文字' : fieldNames[field] || '其他文字'
}
let generation = 0, disposed = false, mounted = false, controller: AbortController | undefined

function effect(value: ContentReviewReport | null) {
  return value ? JSON.stringify({ ranges: value.effective_ranges, audio: value.effective_audio_ranges,
    audioMode: value.effective_audio_ranges.length ? value.audio_mode : null,
    ids: value.blocked_ids, comments: value.comment_findings.filter(row => row.active).map(row => row.key).sort() }) : ''
}

watch(dirty, value => { emit('dirty-change', value); if (value) selected.value = null }, { flush: 'sync' })
watch(mutating, value => emit('busy-change', value), { flush: 'sync' })
function apply(value: ContentReviewReport) {
  selected.value = null
  const previous = new Map(groups.value.flatMap(group => group.rows).map(row => [row.key, row.evidence_fingerprint]))
  const current = new Map([...value.findings, ...value.original_findings, ...value.comment_findings].map(row => [row.key, row.evidence_fingerprint]))
  // 换证据时撤下尚未提交的旧备注，避免把原语境的理由套到新文字。
  reasons.value = Object.fromEntries(Object.entries(reasons.value).filter(([key]) => previous.get(key) === current.get(key) && current.has(key)))
  report.value = value
  enabled.value = value.enabled; muteAudio.value = value.mute_audio
  audioMode.value = value.audio_mode
  terms.value = value.blocked_terms.join('\n'); ranges.value = value.blocked_ranges
  baseline.value = JSON.stringify(draft.value)
}
function discard() {
  if (!report.value || operation.value || previewBusy.value || isDisabled.value) return
  apply(report.value); error.value = ''; notice.value = '已放弃未保存的修改'
}
async function request(kind: Exclude<typeof operation.value, null>, body?: Record<string, unknown>, keepDraft = false) {
  if (disposed || operation.value || previewBusy.value || !props.uid || isDisabled.value) return
  const ticket = generation, target = props.uid, previousEffect = effect(report.value)
  operation.value = kind; error.value = ''; notice.value = ''; selected.value = null; controller = new AbortController()
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(target)}/content-review${kind === 'decision' ? '/decisions' : ''}`, {
      method: kind === 'read' ? 'GET' : kind === 'refresh' || kind === 'decision' ? 'POST' : 'PUT', cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    })
    const value = await response.json().catch(() => ({}))
    if (disposed || ticket !== generation || props.uid !== target) return
    if (!response.ok) {
      if (response.status === 409 || value?.code === 'CONTENT_REVIEW_CONFLICT' || value?.code === 'CONTENT_REVIEW_DECISION_CONFLICT') conflict.value = true
      throw new Error(value?.message || '内容审查请求失败，请稍后重试')
    }
    const parsed = normalizeContentReview(value)
    // 重读和逐条复核均保留用户的配置草稿，只有显式保存才提交设置。
    if (kind === 'read' && dirty.value && !keepDraft) return
    const editing = keepDraft ? { enabled: enabled.value, muteAudio: muteAudio.value, audioMode: audioMode.value, terms: terms.value, ranges: ranges.value } : null
    apply(parsed)
    if (editing) {
      enabled.value = editing.enabled; muteAudio.value = editing.muteAudio; terms.value = editing.terms; ranges.value = editing.ranges
      audioMode.value = editing.audioMode
      if (kind === 'read') notice.value = '已读取最新结果并保留编辑内容。请重新核对证据；保存设置将以当前面板内容覆盖本项目的审查设置。'
    }
    conflict.value = false
    if (kind === 'save' || kind === 'reset' || kind === 'decision') {
      if (kind === 'decision') {
        const latest = [...parsed.findings, ...parsed.original_findings, ...parsed.comment_findings].find(row => row.key === body?.key)
        const result = latest ? findingEffect(latest, parsed, parsed.comment_findings.includes(latest)) : ''
        notice.value = effect(parsed) === previousEffect
          ? `已保存复核判断；当前处理效果未变化。${result.includes('仍覆盖') ? result + '。' : ''}`
          : `复核判断已保存，处理结果已变化。请重新生成受影响的字幕或成片，再核对待发布文件。${result.includes('仍覆盖') ? result + '。' : ''}`
      } else notice.value = kind === 'reset' ? '已恢复继承设置。请重新生成字幕和成片，再核对待发布文件。' : '审查设置已保存。请重新生成字幕和成片，再核对待发布文件。'
      emit('saved', parsed)
    } else if (kind === 'refresh') notice.value = parsed.enabled ? '已重新检查当前文字；字幕文件和成片尚未重新生成。' : '审查已关闭，本次未执行文本筛查'
  } catch (e: any) {
    if (!disposed && ticket === generation && props.uid === target && e.name !== 'AbortError') {
      error.value = e.name === 'TimeoutError' ? '内容审查请求超时，请读取最新结果确认' : e.name === 'TypeError' ? '无法读取内容审查，请检查后端连接后重试' : e.message || '内容审查请求失败'
      // 超时或响应结构异常时写入可能已发生，需要重读，避免使用旧凭据继续判断。
      if (kind === 'decision' || kind === 'save' || kind === 'reset') conflict.value = true
    }
  } finally { if (!disposed && ticket === generation) { operation.value = null; decisionKey.value = ''; decisionAction.value = '' } }
}
async function load() { await request('read') }
async function rebase() { if (!isDisabled.value && conflict.value) await request('read', undefined, true) }
async function refresh() { if (!isDisabled.value && !dirty.value && !conflict.value) await request('refresh') }
async function save() {
  if (controlsDisabled.value || !dirty.value) return
  await request('save', { ...draft.value, revision: report.value!.revision, configuration_fingerprint: report.value!.configuration_fingerprint })
}
async function reset() {
  if (controlsDisabled.value || dirty.value || report.value?.inherited !== false) return
  await request('reset', { reset: true, revision: report.value!.revision, configuration_fingerprint: report.value!.configuration_fingerprint })
}
async function decide(finding: ContentFinding, decision: ReviewDecisionAction) {
  if (!decisionWritable.value || !finding.key || !finding.evidence_fingerprint) return
  const current = groups.value.flatMap(group => group.rows).find(row => row.key === finding.key)
  if (!current || current.evidence_fingerprint !== finding.evidence_fingerprint) { conflict.value = true; error.value = '证据已变化，请读取最新结果后重新核对'; return }
  decisionKey.value = finding.key; decisionAction.value = decision
  const body = { key: finding.key, decision, evidence_fingerprint: finding.evidence_fingerprint,
    decision_revision: report.value!.decision_revision, revision: report.value!.revision,
    configuration_fingerprint: report.value!.configuration_fingerprint, reason: (reasons.value[finding.key] || '').trim() }
  await request('decision', body, true)
}
function selectPreview(finding: ContentFinding) {
  if (!decisionWritable.value || dirty.value || finding.start === null || finding.end === null || finding.end <= finding.start || !finding.key || !finding.evidence_fingerprint) return
  selected.value = selected.value?.key === finding.key ? null : finding
}
function previewConflict() { selected.value = null; conflict.value = true; error.value = '片段证据或审查设置已变化，请读取最新结果后重新核对' }
function resetProject() {
  generation++; controller?.abort(); operation.value = null; selected.value = null; previewBusy.value = false; report.value = null; baseline.value = ''
  enabled.value = true; muteAudio.value = true; terms.value = ''; ranges.value = ''; reasons.value = {}; error.value = ''; notice.value = ''; conflict.value = false
  audioMode.value = 'voice_only'
  if (mounted) void load()
}
watch(() => props.uid, resetProject, { flush: 'sync' })
watch(isDisabled, value => {
  if (value) {
    const wasWriting = operation.value === 'save' || operation.value === 'reset' || operation.value === 'decision'
    generation++; controller?.abort(); operation.value = null; selected.value = null; previewBusy.value = false
    if (wasWriting) { conflict.value = true; error.value = '操作已中断，请读取最新结果确认是否已保存' }
  } else if (mounted && !report.value) void load()
}, { flush: 'sync' })
onMounted(() => { mounted = true; void load() })
onBeforeUnmount(() => { disposed = true; generation++; controller?.abort(); emit('dirty-change', false); emit('busy-change', false) })
</script>

<template>
  <n-card title="内容审查与屏蔽" size="small" class="content-review-panel">
    <template #header-extra><n-tag v-if="report" size="small" :type="report.enabled ? 'info' : 'default'">{{ report.enabled ? '自动屏蔽已启用' : '自动屏蔽已关闭 · 未检查' }}</n-tag></template>
    <p class="review-scope">字幕导出时隐藏命中句，合成成片时处理命中时段的声音；发布前核对所选文件是否已应用当前规则。原始文字和配音文件保留。</p>
    <p class="review-muted">本地规则只筛查文字，不判断画面，不能保证审核通过。请结合原声片段核对完整语境；重新检查文字不调用模型，也不生成字幕文件或成片。</p>
    <n-alert v-if="error" type="warning" :show-icon="false" role="status">{{ error }}</n-alert>
    <n-alert v-if="notice" type="info" :show-icon="false" role="status">{{ notice }}</n-alert>
    <p v-if="!report" class="review-muted">{{ operation ? '正在读取项目内容审查…' : '尚未取得内容审查结果。' }}</p>
    <template v-if="report">
      <div v-if="report.enabled" class="review-coverage" aria-label="本次文字检查覆盖">
        <div v-for="item in coverage" :key="item.label" :class="{missing:item.count === null || item.count === 0}"><span>{{ item.label }}</span><strong>{{ item.count === null ? '覆盖未提供' : item.count === 0 ? '无可检查文字' : `${item.count} ${item.unit}` }}</strong></div>
      </div>
      <p v-else class="review-disabled">本次未执行文本筛查。启用并保存审查设置后，才能查看当前文字的规则命中与生效范围。</p>
      <div class="review-meta"><span>规则 {{ report.rule_version || '版本尚未提供' }}</span><span>{{ report.enabled ? `本次检查 ${reviewDate(report.checked_at)}` : '检查时间：未检查' }}</span><span>{{ report.origin || (report.inherited === true ? '当前继承上层设置' : report.inherited === false ? '当前使用本项目审查设置' : '配置来源暂未提供') }}</span></div>
      <n-alert v-if="report.blocker" type="warning" :show-icon="false">当前文字无法完整核对：{{ report.blocker.message }} 请修正后刷新结果。</n-alert>
      <n-alert v-else-if="report.enabled && !report.coverage.complete_text" type="warning" :show-icon="false">本次检查仅覆盖已列出的文字；原声识别稿、整理原文或译文尚未齐备，当前结果无法代表完整视频。</n-alert>
      <div v-if="report.enabled && groups.some(group => group.rows.length)" class="review-summary">
        <strong>待复核 {{ report.summary.pending_findings ?? groups.flatMap(group => group.rows).filter(row => row.decision === 'pending').length }} 项</strong>
        <span>已确认屏蔽 {{ report.summary.confirmed_findings ?? groups.flatMap(group => group.rows).filter(row => row.decision === 'block').length }} 项</span>
        <span>已允许 {{ report.summary.allowed_findings ?? groups.flatMap(group => group.rows).filter(row => row.decision === 'allow').length }} 项</span>
        <span>实际隐藏 {{ report.summary.blocked_lines }} 句 · 屏蔽评论 {{ report.summary.blocked_comments }} 条</span>
      </div>
      <n-alert v-if="report.enabled && report.stale_decision_count" type="warning" :show-icon="false">{{ report.stale_decision_count }} 项旧复核的文字或规则已变化，原判断不再应用；请按当前证据重新核对。</n-alert>
      <n-alert v-if="conflict" type="warning" :show-icon="false">当前报告已失效，请先读取最新结果；未保存的配置修改会保留。<n-button size="small" :disabled="isDisabled || !!operation || previewBusy" @click="rebase">读取最新结果并保留修改</n-button></n-alert>
      <n-alert v-if="!writable" type="warning" :show-icon="false">当前报告缺少配置版本，暂不能保存；请刷新后再编辑。</n-alert>
      <div v-if="report.enabled" class="review-effective">
        <span>当前生效</span><strong>{{ report.effective_ranges.length }} 段屏蔽时段</strong><small>字幕隐藏 · {{ reviewAudioEffect(report) }}</small>
        <p v-if="report.mute_audio && report.audio_processing?.fallback_reason">声音处理兜底：{{ report.audio_processing.fallback_reason }}</p>
        <details v-if="report.effective_ranges.length"><summary>查看生效范围</summary><p>字幕：{{ report.effective_ranges.map(range => `${reviewTime(range.start)}–${reviewTime(range.end)}`).join('、') }}</p><p v-if="report.effective_audio_ranges.length">声音处理范围（含安全边界及配音尾音）：{{ report.effective_audio_ranges.map(range => `${reviewTime(range.start)}–${reviewTime(range.end)}`).join('、') }}</p></details>
        <p v-else>当前没有生效屏蔽时段；检查覆盖以各类文字的数量为准，仍需核对原视频。</p>
      </div>
      <ContentReviewPreview v-if="selected" :key="selected.key" :uid="uid" :finding="selected" :report="report" :disabled="isDisabled || !!operation || dirty || conflict" @busy-change="previewBusy = $event" @conflict="previewConflict" @close="selected = null" />
      <template v-if="report.enabled">
        <details v-for="group in groups.filter(group => group.rows.length)" :key="group.id" class="review-findings" :open="group.id === 'current'"><summary>{{ group.title }} · {{ group.rows.length }} 条</summary>
          <div class="review-list"><article v-for="(finding, index) in group.rows" :key="finding.key || index" class="review-finding" :class="{allowed:finding.decision === 'allow',confirmed:finding.decision === 'block'}">
            <header><div class="finding-location"><strong v-if="group.id !== 'comments'">{{ reviewTime(finding.start) }} – {{ reviewTime(finding.end) }}</strong><strong v-else>评论 {{ finding.ids.join('、') || index + 1 }}</strong><span v-if="group.id !== 'comments' && finding.ids.length">句 {{ finding.ids.join('、') }}</span></div><n-tag size="small" :bordered="false" :type="finding.decision === 'allow' ? 'default' : finding.decision === 'block' ? 'error' : 'warning'">{{ decisionLabel(finding) }}</n-tag></header>
            <p class="finding-reason"><b>原因</b>{{ finding.reason }}<small v-if="finding.rule"> · {{ finding.rule }}</small></p>
            <p v-if="finding.fields.length" class="finding-fields">命中位置：{{ [...new Set(finding.fields.map(field => fieldLabel(field, group.id)))].join('、') }}</p>
            <div class="finding-text"><p><b>原文</b>{{ finding.source_text || '（无原文）' }}</p><p v-if="group.id !== 'original' || finding.text"><b>译文</b>{{ finding.text || '（尚无译文）' }}</p><p v-if="finding.source_audio_text && finding.source_audio_text !== finding.source_text"><b>原声文字</b>{{ finding.source_audio_text }}</p></div>
            <div v-if="finding.evidence_texts.some(value => value !== finding.source_text && value !== finding.text && value !== finding.source_audio_text)" class="finding-text"><p v-for="(value, evidenceIndex) in finding.evidence_texts.filter(value => value !== finding.source_text && value !== finding.text && value !== finding.source_audio_text)" :key="evidenceIndex"><b>命中文字证据</b>{{ value }}</p></div>
            <details v-if="finding.context_rows.length" class="finding-context"><summary>查看附近完整上下文 · {{ finding.context_rows.length }} 句</summary><div v-for="row in finding.context_rows" :key="row.id" :class="{hit:finding.context_ids.includes(row.id)}"><small>句 {{ row.id }} · {{ reviewTime(row.start) }} – {{ reviewTime(row.end) }}<template v-if="finding.context_ids.includes(row.id)"> · 参与命中</template></small><p><b>原文</b>{{ row.source_text || '（无原文）' }}</p><p v-if="row.text"><b>译文</b>{{ row.text }}</p><p v-if="row.source_audio_text && row.source_audio_text !== row.source_text"><b>原声文字</b>{{ row.source_audio_text }}</p></div></details>
            <p v-if="finding.decision_reason" class="review-muted">复核备注：{{ finding.decision_reason }}<template v-if="finding.decided_at"> · {{ reviewDate(finding.decided_at) }}</template></p>
            <p class="finding-effect">{{ findingEffect(finding, report, group.id === 'comments') }}</p>
            <div class="finding-actions"><n-button size="tiny" secondary type="warning" :disabled="!decisionWritable || !finding.key || !finding.evidence_fingerprint || finding.decision === 'block'" :loading="operation === 'decision' && decisionKey === finding.key && decisionAction === 'block'" @click="decide(finding, 'block')">确认屏蔽</n-button><n-button size="tiny" secondary :disabled="!decisionWritable || !finding.key || !finding.evidence_fingerprint || finding.decision === 'allow'" :loading="operation === 'decision' && decisionKey === finding.key && decisionAction === 'allow'" @click="decide(finding, 'allow')">误报，允许此项</n-button><n-button v-if="finding.decision !== 'pending'" size="tiny" quaternary :disabled="!decisionWritable" :loading="operation === 'decision' && decisionKey === finding.key && decisionAction === 'reset'" @click="decide(finding, 'reset')">恢复自动判断</n-button><n-button v-if="group.id !== 'comments' && finding.start !== null && finding.end !== null" size="tiny" quaternary :disabled="!decisionWritable || dirty || !finding.key || !finding.evidence_fingerprint" @click="selectPreview(finding)">{{ selected?.key === finding.key ? '收起片段' : '核对这个片段' }}</n-button></div>
            <details v-if="finding.key" class="finding-note"><summary>添加复核备注（可选）</summary><n-input v-model:value="reasons[finding.key]" size="small" :maxlength="2000" :disabled="!decisionWritable" placeholder="例如：结合上下文属于普通旅行介绍" :aria-label="`复核备注 ${finding.key}`" /></details>
          </article></div>
        </details>
        <p v-if="!report.findings.length && !report.blocker" class="review-muted">{{ report.coverage.source || report.coverage.translated || report.summary.lines ? '当前整理稿未命中已配置的文本规则；原声识别稿与人工时段另行处理，仍需核对原视频和最终成片。' : '尚无可检查的整理稿，不能形成当前字幕与配音的审查结论。' }}</p>
        <p v-if="!report.comment_findings.length && report.summary.blocked_comments" class="review-muted">命中的评论详情尚未提供，请刷新后核对。</p>
        <p v-if="dirty" class="review-muted">逐条复核使用已保存的规则并保留配置草稿；请先保存或放弃设置修改，再核对片段效果。</p>
      </template>
      <details class="review-settings" :open="dirty || !report.enabled"><summary>审查设置<span>{{ dirty ? '有未保存的修改' : '屏蔽词、声音与人工时段' }}</span></summary>
        <div class="review-switches"><n-checkbox v-model:checked="enabled" :disabled="controlsDisabled">自动隐藏命中句的全部字幕并屏蔽命中评论</n-checkbox><n-checkbox v-model:checked="muteAudio" :disabled="controlsDisabled">同时屏蔽命中时段的声音</n-checkbox></div>
        <n-radio-group v-model:value="audioMode" :disabled="controlsDisabled || !muteAudio" aria-label="命中时段声音处理方式" class="review-audio-modes"><n-radio value="voice_only">仅屏蔽语音，保留背景</n-radio><n-radio value="all_audio">整段静音（含背景）</n-radio></n-radio-group>
        <p class="review-muted">屏蔽覆盖原文与译文整句，原声版与配音版均处理声音。仅屏蔽语音会用分离后的背景音替换命中时段；背景文件缺失或不可用时，该时段改为整段静音。整段静音会同时去掉背景音和音乐。关闭静音仍可能保留原片或配音中的相关声音。</p>
        <div class="review-inputs"><label>自定义屏蔽词 · 每行一个<n-input v-model:value="terms" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :disabled="controlsDisabled" placeholder="输入需要屏蔽的词，每行一个" aria-label="自定义屏蔽词" /></label><label>人工屏蔽时段 · 每行一段<n-input v-model:value="ranges" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :disabled="controlsDisabled" placeholder="0-12.5:需核对的画面或台词" aria-label="人工屏蔽时段" /><small>格式：开始秒-结束秒:原因，按原视频时间轴填写；例如 60.5-72:平台指出的片段。时段内全部字幕隐藏，声音按上方模式屏蔽；画面需另行核对与处理。</small></label></div>
        <p class="review-muted">保存作用于本项目。请重新生成字幕和成片后检查效果；已有成片、稿件已选文件和已上传文件不会自动替换。</p>
        <p class="review-muted">后续自动制作会按当前已保存的规则生成。重新检查文字只更新审查报告；已完成文件需重新生成才能应用改动。自动发布遇到选中的失效文件会停止并提示，请先更新成片和选片。</p>
        <n-alert v-if="dirty" type="warning" :show-icon="false">审查设置有未保存的修改，请保存或放弃后再生成成片或上传。</n-alert>
        <div class="review-actions"><n-button type="primary" size="small" :disabled="controlsDisabled || !dirty" :loading="operation === 'save'" @click="save">保存审查设置</n-button><n-button v-if="dirty" size="small" :disabled="isDisabled || !!operation || previewBusy" @click="discard">放弃修改</n-button><n-button v-if="report.inherited === false" size="small" :disabled="controlsDisabled || dirty" :loading="operation === 'reset'" @click="reset">恢复继承</n-button></div>
      </details>
      <details v-if="report.warnings.length" class="review-warnings"><summary>检查范围与提示 · {{ report.warnings.length }}</summary><p v-for="(warning, index) in report.warnings" :key="index">{{ warning }}</p></details>
    </template>
    <div class="review-refresh"><n-button size="small" :disabled="isDisabled || !!operation || previewBusy || dirty || conflict" :loading="operation === 'refresh' || operation === 'read'" @click="refresh">重新检查文字</n-button></div>
  </n-card>
</template>

<style scoped>
.review-audio-modes{display:flex;flex-wrap:wrap;gap:8px 20px;margin:10px 0}
.content-review-panel{margin:18px 0}.review-scope,.review-muted,.review-disabled{font-size:12px;line-height:1.75;color:var(--df-muted);margin:10px 0}.review-scope{margin-top:0}.review-disabled{padding:12px;border:1px dashed var(--df-border);border-radius:8px}.review-coverage{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:14px}.review-coverage>div{display:grid;gap:4px;padding:10px 12px;border:1px solid var(--df-border);border-radius:8px}.review-coverage span{font-size:11px;color:var(--df-muted)}.review-coverage strong{font-size:13px;font-weight:600}.review-coverage .missing strong{color:var(--df-muted);font-size:12px;font-weight:400}.review-meta,.review-summary,.review-switches,.review-actions,.finding-actions,.review-effective{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}.review-meta{font-size:11px;color:var(--df-muted);margin:10px 0}.review-summary{font-size:12px;margin:14px 0}.review-summary strong{color:#bd8122}.review-effective{font-size:12px;background:color-mix(in srgb,var(--df-muted) 6%,transparent);border-radius:8px;padding:12px;margin:12px 0}.review-effective>span,.review-effective small{color:var(--df-muted)}.review-effective details,.review-effective>p{width:100%;margin:0}.review-effective p{color:var(--df-muted);font-size:11px;line-height:1.8}.review-switches{margin:16px 0 8px}.review-inputs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:14px}.review-inputs label{display:grid;gap:8px;align-content:start;font-size:12px}.review-inputs small,.review-finding small,.finding-location span{font-size:11px;color:var(--df-muted);line-height:1.7}.review-actions{margin:14px 0}.review-findings,.review-settings,.review-warnings{margin-top:16px}summary{font-size:12px;cursor:pointer;line-height:1.8}.review-settings{border-top:1px solid var(--df-border);padding-top:14px}.review-settings>summary{font-weight:600}.review-settings>summary span{font-weight:400;color:var(--df-muted);margin-left:14px}.review-list{max-height:520px;overflow:auto;display:grid;gap:10px;margin-top:10px;padding-right:2px}.review-finding{border:1px solid var(--df-border);border-left:3px solid #c99a42;border-radius:8px;padding:12px 14px;min-width:0}.review-finding.allowed{border-left-color:var(--df-muted)}.review-finding.confirmed{border-left-color:#d56868}.review-finding header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;font-size:12px}.finding-location{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.review-finding p{font-size:12px;line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.review-finding b{display:inline-block;color:var(--df-muted);font-weight:400;margin-right:10px}.finding-fields{color:var(--df-muted);font-size:11px!important}.finding-text{padding:3px 0 8px}.finding-context{border-top:1px dashed var(--df-border);padding-top:8px;margin-top:4px}.finding-context>div{padding:8px 10px;margin:6px 0;border-radius:5px;background:color-mix(in srgb,var(--df-muted) 4%,transparent)}.finding-context>div.hit{border-left:2px solid #c99a42}.finding-context p{margin-top:4px}.finding-effect{font-size:11px!important;color:var(--df-muted)}.finding-actions{margin-top:12px;gap:6px}.finding-note{margin-top:8px;color:var(--df-muted)}.finding-note summary{font-size:11px}.finding-note :deep(.n-input){margin-top:7px;max-width:500px}.review-warnings p{font-size:12px;line-height:1.75;color:var(--df-muted);margin:6px 0}.review-refresh{display:flex;justify-content:flex-end;margin-top:14px}.content-review-panel :deep(.n-alert){margin:10px 0}.content-review-panel :deep(.n-alert .n-button){margin:6px 0 0 8px}@media(max-width:800px){.review-inputs{grid-template-columns:1fr}.review-coverage{grid-template-columns:repeat(2,minmax(0,1fr))}.review-finding{padding:12px}.review-meta{gap:4px 10px}}
</style>
