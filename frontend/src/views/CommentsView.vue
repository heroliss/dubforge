<script setup lang="ts">
// 评论使用独立工作区；继续复用已有采集、评分、翻译和确认任务。
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NLayoutContent } from 'naive-ui'
import VideoInfoPanel from '../components/VideoInfoPanel.vue'
import CommentDiagnostics from '../components/CommentDiagnostics.vue'
import WorkflowStageNavigation from '../components/WorkflowStageNavigation.vue'
import AppIcon from '../components/AppIcon.vue'
import { useNavStore } from '../stores/nav'
import { commentAction, type CommentAction } from '../utils/commentWorkflow'

const route = useRoute(), router = useRouter(), nav = useNavStore()
const uid = computed(() => String(route.params.uid || ''))
const artifact = computed(() => (nav.stepMap.download?.artifacts || []).find((item: any) => item.name === 'video_info.json'))
const infoVersion = computed(() => String(artifact.value?.file_revision || artifact.value?.sha256 || artifact.value?.size || ''))
const diagnosticsAction = ref<CommentAction | undefined>(commentAction(route.query.comment_action))
const diagnosticsOpen = ref(false), diagnosticsLoaded = ref(false)
const diagnosticsSection = ref<HTMLDetailsElement | null>(null)
const infoPanel = ref<InstanceType<typeof VideoInfoPanel> | null>(null)

async function showDiagnostics(action?: CommentAction, scroll = true) {
  if (action) diagnosticsAction.value = action
  diagnosticsLoaded.value = true; diagnosticsOpen.value = true
  await nextTick()
  if (scroll) diagnosticsSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function diagnosticsToggled(event: Event) { diagnosticsOpen.value = (event.target as HTMLDetailsElement).open; if (diagnosticsOpen.value) diagnosticsLoaded.value = true }
function showSettings() { void router.push({ path: route.path, query: route.query, hash: '#comment-settings' }); infoPanel.value?.showSettings() }
watch(() => [route.hash, route.query.comment_action], () => {
  const action = commentAction(route.query.comment_action)
  if (action) diagnosticsAction.value = action
  if (route.hash === '#llm-conversation') void showDiagnostics(action)
}, { immediate: true })
</script>

<template>
  <n-layout-content class="comments-view">
    <header class="comments-page-heading">
      <div class="page-introduction"><div class="page-title-row"><AppIcon name="bookmark" :size="21" /><h1>上屏评论</h1></div><p>采集评论原文，按需评分与翻译，核对后确认上屏方案。已确认方案供启用“叠加评论”的成片版本共用。</p></div>
      <div class="page-actions" role="group" aria-label="评论工作区工具"><n-button class="page-tool" size="small" quaternary title="打开本项目的自动化与评分设置" @click="showSettings"><template #icon><AppIcon name="settings" :size="16" /></template>评论设置</n-button><n-button class="page-tool" size="small" secondary title="查看评论评分与翻译的模型输入、输出和运行日志" :aria-expanded="diagnosticsOpen" aria-controls="llm-conversation" @click="showDiagnostics()"><template #icon><AppIcon name="language" :size="16" /></template>LLM 对话与日志</n-button></div>
    </header>
    <WorkflowStageNavigation :uid="uid" stage="comments" />
    <VideoInfoPanel ref="infoPanel" :key="uid" :uid="uid" mode="comments" :available="!!artifact" :version="infoVersion" :automation="nav.commentsProgress" @show-diagnostics="showDiagnostics" />
    <details id="llm-conversation" ref="diagnosticsSection" class="comments-diagnostics-section" :open="diagnosticsOpen" @toggle="diagnosticsToggled">
      <summary><span class="diagnostics-identity"><AppIcon name="language" :size="18" /><span><strong>LLM 对话与运行日志</strong><small>评论评分与评论翻译</small></span></span><span class="diagnostics-toggle">{{ diagnosticsOpen ? '收起' : '展开' }}<AppIcon class="diagnostics-chevron" name="chevron" :size="15" /></span></summary>
      <!-- 收起日志不卸载工作区，也不清除已读取的诊断记录。 -->
      <CommentDiagnostics v-if="diagnosticsLoaded" :uid="uid" :initial-action="diagnosticsAction" />
    </details>
  </n-layout-content>
</template>

<style scoped>
.comments-view{padding:26px 32px 40px;min-width:0}
.comments-page-heading{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:16px 24px;padding-bottom:10px}
.page-introduction{flex:1;min-width:240px}.page-title-row{display:flex;align-items:center;gap:10px}.page-title-row>svg{flex:none;color:var(--df-accent)}
.comments-page-heading h1{margin:0;font-size:24px;line-height:1.5;font-weight:600;color:var(--df-text)}
.comments-page-heading p{margin:8px 0 0;max-width:760px;font-size:12px;line-height:1.85;color:var(--df-muted)}
.page-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding-top:3px}.page-tool{font-size:12px}
.comments-diagnostics-section{margin-top:28px;padding-top:12px;border-top:1px solid var(--df-border);scroll-margin-top:24px}
.comments-diagnostics-section>summary{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 4px;list-style:none;cursor:pointer;color:var(--df-text);border-radius:6px}
.comments-diagnostics-section>summary::-webkit-details-marker{display:none}.comments-diagnostics-section>summary:hover{background:color-mix(in srgb,var(--df-accent) 4%,transparent)}
.comments-diagnostics-section>summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}
.diagnostics-identity{display:flex;align-items:center;gap:10px;min-width:0}.diagnostics-identity>svg{flex:none;color:var(--df-muted)}.diagnostics-identity>span{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 12px}.diagnostics-identity strong{font-size:13px;font-weight:550;line-height:1.7}.diagnostics-identity small{font-size:11px;color:var(--df-muted)}
.diagnostics-toggle{display:flex;align-items:center;gap:7px;flex-shrink:0;font-size:11px;color:var(--df-muted)}.comments-diagnostics-section[open] .diagnostics-chevron{transform:rotate(90deg)}
@media(max-width:700px){.comments-view{padding:20px 16px 32px}.comments-page-heading{gap:12px}.page-introduction{min-width:0;flex-basis:100%}.page-actions{padding:0}.comments-page-heading h1{font-size:23px}.comments-diagnostics-section>summary{padding-inline:0}.diagnostics-identity>span{align-items:flex-start;flex-direction:column;gap:0}}
</style>
