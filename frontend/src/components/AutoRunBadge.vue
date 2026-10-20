<script setup lang="ts">
// 自动运行统一标识 (ADR-0076): 全流程 / 手动链 / 手动单步三类运行状态
// 在项目列表、步骤页等处使用同一套 ⚙/⛓/⏸ 图标与 info/warning 配色。
// 当前运行优先显示；旧完整流程中断另外标注，不覆盖正在进行的制作。
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
  if (props.chain?.state === 'active') {
    return { icon: '⛓', type: 'info',
             text: props.runningLabel ? `制作续跑 · ${props.runningLabel}` : '制作计划执行中' }
  }
  if (props.runningLabel) {
    return { icon: '🔄', type: 'info', text: props.runningLabel }
  }
  if (status === 'monitoring') {
    return { icon: '⚙', type: 'info', text: '已投稿 · 监测审核中' }
  }
  if (props.chain?.state === 'held') {
    return { icon: '⏸', type: 'warning', text: '制作计划已挂起' }
  }
  if (['paused', 'failed', 'attention'].includes(status)) {
    return { icon: '⚙', type: 'warning', text: '上次完整流程需处理' }
  }
  return null
})
const historicalWorkflow = computed(() => ['paused', 'failed', 'attention'].includes(props.workflowStatus || '')
  && badge.value?.icon !== '⚙')
</script>
<template>
  <NTag v-if="badge" size="small" :type="badge.type" :bordered="false"
       :data-run-badge="badge.icon" :title="badge.text">{{ badge.icon }} {{ badge.text }}</NTag>
  <NTag v-if="historicalWorkflow" size="small" type="warning" :bordered="false"
       data-workflow-history="true" title="上次完整流程尚未继续，当前制作进度单独显示">上次完整流程需处理</NTag>
</template>
