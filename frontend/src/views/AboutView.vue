<script setup lang="ts">
// 关于页：说明当前版本的能力、使用入口和独立项目定位。
import { NCard, NDivider, NGi, NGrid, NH1, NSpace, NTable, NTag, NText } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { version } from '../../package.json'
import { WORKFLOW_STAGES } from '../utils/workflowStages'

const capabilities = [
  ['项目与模板', '本地视频和链接导入、系列模板、项目独立设置与批量计划。'],
  ['字幕与配音', '识别、语义整理、翻译、逐句校对、试配与多条语音选择。'],
  ['成片版本', '原声与配音版本、字幕和评论排版、非破坏性剪辑与试片。'],
  ['发布与回执', 'B站稿件、分P、章节和发布记录；审核结果以平台实际回执为准。'],
  ['执行与恢复', '依赖检查、输入指纹、运行快照、日志和中断后的继续执行。'],
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
        <n-gi><n-card size="small"><n-text depth="3">工作阶段</n-text><div class="stat">{{ WORKFLOW_STAGES.length }}</div><n-text depth="3">制作、评论与发布统一编排</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">默认语言</n-text><div class="stat">中文</div><n-text depth="3">同时支持多语言扩展</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">运行方式</n-text><div class="stat">本地</div><n-text depth="3">模型服务按需连接</n-text></n-card></n-gi>
        <n-gi><n-card size="small"><n-text depth="3">批处理</n-text><div class="stat">支持</div><n-text depth="3">多项目自动续跑</n-text></n-card></n-gi>
      </n-grid>

      <n-card title="一条视频如何走完？" size="small" class="card">
        <div class="flow">
          <template v-for="(stage,index) in WORKFLOW_STAGES" :key="stage.key"><b v-if="index">→</b><span>{{ stage.label }}</span></template>
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

      <n-card title="v1 稳定版能力" size="small" class="card">
        <n-table size="small" :single-line="false">
          <thead><tr><th style="width:140px">工作区</th><th>当前能力</th></tr></thead>
          <tbody>
            <tr v-for="[name, desc] in capabilities" :key="name">
              <td>{{ name }}</td><td>{{ desc }}</td>
            </tr>
          </tbody>
        </n-table>
      </n-card>

      <n-card size="small" class="card footer-card">
        <n-space vertical size="small">
          <n-text><b>使用与开发文档</b></n-text>
          <n-text depth="3"><RouterLink to="/help">使用指南</RouterLink> 介绍各工作区的操作；<RouterLink to="/settings">设置</RouterLink> 管理模型连接和制作参数。仓库中的 <code>docs/README.md</code> 汇总安装、架构、开发与故障排查文档。</n-text>
          <n-text depth="3">DubForge 是独立开发的本地视频译制工作台。模型服务与平台账号由用户自行配置，项目数据和制作结果保存在自己的工作目录。</n-text>
          <n-divider style="margin: 4px 0" />
          <n-text depth="3">版本 {{ version }} · DubForge AI · 数据默认保存在本机工作目录</n-text>
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
