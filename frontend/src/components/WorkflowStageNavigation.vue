<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { adjacentStage, stageNumber, stagePath, stageSettingsPath, WORKFLOW_STAGES } from '../utils/workflowStages'
const props=defineProps<{uid:string;stage:string}>()
const previous=computed(()=>adjacentStage(props.stage,-1)),next=computed(()=>adjacentStage(props.stage,1))
</script>
<template>
  <nav class="workflow-stage-navigation" aria-label="工作流阶段导航">
    <span class="stage-position">第 {{ stageNumber(stage) }} / {{ WORKFLOW_STAGES.length }} 阶段</span>
    <RouterLink v-if="previous" :to="stagePath(uid,previous.key)">上一步 · {{ previous.label }}</RouterLink>
    <RouterLink v-if="next" :to="stagePath(uid,next.key)">下一步 · {{ next.label }}</RouterLink>
    <RouterLink class="stage-settings" :to="stageSettingsPath(stage)" title="查看此阶段的全局或模板默认设置；项目当前值仍在本页编辑">此阶段默认设置</RouterLink>
  </nav>
</template>
<style scoped>
.workflow-stage-navigation{display:flex;align-items:center;flex-wrap:wrap;gap:8px 18px;margin:12px 0 18px;font-size:12px;line-height:1.6}
.stage-position{color:var(--df-muted);font-variant-numeric:tabular-nums}.workflow-stage-navigation a{text-decoration:none}.stage-settings{margin-left:auto}
@media(max-width:600px){.stage-position{flex-basis:100%}.stage-settings{margin-left:0}}
</style>
