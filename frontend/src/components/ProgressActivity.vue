<script setup lang="ts">
// 旧入口保留为兼容包装，所有执行状态共用同一个展示层。
import ExecutionProgress from './ExecutionProgress.vue'
import type { ProgressActivity } from '../utils/progressActivity'
defineProps<{ activity?: ProgressActivity | null; compact?: boolean }>()
</script>
<template><ExecutionProgress :activity="activity" :compact="compact" class="progress-activity" /></template>
