<script setup lang="ts">
// 关于页：先说明产品定位，再说明管线、原则和进度。
import { NCard, NDivider, NGi, NGrid, NH1, NLayoutContent, NSpace, NTable, NTag, NText } from 'naive-ui'
import { NAV_STEP_ORDER } from '../stores/nav'

const milestones = [
  ['M0', '脚手架', '后端、前端、数据库、配置和启动脚本', true],
  ['M1', '项目域', '项目管理、步骤执行器、中断恢复和输入指纹', true],
  ['M2', '文本管线', '下载、分离、转录、分句、翻译和字幕', true],
  ['M3', '配音成片', '配音计划、试配、批量配音和视频合成', true],
  ['M4', '前端应用', '项目列表、管线页、设置和帮助', true],
  ['M5', '试配工作台', '少量试听、调参、满意标记和缓存复用', true],
  ['M6', '持续打磨', '错误中心、端到端验证和更多模型适配', false],
] as const
const principles = [
  ['步骤透明', '每一步都可查看状态、进度、输入、产物和日志。'],
  ['可恢复', '产物采用临时文件加原子替换，重启后可以识别中断任务。'],
  ['可选择', '在字幕与配音页逐句试听、校对，也可以选择所需步骤批量运行。'],
  ['中文优先', '用户提示、错误建议和新手说明都使用清晰的中文。'],
]
</script>

<template>
  <div class="workspace-page">
    <div class="about">
      <span class="page-kicker">让声音跨越语言</span><n-h1 class="title">关于 DubForge</n-h1>
      <p class="lead">把视频翻译、配音和成片整理成一条可以观察、可以恢复、可以批量运行的本地工作流。</p>

      <n-grid cols="1 s:2 m:4" :x-gap="12" :y-gap="12" responsive="screen" class="stats">
        <n-gi><n-card size="small"><n-text depth="3">制作工作区</n-text><div class="stat">{{ NAV_STEP_ORDER.length }}</div><n-text depth="3">字幕与配音统一编辑</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">默认语言</n-text><div class="stat">中文</div><n-text depth="3">同时支持多语言扩展</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">运行方式</n-text><div class="stat">本地</div><n-text depth="3">模型服务按需连接</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">批处理</n-text><div class="stat">支持</div><n-text depth="3">多项目自动续跑</n-text></n-card></n-gi>
      </n-grid>

      <n-card title="一条视频如何走完？" size="small" class="card">
        <div class="flow">
          <span>获取视频</span><b>→</b><span>音轨分离</span><b>→</b><span>转录/翻译</span><b>→</b>
          <span>字幕与配音</span><b>→</b><span>合成成片</span>
        </div>
        <n-text depth="3" class="hint">可先试听少量句子再批量合成，也可只校对并导出字幕。每个步骤按实际输入检查依赖，最新结果自动复用。</n-text>
      </n-card>

      <n-card title="设计原则" size="small" class="card">
        <n-grid cols="1 m:2" :x-gap="24" :y-gap="14" responsive="screen">
          <n-gi v-for="([name, desc]) in principles" :key="name">
            <div class="principle"><n-tag size="small" type="info">{{ name }}</n-tag><n-text>{{ desc }}</n-text></div>
          </n-gi>
        </n-grid>
      </n-card>

      <n-card title="开发进度" size="small" class="card">
        <n-table size="small" :single-line="false">
          <thead><tr><th style="width:70px">里程碑</th><th style="width:130px">主题</th><th>内容</th><th style="width:80px">状态</th></tr></thead>
          <tbody>
            <tr v-for="[m, name, desc, done] in milestones" :key="m">
              <td><n-tag size="small" :type="done ? 'success' : 'default'">{{ m }}</n-tag></td>
              <td>{{ name }}</td><td>{{ desc }}</td><td>{{ done ? '✅ 已完成' : '⏳ 计划中' }}</td>
            </tr>
          </tbody>
        </n-table>
      </n-card>

      <n-card size="small" class="card footer-card">
        <n-space vertical size="small">
          <n-text><b>技术选型和升级建议</b></n-text>
          <n-text depth="3">仓库中的 <code>docs/research/technology-landscape-2026.md</code> 按步骤整理了当前方案、候选模型、优缺点和升级优先级。</n-text>
          <n-divider style="margin: 4px 0" />
          <n-text depth="3">版本 0.1.0 · DubForge AI · 数据默认保存在本机工作目录</n-text>
        </n-space>
      </n-card>
    </div>
  </div>
</template>

<style scoped>
.about { max-width: 1120px; margin: 0 auto; }
.title { margin-bottom: 4px; }
.lead { margin: 10px 0 28px; color: var(--df-muted); line-height: 1.8; }
.stats { margin-bottom: 14px; }
.stat { margin: 6px 0 2px; font-size: 25px; font-weight: 650; color: #e6edf3; }
.card { margin-bottom: 14px; }
.flow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; line-height: 1.8; }
.flow span { padding: 4px 9px; border: 1px solid rgba(120, 160, 200, .35); border-radius: 6px; background: rgba(80, 120, 160, .12); }
.flow .optional { border-color: rgba(224, 174, 74, .55); background: rgba(224, 174, 74, .12); }
.flow b { color: #718096; }
.hint { display: block; margin-top: 12px; line-height: 1.7; }
.principle { display: flex; align-items: flex-start; gap: 10px; line-height: 1.7; }
.footer-card { margin-bottom: 0; }
code { padding: 1px 5px; border-radius: 4px; background: rgba(128, 128, 128, .16); font-size: 12px; }
</style>
