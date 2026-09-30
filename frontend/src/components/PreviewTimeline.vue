<script setup lang="ts">
import { computed, h } from 'vue'
import { NButton, NSlider } from 'naive-ui'
import { adjacentCue, cuePosition, timelineCues, type PreviewCue } from '../utils/previewTimeline'
import { subtitleTimeLabel } from '../utils/subtitleWorkspace'

const props = defineProps<{ value: number; max: number; cues: PreviewCue[]; label: string; kind: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:value': [value: number]; dragstart: []; dragend: [] }>()
const visibleCues = computed(() => timelineCues(props.cues, props.max))
const previous = computed(() => adjacentCue(visibleCues.value, props.value, -1, props.max))
const next = computed(() => adjacentCue(visibleCues.value, props.value, 1, props.max))
function seek(cue?: PreviewCue) {
  if (cue && !props.disabled) emit('update:value', cuePosition(cue, props.max))
}
const marks = computed(() => Object.fromEntries(visibleCues.value.map(cue => [cue.start, () => h('button', {
  type: 'button', class: ['cue-marker', { current: props.value >= cue.start && props.value < cue.end }],
  title: `${subtitleTimeLabel(cue.start)} · ${cue.label}`, 'aria-label': `定位到 ${subtitleTimeLabel(cue.start)} · ${cue.label}`,
  disabled: props.disabled, tabindex: -1,
  // 刻度点击独立定位，不冒泡触发滑块拖动；键盘通过前后按钮逐项访问。
  onMousedown: (event: Event) => event.stopPropagation(), onTouchstart: (event: Event) => event.stopPropagation(),
  onClick: (event: Event) => { event.stopPropagation(); seek(cue) },
}, h('span'))])))
</script>

<template>
  <div class="preview-cue-timeline" role="group" :aria-label="label">
    <n-slider :value="value" :min="0" :max="max || 1" :step=".1" :marks="visibleCues.length ? marks : undefined"
      :disabled="disabled || max <= 0" :format-tooltip="subtitleTimeLabel" :aria-label="label"
      @update:value="emit('update:value', $event)" @dragstart="emit('dragstart')" @dragend="emit('dragend')" />
    <div class="cue-navigation">
      <span>{{ visibleCues.length ? `刻度为${kind}开始点 · 点击定位` : `暂无可预览的${kind}开始点` }}</span>
      <div><n-button size="tiny" quaternary :disabled="disabled || !previous" :aria-label="`上一处${kind}`" @click="seek(previous)">上一处</n-button><n-button size="tiny" quaternary :disabled="disabled || !next" :aria-label="`下一处${kind}`" @click="seek(next)">下一处</n-button></div>
    </div>
  </div>
</template>

<style scoped>
.preview-cue-timeline { min-width:0; }
.preview-cue-timeline :deep(.n-slider--with-mark) { margin:0 0 24px; }
.preview-cue-timeline :deep(.n-slider-dot) { width:2px; height:7px; border:0; border-radius:1px; background:var(--df-accent); }
.preview-cue-timeline :deep(.cue-marker) { display:flex; align-items:flex-start; justify-content:center; width:8px; height:16px; padding:0; border:0; background:transparent; cursor:pointer; }
.preview-cue-timeline :deep(.cue-marker span) { width:2px; height:7px; border-radius:1px; background:var(--df-accent); opacity:.6; transition:height .15s; }
.preview-cue-timeline :deep(.cue-marker:hover span),.preview-cue-timeline :deep(.cue-marker.current span) { height:12px; opacity:1; }
.preview-cue-timeline :deep(.cue-marker:disabled) { cursor:not-allowed; opacity:.4; }
.cue-navigation { display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--df-muted); font-size:11px; }
.cue-navigation > div { display:flex; flex-shrink:0; }
</style>
