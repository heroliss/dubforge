<script setup lang="ts">
import { computed } from 'vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { publicationUploadProgress } from '../utils/publishing'
import { fmtSize } from '../utils/format'
import { executionForPublication, executionForChapter } from '../utils/executionProgress'
import { publicationChapterState } from '../utils/publicationChapters'
import { publicationActivity } from '../utils/progressActivity'
import { publicationUpdateProgress } from '../utils/publicationUpdate'

const props = defineProps<{ publication: any; compact?: boolean; chapters?: boolean }>()
const progress = computed(() => publicationUploadProgress(props.publication))
const execution = computed(() => {
  const value = executionForPublication(props.publication, progress.value)
  // 兼容聚合接口把当前章节放在投稿execution；章节只在独立叶任务显示一次。
  return value?.owner === 'chapters' ? null : value
})
const chapter = computed(() => publicationChapterState(props.publication))
const chapterExecution = computed(() => props.chapters !== false && props.publication?.receipt?.bvid
  ? executionForChapter(props.publication, chapter.value) : null)
const update = computed(() => publicationUpdateProgress(props.publication))
const updateExecution = computed(() => {
  const value = update.value
  if (!value) return null
  const state = value.status === 'completed' ? 'completed'
    : value.status === 'failed' ? 'failed'
    : value.status === 'unknown' ? 'unknown'
    : value.status === 'paused' ? 'paused'
    : value.active ? (value.status === 'waiting' ? 'waiting' : 'running') : 'queued'
  return { schema: 1, state, label: value.phase || value.text,
    detail: value.current?.label || '', completed: value.progress, total: 100, unit: '%',
    metrics: value.current?.progress !== null && value.current?.progress !== undefined
      ? { current_stage: value.current.label, stage_progress: value.current.progress } : undefined }
})
</script>

<template>
  <div v-if="progress || execution || chapterExecution || updateExecution" class="publication-progress" :class="{ compact }" data-publication-progress>
    <div v-if="progress?.hasBytes" class="progress-heading"><span>{{ progress.merging ? '视频字节已传完' : progress.complete ? '视频文件已上传' : '视频文件上传' }}</span><span>{{ fmtSize(progress.loaded) }} / {{ fmtSize(progress.total) }}</span></div>
    <ExecutionProgress :execution="execution" :percentage="progress?.hasBytes ? progress.percentage : null" :indeterminate="!progress?.hasBytes" :activity="publicationActivity(publication)" :compact="compact" progress-label="视频文件" />
    <ExecutionProgress :execution="updateExecution" :percentage="update?.progress" :indeterminate="update?.progress === null || !!update?.current && update.current.progress === null" :compact="compact" progress-label="原稿更新" />
    <ExecutionProgress :execution="chapterExecution" :compact="compact" />
  </div>
</template>

<style scoped>
.publication-progress { width:100%; margin:12px 0; min-width:0; }
.progress-heading { display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; font-size:12px; color:var(--df-muted); margin-bottom:7px; }
p { margin:7px 0 0; font-size:12px; color:var(--df-muted); overflow-wrap:anywhere; }
.compact { margin:8px 0 0; }.compact .progress-heading,.compact p { font-size:11px; }
.activity-dot { display:inline-block; width:6px; height:6px; margin-right:7px; border-radius:50%; background:var(--df-accent); animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.35; } }
@media (prefers-reduced-motion:reduce) { .activity-dot { animation:none; } }
</style>
