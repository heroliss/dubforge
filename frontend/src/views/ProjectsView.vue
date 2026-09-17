<script setup lang="ts">
// 项目列表页: M1 起接入真实数据 (Project CRUD + 管线状态卡)
import { ref } from 'vue'
import { onMounted } from 'vue'

const health = ref<null | { ok: boolean; version: string }>(null)

onMounted(async () => {
  try {
    const r = await fetch('/api/health')
    health.value = await r.json()
  } catch {
    health.value = null
  }
})
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>项目</n-h1>
    <n-space vertical size="large">
      <n-card title="🚧 M1 里程碑预告">
        <n-space vertical>
          <n-text>本页将展示全部视频项目（步骤状态卡 + 单步重跑 + 新建项目）。</n-text>
          <n-text depth="3">
            新建项目支持两种方式：粘贴 YouTube 链接（yt-dlp 下载、全局去重归档）或导入本地视频。
          </n-text>
        </n-space>
      </n-card>

      <n-card title="后端连接" size="small">
        <n-tag v-if="health" type="success">已连接 · v{{ health.version }}</n-tag>
        <n-tag v-else type="error">后端未连接（请确认 FastAPI 运行在 :8600）</n-tag>
      </n-card>
    </n-space>
  </n-layout-content>
</template>
