<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { createReferenceAudioPreview, type ReferencePreviewState } from '../utils/referenceAudioPreview'

const props = defineProps<{ path?: string; label: string }>()
const state = ref<ReferencePreviewState>('idle'), error = ref('')
const active = computed(() => state.value !== 'idle')
const disabled = computed(() => !props.path?.trim() || props.path.includes('\0'))
const title = computed(() => active.value ? '停止试听'
  : disabled.value ? '选择参考音频文件后可试听' : `试听${props.label}（完整文件）`)
const preview = createReferenceAudioPreview({ state: value => { state.value = value }, error: value => { error.value = value } })
function togglePreview() {
  if (active.value) { preview.stop(); return }
  if (!disabled.value) void preview.playFile(props.path!)
}
// 试听是只读操作，与该路径设置当前是否适用、能否修改无关。
watch(() => props.path, () => { preview.stop(); error.value = '' }, { flush: 'sync' })
onUnmounted(() => preview.stop())
</script>

<template>
  <span class="reference-file-preview">
    <n-button class="reference-preview" size="small" quaternary :type="active ? 'primary' : 'default'"
              :disabled="!active && disabled" :title="title" :aria-busy="state === 'loading'"
              :aria-pressed="active" :aria-label="`${active ? '停止' : '试听'}${label}`" @click="togglePreview">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false">
        <circle v-if="state === 'loading'" class="preview-spinner" cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="28 16" />
        <rect v-else-if="active" x="5" y="5" width="10" height="10" rx="1" fill="currentColor" />
        <path v-else d="M6 3.5 16 10 6 16.5Z" fill="currentColor" />
      </svg>
    </n-button>
    <small v-if="error" class="reference-error" role="alert">{{ error }}</small>
  </span>
</template>

<style scoped>
.reference-file-preview { display:contents; }
.reference-preview { flex:none; width:32px; padding-inline:0; }
.reference-error { flex-basis:100%; order:1; min-width:0; overflow-wrap:anywhere; line-height:1.5; font-size:11px; color:var(--df-warning, #edba68); }
.preview-spinner { transform-origin:10px 10px; animation:reference-spin .8s linear infinite; }
@keyframes reference-spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion:reduce) { .preview-spinner { animation:none; } }
</style>
