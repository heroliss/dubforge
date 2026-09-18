<script setup lang="ts">
// 主布局: 左侧导航 (上下文感知) + 内容区
// - 全局四项 (项目/设置/帮助/关于) 永远固定在顶部, 位置不变 —— 视觉稳定
// - 项目模式 (路由含 :uid): 项目导航项 (总览 + 九步骤实时徽章 + 精修工作台) 直接接在下方
import { computed, h, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useNavStore, STEP_ORDER, STATUS_EMOJI } from '../stores/nav'

const route = useRoute()
const collapsed = ref(false)
const nav = useNavStore()

const inProject = computed(() => !!route.params.uid)
watch(() => route.params.uid, (u) => {
  if (u) nav.enter(u as string)
  else nav.leave()
}, { immediate: true })

const globalOptions = [
  { label: () => h(RouterLink, { to: '/' }, { default: () => '📁 项目' }), key: 'projects' },
  { label: () => h(RouterLink, { to: '/settings' }, { default: () => '⚙️ 设置' }), key: 'settings' },
  { label: () => h(RouterLink, { to: '/help' }, { default: () => '📖 帮助' }), key: 'help' },
  { label: () => h(RouterLink, { to: '/about' }, { default: () => 'ℹ️ 关于' }), key: 'about' },
]

const projectOptions = computed(() => [
  {
    label: () => h(RouterLink, { to: `/projects/${nav.uid}` },
      { default: () => `📊 总览 (${nav.doneCount}/${STEP_ORDER.length})` }),
    key: 'overview',
  },
  ...STEP_ORDER.map((k) => {
    const s = nav.stepMap[k]
    const emoji = s ? (STATUS_EMOJI[s.status] ?? '⚪') : '⚪'
    return {
      label: () => h(RouterLink, { to: `/projects/${nav.uid}/steps/${k}` },
        { default: () => `${emoji} ${s?.label ?? k}` }),
      key: k,
    }
  }),
  { type: 'divider', key: 'd2' },
  {
    label: () => h(RouterLink, { to: `/projects/${nav.uid}/workbench` },
      { default: () => '🎧 精修工作台' }),
    key: 'workbench',
  },
])

const menuOptions = computed(() =>
  inProject.value ? [...globalOptions, { type: 'divider', key: 'dg' }, ...projectOptions.value]
                  : globalOptions)

const activeKey = computed(() => {
  if (route.name === 'step') return route.params.key as string
  if (route.name === 'workbench-p') return 'workbench'
  if (route.name === 'pipeline') return 'overview'
  return route.name as string
})
</script>

<template>
  <n-layout has-sider style="height: 100vh">
    <n-layout-sider
      bordered
      collapse-mode="width"
      :collapsed="collapsed"
      :collapsed-width="64"
      :width="210"
      show-trigger
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="brand">
        <span class="brand-icon">⚒️</span>
        <span v-if="!collapsed">DubForge</span>
      </div>
      <div v-if="inProject && !collapsed" class="proj-title" :title="nav.title">
        {{ nav.title || '…' }}
      </div>
      <n-menu :options="menuOptions"
              :value="activeKey" :collapsed="collapsed" :collapsed-width="64"
              :collapsed-icon-size="20" />
    </n-layout-sider>
    <n-layout>
      <router-view />
    </n-layout>
  </n-layout>
</template>

<style scoped>
.brand {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-icon { font-size: 18px; }
.proj-title {
  padding: 0 20px 8px;
  font-size: 12px;
  opacity: 0.75;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
