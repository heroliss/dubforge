<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NInputNumber, useMessage } from 'naive-ui'
import PreviewTimeline from './PreviewTimeline.vue'
import { versionSpec } from '../utils/assemblyVersions'
import { newestVideoFirst, vReleaseMedia } from '../utils/preview'
import { useNavStore } from '../stores/nav'
import { refreshInBackground } from '../utils/backgroundRefresh'
const props = defineProps<{ uid: string; version: any; overlayRevision: string; disabled?: boolean }>()
const nav = useNavStore(), message = useMessage()
const context = ref<any>({duration:0, subtitles:[], comments:[]}), seconds = ref(0), length = ref(10)
const image = ref(''), error = ref(''), loading = ref(false), submitting = ref(false)
const rendered = ref(''), scrubbing = ref(false)
let disposed = false, timer: ReturnType<typeof setTimeout> | undefined, controller: AbortController | undefined, pending = false
const signature = computed(() => JSON.stringify([props.uid, versionSpec(props.version), props.overlayRevision, seconds.value]))
const outdated = computed(() => !!image.value && rendered.value !== signature.value)
const cues = computed(() => [
  ...(props.version.burn_subtitles ? context.value.subtitles.map((v: any, i: number) => ({...v,label:`字幕 ${i+1}`})) : []),
  ...(props.version.include_comments ? context.value.comments.map((v: any, i: number) => ({...v,label:`评论 ${i+1}`})) : []),
])
const preview = computed(() => (nav.stepMap.assemble?.artifacts || []).filter((a: any) => a.exists !== false && a.name.startsWith(`preview_version_${props.version.id}_`)).sort(newestVideoFirst)[0])
const previewUrl = computed(() => preview.value ? `/api/projects/${props.uid}/steps/assemble/artifacts/${encodeURIComponent(preview.value.name)}/raw?v=${preview.value.sha256}` : '')
async function loadContext() {
  try {
    const r = await fetch(`/api/projects/${props.uid}/assembly-versions/preview-context`), data = await r.json()
    if (!r.ok) throw new Error(data.message || '时间轴读取失败')
    if (!disposed) { context.value = data; schedule() }
  } catch (e: any) { if (!disposed) error.value = e.message }
}
function schedule() { if (timer) clearTimeout(timer); if (!scrubbing.value && !disposed) timer = setTimeout(render, 500) }
function beginScrub() { scrubbing.value=true; if(timer) clearTimeout(timer) }
function endScrub() { scrubbing.value=false; schedule() }
async function render() {
  if (disposed || scrubbing.value || !context.value.duration || props.disabled) return
  if (loading.value) { pending = true; return }
  const stamp = signature.value
  loading.value = true; error.value = ''; controller = new AbortController()
  try {
    const response = await fetch(`/api/projects/${props.uid}/assembly-versions/frame`, {method:'POST', signal:controller.signal,
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:versionSpec(props.version), seconds:seconds.value, overlay_revision:props.overlayRevision})})
    if (!response.ok) throw new Error((await response.json()).message || '预览失败')
    const blob = await response.blob()
    if (!disposed && signature.value === stamp) {
      if (image.value) URL.revokeObjectURL(image.value)
      image.value = URL.createObjectURL(blob)
      rendered.value = stamp
    }
  } catch (e: any) { if (!disposed && signature.value === stamp && e.name !== 'AbortError') error.value = e.message }
  finally { loading.value = false; if (pending && !disposed) { pending = false; schedule() } }
}
async function trial() {
  if (props.disabled || submitting.value) return
  submitting.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${props.uid}/assembly-versions/preview`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({version:versionSpec(props.version), start:seconds.value, end:Math.min(context.value.duration, seconds.value + length.value), overlay_revision:props.overlayRevision})})
    const result = await response.json()
    if (!response.ok) throw new Error(result.message || '试片启动失败')
    nav.stepStarted(props.uid, 'assemble', result); refreshInBackground(() => nav.poll()); message.success('正在生成当前版本试片，不修改正式成品或已保存设置')
  } catch (e: any) { error.value = e.message } finally { submitting.value = false }
}
watch(signature, schedule)
watch(() => props.disabled, value => { if (!value) schedule() })
watch(() => props.overlayRevision, loadContext)
onMounted(loadContext)
onBeforeUnmount(() => { disposed = true; if(timer) clearTimeout(timer); controller?.abort(); if(image.value) URL.revokeObjectURL(image.value) })
</script>
<template>
  <section class="version-preview" aria-label="当前版本预览">
    <div class="heading"><strong>当前版本预览</strong><small>{{ loading ? '画面更新中…' : outdated ? '旧画面 · 等待更新' : '调整后自动更新 · 无需保存' }}</small><n-button size="tiny" :disabled="loading || disabled" @click="loadContext">刷新</n-button></div>
    <img v-if="image" :src="image" :alt="outdated ? '上一次成功预览（待更新）' : `${version.name}效果预览`" :class="{pending:loading||outdated}" />
    <p v-else-if="!context.duration" class="empty">原片就绪后可预览字幕与评论效果</p>
    <n-alert v-if="error" type="warning" :show-icon="false">{{ error }}</n-alert>
    <PreviewTimeline v-model:value="seconds" :max="Math.max(0, context.duration - .1)" :cues="cues" label="版本预览位置" kind="字幕 / 评论" :disabled="disabled" @dragstart="beginScrub" @dragend="endScrub" />
    <div v-if="version.dubbing || version.burn_subtitles || version.include_comments" class="trial"><span>从当前位置试合成</span><n-input-number v-model:value="length" :min=".2" :max="60" :disabled="disabled" :input-props="{'aria-label':'试片长度'}"><template #suffix>秒</template></n-input-number><n-button size="small" :disabled="disabled || !context.duration" :loading="submitting" @click="trial">生成试片</n-button></div>
    <small v-else>此版本直接使用原片，不额外创建成品或试片。</small>
    <details v-if="previewUrl"><summary>最近试片（可能早于当前调整）</summary><video v-release-media :key="previewUrl" :src="previewUrl" controls preload="metadata" /></details>
  </section>
</template>
<style scoped>
.version-preview{min-width:0;border-top:1px solid var(--df-border);padding-top:16px;margin-top:18px}.heading,.trial{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.heading small,.empty,.trial,summary{color:var(--df-muted);font-size:12px}.heading small{flex:1}.version-preview img,.version-preview video{width:100%;max-height:420px;object-fit:contain;background:#111;border-radius:8px;margin-bottom:12px}.pending{opacity:.65}.trial{margin-top:16px}.trial :deep(.n-input-number){width:110px}summary{cursor:pointer;margin:10px 0}
</style>
