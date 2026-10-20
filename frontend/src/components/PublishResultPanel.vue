<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NButton, NCard, NTag } from 'naive-ui'
import PublicationProgress from './PublicationProgress.vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { executionForChapter } from '../utils/executionProgress'
import { PUBLICATION_STATUS, PUBLISH_ACTIVE, publicationStage } from '../utils/publishing'
import { publicationChapterState, chapterPartStatus } from '../utils/publicationChapters'
import { chapterSourceLabel } from '../utils/chapterRules'
import ChapterAdaptationSummary from './ChapterAdaptationSummary.vue'
import LlmConversationButton from './LlmConversationButton.vue'

const props = defineProps<{
  publication: any
  recordOnly?: boolean
  busy?: boolean
  projectLocked?: boolean
  confirming?: boolean
  copying?: boolean
  reviewDraftChecking?: boolean
  dirty?: boolean
  reviewDirty?: boolean
  reviewBusy?: boolean
  canCreateReviewDraft?: boolean
  remoteSyncAvailable?: boolean
  surface?: 'all' | 'status' | 'more'
}>()
const emit = defineEmits<{ pause: []; check: []; collection: []; copy: []; prepareReview: [];
  chaptersRetry: [enable: boolean]; chaptersReconcile: []; chaptersRefresh: [] }>()
const status = computed(() => props.publication?.status || '')
const receipt = computed(() => props.publication?.receipt || {})
const studioHref = computed(() => {
  const bvid = String(receipt.value.bvid || '').trim()
  const account = String(props.publication?.snapshot?.account_id || props.publication?.draft?.account_id || '').trim()
  if (!bvid) return ''
  if (props.remoteSyncAvailable) return '#publish-remote-sync'
  // 已删除/只读记录只保留高级管理入口；不把旧BV作为设置页导航指令，
  // 避免打开设置就自动读取远端并把用户带离当前发布上下文。
  return account ? `/settings?section=bilibili&account=${encodeURIComponent(account)}&manage=studio` : ''
})
const studioLabel = computed(() => props.remoteSyncAvailable ? '同步已发布文案' : '高级管理与更多字段')
const active = computed(() => PUBLISH_ACTIVE.includes(status.value))
const stage = computed(() => publicationStage(status.value))
const title = computed(() => props.surface === 'more' ? '更多操作' : !props.recordOnly && status.value === 'draft' ? '4 · 进度与回执' : '发布状态与回执')
const showStatus = computed(() => props.surface !== 'more')
const showOtherActions = computed(() => props.surface !== 'status')
const hasBV = computed(() => !!receipt.value.bvid)
const hasChapterContext = computed(() => !!(props.publication?.chapter_sync?.status || props.publication?.snapshot?.chapter_options))
const chapterSuperseded = computed(() => props.publication?.chapter_sync?.status === 'superseded')
const originalRuntimeSuperseded = computed(() => chapterSuperseded.value
  || props.publication?.latest_update?.execution?.stages?.edit?.status === 'completed'
  && Array.isArray(props.publication?.latest_update?.preview?.parts)
  && props.publication.latest_update.preview.parts.some((part: any) => part.replaced))
const chapters = computed(() => {
  const state = publicationChapterState(props.publication)
  // 新视频的章节由原稿更新任务接管，历史交付结果不再表示当前视频已设置。
  return chapterSuperseded.value ? { ...state, label: props.publication.chapter_sync.phase || '章节已由原稿更新任务接管',
    tone: 'info' as const, retry: false, supplement: false, reconcile: false, active: false, parts: [] } : state
})
const chapterExecution = computed(() => chapterSuperseded.value ? null : executionForChapter(props.publication, chapters.value))
const chapterActionAllowed = computed(() => hasBV.value && !active.value && !chapterSuperseded.value
  && ['published','private','reviewed'].includes(status.value))
const chapterWriteDisabled = computed(() => !!(props.recordOnly || props.projectLocked || props.busy || props.confirming
  || props.dirty || props.reviewDirty || props.reviewBusy))
const showPause = computed(() => !props.recordOnly && ['queued', 'uploading'].includes(status.value))
const showCollection = computed(() => !props.recordOnly && status.value === 'collection_pending')
const showCopy = computed(() => !props.recordOnly && !!status.value && !['draft', 'unknown'].includes(status.value) && !active.value)
const showReview = computed(() => !props.recordOnly && !!props.canCreateReviewDraft && hasBV.value
  && !['draft', 'unknown'].includes(status.value) && !active.value)
const copyDisabled = computed(() => !!(props.projectLocked || props.busy || props.copying || props.confirming
  || props.dirty || props.reviewDirty || props.reviewBusy))
const reviewDisabled = computed(() => !!(props.projectLocked || props.busy || props.confirming || props.reviewDraftChecking
  || props.dirty || props.reviewDirty || props.reviewBusy))
const showMore = computed(() => !props.recordOnly && (showCopy.value || showReview.value || hasBV.value))

// 点击处理也核对可见性与忙态，防止组件状态变化后触发过时动作。
function pauseUpload() { if (showPause.value && !props.busy) emit('pause') }
function checkState() { if (hasBV.value && !props.busy && !active.value) emit('check') }
function retryCollection() { if (showCollection.value && !props.projectLocked && !props.busy) emit('collection') }
function copyDraft() { if (showCopy.value && !copyDisabled.value) emit('copy') }
function prepareReview() { if (showReview.value && !reviewDisabled.value) emit('prepareReview') }
function retryChapters() {
  if (hasChapterContext.value && chapterActionAllowed.value && !chapterWriteDisabled.value && (chapters.value.retry || chapters.value.supplement))
    emit('chaptersRetry', chapters.value.supplement)
}
function reconcileChapters() { if (hasBV.value && chapters.value.reconcile && !props.busy && !active.value) emit('chaptersReconcile') }
function refreshChapters() { if (hasBV.value && !props.busy && !active.value) emit('chaptersRefresh') }
function chapterTime(value: unknown) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0))
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), rest = seconds % 60
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `${minutes}:${String(rest).padStart(2, '0')}`
}
</script>

<template>
  <n-card :id="showStatus ? 'publish-receipt' : undefined" :title="title" size="small" class="publication-stage publication-panel result-panel" :data-result-surface="surface || 'all'">
    <template v-if="showStatus">
    <div class="result-status" aria-live="polite" :data-status="status">
      <span v-if="hasBV" class="account-name">B站当前状态</span>
      <n-tag :type="stage.tone" :bordered="false">{{ PUBLICATION_STATUS[status] || '待准备' }}</n-tag>
      <span v-if="publication?.snapshot?.account_name" class="account-name">{{ publication.snapshot.account_name }}</span>
      <span v-if="receipt.collection_joined" class="collection-complete">已加入合集</span>
    </div>
    <div v-if="hasBV" class="receipt">
      <a :href="`https://www.bilibili.com/video/${receipt.bvid}`" target="_blank" rel="noopener noreferrer">{{ receipt.bvid }} ↗</a>
      <span>{{ receipt.state_desc || '已取得投稿回执，上传完成不代表审核通过' }}</span>
      <a v-if="studioHref" :href="studioHref" class="studio-link">{{ studioLabel }}</a>
    </div>
    <n-alert v-if="publication?.error" type="warning" :show-icon="false" class="result-notice">{{ publication.error }}</n-alert>
    <n-alert v-if="receipt.reject_reason" type="warning" :show-icon="false" class="result-notice"><strong>审核退回原因</strong><p>{{ receipt.reject_reason }}</p></n-alert>

    <div v-if="status !== 'draft' && !originalRuntimeSuperseded" class="result-runtime">
      <PublicationProgress :publication="publication" :chapters="false" />
    </div>
    <p v-if="status === 'draft'" class="stage-description">尚未启动上传；确认发布后在这里查看进度与回执。</p>
    <p v-else-if="status === 'uploaded'" class="stage-description">视频与封面已上传，尚未正式投稿。核对投稿资料后，再确认提交。</p>
    <p v-else-if="status === 'unknown'" class="stage-description">投稿结果尚未确认，请先在B站创作中心核对原稿件，并补充回执，避免重复投稿。</p>

    <div v-if="showPause || hasBV || showCollection" class="receipt-actions">
      <n-button v-if="showPause" secondary :disabled="busy" @click="pauseUpload">暂停上传</n-button>
      <n-button v-if="hasBV" secondary :disabled="busy || active" @click="checkState">查询平台状态</n-button>
      <n-button v-if="showCollection" type="primary" secondary :disabled="projectLocked || busy" @click="retryCollection">重试加入合集</n-button>
    </div>
    <details v-if="receipt.videos?.length" class="part-receipts">
      <summary>各分P审核 / 转码信息 <span>{{ receipt.videos.length }} 个分P</span></summary>
      <div v-for="(part, index) in receipt.videos" :key="part.cid || index" class="part-receipt">
        <strong>P{{ index + 1 }} · {{ part.title }}</strong>
        <p>{{ part.state_desc || '平台未提供状态说明' }}<span v-if="part.xcode_state !== undefined && part.xcode_state !== null"> · 转码状态码 {{ part.xcode_state }}</span></p>
        <p v-if="part.reject_reason" class="part-rejection">{{ part.reject_reason }}</p>
      </div>
    </details>

    <section v-if="hasBV && hasChapterContext" class="chapter-sync" aria-label="视频章节同步状态" :data-chapter-status="chapters.status || 'disabled'">
      <div class="chapter-sync-heading"><div class="chapter-sync-title"><strong>视频章节</strong><n-tag v-if="!chapterExecution" size="small" :type="chapters.tone" :bordered="false">{{ chapters.label }}</n-tag></div><LlmConversationButton prominent :project-uid="publication.project_uid" operation="chapters" :owner-id="chapterSuperseded ? publication.chapter_sync.superseded_by_update : publication.id" title="查看本稿件章节整理的输入、模型回复和纠错过程" /></div>
      <ExecutionProgress :execution="chapterExecution" compact />
      <p v-if="chapterSuperseded">新视频的章节进度以<a href="#publication-original-update">“更新原稿件”</a>中的任务为准。</p>
      <p v-else-if="chapters.reconcile">上次章节写入结果未知，请先核对已有章节；核对不会重新提交章节，也不会重新投稿。</p>
      <p v-else-if="chapters.active">章节处理独立于视频投稿，可刷新查看进度。</p>
      <p v-else-if="chapters.retry">仅重试章节设置，保留当前BV及视频，不重新上传或投稿。</p>
      <p v-else-if="chapters.supplement">旧投稿保留当时的关闭设置；可明确确认后补充源视频章节。源视频无章节时跳过。</p>
      <ul v-if="chapters.parts.length" class="chapter-part-status"><li v-for="(part,index) in chapters.parts" :key="part.cid || index">P{{ index + 1 }} · {{ part.title || '视频' }}：{{ chapterPartStatus(part.status) }}<span v-if="part.error"> · {{ part.error }}</span>
        <details v-if="part.items?.length" class="chapter-delivery-details"><summary>查看本次整理清单 · {{ part.adaptation?.source_count ?? part.items.length }} 条源章节 → {{ part.items.length }} 章</summary>
          <ChapterAdaptationSummary :value="part.adaptation" :local="false" />
          <ol class="chapter-delivery-items"><li v-for="(item,itemIndex) in part.items" :key="itemIndex"><time>{{ chapterTime(item.start_time) }}–{{ chapterTime(item.end_time) }}</time><span>{{ item.title }}</span><small v-if="chapterSourceLabel(item.source_indices)">{{ chapterSourceLabel(item.source_indices) }}</small></li></ol>
        </details>
      </li></ul>
      <div class="receipt-actions">
        <n-button v-if="!recordOnly && chapterActionAllowed && (chapters.retry || chapters.supplement)" size="small" secondary :disabled="chapterWriteDisabled" @click="retryChapters">{{ chapters.supplement ? '补充视频章节' : '重试设置章节' }}</n-button>
        <n-button v-if="chapters.reconcile" size="small" secondary :disabled="busy || active" @click="reconcileChapters">核对章节</n-button>
        <n-button size="small" quaternary :disabled="busy || active" @click="refreshChapters">刷新章节状态</n-button>
      </div>
    </section>

    </template>
    <details v-if="showOtherActions && showMore" :open="surface === 'more'" class="republication-options">
      <summary>另发新稿与官网操作<span class="summary-note">以下操作会另建草稿，保留当前稿件</span></summary>
      <section v-if="showReview" class="revision-workflow">
        <div class="revision-workflow-heading"><div><h3>使用审查修订版另发新稿</h3><p>采用已经生成的审查修订成片创建本地草稿。发布后得到新BV；原BV及其视频保持原状。</p></div><n-tag size="small" type="warning" :bordered="false">新BV</n-tag></div>
        <n-button type="primary" secondary :loading="reviewDraftChecking" :disabled="reviewDisabled" title="根据审查修订成片创建新的本地发布草稿，不修改原 BV" @click="prepareReview">使用审查修订版新建草稿</n-button>
        <p v-if="reviewDraftChecking" role="status" aria-live="polite">正在核对修订文件，请稍候；大文件的完整性检查可能需要一些时间。</p>
      </section>
      <div v-if="showCopy" class="copy-draft-option">
        <div><strong>沿用当前资料另发新稿</strong><p>保留当前资料与已选成片，不会自动换成审查修订版。新草稿只保存在本地，需另行确认上传与投稿。</p></div>
        <n-button secondary :loading="copying" :disabled="copyDisabled" @click="copyDraft">{{ copying ? '正在复制草稿' : '复制为新草稿' }}</n-button>
      </div>
      <div v-if="hasBV" class="original-bv-guide">
        <strong>官网手动处理</strong>
        <p>也可在B站创作中心手动编辑原稿；本页“更新原稿件”支持保留原BV替换对应视频。</p>
        <a href="https://member.bilibili.com/platform/home" target="_blank" rel="noopener noreferrer">到官网替换原BV视频 ↗</a>
      </div>
    </details>
  </n-card>
</template>

<style scoped>
.result-panel { scroll-margin-top:20px; min-width:0; }
.result-panel :deep(.n-card-header) { padding-bottom:16px; }
.result-status,.receipt,.receipt-actions,.chapter-sync-heading,.chapter-sync-title { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
.result-status { min-height:30px; }
.account-name,.receipt span,.stage-description { color:var(--df-muted); font-size:12px; }
.collection-complete { color:var(--df-success,#83c7b4); font-size:12px; }
.receipt { margin-top:16px; padding:16px 0; border-top:1px solid var(--df-border); border-bottom:1px solid var(--df-border); gap:8px 18px; line-height:1.8; }
.receipt a { font-weight:600; font-size:14px; }
.receipt span { flex:1 1 220px; overflow-wrap:anywhere; }
.receipt .studio-link { font-weight:400; font-size:12px; color:var(--df-accent); }
.result-notice { margin-top:16px; }
.result-notice strong { font-weight:600; font-size:12px; }
.result-notice p { margin:6px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.result-runtime { display:grid; gap:10px; min-width:0; }
.result-runtime>:deep(.progress-activity) { margin-top:16px; }
.result-runtime>:deep(.publication-progress) { margin-bottom:0; }
.stage-description { margin:14px 0 0; line-height:1.8; max-width:75ch; }
.receipt-actions { margin-top:18px; padding-top:14px; border-top:1px solid var(--df-border); }
.receipt+.receipt-actions { border-top:0; padding-top:0; }
.part-receipts,.republication-options { border-top:1px solid var(--df-border); margin-top:24px; padding-top:18px; font-size:12px; }
summary { cursor:pointer; color:var(--df-text); font-size:12px; line-height:1.8; font-weight:500; }
summary:focus-visible { outline:2px solid var(--df-accent); outline-offset:4px; border-radius:3px; }
summary span { margin-left:8px; font-size:11px; font-weight:400; color:var(--df-muted); }
.part-receipt { padding:16px 0; border-bottom:1px solid var(--df-border); }
.part-receipt:last-child { border-bottom:0; padding-bottom:0; }
.part-receipt strong { font-weight:550; overflow-wrap:anywhere; }
.part-receipt p { margin:6px 0 0; color:var(--df-muted); line-height:1.8; }
.part-receipt .part-rejection { color:var(--df-warning,#dba871); }
.chapter-sync { margin-top:24px; padding-top:20px; border-top:1px solid var(--df-border); }
.chapter-sync-heading { justify-content:space-between; gap:12px; margin-bottom:12px; }
.chapter-sync-title { font-size:13px; }
.chapter-sync-title strong { font-weight:600; }
.chapter-sync p,.chapter-part-status { font-size:12px; color:var(--df-muted); line-height:1.8; }
.chapter-sync p { max-width:75ch; }
.chapter-part-status { list-style:none; padding:0; margin:16px 0 0; }
.chapter-part-status>li { padding:12px 0; border-top:1px solid var(--df-border); overflow-wrap:anywhere; }
.chapter-part-status>li:last-child { padding-bottom:0; }
.chapter-sync .receipt-actions { border-top:0; padding-top:0; margin-top:14px; }
.chapter-delivery-details { margin:10px 0 0; padding:0; }
.chapter-delivery-details summary { color:var(--df-accent); font-weight:400; }
.chapter-delivery-items { padding:0; list-style:none; margin:10px 0 0; }
.chapter-delivery-items li { display:grid; grid-template-columns:110px minmax(0,1fr); gap:3px 14px; padding:9px 0; border-bottom:1px solid var(--df-border); }
.chapter-delivery-items li:last-child { border-bottom:0; }
.chapter-delivery-items time { font-size:11px; font-variant-numeric:tabular-nums; }
.chapter-delivery-items small { grid-column:2; color:var(--df-primary,#93b7ed); }
.revision-workflow { margin-top:20px; padding-bottom:20px; border-bottom:1px solid var(--df-border); }
.revision-workflow-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.revision-workflow-heading>div { min-width:0; }
.revision-workflow h3 { margin:0; font-size:14px; line-height:1.6; font-weight:600; }
.revision-workflow p,.copy-draft-option p,.original-bv-guide p { font-size:12px; color:var(--df-muted); line-height:1.8; margin:8px 0 14px; max-width:75ch; overflow-wrap:anywhere; }
.revision-workflow>p:last-child { margin-bottom:0; }
.copy-draft-option { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding:20px 0; }
.copy-draft-option strong,.original-bv-guide strong { font-size:14px; font-weight:600; }
.copy-draft-option>div { min-width:0; }
.copy-draft-option p { margin-bottom:0; }
.copy-draft-option :deep(.n-button) { flex-shrink:0; }
.original-bv-guide { border-top:1px solid var(--df-border); padding-top:20px; }
.original-bv-guide a { font-size:12px; }
.result-panel[data-result-surface=more] .republication-options { margin-top:0; padding-top:0; border-top:0; }
@media(max-width:640px) { .copy-draft-option { flex-direction:column; gap:12px; } .revision-workflow :deep(.n-button) { max-width:100%; white-space:normal; height:auto; min-height:34px; } .receipt span { flex-basis:100%; } .receipt .studio-link { margin-top:2px; } .chapter-delivery-items li { grid-template-columns:90px minmax(0,1fr); gap:3px 8px; } .summary-note { display:block; margin-left:16px; } }
</style>
