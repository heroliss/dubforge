<script setup lang="ts">
// 原视频信息 (共享): 总览页与「获取视频」步骤页复用同一份描述列表
import { fmtSize, fmtDurCN as fmtDur } from '../utils/format'

defineProps<{ media: any }>()
</script>

<template>
  <n-descriptions :column="1" label-placement="left" size="small" bordered>
    <n-descriptions-item label="文件名">{{ media.name || '—' }}</n-descriptions-item>
    <n-descriptions-item label="大小">{{ fmtSize(media.size) }}</n-descriptions-item>
    <n-descriptions-item label="时长">{{ fmtDur(media.duration) }}</n-descriptions-item>
    <n-descriptions-item label="分辨率">
      {{ media.width ? `${media.width} × ${media.height}` : '—' }}
    </n-descriptions-item>
    <n-descriptions-item label="封装格式">{{ media.container || '—' }}</n-descriptions-item>
    <n-descriptions-item label="视频编码">
      {{ media.vcodec || '—' }}
      <n-text v-if="media.fps" depth="3" style="font-size: 11px"> @{{ media.fps }}</n-text>
    </n-descriptions-item>
    <n-descriptions-item label="音频编码">
      {{ media.acodec || '—'
      }}<n-text v-if="media.sample_rate" depth="3" style="font-size: 11px">
        {{ (media.sample_rate / 1000).toFixed(1) }}kHz</n-text>
    </n-descriptions-item>
    <n-descriptions-item label="文件位置">
      <n-text :title="media.path" depth="3" style="font-size: 11px; word-break: break-all">
        {{ media.path || '—' }}
        <n-tag v-if="media.external" size="tiny" :bordered="false"
               style="margin-left: 4px">外部引用</n-tag>
      </n-text>
    </n-descriptions-item>
  </n-descriptions>
</template>
