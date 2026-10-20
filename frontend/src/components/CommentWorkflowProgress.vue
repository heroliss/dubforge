<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useThemeVars } from 'naive-ui'
import CommentProcessPanel from './CommentProcessPanel.vue'
import { useCommentProgress } from '../composables/useCommentProgress'
import { commentWorkspaceLink, type CommentAction } from '../utils/commentWorkflow'

const props = defineProps<{ uid: string; automation: { state: string; message: string } }>()
const legacyState = ref<any>(null)
let disposed = false
let controller: AbortController | undefined
// 总览只读取轻量状态；正在运行的旧后端无此接口时兼容读取已有进度。
async function fallback() {
  const uid = props.uid
  controller?.abort(); controller = new AbortController()
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/comments`, {
    cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
  })
  const value = await response.json()
  if (!response.ok) throw new Error(value.message || '评论进度读取失败')
  if (!disposed && props.uid === uid) legacyState.value = value
}
const progress = useCommentProgress({
  uid: () => props.uid, active: () => props.automation.state === 'running', fallback,
})
watch(() => props.uid, () => { controller?.abort(); legacyState.value = null })
onBeforeUnmount(() => { disposed = true; controller?.abort() })
const diagnosticLink = (action?: CommentAction) => commentWorkspaceLink(props.uid, true, action)
const theme = useThemeVars()
const linkStyle = computed(() => ({
  '--comment-link': theme.value.infoColor,
  '--comment-link-muted': theme.value.textColor3,
  '--comment-link-border': theme.value.borderColor,
  '--comment-link-warning': theme.value.warningColor,
}))
</script>

<template>
  <div class="workflow-comments" :style="linkStyle">
    <CommentProcessPanel :progress="progress.snapshot.value" :state="legacyState" :automation="automation" compact />
    <nav class="comment-progress-links" aria-label="评论处理详情">
      <RouterLink :to="commentWorkspaceLink(uid)"><span>评论与上屏方案</span><span aria-hidden="true">→</span></RouterLink>
      <RouterLink :to="diagnosticLink(progress.snapshot.value?.job?.action || legacyState?.job?.action)"><span>当前 LLM 对话</span><span aria-hidden="true">→</span></RouterLink>
    </nav>
    <p v-if="progress.error.value" class="progress-error" role="status">{{ progress.error.value }}</p>
  </div>
</template>

<style scoped>
.workflow-comments{min-width:0}.comment-progress-links{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:6px 20px;margin:8px 0 0;padding:0 2px;font-size:12px;line-height:1.7}.comment-progress-links>a{display:inline-flex;align-items:center;gap:7px;padding:4px 0;color:var(--comment-link-muted);text-decoration:none;transition:color .15s}.comment-progress-links>a>span:last-child{color:var(--comment-link);font-size:13px}.comment-progress-links>a:hover{color:var(--comment-link)}.comment-progress-links>a:hover>span:first-child{text-decoration:underline;text-underline-offset:4px}.progress-error{margin:8px 0 0;padding:9px 12px;border:1px solid color-mix(in srgb,var(--comment-link-warning) 25%,var(--comment-link-border));border-radius:7px;background:color-mix(in srgb,var(--comment-link-warning) 5%,transparent);color:var(--comment-link-warning);font-size:12px;line-height:1.8;overflow-wrap:anywhere}
@media(max-width:600px){.comment-progress-links{justify-content:flex-start;gap:4px 18px;padding:0}.comment-progress-links>a{padding:5px 0}}
</style>
