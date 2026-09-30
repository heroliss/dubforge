<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NTag } from 'naive-ui'

const props = defineProps<{ publicationId: string; revision: number; values: any; disabled?: boolean }>()
const emit = defineEmits<{ apply: [values: Record<string, any>] }>()
const labels: Record<string, string> = { title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态' }
const instruction = ref(''), busy = ref(false), error = ref(''), suggestion = ref<any>(null), base = ref(''), applied = ref(false)
const selected = ref(Object.keys(labels))
const copyValues = computed(() => Object.fromEntries(Object.keys(labels).map(key => [key, props.values[key] || (key === 'tags' ? [] : '')])))
const signature = computed(() => JSON.stringify([props.publicationId, props.revision, copyValues.value]))
const stale = computed(() => !!suggestion.value && signature.value !== base.value)
let request: AbortController | undefined, disposed = false
watch(() => props.publicationId, () => { request?.abort(); suggestion.value = null; error.value = ''; applied.value = false; instruction.value = '' })
onBeforeUnmount(() => { disposed = true; request?.abort() })

async function generate() {
  if (busy.value || props.disabled) return
  const initial = signature.value
  request = new AbortController()
  busy.value = true; error.value = ''; applied.value = false; suggestion.value = null
  try {
    const response = await fetch(`/api/publishing/publications/${props.publicationId}/suggest`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: props.revision, current: copyValues.value, instruction: instruction.value }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(300000)]),
    })
    const data = await response.json()
    if (disposed || request.signal.aborted) return
    if (!response.ok) throw new Error(data.message || 'AI 文案生成失败，请重试')
    if (initial !== signature.value || props.disabled) { error.value = '生成期间稿件已变化，候选文案未应用，请重新生成。'; return }
    base.value = initial; selected.value = Object.keys(labels); suggestion.value = data
  } catch (e: any) {
    if (!disposed && !request.signal.aborted) error.value = e.name === 'TimeoutError' ? 'AI 响应超时，请稍后重试；现有资料未修改。' : e.message || 'AI 文案生成失败'
  } finally { busy.value = false }
}
function apply(emptyOnly = false) {
  if (!suggestion.value || stale.value || props.disabled || applied.value) return
  const values = Object.fromEntries(selected.value.filter(key => !emptyOnly || !copyValues.value[key]?.length)
    .map(key => [key, suggestion.value.values[key]]))
  if (!Object.keys(values).length) { error.value = '所选字段没有空缺，可选择“应用所选文案”替换。'; return }
  emit('apply', values); applied.value = true; error.value = ''
}
</script>

<template>
  <section class="copy-assistant" aria-label="AI 文案助手">
    <div class="copy-heading"><div><strong>AI 文案助手</strong><small title="使用已配置的 LLM，仅发送视频资料、字幕节选与现有文案；不发送账号凭据，不决定发布权限或创作声明。">标题 · 简介 · 标签 · 动态 ⓘ</small></div><n-button size="small" secondary type="primary" :loading="busy" :disabled="disabled || busy" @click="generate">{{ suggestion ? '重新生成候选' : '生成候选文案' }}</n-button></div>
    <details class="copy-requirements"><summary>文案偏好（可选）</summary><n-input v-model:value="instruction" :input-props="{'aria-label':'AI 文案偏好'}" :disabled="busy || disabled" type="textarea" :autosize="{minRows:2,maxRows:4}" maxlength="1000" show-count placeholder="例如：自然活泼，突出当地美食体验，避免夸张标题。" /></details>
    <n-alert v-if="error" type="warning" class="copy-notice">{{ error }}</n-alert>
    <div v-if="suggestion" class="copy-candidates">
      <p class="copy-basis">依据：{{ suggestion.basis.join('、') }} · 候选尚未保存</p>
      <p v-for="warning in suggestion.warnings" :key="warning" class="copy-basis">{{ warning }}</p>
      <div v-for="(label, key) in labels" :key="key" class="copy-candidate">
        <n-checkbox :checked="selected.includes(key)" :disabled="disabled || stale || applied" @update:checked="checked => selected = checked ? [...selected, key] : selected.filter(k => k !== key)">{{ label }}</n-checkbox>
        <div v-if="key === 'tags'" class="copy-tags"><n-tag v-for="tag in suggestion.values.tags" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div><p v-else class="copy-text">{{ suggestion.values[key] || '（留空）' }}</p>
      </div>
      <p v-if="applied" role="status" class="copy-basis">已填入编辑区，尚未保存或投稿。</p>
      <p v-else-if="stale" role="status" class="copy-basis">资料已修改，请重新生成以免覆盖新内容。</p>
      <div v-else class="copy-actions"><n-button size="small" :disabled="disabled || !selected.length" @click="apply(true)">只填空缺</n-button><n-button size="small" type="primary" secondary :disabled="disabled || !selected.length" @click="apply(false)">应用所选文案</n-button></div>
    </div>
  </section>
</template>

<style scoped>
.copy-assistant { border:1px solid var(--df-border); border-radius:8px; padding:16px; margin:16px 0 24px; background:var(--df-accent-soft); }
.copy-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }.copy-heading strong { font-size:13px; }.copy-heading small { display:block; color:var(--df-muted); margin-top:5px; font-size:11px; }
.copy-requirements { margin-top:12px; }.copy-requirements summary { cursor:pointer; color:var(--df-muted); font-size:11px; margin-bottom:8px; }
.copy-notice { margin-top:12px; }.copy-basis { color:var(--df-muted); font-size:11px; line-height:1.6; }.copy-candidates { border-top:1px solid var(--df-border); margin-top:14px; padding-top:8px; }
.copy-candidate { padding:12px 0; border-bottom:1px solid var(--df-border); }.copy-text { margin:7px 0 0 26px; font-size:13px; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.8; max-height:260px; overflow:auto; }.copy-tags { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 0 26px; }.copy-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; margin-top:14px; }
</style>
