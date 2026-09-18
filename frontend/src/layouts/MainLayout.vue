<script setup lang="ts">
// 主布局: 左侧导航 + 顶栏品牌 + 内容区 (移动端 sider 可折叠)
import { h, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { NIcon } from 'naive-ui'

const route = useRoute()
const collapsed = ref(false)

const menuOptions = [
  {
    label: () => h(RouterLink, { to: '/' }, { default: () => '📁 项目' }),
    key: 'projects',
  },
  {
    label: () => h(RouterLink, { to: '/workbench' }, { default: () => '🎧 精修工作台' }),
    key: 'workbench',
  },
  {
    label: () => h(RouterLink, { to: '/settings' }, { default: () => '⚙️ 设置' }),
    key: 'settings',
  },
  {
    label: () => h(RouterLink, { to: '/help' }, { default: () => '📖 帮助' }),
    key: 'help',
  },
  {
    label: () => h(RouterLink, { to: '/about' }, { default: () => 'ℹ️ 关于' }),
    key: 'about',
  },
]

const activeKey = ref(route.name as string)
watch(() => route.name, (n) => (activeKey.value = n as string))
</script>

<template>
  <n-layout has-sider style="height: 100vh">
    <n-layout-sider
      bordered
      collapse-mode="width"
      :collapsed="collapsed"
      :collapsed-width="64"
      :width="200"
      show-trigger
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="brand">
        <span v-if="!collapsed">⚒️ DubForge</span>
        <span v-else>⚒️</span>
      </div>
      <n-menu :options="menuOptions" :value="activeKey" :collapsed="collapsed" />
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
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
</style>
