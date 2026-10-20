<script setup lang="ts">
import { computed } from 'vue'
import { NButton } from 'naive-ui'

const props = withDefaults(defineProps<{ label: string; action?: string; help?: string; disabled?: boolean }>(), {
  action: '恢复继承值', disabled: false,
})
const emit = defineEmits<{ reset: [] }>()
const hint = computed(() => props.help || props.action)
function reset() { if (!props.disabled) emit('reset') }
</script>

<template>
  <span class="parameter-reset-target" :title="hint">
    <n-button class="parameter-reset" size="tiny" quaternary circle :disabled="disabled"
              :aria-label="`${label}${action}`" :title="hint" @click.stop.prevent="reset">
      <svg aria-hidden="true" focusable="false" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 8a6 6 0 1 1 .8 6.1M4 3.5V8h4.5" />
      </svg>
    </n-button>
  </span>
</template>

<style scoped>
.parameter-reset-target{display:inline-flex;align-items:center;flex:0 0 auto;vertical-align:middle}
.parameter-reset{width:24px;height:24px;padding:4px}
</style>
