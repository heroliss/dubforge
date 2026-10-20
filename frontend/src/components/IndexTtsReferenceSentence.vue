<script setup lang="ts">
// 原声参考按源稿句子选择，避免把译文时间或自适应排布误当成原声切片时间。
import { computed, h, onUnmounted, ref, watch } from 'vue'
import { NButton, NInputNumber, NSelect } from 'naive-ui'
import { loadReferenceSentences, referenceSentenceOptions, type ReferenceSentence } from '../utils/indexTtsReferenceSentence'
import { createReferenceAudioPreview, type ReferencePreviewState } from '../utils/referenceAudioPreview'

const props = withDefaults(defineProps<{ projectUid?: string; modelValue?: number; disabled?: boolean; followCurrent?: boolean;
  previewEnabled?: boolean; maxSeconds?: number; label: string }>(), { previewEnabled: undefined, maxSeconds: 15 })
const emit = defineEmits<{ (e: 'update:modelValue', value: number | null): void }>()
const lines = ref<ReferenceSentence[]>([]), loading = ref(false), loaded = ref(false), error = ref('')
const value = computed(() => props.modelValue ?? 1)
const options = computed(() => referenceSentenceOptions(lines.value, value.value))
const missing = computed(() => loaded.value && !error.value && !lines.value.some(line => line.id === value.value))
const selectedLine = computed(() => lines.value.find(line => line.id === value.value))
const previewState = ref<ReferencePreviewState>('idle'), previewError = ref('')
const previewActive = computed(() => previewState.value !== 'idle')
const previewAllowed = computed(() => props.previewEnabled ?? (!props.disabled || props.followCurrent === true))
const previewDisabled = computed(() => !props.projectUid || !previewAllowed.value || !selectedLine.value || !!error.value)
const previewTitle = computed(() => previewActive.value ? '停止试听'
  : !props.projectUid ? '请在项目内试听所选参考句'
  : !previewAllowed.value ? '当前来源未使用此参考句'
  : !selectedLine.value ? '原文句子就绪后可试听'
  : `试听第 ${value.value} 句分离人声（含前后缓冲）；实际合成参考最多 ${props.maxSeconds ?? 15} 秒，不受试听缓冲影响`)
const preview = createReferenceAudioPreview({ state: state => { previewState.value = state }, error: message => { previewError.value = message } })
function togglePreview() {
  if (previewActive.value) { preview.stop(); return }
  if (!previewDisabled.value) void preview.play(props.projectUid!, value.value, props.maxSeconds ?? 15)
}
watch(() => [props.projectUid, value.value, props.maxSeconds, previewAllowed.value, selectedLine.value?.start, selectedLine.value?.end],
  () => { preview.stop(); previewError.value = '' }, { flush: 'sync' })
function renderLabel(option: { label?: string }) {
  return h('span', { title: option.label, style: { display: 'block', minWidth: 0, maxWidth: '100%',
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, option.label)
}
let generation = 0, disposed = false
async function load() {
  if (!props.projectUid || !previewAllowed.value) return
  const current = ++generation, uid = props.projectUid
  loading.value = true; error.value = ''
  try {
    const result = await loadReferenceSentences(uid)
    if (disposed || current !== generation || uid !== props.projectUid) return
    lines.value = result; loaded.value = true
  } catch (err: any) {
    if (!disposed && current === generation) error.value = err.message || '读取参考句失败'
  } finally { if (current === generation) loading.value = false }
}
watch(() => [props.projectUid, props.disabled, props.followCurrent, previewAllowed.value], ([uid], previous) => {
  if (previous?.[0] !== uid) { generation++; lines.value = []; loaded.value = false; error.value = ''; loading.value = false }
  if (uid && previewAllowed.value && !loaded.value) void load()
}, { immediate: true })
onUnmounted(() => { disposed = true; generation++; preview.stop() })
</script>

<template>
  <div class="reference-sentence-control">
    <div class="reference-sentence-row">
    <n-select v-if="projectUid" :value="value" :options="options" :render-label="renderLabel" filterable :loading="loading" :disabled="disabled"
              :aria-label="label" placeholder="搜索句子编号或原文"
              @update:show="show => { if (show) load() }" @update:value="emit('update:modelValue', $event)" />
    <n-input-number v-else :value="value" :min="1" :precision="0" :disabled="disabled" :input-props="{ 'aria-label': label }"
                    @update:value="emit('update:modelValue', $event)"><template #suffix>句</template></n-input-number>
    <n-button class="reference-preview" size="small" quaternary :type="previewActive ? 'primary' : 'default'"
              :disabled="!previewActive && previewDisabled" :title="previewTitle" :aria-busy="previewState === 'loading'"
              :aria-pressed="previewActive" :aria-label="`${previewActive ? '停止' : '试听'}${label}原声`" @click="togglePreview">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false">
        <circle v-if="previewState === 'loading'" class="preview-spinner" cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="28 16" />
        <rect v-else-if="previewActive" x="5" y="5" width="10" height="10" rx="1" fill="currentColor" />
        <path v-else d="M6 3.5 16 10 6 16.5Z" fill="currentColor" />
      </svg>
    </n-button>
    </div>
    <small v-if="error" class="reference-note reference-error">{{ error }} <n-button size="tiny" text :disabled="disabled" @click="load">重试</n-button></small>
    <small v-else-if="missing && !disabled" class="reference-note reference-error">{{ lines.length ? '源稿中没有此句，请重新选择' : '尚无原文句子，语义整理完成后可选择' }}</small>
    <small v-if="previewError" class="reference-note reference-error" role="alert">{{ previewError }}</small>
  </div>
</template>

<style scoped>
.reference-sentence-control { min-width:0; width:100%; }
.reference-sentence-control :deep(.n-input-number) { width:150px; }
.reference-sentence-row { display:flex; align-items:center; gap:6px; min-width:0; }
.reference-sentence-row :deep(.n-select) { flex:1; min-width:0; }
.reference-preview { flex:none; width:32px; padding-inline:0; }
.preview-spinner { transform-origin:10px 10px; animation:reference-spin .8s linear infinite; }
@keyframes reference-spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion:reduce) { .preview-spinner { animation:none; } }
.reference-note { display:block; margin-top:5px; line-height:1.5; font-size:11px; }
.reference-error { color:var(--df-warning, #edba68); }
</style>
