<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NInput, NTag } from 'naive-ui'
import FilePathLink from './FilePathLink.vue'
import CandidateReviewBadge from './CandidateReviewBadge.vue'
import { fmtSize, fmtDurCN } from '../utils/format'
import { videoFileInfo } from '../utils/publishVideo'

const props = withDefaults(defineProps<{
  parts: any[]
  files?: any[]
  snapshotParts?: any[]
  frozen?: boolean
  unconfirmed?: boolean
  disabled?: boolean
  loading?: boolean
  ready?: boolean
}>(), { files: () => [], snapshotParts: () => [], frozen: false, unconfirmed: false, disabled: false, loading: false, ready: true })
const emit = defineEmits<{
  add: [file: any]
  remove: [index: number]
  move: [index: number, delta: number]
  rename: [index: number, value: string]
  refresh: []
}>()
const mutable = computed(() => !props.disabled && !props.frozen)
const filesKnown = computed(() => props.ready && !props.loading)
const rows = computed(() => props.parts.map((part, index) => {
  // 已确认稿件只能使用当时的文件快照，不能用今天的版本名或审查结果倒推。
  const file = props.frozen
    ? props.snapshotParts.find(file => file.artifact_id === part.artifact_id)
    : props.files.find(file => file.id === part.artifact_id)
  return { part, index, file, info: videoFileInfo(file) }
}))
const candidates = computed(() => props.files.map(file => ({ file, info: videoFileInfo(file) })))
function hasVersionName(file: any) {
  return !!(file?.version_generated_name || file?.version_name || file?.is_source)
}
function selectedIndex(file: any) {
  return props.parts.findIndex(part => part.artifact_id === file?.id)
}
function canAdd(file: any) {
  return mutable.value && filesKnown.value && file?.id != null && file.exists === true && selectedIndex(file) < 0
}
function add(file: any) { if (canAdd(file)) emit('add', file) }
function validIndex(index: number) { return Number.isInteger(index) && index >= 0 && index < props.parts.length }
function remove(index: number) { if (mutable.value && validIndex(index)) emit('remove', index) }
function move(index: number, delta: number) {
  if (mutable.value && validIndex(index) && (delta === -1 || delta === 1) && validIndex(index + delta)) emit('move', index, delta)
}
function rename(index: number, value: string) {
  if (mutable.value && validIndex(index) && typeof value === 'string' && value !== props.parts[index].title) emit('rename', index, value)
}
function refresh() { if (mutable.value && !props.loading) emit('refresh') }
function missing(file: any) { return !props.frozen && filesKnown.value && (!file || file.exists === false) }
</script>

<template>
  <div class="parts-editor">
    <div class="parts-heading">
      <div><h3>{{ frozen ? unconfirmed ? '草稿分P清单' : '已确认分P' : '投稿分P' }} <span>{{ parts.length }} 个视频</span></h3>
        <p>一个视频文件对应一个分P；加入多个文件会发布为同一稿件的多个分P，不会自动切片。</p>
      </div>
      <n-button v-if="!frozen" size="small" quaternary :loading="loading" :disabled="disabled || loading" @click="refresh">刷新视频列表</n-button>
    </div>
    <p v-if="!frozen && parts.length" class="order-hint">按下方顺序发布；分P名称只修改B站显示名称，成片文件保持不变。</p>
    <ol v-if="parts.length" class="part-list" aria-label="B站分P与视频文件对应清单">
      <li v-for="row in rows" :key="row.part.artifact_id" class="part-card" :class="{ 'part-missing': missing(row.file) }">
        <span class="part-number">P{{ row.index + 1 }}</span>
        <div class="part-content">
          <div class="part-title">
            <label :for="`publish-part-title-${row.index}`">B站分P名称</label>
            <n-input v-if="!frozen" :input-props="{ id: `publish-part-title-${row.index}`, 'aria-label': `P${row.index + 1} B站分P名称` }" :value="row.part.title" :disabled="disabled" maxlength="80" @update:value="rename(row.index, $event)" />
            <strong v-else class="frozen-title">{{ row.part.title || '未命名分P' }}</strong>
          </div>
          <div class="file-mapping">
            <span class="mapping-label">{{ frozen ? unconfirmed ? '已保存的视频引用' : '已确认文件' : '对应视频' }}</span>
            <template v-if="row.file">
              <strong v-if="!frozen || hasVersionName(row.file)" class="version-name">{{ row.info.title }}</strong>
              <time v-if="row.info.timeText" class="generation-time" :datetime="row.info.timeIso || undefined">{{ row.info.timeText }}</time>
              <p v-if="row.info.currentName" class="version-summary">当前版本名：{{ row.info.currentName }}</p>
              <p v-if="row.info.summary" class="version-summary">{{ row.info.summary }}</p>
              <span v-if="row.info.filename" class="actual-file">{{ row.info.filename }}</span>
              <span v-else class="mapping-empty">{{ frozen ? '原文件路径未记录' : '文件名称未记录' }}</span>
              <div class="file-details"><span>{{ fmtSize(row.file.size) }}</span><span v-if="row.file.duration">{{ fmtDurCN(row.file.duration) }}</span>
                <span v-if="frozen && row.file.has_dubbing === true">含AI配音</span><span v-else-if="frozen && row.file.has_dubbing === false">原声视频</span>
                <FilePathLink v-if="row.file.path" :path="row.file.path" label="定位文件" />
              </div>
              <CandidateReviewBadge v-if="!frozen" :file="row.file" />
            </template>
            <p v-else class="mapping-empty">{{ frozen ? unconfirmed ? '尚未确认上传文件信息' : '原文件信息未记录' : loading ? '正在读取对应文件信息…' : !ready ? '对应文件信息尚未读取' : '文件已不在当前视频列表中' }}</p>
            <p v-if="missing(row.file)" class="missing-hint">本地文件缺失，已保留此分P。还原文件后刷新，或移除并选择其他视频。</p>
          </div>
          <div v-if="!frozen" class="part-actions">
            <n-button size="tiny" secondary :disabled="disabled || row.index === 0" :aria-label="`上移 P${row.index + 1}`" @click="move(row.index, -1)">↑ 上移</n-button>
            <n-button size="tiny" secondary :disabled="disabled || row.index === parts.length - 1" :aria-label="`下移 P${row.index + 1}`" @click="move(row.index, 1)">↓ 下移</n-button>
            <n-button size="tiny" quaternary :disabled="disabled" :aria-label="`移除 P${row.index + 1}`" @click="remove(row.index)">移除</n-button>
          </div>
        </div>
      </li>
    </ol>
    <div v-else class="parts-empty">{{ frozen ? '此记录未保存分P清单。' : '尚未加入视频。从下方选择一个文件，即成为 P1。' }}</div>
    <p v-if="frozen" class="snapshot-hint">{{ unconfirmed ? '草稿仅保存视频引用，尚未核对文件和开始上传。' : '以上为确认时的文件信息；本地文件是否仍存在，不代表B站上传或审核状态。' }}</p>
    <section v-if="!frozen" class="available-videos" aria-label="可添加的视频">
      <div class="candidate-heading"><h4>可添加的视频</h4><span v-if="files.length">{{ files.length }} 个</span></div>
      <p v-if="loading" class="read-status" role="status">正在读取视频列表，已加入的分P保留。</p>
      <p v-else-if="!ready" class="read-status" role="status">视频列表尚未读取，已加入的分P保留。请刷新后核对。</p>
      <div v-for="{ file, info } in candidates" :key="file.id" class="candidate-card" :class="{ 'candidate-selected': selectedIndex(file) >= 0 }">
        <div class="candidate-content">
          <strong class="version-name">{{ info.title }}</strong>
          <time v-if="info.timeText" class="generation-time" :datetime="info.timeIso || undefined">{{ info.timeText }}</time>
          <p v-if="info.currentName" class="version-summary">当前版本名：{{ info.currentName }}</p>
          <p v-if="file.is_source" class="version-summary">不叠加字幕或评论</p><p v-else-if="info.summary" class="version-summary">{{ info.summary }}</p>
          <span class="actual-file">{{ info.filename || '文件名称未记录' }}</span>
          <div class="file-details"><span>{{ fmtSize(file.size) }}</span><span v-if="file.duration">{{ fmtDurCN(file.duration) }}</span>
            <span v-if="filesKnown && file.exists === false" class="missing-label">文件不存在</span>
            <span v-else-if="filesKnown && file.exists !== true">文件状态未确认</span>
            <FilePathLink v-if="file.path" :path="file.path" label="定位文件" />
          </div>
          <CandidateReviewBadge :file="file" />
        </div>
        <n-tag v-if="selectedIndex(file) >= 0" size="small" :bordered="false" type="info">已加入 P{{ selectedIndex(file) + 1 }}</n-tag>
        <n-button v-else size="small" secondary :disabled="!canAdd(file)" :aria-label="`将 ${info.title}${info.timeText ? `（${info.timeText}）` : ''} 加入 P${parts.length + 1}`" @click="add(file)">加入 P{{ parts.length + 1 }}</n-button>
      </div>
      <p v-if="filesKnown && !files.length" class="read-status">暂无可发布的原片或成片。请先生成成片，试片不会列入发布清单。</p>
      <slot name="footer" />
    </section>
  </div>
</template>

<style scoped>
.parts-editor{min-width:0}.parts-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.parts-heading h3,.candidate-heading h4{margin:0;font-size:14px;font-weight:600}.parts-heading h3 span{margin-left:8px;font-size:11px;font-weight:400;color:var(--df-muted)}.parts-heading p,.order-hint,.snapshot-hint{margin:8px 0 0;color:var(--df-muted);font-size:12px;line-height:1.8}.parts-heading>.n-button{flex-shrink:0}.order-hint{margin-bottom:12px}.part-list{list-style:none;padding:0;display:grid;gap:12px;margin:16px 0 0}.part-card{display:grid;grid-template-columns:46px minmax(0,1fr);gap:16px;border:1px solid var(--df-border);border-radius:12px;padding:18px;background:var(--df-panel-raised);min-width:0}.part-number{display:flex;align-items:center;justify-content:center;width:46px;height:38px;font-size:16px;font-weight:700;color:var(--df-accent);background:var(--df-accent-soft);border-radius:9px}.part-content,.candidate-content{min-width:0}.part-title{display:grid;gap:7px}.part-title label{font-size:11px;color:var(--df-muted)}.frozen-title{font-size:14px;line-height:1.7;overflow-wrap:anywhere}.file-mapping{display:grid;gap:5px;margin-top:14px;padding-top:12px;border-top:1px solid var(--df-border)}.mapping-label{font-size:10px;color:var(--df-muted)}.version-name{font-size:12px;line-height:1.7;overflow-wrap:anywhere}.version-summary{font-size:11px;color:var(--df-muted);line-height:1.7;margin:0;overflow-wrap:anywhere}.actual-file{font-size:11px;color:var(--df-muted);line-height:1.7;overflow-wrap:anywhere}.file-details{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11px;color:var(--df-muted);line-height:1.7}.file-mapping :deep(.candidate-review),.candidate-content :deep(.candidate-review){margin-top:5px}.part-actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:6px;margin-top:12px}.part-missing{border-color:#8e7046}.missing-hint,.missing-label{color:#e2b36b;font-size:11px;line-height:1.7}.missing-hint{margin:3px 0 0}.mapping-empty{font-size:12px;color:var(--df-muted);line-height:1.7;margin:0}.parts-empty{border:1px dashed var(--df-border);border-radius:10px;margin-top:16px;padding:24px 18px;color:var(--df-muted);font-size:12px;line-height:1.8}.available-videos{margin-top:24px;padding-top:20px;border-top:1px solid var(--df-border)}.candidate-heading{display:flex;align-items:center;gap:8px;margin-bottom:12px}.candidate-heading span{font-size:11px;color:var(--df-muted)}.candidate-card{display:flex;align-items:center;gap:16px;padding:14px 16px;border:1px solid var(--df-border);border-radius:10px;margin-top:10px}.candidate-selected{background:var(--df-accent-soft)}.candidate-content{flex:1;display:grid;gap:4px}.candidate-card>.n-button,.candidate-card>.n-tag{flex-shrink:0}.read-status{font-size:12px;color:var(--df-muted);line-height:1.8;margin:10px 0}.snapshot-hint{font-size:11px;margin-top:12px}
.generation-time{display:block;font-size:12px;line-height:1.7;color:var(--df-text);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
@media(max-width:680px){.parts-heading{flex-wrap:wrap;gap:10px}.part-card{grid-template-columns:36px minmax(0,1fr);padding:14px 12px;gap:10px}.part-number{width:36px;height:34px;font-size:14px}.candidate-card{align-items:flex-start;flex-wrap:wrap;padding:12px}.candidate-content{flex-basis:100%}.candidate-card>.n-button,.candidate-card>.n-tag{margin-left:auto}.part-actions{justify-content:flex-start}}
</style>
