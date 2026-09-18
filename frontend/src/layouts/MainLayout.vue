<script setup lang="ts">
// 主布局: 左侧导航 (全局 + 项目两段) + 内容区
// - 全局四项 (项目/设置/帮助/关于) 永远固定在顶部, 位置不变 —— 视觉稳定
// - 打开过项目后, 项目导航区 (项目名 + 总览 + 九步骤实时徽章 + 精修工作台)
//   常驻在全局项下方: 切到设置/帮助等全局页不会清掉; 只有打开另一个项目才替换
import { computed, h, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useNavStore, STEP_ORDER, STATUS_EMOJI } from '../stores/nav'

const route = useRoute()
const collapsed = ref(false)
const nav = useNavStore()

watch(() => route.params.uid, (u) => {
  if (u && u !== nav.uid) nav.enter(u as string)
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

const GLOBAL_KEYS = ['projects', 'settings', 'help', 'about']
const globalActive = computed(() =>
  GLOBAL_KEYS.includes(route.name as string) ? route.name as string : null)
const projActive = computed(() => {
  if (route.name === 'step') return route.params.key as string
  if (route.name === 'workbench-p') return 'workbench'
  if (route.name === 'pipeline') return 'overview'
  return null
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
      <n-menu :options="globalOptions" :value="globalActive"
              :collapsed="collapsed" :collapsed-width="64" :collapsed-icon-size="20" />
      <template v-if="nav.uid">
        <div v-if="!collapsed" class="proj-title" :title="nav.title">
          {{ nav.title || '…' }}
        </div>
        <n-menu :options="projectOptions" :value="projActive"
                :collapsed="collapsed" :collapsed-width="64" :collapsed-icon-size="20" />
      </template>
    </n-layout-sider>
    <n-layout>
      <!-- :key 强制同组件路由切换时重挂载 (步骤页参数在 setup 读取, 复用组件不会刷新)。
           用 path 不用 fullPath: query 变化 (如工作台切换项目) 不必整页重挂载 -->
      <router-view :key="route.path" />
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
  padding: 10px 20px 4px;
  font-size: 12px;
  opacity: 0.7;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
