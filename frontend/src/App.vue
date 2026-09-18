<script setup lang="ts">
// 全局壳: 暗色主题 + 中文 locale + 主布局
import { darkTheme, zhCN, dateZhCN } from 'naive-ui'
import MainLayout from './layouts/MainLayout.vue'

// 暗色主题下未选中的开关轨/勾选框边框太暗, 几乎看不见 —— 提亮
const themeOverrides = {
  Switch: { railColor: 'rgba(255,255,255,0.30)' },
  Checkbox: { border: '1px solid rgba(255,255,255,0.40)' },
}
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides"
                     :locale="zhCN" :date-locale="dateZhCN">
    <n-dialog-provider>
      <n-message-provider>
        <MainLayout />
      </n-message-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
</style>
