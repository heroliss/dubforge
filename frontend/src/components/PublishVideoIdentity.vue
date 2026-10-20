<script setup lang="ts">
import { computed } from 'vue'
import { videoFileInfo } from '../utils/publishVideo'
const props = defineProps<{ file: any; compact?: boolean }>()
const info = computed(() => videoFileInfo(props.file))
</script>
<template>
  <span v-if="compact" class="video-identity-compact" :title="[info.title,info.timeText].filter(Boolean).join(' · ')">{{ info.title }}</span>
  <div v-else class="video-identity">
    <strong>{{ info.title }}</strong>
    <time v-if="info.timeValue" :datetime="info.timeIso">{{ info.timeText }}</time>
    <span v-if="info.summary" class="video-settings">{{ info.summary }}</span>
    <span v-if="info.currentName" class="video-settings">当前版本名：{{ info.currentName }}</span>
    <span v-if="info.filename" class="video-filename">{{ info.filename }}</span>
    <span v-if="info.status" class="video-status">{{ info.status }}</span>
  </div>
</template>
<style scoped>
.video-identity{display:grid;gap:4px;min-width:0;padding:3px 0;line-height:1.6;text-align:left;white-space:normal;overflow-wrap:anywhere}
.video-identity strong{font-size:13px;font-weight:600;color:var(--df-text)}
.video-identity time{font-size:12px;font-variant-numeric:tabular-nums;color:var(--df-text)}
.video-settings,.video-filename{font-size:11px;color:var(--df-muted)}
.video-status{font-size:11px;color:var(--df-warning)}
.video-identity-compact{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
</style>
