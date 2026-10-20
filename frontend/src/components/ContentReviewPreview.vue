<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton } from 'naive-ui'
import VideoPreview from './VideoPreview.vue'
import { normalizeReviewAudioProcessing, reviewAudioEffect, reviewTime, type ContentFinding, type ContentReviewRange, type ContentReviewReport, type ReviewAudioProcessing } from '../utils/contentReview'

const props = defineProps<{ uid: string; finding: ContentFinding; report: ContentReviewReport; disabled?: boolean }>()
const emit = defineEmits<{
  (event: 'busy-change', value: boolean): void
  (event: 'conflict'): void
  (event: 'close'): void
}>()
interface Clip {
  start: number; end: number; focus_start: number; focus_end: number; truncated: boolean
  original_url: string; processed_url: string; muted_ranges: ContentReviewRange[]; hidden_ids: number[]
  subtitles_available: boolean; warnings: string[]; fingerprint: string
  audio_processing: ReviewAudioProcessing | null
}
const clip = ref<Clip | null>(null), mode = ref<'original' | 'processed'>('original')
const loading = ref(false), error = ref(''), conflict = ref(false)
const source = computed(() => clip.value ? mode.value === 'original' ? clip.value.original_url : clip.value.processed_url : '')
const actualAudio = computed(() => clip.value?.audio_processing?.effective_mode === 'voice_only'
  ? '已屏蔽语音，使用分离背景保留环境声与音乐'
  : clip.value?.audio_processing?.effective_mode === 'all_audio' ? '命中时段整段静音（含背景）'
    : clip.value?.audio_processing?.effective_mode === 'unchanged' ? '声音未改动（无生效屏蔽范围）' : '')
const signature = computed(() => JSON.stringify([props.uid, props.finding.key, props.finding.evidence_fingerprint,
  props.report.revision, props.report.configuration_fingerprint, props.report.decision_revision, props.report.checked_at,
  props.report.mute_audio, props.report.audio_mode]))
let controller: AbortController | undefined, generation = 0, disposed = false
function cancel() {
  generation++; controller?.abort(); clip.value = null; loading.value = false; emit('busy-change', false)
}
async function generate() {
  if (disposed || props.disabled || loading.value || conflict.value) return
  const ticket = generation, stamp = signature.value
  const finding = props.finding, report = props.report
  controller = new AbortController(); loading.value = true; error.value = ''; emit('busy-change', true)
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(props.uid)}/content-review/preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120000)]),
      body: JSON.stringify({ key: finding.key, evidence_fingerprint: finding.evidence_fingerprint,
        decision_revision: report.decision_revision, revision: report.revision, configuration_fingerprint: report.configuration_fingerprint }),
    })
    const value = await response.json().catch(() => ({}))
    if (disposed || ticket !== generation || stamp !== signature.value) return
    if (!response.ok) {
      if (response.status === 409) { conflict.value = true; emit('conflict') }
      throw new Error(value.message || (response.status === 429 ? '其他片段正在生成，请稍后重试' : '片段生成失败，请稍后重试'))
    }
    const validUrl = (url: unknown) => typeof url === 'string' && url.startsWith(`/api/projects/${encodeURIComponent(props.uid)}/content-review/`) && !url.includes('\\') && !url.includes('..')
    if (value.scope !== 'source_audio' || value.key !== finding.key || !validUrl(value.original_url) || !validUrl(value.processed_url)
        || !Number.isFinite(value.start) || !Number.isFinite(value.end) || value.end <= value.start || !value.fingerprint) {
      throw new Error('片段结果不完整，请刷新审查后重试')
    }
    const audio_processing = normalizeReviewAudioProcessing(value.audio_processing)
    clip.value = { ...value, audio_processing, hidden_ids: Array.isArray(value.hidden_ids) ? value.hidden_ids : [],
      muted_ranges: Array.isArray(value.muted_ranges) ? value.muted_ranges : [], warnings: Array.isArray(value.warnings) ? value.warnings : [] }
    mode.value = 'original'
  } catch (e: any) {
    if (!disposed && ticket === generation && stamp === signature.value && e.name !== 'AbortError') {
      error.value = e.name === 'TimeoutError' ? '片段生成超时，请稍后重新生成' : e.message || '无法生成核对片段'
    }
  } finally {
    if (!disposed && ticket === generation) { loading.value = false; emit('busy-change', false) }
  }
}
watch(signature, () => { cancel(); error.value = ''; conflict.value = false }, { flush: 'sync' })
watch(() => props.disabled, value => { if (value) cancel() }, { flush: 'sync' })
// 只在用户从命中项打开此组件后生成，不预取其他片段、不自动播放。
onMounted(generate)
onBeforeUnmount(() => { disposed = true; cancel() })
</script>

<template>
  <section class="review-preview" aria-label="原声片段核对">
    <header><strong>原声片段核对</strong><n-button size="tiny" quaternary @click="emit('close')">收起片段</n-button></header>
    <p>演示当前字幕与静音规则，声音来自原片；字幕用于核对，成片字幕开关以版本方案为准。最终配音、评论和画面仍需在成片中核对。</p>
    <p>声音设置：{{ reviewAudioEffect(report) }}。<template v-if="report.mute_audio && report.audio_mode === 'voice_only'">使用分离后的背景音保留环境声与音乐；不可用时整段静音，实际效果请听验处理后片段。</template></p>
    <n-alert v-if="error" type="warning" :show-icon="false" role="status">{{ error }}</n-alert>
    <template v-if="clip && !disabled">
      <div class="clip-modes" role="group" aria-label="片段效果">
        <n-button size="small" :type="mode === 'original' ? 'primary' : 'default'" :aria-pressed="mode === 'original'" @click="mode = 'original'">处理前</n-button>
        <n-button size="small" :type="mode === 'processed' ? 'primary' : 'default'" :aria-pressed="mode === 'processed'" @click="mode = 'processed'">当前屏蔽效果</n-button>
        <span>原片 {{ reviewTime(clip.start) }} – {{ reviewTime(clip.end) }}<template v-if="clip.truncated"> · 已截取前 20 秒，请另行核对完整长段</template></span>
      </div>
      <VideoPreview :key="`${clip.fingerprint}:${mode}`" :src="source" />
      <p class="clip-result">当前效果：隐藏 {{ clip.hidden_ids.length }} 句字幕；<template v-if="clip.muted_ranges.length">声音处理范围 {{ clip.muted_ranges.map(range => `${reviewTime(range.start)}–${reviewTime(range.end)}`).join('、') }}（片段内时间）</template><template v-else>此片段没有生效的声音屏蔽范围</template>。<template v-if="!clip.subtitles_available">尚无可展示的字幕轨。</template></p>
      <p v-if="actualAudio" class="clip-result">本次声音处理：{{ actualAudio }}。<template v-if="clip.audio_processing?.fallback_reason">{{ clip.audio_processing.fallback_reason }}</template></p>
      <p v-for="(warning, index) in clip.warnings.filter(item => item !== clip?.audio_processing?.fallback_reason)" :key="index" class="clip-warning">{{ warning }}</p>
    </template>
    <n-button v-else size="small" :disabled="disabled || loading || conflict" :loading="loading" @click="generate">{{ loading ? '正在生成短片段…' : '生成这段的对比片段' }}</n-button>
  </section>
</template>

<style scoped>
.review-preview{margin:14px 0 18px;padding:16px;border:1px solid var(--df-border);border-radius:10px;background:color-mix(in srgb,var(--df-accent,#18a058) 4%,transparent);--df-preview-height:340px}.review-preview header,.clip-modes{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.review-preview header{justify-content:space-between;font-size:13px}.review-preview p,.clip-modes span{font-size:12px;color:var(--df-muted);line-height:1.75}.clip-modes{margin:12px 0}.clip-modes span{margin-left:4px}.clip-result{margin-bottom:0}.clip-warning{margin:4px 0}
</style>
