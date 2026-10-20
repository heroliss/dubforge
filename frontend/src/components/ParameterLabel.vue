<script setup lang="ts">
import { computed } from 'vue'
import { parameterPresentation } from '../utils/parameterPresentation'
const props = defineProps<{ field: { label: string; path?: string } }>()
const presentation = computed(() => parameterPresentation(props.field))
</script>

<template>
  <span class="parameter-heading">
    <span class="parameter-heading-title" :title="field.label">{{ presentation.title }}</span>
    <span class="parameter-heading-meta"><slot /></span>
  </span>
</template>

<style scoped>
.parameter-heading { display:flex; align-items:center; flex-wrap:wrap; gap:4px 8px; min-width:0; line-height:1.5; }
.parameter-heading-title { min-width:0; overflow-wrap:anywhere; }
.parameter-heading-meta { display:inline-flex; align-items:center; gap:6px; flex-shrink:0; }
.parameter-heading-meta:empty { display:none; }
</style>
