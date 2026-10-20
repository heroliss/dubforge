<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NTag } from 'naive-ui'
import LlmConversationButton from './LlmConversationButton.vue'

const props = defineProps<{ publicationId?: string; projectUid?: string; revision: number;
  fingerprint?: string; contentFingerprint?: string; values: any; sourceLinks?: any[]; disabled?: boolean }>()
const emit = defineEmits<{ apply: [values: Record<string, any>]; 'busy-change': [busy: boolean] }>()
const labels: Record<string, string> = { title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态' }
const instruction = ref(''), busy = ref(false), error = ref(''), suggestion = ref<any>(null), base = ref(''), applied = ref(false)
// 候选文案是给用户核对和手动粘贴的结果；复制只写入本机剪贴板，不保存、不投稿，也不触碰远端稿件。
const copyNotice = ref('')
const copyOptions = ref({ include_resource_links: true, include_social_links: true,
  include_unknown_links: false, include_sponsor_links: false,
  include_original_author: true, include_original_published_date: true,
  include_original_video_url: true })
const selected = ref(Object.keys(labels))
const copyValues = computed(() => Object.fromEntries(Object.keys(labels).map(key => [key, props.values[key] || (key === 'tags' ? [] : '')])))
const context = computed(() => props.projectUid ? `project:${props.projectUid}` : `publication:${props.publicationId || ''}`)
const signature = computed(() => JSON.stringify([context.value, props.revision, props.fingerprint,
  props.contentFingerprint, copyValues.value, copyOptions.value, instruction.value]))
const stale = computed(() => !!suggestion.value && signature.value !== base.value)
let request: AbortController | undefined, disposed = false, generation = 0
let copyTimer: ReturnType<typeof setTimeout> | undefined
watch(context, () => {
  generation++; request?.abort(); request = undefined; busy.value = false
  suggestion.value = null; error.value = ''; applied.value = false; instruction.value = ''
}, { flush: 'sync' })
watch(() => [props.revision, props.fingerprint], () => {
  // 已应用候选在保存后不再显示“尚未保存”；未应用候选保留过期提示。
  if (applied.value) { suggestion.value = null; applied.value = false; error.value = '' }
}, { flush: 'sync' })
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; generation++; request?.abort(); busy.value = false; if (copyTimer) clearTimeout(copyTimer) })

function cancel() {
  generation++; request?.abort(); request = undefined; busy.value = false
  error.value = '已停止等待候选；现有文案保持不变。'
}

async function generate() {
  if (busy.value || props.disabled || (!props.projectUid && !props.publicationId)) return
  if (props.projectUid && (!props.fingerprint || !props.contentFingerprint)) {
    error.value = '请先刷新基础资料，再生成候选文案。'; return
  }
  const initial = signature.value
  const ticket = ++generation, controller = new AbortController()
  request = controller
  busy.value = true; error.value = ''; applied.value = false; suggestion.value = null
  try {
    const path = props.projectUid ? `/api/publishing/projects/${encodeURIComponent(props.projectUid)}/suggest`
      : `/api/publishing/publications/${props.publicationId}/suggest`
    const response = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: props.revision, current: copyValues.value, instruction: instruction.value,
        copy_options: copyOptions.value,
        ...(props.projectUid ? { fingerprint: props.fingerprint, content_fingerprint: props.contentFingerprint } : {}) }),
      signal: controller.signal,
    })
    const data = await response.json()
    if (disposed || ticket !== generation || controller.signal.aborted) return
    if (!response.ok) throw new Error(data.message || 'AI 文案生成失败，请重试')
    // 可编辑状态会因本次生成任务暂时锁定，只有资料版本或文案变化才使结果失效。
    if (initial !== signature.value) {
      error.value = `生成期间${props.projectUid ? '项目资料' : '稿件'}已变化，候选文案未应用，请重新生成。`; return
    }
    if (props.projectUid && (data.project_uid !== props.projectUid || data.revision !== props.revision
      || data.fingerprint !== props.fingerprint || data.content_fingerprint !== props.contentFingerprint)) {
      error.value = '候选对应的项目资料版本不一致，未应用，请刷新基础资料后重新生成。'; return
    }
    if (!data.values || !Object.keys(labels).every(key => key === 'tags'
      ? Array.isArray(data.values.tags) && data.values.tags.every((tag: any) => typeof tag === 'string')
      : typeof data.values[key] === 'string') || !Array.isArray(data.basis) || !Array.isArray(data.warnings)) {
      error.value = '候选文案不完整，未应用，请重新生成。'; return
    }
    base.value = initial; selected.value = Object.keys(labels); suggestion.value = data
  } catch (e: any) {
    if (!disposed && ticket === generation && !controller.signal.aborted) error.value = e.name === 'TimeoutError'
      ? 'AI 响应超时，请稍后重试；现有资料未修改。' : e.message || 'AI 文案生成失败'
  } finally { if (ticket === generation) { busy.value = false; request = undefined } }
}
function apply(emptyOnly = false) {
  if (busy.value || !suggestion.value || stale.value || props.disabled || applied.value) return
  const values = Object.fromEntries(selected.value.filter(key => !emptyOnly || !copyValues.value[key]?.length)
    .map(key => [key, suggestion.value.values[key]]))
  if (!Object.keys(values).length) { error.value = '所选字段没有空缺，可选择“应用所选文案”替换。'; return }
  emit('apply', values); applied.value = true; error.value = ''
}
function textValue(key: string, value: any) {
  return key === 'tags' ? (value || []).join('、') || '（留空）' : value || '（留空）'
}

async function writeClipboard(text: string) {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  if (typeof document === 'undefined') throw new Error('当前环境不支持剪贴板')
  const area = document.createElement('textarea')
  area.value = text; area.setAttribute('readonly', 'true')
  area.style.position = 'fixed'; area.style.opacity = '0'
  document.body.appendChild(area); area.select()
  try { if (!document.execCommand('copy')) throw new Error('复制命令未执行') }
  finally { area.remove() }
}

function candidateText(key: string) {
  if (!suggestion.value) return ''
  const value = suggestion.value.values?.[key]
  return key === 'tags' ? (Array.isArray(value) ? value.join('、') : '') : String(value || '')
}

function fullCandidateText() {
  if (!suggestion.value) return ''
  const values = suggestion.value.values || {}
  return [
    `标题：${values.title || ''}`,
    `简介：\n${values.description || ''}`,
    `标签：${Array.isArray(values.tags) ? values.tags.join('、') : ''}`,
    `粉丝动态：\n${values.dynamic || ''}`,
  ].join('\n\n').trim()
}

async function copyCandidate(key: string) {
  const text = key === 'all' ? fullCandidateText() : candidateText(key)
  if (!text.trim()) { error.value = '该项候选内容为空，暂时没有可复制的文字。'; return }
  try {
    await writeClipboard(text)
    copyNotice.value = key
    error.value = ''
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { if (!disposed) copyNotice.value = '' }, 1800)
  } catch { error.value = '复制失败，请手动选择候选文字复制。' }
}
</script>

<template>
  <section class="copy-assistant" aria-label="AI 文案助手">
    <div class="copy-heading"><div><strong>AI 文案助手</strong><small title="使用已配置的 LLM，仅发送视频资料、字幕节选与现有文案；不发送账号凭据，不决定发布权限或创作声明。">标题 · 简介 · 标签 · 动态 ⓘ</small></div><div class="copy-heading-actions"><n-button v-if="busy" size="small" @click="cancel">取消等待</n-button><n-button size="small" secondary type="primary" :loading="busy" :disabled="disabled || busy" @click="generate">{{ suggestion ? '重新生成候选' : '生成候选文案' }}</n-button></div></div>
    <LlmConversationButton :project-uid="projectUid" operation="publish_copy" :owner-id="publicationId" />
    <details class="copy-requirements"><summary>简介来源与生成规则（可选）</summary>
      <div class="copy-option-grid">
        <n-checkbox v-model:checked="copyOptions.include_resource_links" :disabled="busy || disabled">保留文档、项目和代码链接</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_social_links" :disabled="busy || disabled">保留作者社交链接</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_unknown_links" :disabled="busy || disabled">保留未分类链接</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_sponsor_links" :disabled="busy || disabled">保留赞助、推广和联盟链接</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_original_author" :disabled="busy || disabled">保留原作者署名</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_original_published_date" :disabled="busy || disabled">保留原视频发布时间</n-checkbox>
        <n-checkbox v-model:checked="copyOptions.include_original_video_url" :disabled="busy || disabled">保留原视频链接</n-checkbox>
      </div>
      <p class="copy-help">链接和来源信息由程序从原资料中提取并原样追加；模型不能新增或改写网址。赞助选项只影响简介文字，不改变 B 站商业推广或创作声明。</p>
      <div v-if="props.sourceLinks?.length" class="copy-source-evidence">
        <small>已从原简介检测到 {{ props.sourceLinks.length }} 条链接：</small>
        <ul><li v-for="item in props.sourceLinks" :key="item.url"><span>{{ item.label || item.url }}</span>：{{ item.url }}<n-tag size="tiny" :type="item.category==='sponsor'?'warning':item.category==='resource'?'success':'default'">{{ ({resource:'资源',social:'社交',sponsor:'赞助/推广',unknown:'未分类'} as any)[item.category] || item.category || '未分类' }}</n-tag></li></ul>
      </div>
    </details>
    <details class="copy-requirements"><summary>自定义提示词 / 写作要求（可选）</summary>
      <n-input v-model:value="instruction" :input-props="{'aria-label':'AI 自定义提示词'}" :disabled="busy || disabled" type="textarea" :autosize="{minRows:3,maxRows:8}" maxlength="2000" show-count placeholder="可直接写完整要求，例如：简介分为主题、过程、相关资源三段；语气克制；不要使用夸张词。系统仍会保护事实、网址和平台声明。" />
    </details>
    <n-alert v-if="error" type="warning" class="copy-notice">{{ error }}</n-alert>
    <p v-if="busy" class="copy-basis" role="status" aria-live="polite">正在生成候选文案，等待模型返回；完成后先展示候选供核对。</p>
    <div v-if="suggestion" class="copy-candidates">
      <p class="copy-basis">依据：{{ suggestion.basis.join('、') }} · 候选尚未保存</p>
      <p v-for="warning in suggestion.warnings" :key="warning" class="copy-basis">{{ warning }}</p>
      <details v-if="suggestion.evidence?.source_links?.length" class="copy-evidence">
        <summary>已收集的来源链接（{{ suggestion.evidence.source_links.length }} 条）</summary>
        <ul><li v-for="item in suggestion.evidence.source_links" :key="item.url"><strong>{{ item.label || item.url }}</strong>：{{ item.url }}<n-tag v-if="item.sponsor" size="tiny" type="warning">赞助/推广</n-tag></li></ul>
      </details>
      <div v-for="(label, key) in labels" :key="key" class="copy-candidate">
        <div class="copy-candidate-heading"><n-checkbox :checked="selected.includes(key)" :disabled="disabled || busy || stale || applied" @update:checked="checked => selected = checked ? [...selected, key] : selected.filter(k => k !== key)">{{ label }}</n-checkbox><n-button size="tiny" quaternary :disabled="busy || stale || !suggestion.values[key]?.length" @click="copyCandidate(key)">{{ copyNotice === key ? '已复制' : '复制候选' }}</n-button></div>
        <div class="copy-comparison"><div><small>当前文案</small><p class="copy-text">{{ textValue(key, copyValues[key]) }}</p></div><div><small>AI 候选</small><div v-if="key === 'tags'" class="copy-tags"><n-tag v-for="tag in suggestion.values.tags" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div><p v-else class="copy-text">{{ suggestion.values[key] || '（留空）' }}</p></div></div>
      </div>
      <p v-if="applied" role="status" class="copy-basis">{{ projectUid ? '已填入项目编辑区，保存后生效；未创建草稿或投稿。' : '已填入编辑区，尚未保存或投稿。' }}</p>
      <p v-else-if="stale" role="status" class="copy-basis">资料已修改，请重新生成以免覆盖新内容。</p>
      <template v-else><p v-if="disabled" role="status" class="copy-basis">候选已生成；正在核对编辑权限或项目仍有任务，请等待可编辑后再应用。</p><div class="copy-actions"><n-button size="small" quaternary :disabled="busy || stale" @click="copyCandidate('all')">{{ copyNotice === 'all' ? '完整文案已复制' : '复制完整发布文案' }}</n-button><n-button size="small" :disabled="disabled || busy || !selected.length" @click="apply(true)">只填空缺</n-button><n-button size="small" type="primary" secondary :disabled="disabled || busy || !selected.length" @click="apply(false)">应用所选文案</n-button></div></template>
    </div>
  </section>
</template>

<style scoped>
.copy-assistant { border-top:1px solid var(--df-border); padding:18px 0 0; margin:20px 0 16px; }
.copy-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }.copy-heading strong { font-size:13px; }.copy-heading small { display:block; color:var(--df-muted); margin-top:5px; font-size:11px; }.copy-heading-actions { display:flex; gap:8px; }
.copy-requirements { margin-top:12px; }.copy-requirements summary { cursor:pointer; color:var(--df-muted); font-size:11px; margin-bottom:8px; }
.copy-notice { margin-top:12px; }.copy-basis { color:var(--df-muted); font-size:11px; line-height:1.6; }.copy-help { color:var(--df-muted); font-size:11px; line-height:1.6; margin:8px 0 0; }.copy-option-grid { display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; }.copy-candidates { border-top:1px solid var(--df-border); margin-top:14px; padding-top:8px; }
.copy-source-evidence { margin-top:10px; padding:8px 10px; border:1px dashed var(--df-border); border-radius:6px; color:var(--df-muted); font-size:11px; }.copy-source-evidence ul { margin:6px 0 0 18px; padding:0; line-height:1.7; overflow-wrap:anywhere; }.copy-source-evidence li { margin:2px 0; }.copy-source-evidence .n-tag { margin-left:6px; }
.copy-evidence { margin:12px 0; border:1px solid var(--df-border); border-radius:6px; padding:8px 10px; }.copy-evidence summary { cursor:pointer; color:var(--df-muted); font-size:11px; }.copy-evidence ul { margin:8px 0 0 18px; padding:0; font-size:11px; line-height:1.7; overflow-wrap:anywhere; }.copy-evidence li { margin:3px 0; }.copy-evidence .n-tag { margin-left:6px; }
.copy-candidate { padding:12px 0; border-bottom:1px solid var(--df-border); }.copy-candidate-heading { display:flex; align-items:center; justify-content:space-between; gap:10px; }.copy-text { margin:7px 0 0 26px; font-size:13px; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.8; max-height:260px; overflow:auto; }.copy-tags { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 0 26px; }.copy-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; margin-top:14px; }
.copy-comparison { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:10px 0 0 26px; }.copy-comparison>div { min-width:0; }.copy-comparison small { color:var(--df-muted); font-size:11px; }.copy-comparison .copy-text,.copy-comparison .copy-tags { margin-left:0; }
@media(max-width:650px) { .copy-comparison { grid-template-columns:1fr; } }
</style>
