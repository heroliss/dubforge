<script setup lang="ts">
// 自动运行统一标识 (ADR-0076): 全流程 / 手动链 / 手动单步三类运行状态
// 在项目列表、步骤页等处使用同一套 ⚙/⛓/⏸ 图标与 info/warning 配色。
// 优先级: 全流程 > 自动链 > 手动单步 (引擎互斥保证前两者不同时存在)。
import { computed } from 'vue'
import { NTag } from 'naive-ui'
import { WORKFLOW_BUSY } from '../utils/workflow'
import type { ChainInfo } from '../utils/chainStatus'

const props = defineProps<{
  workflowStatus?: string | null
  chain?: ChainInfo | null
  runningLabel?: string | null
}>()

interface Badge { icon: string; text: string; type: 'info' | 'warning' | 'success' }

const badge = computed<Badge | null>(() => {
  const status = props.workflowStatus || ''
  if (WORKFLOW_BUSY.includes(status)) {
    return { icon: '⚙', type: 'info',
             text: props.runningLabel ? `自动流程 · ${props.runningLabel}` : '自动流程运行中' }
  }
  if (status === 'monitoring') {
    return { icon: '⚙', type: 'success', text: '已投稿 · 监测审核中' }
  }
  if (['paused', 'failed', 'attention'].includes(status)) {
    return { icon: '⚙', type: 'warning', text: '自动流程待继续' }
  }
  if (props.chain?.state === 'active') {
    return { icon: '⛓', type: 'info',
             text: props.runningLabel ? `续跑中 · ${props.runningLabel}` : '自动运行中' }
  }
  if (props.chain?.state === 'held') {
    return { icon: '⏸', type: 'warning', text: '自动运行已挂起' }
  }
  if (props.runningLabel) {
    return { icon: '🔄', type: 'info', text: props.runningLabel }
  }
  return null
})
</script>
<template>
  <NTag v-if="badge" size="small" :type="badge.type" :bordered="false"
       :data-run-badge="badge.icon" :title="badge.text">{{ badge.icon }} {{ badge.text }}</NTag>
</template>
