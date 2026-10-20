<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { NButton, NPopover } from 'naive-ui'

const props = withDefaults(defineProps<{ help?: string; label: string; showIcon?: boolean }>(), { showIcon: true })
const hovered = ref(false), focused = ref(false), requested = ref<boolean | null>(null)
const contentId = useId()
const hasHelp = computed(() => !!props.help?.trim())
const shown = computed(() => hasHelp.value && (requested.value ?? (hovered.value || focused.value)))

function updateHover(value: boolean) {
  const entering = value && !hovered.value
  hovered.value = value
  // 本次关闭保持到移出；再次进入设置区时重新允许悬停打开。
  if (entering && requested.value === false) requested.value = null
}
function focus() { focused.value = true; requested.value = null }
function blur(event: FocusEvent) {
  // 在同一设置行内移动焦点时继续显示；移出设置行才关闭点击打开的说明。
  if ((event.currentTarget as HTMLElement)?.contains(event.relatedTarget as Node | null)) return
  focused.value = false; requested.value = null
}
function toggle() { requested.value = requested.value === true ? false : true }
function close() { hovered.value = false; focused.value = false; requested.value = false }
watch(hasHelp, value => { if (!value) close() })
</script>

<template>
  <n-popover v-if="hasHelp" trigger="hover" :delay="350" :show="shown" placement="top-start"
             style="max-width:min(420px,calc(100vw - 32px))" @update:show="updateHover">
    <template #trigger>
      <div class="parameter-help" @focusin="focus" @focusout="blur" @keydown.esc.stop="close">
        <div class="parameter-help-target"><slot /></div>
        <n-button v-if="showIcon" text size="tiny" class="parameter-help-trigger"
                  :aria-label="`${label}说明`" :aria-expanded="shown" :aria-describedby="shown?contentId:undefined"
                  @click.stop="toggle">?</n-button>
      </div>
    </template>
    <div :id="contentId" role="tooltip" class="parameter-help-content">{{ help }}</div>
  </n-popover>
  <div v-else class="parameter-help-target"><slot /></div>
</template>

<style scoped>
.parameter-help{display:flex;align-items:flex-start;align-self:start;gap:8px;width:100%;min-width:0}.parameter-help-target{display:grid;grid-auto-rows:max-content;align-content:start;align-items:start;gap:9px;flex:1;min-width:0}.parameter-help-trigger{color:var(--df-muted);flex-shrink:0;width:18px;height:18px;margin-top:0;border:1px solid var(--df-border);border-radius:50%}.parameter-help-content{white-space:pre-line;font-size:12px;line-height:1.75;max-height:min(560px,65vh);overflow-y:auto;overscroll-behavior:contain}
</style>
