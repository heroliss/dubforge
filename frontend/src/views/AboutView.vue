<script setup lang="ts">
// 关于页: 项目定位与里程碑
const milestones = [
  ['M0', '脚手架（后端/前端/DB/配置/脚本）', true],
  ['M1', '项目域：Project CRUD + 步骤执行器 + 中断恢复', false],
  ['M2', '管线前半：下载/预处理/转录/分句/翻译三档/字幕', false],
  ['M3', '管线后半：配音任务/IndexTTS2.5+GPT-SoVITS/成片', false],
  ['M4', '前端应用：项目管理/管线页/完整设置', false],
  ['M5', '精修工作台迁入', false],
  ['M6', '打磨验收：错误中心/引导/端到端测试', false],
] as const
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>关于 DubForge</n-h1>
    <n-card>
      <n-space vertical size="large">
        <n-text>
          <b>DubForge</b> —— 视频翻译配音工作台。在 VideoLingo 领域经验之上全新设计：
          步骤常驻可见可单独重跑、中断恢复一等公民、领域库与界面解耦、中文一等支持。
        </n-text>
        <n-table size="small" :single-line="false">
          <thead><tr><th style="width:80px">里程碑</th><th>内容</th><th style="width:90px">状态</th></tr></thead>
          <tbody>
            <tr v-for="[m, desc, done] in milestones" :key="m">
              <td><n-tag size="small" :type="done ? 'success' : 'default'">{{ m }}</n-tag></td>
              <td>{{ desc }}</td>
              <td>{{ done ? '✅' : '⏳' }}</td>
            </tr>
          </tbody>
        </n-table>
      </n-space>
    </n-card>
  </n-layout-content>
</template>
