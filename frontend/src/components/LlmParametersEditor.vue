<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NInputNumber, NSelect, NSpin } from 'naive-ui'
import ParameterHelp from './ParameterHelp.vue'
import {
  llmParameterHelp, llmParameterOptions, llmParameterReference,
  llmParameterShapeError, llmParameterValue, parseLlmParams, writeLlmParameter,
  type LlmParameterPath,
} from '../utils/llmParameters'

const props = withDefaults(defineProps<{
  modelValue: string; model?: string; baseUrl?: string; disabled?: boolean
}>(), { model: '', baseUrl: '', disabled: false })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const parsed = computed(() => parseLlmParams(props.modelValue))
const commonFields: { path: LlmParameterPath; label: string; number?: boolean }[] = [
  { path: 'temperature', label: '温度', number: true },
  { path: 'top_p', label: '累计采样概率', number: true },
  { path: 'max_tokens', label: '输出 token 上限', number: true },
  { path: 'reasoning_effort', label: '思考强度' },
]
const extraFields: { path: LlmParameterPath; label: string }[] = [
  { path: 'thinking.type', label: '思考开关' },
  { path: 'thinking.clear_thinking', label: '清除历史思考' },
  { path: 'do_sample', label: '随机采样' },
  { path: 'stream', label: '流式响应' },
  { path: 'tool_stream', label: '工具调用流式输出' },
]
const allFields = [...commonFields, ...extraFields]
const advancedOpen = ref(!parsed.value.valid)
const previewOpen = ref(false)
const previewMode = ref<'profile' | 'split' | 'translate'>('profile')
const preview = ref<any>(null), previewLoading = ref(false), previewError = ref('')
const modeOptions = [
  { label: '此配置的基础请求', value: 'profile' },
  { label: '语义整理（全局设置）', value: 'split' },
  { label: '翻译（全局设置）', value: 'translate' },
]
const configuredCount = computed(() => Object.keys(parsed.value.params ?? {}).length)
const shapeWarnings = computed(() => [...new Set(allFields.map(field => llmParameterShapeError(parsed.value.params, field.path)).filter(Boolean))])
const previewJson = computed(() => preview.value ? JSON.stringify(preview.value.body, null, 2) : '')

function fieldValue(path: LlmParameterPath): any {
  const value = llmParameterValue(parsed.value.params, path)
  if (['temperature', 'top_p', 'max_tokens'].includes(path)) return typeof value === 'number' ? value : null
  if (typeof value === 'boolean') return String(value)
  return typeof value === 'string' ? value : ''
}
function options(path: LlmParameterPath) {
  const result = llmParameterOptions(path, props.model, parsed.value.params)
  if (path === 'thinking.clear_thinking') return result.map(option => ({ ...option,
    label: option.value === 'true' ? '清除（true）' : option.value === 'false' ? '保留（false）' : option.label }))
  return result
}
function change(path: LlmParameterPath, value: any) {
  if (props.disabled || !parsed.value.valid || llmParameterShapeError(parsed.value.params, path)) return
  const next = writeLlmParameter(props.modelValue, path, value === 'true' ? true : value === 'false' ? false : value)
  if (!next.error && next.json !== props.modelValue) emit('update:modelValue', next.json)
}
function updateJson(value: string) { if (!props.disabled) emit('update:modelValue', value) }
function formatJson() {
  if (props.disabled || !parsed.value.valid) return
  emit('update:modelValue', configuredCount.value ? JSON.stringify(parsed.value.params, null, 2) : '')
}
function samplingNote(path: LlmParameterPath) {
  return ['temperature', 'top_p'].includes(path) && parsed.value.params?.do_sample === false
    ? '已指定 do_sample=false；若接口支持关闭采样，这两项可能不参与采样。当前值保留。' : ''
}
function changeDescription(change: any) {
  const value = (raw: any) => raw === undefined ? '未指定' : JSON.stringify(raw)
  if (change.kind === 'remove') return `${change.path}：${value(change.before)} → 不发送`
  return `${change.path}：${value(change.before)} → ${value(change.after)}`
}

// 仅请求本机预览接口，不携带密钥，也不调用模型。失效旧请求不能覆盖新的草稿。
let previewTimer: ReturnType<typeof setTimeout> | undefined
let previewController: AbortController | undefined
let previewGeneration = 0
function cancelPreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = undefined
  previewController?.abort(); previewController = undefined
  previewGeneration++
  previewLoading.value = false
}
async function loadPreview() {
  cancelPreview()
  preview.value = null; previewError.value = ''
  if (!previewOpen.value) return
  if (!parsed.value.valid) { previewError.value = parsed.value.error; return }
  if (!props.model.trim()) { previewError.value = '填写模型名后可预览请求。'; return }
  const generation = previewGeneration
  const controller = new AbortController()
  previewController = controller; previewLoading.value = true
  const snapshot = { profile: { base_url: props.baseUrl, model: props.model, params: parsed.value.params }, mode: previewMode.value }
  let timedOut = false
  let timeoutTimer: ReturnType<typeof setTimeout> | undefined
  let abortListener: (() => void) | undefined
  const timeoutMessage = '本机请求预览超过 10 秒未响应，请点击“刷新预览”重试。'
  // 同时约束响应正文读取；即使异常 fetch 未响应取消，也让本机预览恢复可重试状态。
  const requestGuard = new Promise<never>((_resolve, reject) => {
    abortListener = () => reject(new DOMException('预览请求已取消', 'AbortError'))
    controller.signal.addEventListener('abort', abortListener, { once: true })
    timeoutTimer = setTimeout(() => { timedOut = true; controller.abort() }, 10000)
  })
  try {
    const { response, result } = await Promise.race([requestGuard, (async () => {
      const response = await fetch('/api/settings/preview_llm', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(snapshot), signal: controller.signal,
      })
      return { response, result: await response.json() }
    })()])
    if (generation !== previewGeneration) return
    if (!response.ok || result.ok === false) throw new Error(result.message || result.detail || '预览请求失败')
    if (!result.body || typeof result.body !== 'object' || Array.isArray(result.body)) throw new Error('预览结果缺少请求 JSON，请刷新后重试。')
    preview.value = result
  } catch (error: any) {
    if (generation === previewGeneration && (timedOut || error?.name !== 'AbortError'))
      previewError.value = timedOut ? timeoutMessage : error?.message || '预览暂时不可用，请重试。'
  } finally {
    if (timeoutTimer) clearTimeout(timeoutTimer)
    if (abortListener) controller.signal.removeEventListener('abort', abortListener)
    if (generation === previewGeneration) { previewLoading.value = false; previewController = undefined }
  }
}
function schedulePreview() {
  cancelPreview(); preview.value = null; previewError.value = ''
  if (!previewOpen.value) return
  if (!parsed.value.valid) { previewError.value = parsed.value.error; return }
  previewLoading.value = true
  previewTimer = setTimeout(() => { previewTimer = undefined; void loadPreview() }, 300)
}
function togglePreview() { previewOpen.value = !previewOpen.value; if (previewOpen.value) void loadPreview(); else cancelPreview() }
watch(() => [props.modelValue, props.model, props.baseUrl, previewMode.value], schedulePreview)
watch(() => parsed.value.valid, valid => { if (!valid) advancedOpen.value = true })
onUnmounted(cancelPreview)
</script>

<template>
  <div class="llm-parameters-editor">
    <div class="parameters-intro">这里提供参数用途与通用建议；具体默认值、范围和支持项请查服务商文档。选项和 JSON 共用一份配置，任务的实际覆盖可在下方预览。</div>
    <n-alert v-if="!parsed.valid" type="error" :show-icon="false" class="parameters-alert">{{ parsed.error }}</n-alert>
    <n-alert v-else-if="shapeWarnings.length" type="warning" :show-icon="false" class="parameters-alert">
      <div v-for="warning in shapeWarnings" :key="warning">{{ warning }}</div>
    </n-alert>
    <div class="parameters-grid df-settings-grid">
      <div v-for="field in commonFields" :key="field.path" class="parameter-field df-setting">
        <ParameterHelp :label="field.label" :help="llmParameterHelp(field.path, model)">
          <div class="parameter-label">{{ field.label }} <code>{{ field.path }}</code></div>
          <n-input-number v-if="field.number" :value="fieldValue(field.path)" size="small" clearable
                          :min="field.path === 'max_tokens' ? 1 : 0" :precision="field.path === 'max_tokens' ? 0 : undefined"
                          :step="field.path === 'max_tokens' ? 1024 : 0.05" placeholder="不指定，使用接口默认"
                          :aria-label="field.path" :disabled="disabled || !parsed.valid || !!llmParameterShapeError(parsed.params, field.path)"
                          @update:value="change(field.path, $event)" />
          <n-select v-else :value="fieldValue(field.path)" :options="options(field.path)" size="small"
                    :aria-label="field.path" :disabled="disabled || !parsed.valid || !!llmParameterShapeError(parsed.params, field.path)"
                    @update:value="change(field.path, $event)" />
          <div class="parameter-reference">{{ llmParameterReference(field.path, model) }}</div>
          <div v-if="samplingNote(field.path)" class="parameter-reference parameter-warning">{{ samplingNote(field.path) }}</div>
        </ParameterHelp>
      </div>
    </div>
    <details class="parameters-details">
      <summary>思考与流式选项 <span>按接口支持情况选填</span></summary>
      <div class="parameters-grid extra-grid df-settings-grid">
        <div v-for="field in extraFields" :key="field.path" class="parameter-field df-setting">
          <ParameterHelp :label="field.label" :help="llmParameterHelp(field.path, model)">
            <div class="parameter-label">{{ field.label }} <code>{{ field.path }}</code></div>
            <n-select :value="fieldValue(field.path)" :options="options(field.path)" size="small"
                      :aria-label="field.path" :disabled="disabled || !parsed.valid || !!llmParameterShapeError(parsed.params, field.path)"
                      @update:value="change(field.path, $event)" />
            <div class="parameter-reference">{{ llmParameterReference(field.path, model) }}</div>
          </ParameterHelp>
        </div>
      </div>
    </details>
    <div class="parameters-toolbar">
      <n-button size="small" quaternary :aria-expanded="advancedOpen" @click="advancedOpen = !advancedOpen">
        {{ advancedOpen ? '收起' : '编辑' }}高级 JSON <span class="parameters-count">{{ configuredCount }} 项</span>
      </n-button>
      <n-button size="small" secondary :aria-expanded="previewOpen" @click="togglePreview">
        {{ previewOpen ? '收起请求预览' : '预览实际请求 JSON' }}
      </n-button>
    </div>
    <div v-if="advancedOpen" class="parameters-advanced">
      <div class="parameters-subhead"><span>自定义参数 JSON</span>
        <n-button size="tiny" quaternary :disabled="disabled || !parsed.valid" @click="formatJson">格式化</n-button>
      </div>
      <n-input :value="modelValue" type="textarea" size="small" :autosize="{ minRows: 4, maxRows: 15 }"
               :disabled="disabled" aria-label="自定义参数 JSON" placeholder='留空不添加自定义参数；例如 {"temperature": 1}'
               @update:value="updateJson" />
      <div class="parameter-reference">未列出的参数仍可在这里编辑；常用选项只修改对应字段，保留其他内容。</div>
    </div>
    <div v-if="previewOpen" class="parameters-preview">
      <div class="parameters-preview-head">
        <n-select v-model:value="previewMode" :options="modeOptions" size="small" aria-label="请求预览场景" class="preview-mode" />
        <n-button size="small" quaternary :disabled="previewLoading || !parsed.valid" @click="loadPreview">刷新预览</n-button>
      </div>
      <div class="parameter-reference">仅在本机组装请求，不发送给模型、不保存配置。语义整理 / 翻译预览使用当前全局步骤设置，项目可另有覆盖。</div>
      <div v-if="previewLoading" class="preview-loading"><n-spin size="small" /> 正在组装请求…</div>
      <n-alert v-else-if="previewError" type="warning" :show-icon="false">{{ previewError }}</n-alert>
      <template v-else-if="preview">
        <div v-for="note in preview.notes || []" :key="note" class="preview-note">{{ note }}</div>
        <div v-if="preview.changes?.length" class="preview-changes">
          <div class="parameters-subhead">配置到实际请求的变化</div>
          <div v-for="(changeItem, index) in preview.changes" :key="index" class="preview-change">{{ changeDescription(changeItem) }}</div>
        </div>
        <div v-if="preview.redacted_paths?.length" class="parameter-reference">预览已隐藏可能含敏感信息的字段：{{ preview.redacted_paths.join('、') }}</div>
        <pre class="request-json" aria-label="实际请求 JSON">{{ previewJson }}</pre>
      </template>
    </div>
  </div>
</template>

<style scoped>
.llm-parameters-editor{display:grid;gap:12px;min-width:0}.parameters-intro,.parameter-reference{font-size:12px;color:var(--df-muted);line-height:1.65}.parameters-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 22px}.parameter-field{min-width:0}.parameter-label{font-size:12px;font-weight:600;display:flex;flex-wrap:wrap;gap:4px 9px;align-items:baseline}.parameter-label code{font-weight:400;color:var(--df-muted);font-size:11px}.parameter-reference{margin-top:-2px}.parameter-warning{color:var(--df-warning,#bd8220)}.parameters-details{border-top:1px solid var(--df-border);padding-top:11px}.parameters-details summary{font-size:12px;font-weight:600;cursor:pointer;line-height:1.8}.parameters-details summary span{font-weight:400;color:var(--df-muted);margin-left:10px}.extra-grid{margin-top:14px}.parameters-toolbar{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}.parameters-count{font-size:11px;color:var(--df-muted);margin-left:7px}.parameters-advanced,.parameters-preview{display:grid;gap:10px;padding:13px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-surface-alt,rgba(128,128,128,.035));min-width:0}.parameters-subhead{display:flex;align-items:center;justify-content:space-between;font-size:12px;font-weight:600;gap:12px}.parameters-preview-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.preview-mode{width:min(280px,100%)}.preview-loading{font-size:12px;color:var(--df-muted);display:flex;align-items:center;gap:10px;padding:10px}.preview-note{font-size:12px;line-height:1.7;color:var(--df-muted)}.preview-changes{display:grid;gap:6px}.preview-change{font-size:12px;line-height:1.6;overflow-wrap:anywhere}.request-json{margin:0;padding:14px;font-size:11px;line-height:1.65;overflow:auto;max-height:370px;white-space:pre;border-radius:6px;background:var(--df-code-bg,rgba(128,128,128,.07));border:1px solid var(--df-border)}.parameters-alert{font-size:12px}@media(max-width:620px){.parameters-grid{grid-template-columns:1fr}.parameters-details summary span{display:block;margin-left:16px}.parameter-label code{overflow-wrap:anywhere}.parameters-preview,.parameters-advanced{padding:10px}}
</style>
