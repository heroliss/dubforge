<script setup lang="ts">
import { computed } from 'vue'
import { sourcePresentation, type ConfigurationSource } from '../utils/configurationSource'
const props = defineProps<{ source?: ConfigurationSource; inherited?: boolean }>()
const presentation = computed(() => sourcePresentation(props.source, props.inherited))
</script>

<template>
  <span class="configuration-source" :data-source-kind="presentation.kind" role="note"
    :title="presentation.title" :aria-label="presentation.title"
    :style="{ color: presentation.color, backgroundColor: presentation.background }">
    <span class="source-dot" aria-hidden="true"/>{{ presentation.label }}
  </span>
</template>

<style scoped>
.configuration-source{display:inline-flex;align-items:center;gap:5px;max-width:100%;flex-shrink:0;vertical-align:middle;white-space:nowrap;border:1px solid transparent;border-radius:5px;padding:1px 6px;font-size:11px;font-weight:400;line-height:18px;letter-spacing:0;cursor:help}
.source-dot{width:4px;height:4px;border-radius:50%;background:currentColor;flex-shrink:0}
@media(forced-colors:active){.configuration-source{border-color:CanvasText;color:CanvasText!important;background:Canvas!important}}
</style>
