<script setup lang="ts">
import { computed } from 'vue'
import { NProgress } from 'naive-ui'
import { publicationUploadProgress } from '../utils/publishing'
import { fmtSize } from '../utils/format'

const props = defineProps<{ publication: any; compact?: boolean }>()
const progress = computed(() => publicationUploadProgress(props.publication))
</script>

<template>
  <div v-if="progress" class="publication-progress" :class="{ compact }" data-publication-progress>
    <div class="progress-heading"><span>{{ progress.merging ? '视频字节已传完' : progress.complete ? '视频文件已上传' : '视频文件上传' }}</span><span>{{ progress.percentage.toFixed(1) }}%<template v-if="progress.hasBytes"> · {{ fmtSize(progress.loaded) }} / {{ fmtSize(progress.total) }}</template></span></div>
    <n-progress type="line" :percentage="progress.percentage" :show-indicator="false" :height="compact ? 4 : 8" :processing="progress.active && (!progress.complete || progress.merging)" :status="publication.status === 'failed' ? 'error' : 'default'" />
    <p role="status" aria-live="polite"><span v-if="progress.active" class="activity-dot" aria-hidden="true" />{{ progress.phase }}<span v-if="progress.part"> · {{ progress.part }}</span><span v-if="progress.totalParts"> · 已完成 {{ progress.completedParts }} / {{ progress.totalParts }} 个分P</span></p>
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
