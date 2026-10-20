<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NTag, useMessage } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
import ChapterAdaptationSummary from './ChapterAdaptationSummary.vue'
import LlmConversationButton from './LlmConversationButton.vue'
import { chapterIssues, chapterSourceLabel, preserveChapterSources, preserveChapterAdaptation } from '../utils/chapterRules'

type Chapter = { title: string; start_time: number; end_time?: number | null; source_indices?: number[] }
type ChapterPart = { cid: string | number; title: string; duration?: number | null; items: Chapter[]; timelineNote?: string; adaptation?: any }
type PreferredPart = { title?: string; chapter_cid?: string | number; duration?: number; chapter_items?: Chapter[]; chapter_adaptation?: any; chapter_timeline?: { kind?: string; label?: string } }

const props = defineProps<{
  accountId: string | number
  bvid: string
  aid?: string | number
  preferredChapters?: Chapter[]
  preferredParts?: PreferredPart[]
  preferredPermanent?: boolean
  preferredInstruction?: string
  projectUid?: string
  publicationId?: string
  disabled?: boolean
}>()

const message = useMessage()
const parts = ref<ChapterPart[]>([])
const selectedPart = ref('')
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const copyNotice = ref('')
const syncPreview = ref<any>(null)
const syncLoading = ref(false)
const syncError = ref('')
const syncResult = ref('')
const adapting = ref(false)
const translateTitles = ref(true)
const adaptingCount = ref(0)
const instruction = ref(props.preferredInstruction || '')
const usePreferred = ref(true)
const syncPermanent = ref(props.preferredPermanent !== false)
let disposed = false
let request = 0
let syncGeneration = 0
let copyTimer: ReturnType<typeof setTimeout> | undefined

const activePart = computed(() => parts.value.find(part => String(part.cid) === selectedPart.value) || parts.value[0] || null)
const chapters = computed(() => activePart.value?.items || [])
const partOptions = computed(() => parts.value.map((part, index) => ({ value: String(part.cid), label: `P${index + 1} · ${part.title || '未命名分P'}` })))
const hasChapters = computed(() => chapters.value.length > 0)
const totalChapterCount = computed(() => parts.value.reduce((sum, part) => sum + part.items.length, 0))
const issues = computed(() => chapterIssues(chapters.value, activePart.value?.duration, activePart.value?.adaptation?.rules))
const invalidCount = computed(() => issues.value.length)
const chapterText = computed(() => chapters.value.map(chapter => `${formatTime(chapter.start_time)} ${chapter.title.trim()}`).join('\n'))
const chapterJson = computed(() => JSON.stringify(chapters.value.map(chapter => ({
  title: chapter.title.trim(), start_time: round(chapter.start_time), end_time: chapter.end_time == null ? null : round(chapter.end_time),
})), null, 2))

function round(value: number) { return Math.round(value * 1000) / 1000 }
function formatTime(value: number) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
    : `${minutes}:${String(rest).padStart(2, '0')}`
}
function normalize(value: any): Chapter[] {
  const rows = Array.isArray(value) ? value : []
  return rows.map((row: any) => ({
    title: String(row?.title || row?.name || '').trim(),
    start_time: Number(row?.start_time ?? row?.start ?? row?.from ?? 0),
    end_time: row?.end_time == null && row?.end == null && row?.to == null
      ? null : Number(row?.end_time ?? row?.end ?? row?.to),
    ...(Array.isArray(row?.source_indices) ? { source_indices: [...row.source_indices] } : {}),
  })).filter((row: Chapter) => Number.isFinite(row.start_time) && row.start_time >= 0)
    .sort((a: Chapter, b: Chapter) => a.start_time - b.start_time)
}
function normalizeParts(value: any): ChapterPart[] {
  const rows = Array.isArray(value?.parts) ? value.parts : []
  if (rows.length) return rows.map((part: any, index: number) => ({
    cid: part?.cid ?? part?.id ?? index,
    title: String(part?.title || part?.name || `分P ${index + 1}`).trim(),
    duration: part?.duration == null ? null : Number(part.duration),
    items: normalize(part?.items ?? part?.chapters ?? part?.view_points),
  }))
  const items = normalize(value?.items ?? value?.chapters ?? value?.view_points)
  return [{ cid: 'main', title: '主视频', duration: value?.duration == null ? null : Number(value.duration), items }]
}
function clone(rows: Chapter[]) { return rows.map(row => ({ ...row })) }
function preferred(part: ChapterPart): { items: Chapter[]; note: string; adaptation?: any } | null {
  const candidates = props.preferredParts || []
  let match = candidates.find(row => row.chapter_cid != null && String(row.chapter_cid) === String(part.cid))
  if (!match && candidates.length && !candidates.some(row => row.chapter_cid != null)) {
    // 早期记录缺少上传 cid 时，仅接受唯一标题且总分P数一致的匹配。
    const sameTitle = candidates.filter(row => row.title === part.title)
    if (candidates.length === parts.value.length && sameTitle.length === 1) match = sameTitle[0]
  }
  if (candidates.length) {
    if (!match) return { items: [], note: '未能核对当前分P对应的成片，未套用源视频章节；请人工核对' }
    if (!Array.isArray(match.chapter_items) || match.chapter_timeline?.kind === 'unavailable') {
      return { items: [], note: match.chapter_timeline?.label || '章节时间轴无法核验，请人工核对' }
    }
    return { items: normalize(match.chapter_items), adaptation: match.chapter_adaptation,
      note: `${match.chapter_timeline?.label || '所选成片时间轴'}；尚未同步到B站` }
  }
  // 未提供分P快照的旧界面只允许给单P使用同一份源轴章节。
  return parts.value.length === 1 && props.preferredChapters?.length
    ? { items: normalize(props.preferredChapters), note: '源视频时间轴候选；尚未同步到B站' } : null
}

async function read() {
  const account = String(props.accountId || '').trim()
  const bvid = String(props.bvid || '').trim()
  if (!account || !/^BV[0-9A-Za-z]{10}$/.test(bvid)) return
  const ticket = ++request
  loading.value = true; error.value = ''
  try {
    // 自动读取只查看远端；写入始终经过明确的同步预览与确认。
    const detail = await publishApi(`/accounts/${encodeURIComponent(account)}/studio/read/chapters`, { bvid })
    if (disposed || ticket !== request) return
    parts.value = normalizeParts(detail)
    parts.value = parts.value.map(part => {
      const candidate = usePreferred.value ? preferred(part) : null
      return part.items.length ? { ...part, timelineNote: '已读取B站现有章节，保留远端时间轴' }
        : candidate ? { ...part, items: clone(candidate.items), timelineNote: candidate.note, adaptation: candidate.adaptation } : part
    })
    const aid = Number(props.aid)
    if (aid && parts.value.length) {
      // 公开 view_points 用于发现分P；正式编辑前改读创作中心 allcards，避免把看点误当成章节。
      for (const part of parts.value) {
        try {
          const cards = await publishApi(`/accounts/${encodeURIComponent(account)}/studio/read/chapter_read`,
            { bvid, aid, cid: Number(part.cid) })
          if (disposed || ticket !== request) return
          if (Array.isArray(cards?.items)) {
            const candidate = usePreferred.value ? preferred(part) : null
            if (cards.items.length || !candidate) {
              part.items = normalize(cards.items)
              part.adaptation = undefined
              part.timelineNote = cards.items.length ? '已读取B站现有章节，保留远端时间轴' : ''
            } else {
              part.items = clone(candidate.items); part.timelineNote = candidate.note; part.adaptation = candidate.adaptation
            }
          }
        } catch { /* 章节尚未开放或读取失败时保留公开只读结果，写入预览会再次严格校验。 */ }
      }
    }
    if (!parts.value.some(part => String(part.cid) === selectedPart.value)) selectedPart.value = parts.value[0] ? String(parts.value[0].cid) : ''
    loaded.value = true
  } catch (err: any) {
    if (!disposed && ticket === request) {
      error.value = err?.message || 'B站章节读取失败，请稍后重试'
      loaded.value = false
    }
  } finally {
    if (!disposed && ticket === request) loading.value = false
  }
}

function add() {
  if (props.disabled || syncLoading.value || adapting.value || chapters.value.length >= 10) return
  const rows = chapters.value
  const previous = rows[rows.length - 1]
  rows.push({ title: '', start_time: previous ? round(previous.start_time + 60) : 0, end_time: null })
}
function remove(index: number) {
  if (props.disabled || syncLoading.value || adapting.value) return
  chapters.value.splice(index, 1)
}
function move(index: number, offset: number) {
  const next = index + offset
  if (props.disabled || syncLoading.value || adapting.value || next < 0 || next >= chapters.value.length) return
  const rows = clone(chapters.value)
  ;[rows[index], rows[next]] = [rows[next], rows[index]]
  if (activePart.value) activePart.value.items = rows
}
function reset() { void read() }

async function adapt() {
  const part = activePart.value
  if (disposed || props.disabled || !part || !part.items.length || syncLoading.value || adapting.value) return
  const generation = syncGeneration, original = JSON.stringify(part.items)
  adapting.value = true; adaptingCount.value = part.items.length
  syncPreview.value = null; syncError.value = ''; syncResult.value = ''
  try {
    const result = await publishApi('/chapters/adapt', {
      items: clone(part.items), ...(part.duration != null ? { duration: part.duration } : {}), translate: translateTitles.value,
      instruction: instruction.value,
      ...(props.projectUid ? { project_uid: props.projectUid } : {}),
      ...(props.publicationId ? { publication_id: props.publicationId } : {}),
    })
    if (disposed || props.disabled || generation !== syncGeneration || activePart.value !== part) return
    if (JSON.stringify(part.items) !== original) throw new Error('整理期间章节已变化，请重新生成；当前编辑已保留')
    if (!Array.isArray(result?.items) || !result.items.length) throw new Error('未返回可用章节，原候选已保留')
    part.items = normalize(preserveChapterSources(result.items, JSON.parse(original)))
    part.adaptation = preserveChapterAdaptation(result.adaptation, JSON.parse(original)) || null
    part.timelineNote = '已按当前分P时长整理本地候选；尚未同步到B站'
  } catch (err: any) {
    if (!disposed && generation === syncGeneration) syncError.value = err?.message || '章节整理失败，原候选已保留'
  } finally { if (!disposed && generation === syncGeneration) adapting.value = false }
}

async function prepareSync() {
  const aid = Number(props.aid), part = activePart.value
  if (disposed || props.disabled || !aid || !part || !chapters.value.length || syncLoading.value || adapting.value || invalidCount.value) return
  const generation = syncGeneration
  syncLoading.value = true; syncError.value = ''; syncResult.value = ''
  try {
    const preview = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/changes`, {
      bvid: props.bvid, kind: 'chapters', values: { aid, cid: Number(part.cid), items: chapters.value.map((row, index) => ({
        title: row.title.trim(), start_time: round(row.start_time), end_time: round(row.end_time ?? chapters.value[index + 1]?.start_time ?? part.duration),
      })), permanent: syncPermanent.value },
    })
    if (!disposed && !props.disabled && generation === syncGeneration) syncPreview.value = preview
  } catch (err: any) {
    if (!disposed && generation === syncGeneration) syncError.value = err?.message || '章节同步预览失败，请检查规则和时间'
  }
  finally { if (!disposed && generation === syncGeneration) syncLoading.value = false }
}
async function commitSync() {
  const item = syncPreview.value
  if (disposed || props.disabled || !item || syncLoading.value) return
  const generation = syncGeneration
  syncLoading.value = true; syncError.value = ''; syncResult.value = ''
  try {
    const result = await publishApi(`/accounts/${encodeURIComponent(String(props.accountId))}/studio/changes/${encodeURIComponent(item.id)}/commit`, { confirmed: true })
    if (disposed || generation !== syncGeneration) return
    if (result?.status !== 'done' || result?.result?.verified !== true) throw new Error('章节写入结果尚未完成核验，请到B站核对后再处理')
    syncPreview.value = null; usePreferred.value = false; syncResult.value = '已写入并完成回读核验。B站审核或展示可能需要几分钟。'; await read()
  } catch (err: any) {
    if (!disposed && generation === syncGeneration) {
      // 提交结果未确认时退出旧确认框，让错误可见；再次操作须重新读取并生成计划。
      syncPreview.value = null
      syncError.value = err?.message || '章节同步结果尚未确认，请到B站核对'
    }
  }
  finally { if (!disposed && generation === syncGeneration) syncLoading.value = false }
}

async function writeClipboard(text: string) {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text); return
  }
  if (typeof document === 'undefined') throw new Error('当前环境不支持剪贴板')
  const area = document.createElement('textarea')
  area.value = text; area.setAttribute('readonly', 'true')
  area.style.position = 'fixed'; area.style.opacity = '0'
  document.body.appendChild(area); area.select()
  try { if (!document.execCommand('copy')) throw new Error('复制命令未执行') }
  finally { area.remove() }
}
async function copy(kind: 'text' | 'json') {
  const text = kind === 'json' ? chapterJson.value : chapterText.value
  if (!text.trim()) { message.warning('当前没有可复制的章节'); return }
  try {
    await writeClipboard(text)
    copyNotice.value = kind
    message.success(kind === 'json' ? '章节 JSON 已复制' : '章节清单已复制')
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { if (!disposed) copyNotice.value = '' }, 1800)
  } catch { message.error('复制失败，请手动选择章节文字复制') }
}

watch(() => `${props.accountId}:${props.bvid}`, () => {
  syncGeneration++; syncPreview.value = null; syncLoading.value = false; syncError.value = ''; syncResult.value = ''
  adapting.value = false
  parts.value = []; selectedPart.value = ''; loaded.value = false; error.value = ''; usePreferred.value = true
  syncPermanent.value = props.preferredPermanent !== false
  instruction.value = props.preferredInstruction || ''
  void read()
}, { immediate: true })
watch(() => props.disabled, value => {
  if (!value) return
  // 写权限变化后旧计划永久失效；恢复可写也必须重新核对，迟到预览不能复活确认入口。
  syncGeneration++; syncPreview.value = null; syncLoading.value = false; adapting.value = false
}, { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; request++; if (copyTimer) clearTimeout(copyTimer) })
</script>

<template>
  <section class="chapters-panel publication-panel" aria-label="B站视频章节">
    <header class="chapters-heading">
      <div class="chapters-title-row"><h3>B站视频章节</h3>
      <div class="chapters-actions"><n-tag size="small" :bordered="false">{{ bvid }}</n-tag><LlmConversationButton prominent :project-uid="projectUid" operation="chapters" :owner-id="publicationId" title="查看本稿件章节整理的输入、模型回复和纠错过程" /><n-button size="small" secondary :loading="loading" :disabled="loading || syncLoading || adapting" @click="reset">{{ loaded ? '重新读取' : '读取章节' }}</n-button></div>
      </div><p>按分P读取并编辑章节。候选时间按实际上传成片的剪辑快照调整；已有B站章节会保留，确认同步后才写入。</p>
    </header>
    <p v-if="loading || syncLoading" class="chapters-loading" role="status" aria-live="polite">{{ loading ? '正在读取B站章节…' : syncPreview ? '正在同步并核对当前分P章节…' : '正在核对规则并生成章节同步计划…' }}</p>
    <n-alert v-if="error" type="warning" :show-icon="false" class="chapters-alert">{{ error }}<n-button size="small" quaternary :disabled="loading" @click="reset">重试</n-button></n-alert>
    <template v-else-if="loaded && !parts.length">
      <p class="chapters-empty">B站没有返回章节，或该稿件尚未设置视频章节。可以先在B站官网添加，之后重新读取。</p>
    </template>
    <template v-else-if="loaded">
      <div v-if="parts.length > 1" class="chapters-part-picker"><span>选择分P</span><n-select v-model:value="selectedPart" size="small" :options="partOptions" :disabled="disabled || syncLoading || adapting" /></div>
      <div class="chapters-toolbar"><span>{{ activePart ? `${partOptions.find(option => option.value === selectedPart)?.label || '当前分P'} · ` : '' }}共 {{ chapters.length }} 条<span v-if="parts.length > 1">（全部 {{ totalChapterCount }} 条）</span><span v-if="invalidCount" class="chapters-invalid"> · {{ invalidCount }} 项需调整</span></span><div><n-checkbox v-if="aid" v-model:checked="syncPermanent" size="small" :disabled="disabled || syncLoading || adapting">App默认展示章节</n-checkbox><n-button size="small" quaternary :disabled="disabled || !hasChapters || adapting" @click="copy('text')">{{ copyNotice === 'text' ? '已复制当前P清单' : '复制当前P清单' }}</n-button><n-button size="small" quaternary :disabled="disabled || !hasChapters || adapting" @click="copy('json')">{{ copyNotice === 'json' ? '已复制 JSON' : '复制当前P JSON' }}</n-button><n-button v-if="aid" size="small" type="primary" secondary :loading="syncLoading" :disabled="disabled || !hasChapters || !!invalidCount || adapting" @click="prepareSync">生成同步计划</n-button><n-button size="small" secondary :disabled="disabled || syncLoading || adapting || chapters.length >= 10" @click="add">新增章节</n-button></div></div>
      <ChapterAdaptationSummary :value="activePart?.adaptation" />
      <label class="chapter-review-label">章节标题自定义要求（本次，可选）<n-input v-model:value="instruction" type="textarea" :autosize="{minRows:2,maxRows:5}" :maxlength="2000" show-count :disabled="disabled || syncLoading || adapting" placeholder="例如：标题用简短名词短语；保留模型英文名；避免夸张表述。" /></label>
      <div class="chapter-adapt-tools"><n-checkbox v-model:checked="translateTitles" size="small" :disabled="disabled || syncLoading || adapting">将标题翻译为中文</n-checkbox><n-button size="small" secondary :loading="adapting" :disabled="disabled || syncLoading || adapting || !hasChapters" @click="adapt">{{ translateTitles ? 'AI翻译并整理当前P' : 'AI整理当前P章节' }}</n-button><span>按主题合并超量或过短章节，再生成同步计划。</span></div>
      <p v-if="adapting" class="chapters-loading" role="status" aria-live="polite">正在整理 {{ adaptingCount }} 条源章节，等待模型按主题合并并校验规则…</p>
      <p v-if="issues.length" class="chapters-invalid chapters-rule-issue">{{ issues[0] }}</p>
      <n-alert v-if="syncError" type="warning" :show-icon="false" class="chapters-alert">{{ syncError }}</n-alert><n-alert v-if="syncResult" type="success" :show-icon="false" class="chapters-alert">{{ syncResult }}</n-alert>
      <p v-if="activePart?.timelineNote" class="chapters-footnote">{{ activePart.timelineNote }}</p>
      <p v-if="loaded && !hasChapters" class="chapters-empty">当前分P没有章节；可以切换其他分P，或在本地新增章节。</p>
      <ol v-else class="chapters-list">
        <li v-for="(chapter, index) in chapters" :key="index" class="chapter-row">
          <span class="chapter-index">{{ index + 1 }}</span>
          <n-input-number v-model:value="chapter.start_time" :min="0" :step="1" :precision="3" size="small" :disabled="disabled || syncLoading || adapting" class="chapter-time" />
          <n-input v-model:value="chapter.title" size="small" maxlength="16" show-count :disabled="disabled || syncLoading || adapting" placeholder="章节标题（最多16个字符）" class="chapter-title" />
          <div class="chapter-row-actions"><n-button size="tiny" quaternary :disabled="disabled || syncLoading || adapting || index === 0" @click="move(index, -1)">上移</n-button><n-button size="tiny" quaternary :disabled="disabled || syncLoading || adapting || index === chapters.length - 1" @click="move(index, 1)">下移</n-button><n-button size="tiny" quaternary type="error" :disabled="disabled || syncLoading || adapting" @click="remove(index)">删除</n-button></div>
          <small v-if="chapterSourceLabel(chapter.source_indices)" class="chapter-source">{{ chapterSourceLabel(chapter.source_indices) }}</small>
          <small class="chapter-preview">{{ formatTime(chapter.start_time) }} · {{ chapter.title || '未填写标题' }}</small>
        </li>
      </ol>
      <p class="chapters-footnote">复制的是当前分P，格式为“时间 标题”。点击“生成同步计划”后仍需确认一次；写入会按当前分P提交并回读核验。</p>
    </template>
    <p v-else-if="!loading" class="chapters-loading">等待读取B站章节</p>
    <n-modal :show="!!syncPreview" preset="card" title="确认同步B站章节" style="width:min(680px,calc(100vw - 32px))" :mask-closable="!syncLoading" :closable="!syncLoading" :close-on-esc="!syncLoading" @update:show="value => { if (!value && !syncLoading) syncPreview = null }">
      <template v-if="syncPreview"><p class="chapters-modal-note">服务端已重新读取规则与当前章节。确认后只修改当前分P，不会重新投稿；B站可能重新审核章节。</p><p>将写入 {{ syncPreview.after?.items?.length || 0 }} 条章节，播放器默认展示：{{ syncPreview.after?.permanent ? '是' : '否' }}。</p><div class="chapters-modal-actions"><n-button :disabled="syncLoading" @click="syncPreview=null">返回编辑</n-button><n-button type="primary" :loading="syncLoading" :disabled="disabled || syncLoading" @click="commitSync">确认同步到B站</n-button></div></template>
    </n-modal>
  </section>
</template>

<style scoped>
.chapters-panel{margin-top:20px;padding:20px;border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel,transparent)}
.chapters-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}.chapters-heading h3{margin:0;font-size:14px}.chapters-heading p{margin:7px 0 0;color:var(--df-muted);font-size:11px;line-height:1.7}.chapters-actions,.chapters-toolbar,.chapter-row-actions,.chapters-part-picker{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.chapters-alert{margin-top:14px}.chapters-empty,.chapters-loading,.chapters-footnote{margin:14px 0 0;color:var(--df-muted);font-size:11px;line-height:1.8}.chapters-part-picker{margin:15px 0 2px;color:var(--df-muted);font-size:11px}.chapters-part-picker :deep(.n-select){min-width:260px}.chapters-toolbar{justify-content:space-between;margin:15px 0 10px;color:var(--df-muted);font-size:11px}.chapters-invalid{color:var(--df-warning,#efbd70)}.chapters-list{list-style:none;padding:0;margin:0;border-top:1px solid var(--df-border)}.chapter-row{display:grid;grid-template-columns:24px 112px minmax(180px,1fr) auto;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--df-border)}.chapter-index{color:var(--df-muted);font-size:11px;text-align:center}.chapter-time{width:112px}.chapter-title{min-width:0}.chapter-preview{display:none;color:var(--df-muted);font-size:10px;grid-column:2 / -1}.chapters-footnote{border-top:1px solid var(--df-border);padding-top:10px}@media(max-width:680px){.chapter-row{grid-template-columns:24px 1fr auto}.chapter-time{width:auto}.chapter-title{grid-column:2 / -1}.chapter-row-actions{grid-column:2 / -1}.chapter-preview{display:block}.chapters-toolbar{align-items:flex-start;flex-direction:column}.chapters-toolbar>div{width:100%;display:flex;flex-wrap:wrap;gap:8px}}
.chapters-modal-note{margin:14px 0;color:var(--df-muted);font-size:11px;line-height:1.8}.chapters-modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.chapter-adapt-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--df-muted)}.chapter-source{grid-column:2/-1;color:var(--df-primary,#93b7ed);font-size:10px}.chapters-rule-issue{font-size:11px;line-height:1.8;margin:10px 0}
.chapter-review-label{display:grid;gap:8px;margin:12px 0;font-size:11px;color:var(--df-muted)}
.chapters-heading{display:block}.chapters-title-row{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px 16px}.chapters-actions :deep(.n-button){flex-shrink:0}
</style>
