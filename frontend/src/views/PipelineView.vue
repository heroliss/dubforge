<script setup lang="ts">
// 流程总览: 项目信息 + 九步骤/工作台进度一览 (点击进入步骤专属页)。
// 只负责"看"——运行/日志/配置在每步自己的页面, 由左侧导航直达。
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useNavStore, STEP_ORDER, STATUS_EMOJI, STATUS_TEXT } from '../stores/nav'

const route = useRoute()
const router = useRouter()
const nav = useNavStore()
const uid = route.params.uid as string

const project = computed(() => ({
  title: nav.title,
  status: nav.status,
  steps: nav.steps,
}))

const dubtaskDone = computed(() => nav.stepMap['dubtask']?.status === 'done')
const items = computed(() => [
  ...STEP_ORDER.map((k) => ({ key: k, to: `/projects/${uid}/steps/${k}`, step: nav.stepMap[k] })),
  { key: 'workbench', to: `/projects/${uid}/workbench`, step: null },
])
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-space justify="space-between" align="center">
      <n-h1 style="margin: 0; font-size: 20px">{{ project.title || '…' }}</n-h1>
      <n-tag size="small">{{ project.status }}</n-tag>
    </n-space>

    <n-card size="small" style="margin-top: 16px" title="流程进度">
      <div class="flow">
        <template v-for="(it, i) in items" :key="it.key">
          <div class="node" :class="{ dim: it.key === 'workbench' && !dubtaskDone }"
               @click="router.push(it.to)">
            <div class="emoji">{{ it.step ? (STATUS_EMOJI[it.step.status] ?? '⚪') : '🎧' }}</div>
            <div class="lbl">{{ it.step?.label ?? '精修工作台' }}</div>
            <div class="st">
              {{ it.step ? (STATUS_TEXT[it.step.status] ?? it.step.status)
                          : (dubtaskDone ? '逐句精修' : '需先完成配音任务') }}
            </div>
          </div>
          <div v-if="i < items.length - 1" class="arrow">→</div>
        </template>
      </div>
    </n-card>

    <n-card size="small" style="margin-top: 14px" title="说明">
      <n-text depth="3" style="font-size: 12.5px; line-height: 1.8">
        点击任意步骤进入其专属页面 (运行 / 日志 / 配置 / 产物)；
        左侧导航可随时切换步骤并显示实时状态。目录结构与各产物含义见「📖 帮助」页。
      </n-text>
    </n-card>
  </n-layout-content>
</template>

<style scoped>
.flow {
  display: flex;
  align-items: stretch;
  gap: 6px;
  overflow-x: auto;
  padding: 4px 0;
}
.node {
  min-width: 92px;
  padding: 10px 8px;
  border: 1px solid rgba(128, 128, 128, 0.3);
  border-radius: 8px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.15s;
}
.node:hover { border-color: var(--accent, #4da3ff); }
.node.dim { opacity: 0.55; }
.emoji { font-size: 20px; }
.lbl { font-size: 12.5px; font-weight: 600; margin-top: 4px; }
.st { font-size: 11px; opacity: 0.7; margin-top: 2px; }
.arrow {
  align-self: center;
  opacity: 0.4;
  font-size: 14px;
}
</style>
