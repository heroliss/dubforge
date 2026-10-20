<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { NInput } from 'naive-ui'
import { formatTimecode, parseTimecode, timecodeError } from '../utils/timecode'

const props = withDefaults(defineProps<{
  value: number | null; min?: number; max?: number; precision?: number; disabled?: boolean
  size?: 'small' | 'medium' | 'large'; ariaLabel?: string; inputProps?: Record<string, any>
}>(), { min: 0, precision: 3, size: 'small' })
const emit = defineEmits<{ 'update:value': [value: number]; 'invalid-change': [value: boolean] }>()
const id = useId(), editing = ref(false), dirty = ref(false), touched = ref(false)
const displayValue = () => props.value === null ? '' : formatTimecode(props.value, props.precision)
const text = ref(displayValue())
const validation = computed(() => {
  // 媒体末尾可能不是整数毫秒；未编辑的展示舍入不能把合法时间误判越界。
  if (!dirty.value && typeof props.value === 'number' && Number.isFinite(props.value)) {
    if (props.value < props.min) return `时间不能早于 ${formatTimecode(props.min, 3)}。`
    if (props.max != null && Number.isFinite(props.max) && props.value > props.max)
      return `时间不能晚于 ${formatTimecode(props.max, 3)}。`
    return ''
  }
  return timecodeError(text.value, props.min, props.max)
})
const error = computed(() => touched.value ? validation.value : '')
const inputAttributes = computed(() => ({
  ...props.inputProps,
  'aria-label': props.ariaLabel || props.inputProps?.['aria-label'],
  'aria-invalid': !!error.value,
  'aria-describedby': [props.inputProps?.['aria-describedby'], error.value ? `${id}-error` : ''].filter(Boolean).join(' ') || undefined,
}))
watch(validation, value => emit('invalid-change', !!value), { immediate: true })
watch(() => [props.value, props.precision], () => {
  if (!editing.value || !dirty.value) { text.value = displayValue(); dirty.value = false; touched.value = false }
})
function update(value: string) { text.value = value; dirty.value = true }
function commit() {
  touched.value = true
  if (validation.value || !dirty.value) return
  const seconds = parseTimecode(text.value)!
  dirty.value = false
  text.value = formatTimecode(seconds, props.precision)
  if (seconds !== props.value) emit('update:value', seconds)
}
function blur() { editing.value = false; commit() }
function revert() { text.value = displayValue(); dirty.value = false; touched.value = false }
function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key !== 'Enter' && event.key !== 'Escape') return
  event.preventDefault(); event.stopPropagation()
  if (event.key === 'Enter') commit()
  else revert()
}
</script>

<template>
  <div class="timecode-input">
    <n-input :value="text" :disabled="disabled" :size="size" :status="error ? 'error' : undefined"
      :input-props="inputAttributes" placeholder="时:分:秒.毫秒"
      title="支持时:分:秒.毫秒、分:秒或秒数；Enter 确认，Esc 还原。"
      @update:value="update" @focus="editing=true" @blur="blur"
      @keydown="onKeydown" />
    <span v-if="error" :id="`${id}-error`" class="timecode-error" role="alert">{{ error }}</span>
  </div>
</template>

<style scoped>
.timecode-input{min-width:0;width:100%;font-variant-numeric:tabular-nums}
.timecode-input :deep(input){font-variant-numeric:tabular-nums}
.timecode-error{display:block;color:var(--df-danger,#d03050);font-size:12px;line-height:1.5;margin-top:4px}
</style>
