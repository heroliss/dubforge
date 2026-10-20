<script setup lang="ts">
import { NButton } from 'naive-ui'
import { openLlmConversations } from '../utils/llmDialog'
defineProps<{ projectUid?: string; operation?: string; ownerId?: string; prominent?: boolean }>()
</script>
<template>
  <n-button size="small" :secondary="prominent" :quaternary="!prominent" @click.stop="openLlmConversations({ projectUid, operation, ownerId })">
    <template #icon><svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 18H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1h-9l-5 3v-3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M7 9h10M7 13h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></template>
    查看LLM对话
  </n-button>
</template>
