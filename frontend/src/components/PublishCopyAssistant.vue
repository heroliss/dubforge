<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NTag } from 'naive-ui'

const props = defineProps<{ publicationId?: string; projectUid?: string; revision: number;
  fingerprint?: string; contentFingerprint?: string; values: any; disabled?: boolean }>()
const emit = defineEmits<{ apply: [values: Record<string, any>]; 'busy-change': [busy: boolean] }>()
const labels: Record<string, string> = { title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态' }
const instruction = ref(''), busy = ref(false), error = ref(''), suggestion = ref<any>(null), base = ref(''), applied = ref(false)
const selected = ref(Object.keys(labels))
const copyValues = computed(() => Object.fromEntries(Object.keys(labels).map(key => [key, props.values[key] || (key === 'tags' ? [] : '')])))
const context = computed(() => props.projectUid ? `project:${props.projectUid}` : `publication:${props.publicationId || ''}`)
const signature = computed(() => JSON.stringify([context.value, props.revision, props.fingerprint,
  props.contentFingerprint, copyValues.value]))
const stale = computed(() => !!suggestion.value && signature.value !== base.value)
let request: AbortController | undefined, disposed = false, generation = 0
watch(context, () => {
  generation++; request?.abort(); request = undefined; busy.value = false
  suggestion.value = null; error.value = ''; applied.value = false; instruction.value = ''
}, { flush: 'sync' })
watch(() => [props.revision, props.fingerprint], () => {
  // 已应用候选在保存后不再显示“尚未保存”；未应用候选保留过期提示。
  if (applied.value) { suggestion.value = null; applied.value = false; error.value = '' }
}, { flush: 'sync' })
watch(busy, value => emit('busy-change', value), { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; generation++; request?.abort(); busy.value = false })

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
        ...(props.projectUid ? { fingerprint: props.fingerprint, content_fingerprint: props.contentFingerprint } : {}) }),
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(300000)]),
    })
    const data = await response.json()
    if (disposed || ticket !== generation || controller.signal.aborted) return
    if (!response.ok) throw new Error(data.message || 'AI 文案生成失败，请重试')
    if (initial !== signature.value || props.disabled) {
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
</script>

<template>
  <section class="copy-assistant" aria-label="AI 文案助手">
    <div class="copy-heading"><div><strong>AI 文案助手</strong><small title="使用已配置的 LLM，仅发送视频资料、字幕节选与现有文案；不发送账号凭据，不决定发布权限或创作声明。">标题 · 简介 · 标签 · 动态 ⓘ</small></div><div class="copy-heading-actions"><n-button v-if="busy" size="small" @click="cancel">取消等待</n-button><n-button size="small" secondary type="primary" :loading="busy" :disabled="disabled || busy" @click="generate">{{ suggestion ? '重新生成候选' : '生成候选文案' }}</n-button></div></div>
    <details class="copy-requirements"><summary>文案偏好（可选）</summary><n-input v-model:value="instruction" :input-props="{'aria-label':'AI 文案偏好'}" :disabled="busy || disabled" type="textarea" :autosize="{minRows:2,maxRows:4}" maxlength="1000" show-count placeholder="例如：自然活泼，突出当地美食体验，避免夸张标题。" /></details>
    <n-alert v-if="error" type="warning" class="copy-notice">{{ error }}</n-alert>
    <div v-if="suggestion" class="copy-candidates">
      <p class="copy-basis">依据：{{ suggestion.basis.join('、') }} · 候选尚未保存</p>
      <p v-for="warning in suggestion.warnings" :key="warning" class="copy-basis">{{ warning }}</p>
      <div v-for="(label, key) in labels" :key="key" class="copy-candidate">
        <n-checkbox :checked="selected.includes(key)" :disabled="disabled || busy || stale || applied" @update:checked="checked => selected = checked ? [...selected, key] : selected.filter(k => k !== key)">{{ label }}</n-checkbox>
        <div class="copy-comparison"><div><small>当前文案</small><p class="copy-text">{{ textValue(key, copyValues[key]) }}</p></div><div><small>AI 候选</small><div v-if="key === 'tags'" class="copy-tags"><n-tag v-for="tag in suggestion.values.tags" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div><p v-else class="copy-text">{{ suggestion.values[key] || '（留空）' }}</p></div></div>
      </div>
      <p v-if="applied" role="status" class="copy-basis">{{ projectUid ? '已填入项目编辑区，保存后生效；未创建草稿或投稿。' : '已填入编辑区，尚未保存或投稿。' }}</p>
      <p v-else-if="stale" role="status" class="copy-basis">资料已修改，请重新生成以免覆盖新内容。</p>
      <div v-else class="copy-actions"><n-button size="small" :disabled="disabled || busy || !selected.length" @click="apply(true)">只填空缺</n-button><n-button size="small" type="primary" secondary :disabled="disabled || busy || !selected.length" @click="apply(false)">应用所选文案</n-button></div>
    </div>
  </section>
</template>

<style scoped>
.copy-assistant { border-top:1px solid var(--df-border); padding:18px 0 0; margin:20px 0 16px; }
.copy-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }.copy-heading strong { font-size:13px; }.copy-heading small { display:block; color:var(--df-muted); margin-top:5px; font-size:11px; }.copy-heading-actions { display:flex; gap:8px; }
.copy-requirements { margin-top:12px; }.copy-requirements summary { cursor:pointer; color:var(--df-muted); font-size:11px; margin-bottom:8px; }
.copy-notice { margin-top:12px; }.copy-basis { color:var(--df-muted); font-size:11px; line-height:1.6; }.copy-candidates { border-top:1px solid var(--df-border); margin-top:14px; padding-top:8px; }
.copy-candidate { padding:12px 0; border-bottom:1px solid var(--df-border); }.copy-text { margin:7px 0 0 26px; font-size:13px; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.8; max-height:260px; overflow:auto; }.copy-tags { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 0 26px; }.copy-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; margin-top:14px; }
.copy-comparison { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:10px 0 0 26px; }.copy-comparison>div { min-width:0; }.copy-comparison small { color:var(--df-muted); font-size:11px; }.copy-comparison .copy-text,.copy-comparison .copy-tags { margin-left:0; }
@media(max-width:650px) { .copy-comparison { grid-template-columns:1fr; } }
</style>
