<script setup lang="ts">
import { NH1, NText } from 'naive-ui'
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <header class="step-header">
    <span class="page-kicker">制作流程 / 步骤工作区</span>
    <div class="header-row">
      <div class="title-row">
        <n-h1>{{ title }}</n-h1>
        <slot name="status" />
      </div>
      <div class="actions"><slot name="actions" /></div>
    </div>
    <n-text v-if="description" depth="3" class="description">{{ description }}</n-text>
  </header>
</template>

<style scoped>
.header-row, .title-row, .actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.header-row { justify-content: space-between; gap: 12px; }
.title-row { min-width: 0; }
.title-row :deep(h1) { margin: 0; font-size: 26px; line-height: 1.5; }
.description { display: block; font-size: 13px; margin-top: 8px; line-height: 1.8; max-width: 900px; }
.step-header { margin-bottom: 20px; }
.actions { margin-left: auto; }
@media (max-width: 760px) { .header-row { align-items: flex-start; flex-direction: column; } .actions { margin-left: 0; } }
</style>
