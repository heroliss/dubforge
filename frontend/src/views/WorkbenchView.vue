<script setup lang="ts">
// 精修工作台: M5 由现有 workbench (FastAPI+原生JS) 迁入 Vue
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>精修工作台</n-h1>
    <n-card title="🚧 M5 里程碑">
      <n-space vertical>
        <n-text>
          逐句合成 / 试听混音 / 画面定位 / 采纳确认 —— 已在 VideoLingo 工作区验证过完整链路，
          将以 Vue 组件形式迁入本应用，与主应用共享配置与数据。
        </n-text>
        <n-text depth="3">
          迁移前可继续使用 VideoLingo-Workspace 的 StartWorkbench.bat（:8502）。
        </n-text>
      </n-space>
    </n-card>
  </n-layout-content>
</template>
