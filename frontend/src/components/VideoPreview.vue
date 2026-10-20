<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NSpin } from 'naive-ui'
import { vReleaseMedia } from '../utils/preview'
import { createVideoRecovery, type VideoPreviewState } from '../utils/videoRecovery'
import VideoControls from './VideoControls.vue'

export type VideoPreviewChapter = {
  title: string
  start_time: number
  end_time?: number | null
}

const props = withDefaults(defineProps<{
  src: string
  /** 可选的源视频章节；标记只影响本地预览，不会写入平台。 */
  chapters?: VideoPreviewChapter[]
  /** 在媒体元数据尚未返回时用于绘制标记的时长。 */
  duration?: number
  /** 可选源视频帧率；传入 ffprobe 的 num/den 字符串可获得更准确的逐帧定位。 */
  frameRate?: number | string | null
}>(), { chapters: () => [], duration: 0, frameRate: null })
const state = ref<VideoPreviewState>({ kind: 'idle', message: '' })
const video = ref<HTMLVideoElement | null>(null)
let recovery = createVideoRecovery(() => video.value, () => props.src, value => { state.value = value })
function onMetadata() {
  recovery.metadata()
}
function onTimeUpdate() { recovery.remember() }
watch(() => props.src, () => {
  recovery.dispose()
  state.value = { kind: 'idle', message: '' }
  recovery = createVideoRecovery(() => video.value, () => props.src, value => { state.value = value })
})
onBeforeUnmount(() => recovery.dispose())
</script>

<template>
  <div class="video-preview">
    <video v-release-media ref="video" :key="src" :src="src" preload="metadata"
           @loadstart="recovery.waiting" @waiting="recovery.waiting" @seeking="recovery.waiting" @stalled="recovery.waiting"
           @loadedmetadata="onMetadata" @loadeddata="recovery.ready" @canplay="recovery.ready" @seeked="recovery.ready"
           @timeupdate="onTimeUpdate" @error="recovery.failed"><slot /></video>
    <VideoControls :media="video" :chapters="chapters" :duration="duration" :frame-rate="frameRate" />
    <div v-if="state.message" class="video-feedback" role="status">
      <n-spin v-if="state.kind === 'loading'" :size="12" />
      <span>{{ state.message }}</span>
      <n-button v-if="state.kind === 'stalled' || state.kind === 'error'" size="tiny" secondary @click="recovery.retry">重新加载视频</n-button>
    </div>
  </div>
</template>

<style scoped>
.video-preview { min-width: 0; width: 100%; position: relative; }
video { display: block; width: 100%; max-height: var(--df-preview-height, 72vh); aspect-ratio: 16 / 9; object-fit: contain; background: #000; border-radius: 8px; }
.video-feedback { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding-top: 8px; font-size: 12px; color: var(--df-muted); }
</style>
