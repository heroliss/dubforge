<script setup lang="ts">
// 全局壳: 暗色主题 + 中文 locale + 主布局
import { darkTheme, zhCN, dateZhCN } from 'naive-ui'
import MainLayout from './layouts/MainLayout.vue'
</script>

<template>
  <n-config-provider :theme="darkTheme" :locale="zhCN" :date-locale="dateZhCN">
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
