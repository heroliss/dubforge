<script setup lang="ts">
import { computed, ref } from 'vue'
import { NSelect } from 'naive-ui'
import AssembleOutputsPanel from './AssembleOutputsPanel.vue'
import VideoPreview from './VideoPreview.vue'
import { finalVideoKind, vReleaseMedia } from '../utils/preview'
import { useNavStore } from '../stores/nav'
const props=defineProps<{uid:string;artifacts:any[];sourcePath:string;subtitlePath:string;subtitleName:string;
  disabled?:boolean;suspended?:boolean;preview?:any;options:any[];selected:string;chapters?:any[]}>()
const emit=defineEmits<{'update:selected':[value:string];'recycle-active':[value:boolean]}>()
const expanded=ref(false)
const nav=useNavStore()
const waitingForAssembly=computed(()=>nav.uid===props.uid && ['running','queued'].includes(nav.stepMap.assemble?.status))
const count=computed(()=>props.artifacts.filter(row=>row.exists!==false && finalVideoKind(row.name)).length)
</script>
<template>
  <details class="assembly-delivery" :open="expanded" @toggle="expanded=($event.target as HTMLDetailsElement).open">
    <summary><span><strong>正式成品与原片</strong><small>{{ count?`${count} 份正式成品`:'尚未生成正式成品' }}</small></span><span class="delivery-hint">播放、定位文件与回收</span></summary>
    <div v-if="expanded" class="delivery-content" aria-label="正式成品与原片">
      <p class="delivery-description">这里管理完整成品。当前版本的短片试听与生成进度在上方“试片验证”中查看。</p>
      <div v-if="options.length" class="delivery-player">
        <label for="delivery-preview-select">播放完整成品或原片</label>
        <n-select :id="'delivery-preview-select'" :value="selected" :options="options" aria-label="播放完整成品或原片" @update:value="emit('update:selected',$event)" />
        <template v-if="!suspended && preview">
          <VideoPreview v-if="preview.kind==='video' && preview.url" :key="preview.version || preview.url" :src="preview.url" :chapters="chapters" />
          <div v-else-if="preview.kind==='audio'" v-for="audio in preview.audios || []" :key="audio.name" class="delivery-audio"><span>{{ audio.label }}</span><audio v-release-media :src="audio.url" controls preload="none" /></div>
        </template>
        <p v-else class="delivery-description">{{ waitingForAssembly?'本轮合成尚未结束，为避免影响文件替换，完整成品播放将在本轮结束后恢复；已完成版本的文件和报告可先查看。':'文件正在更新，完成后可继续播放。' }}</p>
      </div>
      <AssembleOutputsPanel :uid="uid" history-only :artifacts="artifacts" :source-path="sourcePath" :subtitle-path="subtitlePath" :subtitle-name="subtitleName" :disabled="disabled" @recycle-active="emit('recycle-active',$event)" />
    </div>
  </details>
</template>
<style scoped>
.assembly-delivery{margin-top:20px;border-top:1px solid var(--df-border);padding:18px 0}.assembly-delivery>summary{display:flex;align-items:center;justify-content:space-between;gap:16px;cursor:pointer;list-style:none}.assembly-delivery>summary::after{content:'⌄';color:var(--df-muted)}.assembly-delivery[open]>summary::after{transform:rotate(180deg)}.assembly-delivery>summary>span:first-child{display:flex;align-items:baseline;gap:12px}.assembly-delivery strong{font-size:15px;font-weight:600}.assembly-delivery small,.delivery-hint,.delivery-description{color:var(--df-muted);font-size:12px;line-height:1.7}.delivery-hint{margin-left:auto}.delivery-content{padding-top:8px;min-width:0}.delivery-description{margin:8px 0 16px}.delivery-player{max-width:1080px;display:grid;gap:12px;margin-bottom:18px}.delivery-player>label{font-size:12px;font-weight:500}.delivery-player :deep(.n-select){max-width:440px}.delivery-audio{display:flex;align-items:center;gap:12px;font-size:12px}.delivery-audio audio{width:100%;min-width:0}.assembly-delivery>summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:5px;border-radius:3px}@media(max-width:600px){.delivery-hint{display:none}.assembly-delivery>summary>span:first-child{flex-wrap:wrap;gap:4px 12px}.delivery-audio{flex-direction:column;align-items:stretch}}
</style>
