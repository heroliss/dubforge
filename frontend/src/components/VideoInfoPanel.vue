<script setup lang="ts">
// 获取视频步骤的整理信息：中文标题 / 内容概要 / 来源与作者描述，全部支持复制
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCollapse, NCollapseItem, NDescriptions, NDescriptionsItem, NInputNumber, NTag, NText, NTooltip, useMessage } from 'naive-ui'
import CommentStudioPanel from './CommentStudioPanel.vue'
import { createLatestRefresh } from '../utils/preview'
import { fmtDurCN } from '../utils/format'

const props = defineProps<{
  uid: string
  available: boolean
  version?: string
  automation?: { state: string; message: string } | null
}>()

const message = useMessage()
const emit = defineEmits<{ 'show-diagnostics': [] }>()
const info = ref<any | null>(null)
const failed = ref(false)
const loading = ref(true)
const busy = ref('')
const pending = ref('')
const error = ref('')
const canRefresh = ref(false)
const canSummarize = ref(false)
const canComments = ref(false)
const commentLimit = ref<number | null>(50)
const commentWork = ref('')
const coverBroken = ref(false)
const validCommentLimit = computed(() => Number.isInteger(commentLimit.value) && Number(commentLimit.value) >= 1 && Number(commentLimit.value) <= 500)
const active = computed(() => pending.value || busy.value || commentWork.value)
let disposed = false
let generation = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
const infoSignature = computed(() => `${props.uid}:${props.available}:${props.version || ''}`)
const loader = createLatestRefresh(async (signal) => {
  const url = `/api/projects/${props.uid}/steps/download/video-info`
  const r = await fetch(url, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取视频信息失败')
  const value = await r.json()
  if (!value || typeof value !== 'object') throw new Error('视频信息格式不正确')
  return value
}, (value) => {
  info.value = value.info; busy.value = value.busy || ''
  canRefresh.value = !!value.can_refresh; canSummarize.value = !!value.can_summarize
  canComments.value = !!value.can_collect_comments
  failed.value = false; loading.value = false
  clearTimeout(pollTimer)
  if (busy.value && !disposed) pollTimer = setTimeout(() => void loader.refresh(), 2000)
}, () => { failed.value = true; loading.value = false })
onUnmounted(() => { disposed = true; generation++; clearTimeout(pollTimer); loader.cancel() })

watch(() => props.uid, () => {
  generation++; loader.cancel(); clearTimeout(pollTimer)
  info.value = null; error.value = ''; pending.value = ''; busy.value = ''
  canRefresh.value = canSummarize.value = canComments.value = false
  loading.value = true
})
watch(infoSignature, () => {
  if (!pending.value) void loader.refresh(true)
}, { immediate: true })

async function runAction(action: 'refresh' | 'summarize' | 'comments') {
  if (active.value || (action === 'comments' && !validCommentLimit.value)) return
  const ticket = generation, uid = props.uid
  loader.cancel(); clearTimeout(pollTimer)
  pending.value = action; error.value = ''
  try {
    const r = await fetch(`/api/projects/${uid}/steps/download/video-info/${action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(action === 'comments' ? { comment_limit: commentLimit.value } : {}),
    })
    const value = await r.json()
    if (!r.ok) throw new Error([value.message, value.suggestion].filter(Boolean).join('；') || '操作失败，旧资料已保留')
    if (!disposed && ticket === generation) {
      info.value = value.info; failed.value = false
      message.success(action === 'refresh' ? '平台资料已更新，未重新下载视频' : action === 'comments'
        ? `已获取 ${value.info?.comments?.count ?? 0} 条热门评论，未加入成片` : '中文概要已重新整理')
    }
  } catch (e) {
    if (!disposed && ticket === generation) error.value = e instanceof Error ? e.message : '请求失败，旧资料仍保留。请重新读取确认结果。'
  } finally {
    if (!disposed && ticket === generation) {
      pending.value = ''; busy.value = ''
      void loader.refresh(true)
    }
  }
}

function safeLink(value: unknown): string {
  try { const url = new URL(String(value)); return ['https:', 'http:'].includes(url.protocol) && !url.username ? url.href : '' } catch { return '' }
}
function dateTime(value: unknown): string {
  const date = new Date(String(value || ''))
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN')
}
function timecode(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}
const content = computed(() => info.value?.content || {})
const coverPreview = computed(() => {
  const url = safeLink(content.value.thumbnail)
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'https:' && ['i.ytimg.com', 'img.youtube.com'].includes(parsed.hostname)
        && !parsed.port && !parsed.username && !parsed.password)
      return `/api/projects/${props.uid}/steps/download/video-info/cover?v=${encodeURIComponent(url)}`
  } catch { /* 无效地址不请求后端。 */ }
  return url
})
watch(() => content.value.thumbnail, () => { coverBroken.value = false })
const comments = computed(() => info.value?.comments)
const title = computed(() => String(info.value?.title_zh || info.value?.title_original || '').trim())
const originalTitle = computed(() => String(info.value?.title_original || '').trim())
const summary = computed(() => String(info.value?.summary_zh || '').trim())
const description = computed(() => String(info.value?.description || '').trim())

const STATUS_TEXT: Record<string, string> = {
  generated: '已自动整理',
  local_no_metadata: '本地基础信息',
  insufficient_source_info: '来源信息不足',
  llm_unavailable: '自动整理不可用',
  pending: '整理中',
  not_generated: '尚未生成概要',
}
const statusText = computed(() =>
  STATUS_TEXT[String(info.value?.summary_status || '')] || '基础信息')

const source = computed(() => info.value?.source || {})
const sourceRows = computed(() => {
  const rows: Array<{ label: string, value: string, link?: boolean }> = []
  const push = (label: string, value: unknown, link = false) => {
    const text = String(value ?? '').trim()
    if (text) rows.push({ label, value: text, link })
  }
  push('平台', source.value.platform)
  push('链接', source.value.url || source.value.url_original, true)
  push('作者 / 频道', source.value.uploader || source.value.channel)
  push('频道 ID', source.value.channel_id || source.value.uploader_id)
  push('频道主页', source.value.channel_url || source.value.uploader_url, true)
  push('视频 ID', source.value.video_id)
  push('上传日期', source.value.upload_date)
  push('首发日期', source.value.release_date)
  if (typeof source.value.timestamp === 'number') push('上线时间', new Date(source.value.timestamp * 1000).toLocaleString('zh-CN'))
  push('备用标题', content.value.alt_title)
  push('时长', Number(source.value.duration) > 0 ? fmtDurCN(Number(source.value.duration)) : '')
  for (const [label, key] of [['播放量', 'view_count'], ['点赞数', 'like_count'], ['评论数', 'comment_count'], ['频道订阅数', 'channel_follower_count']]) {
    const value = source.value[key!]
    push(label!, typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '未获取')
  }
  if (typeof source.value.channel_is_verified === 'boolean') push('认证频道', source.value.channel_is_verified ? '是' : '否')
  push('视频语言', content.value.language)
  push('许可', content.value.license)
  push('拍摄地点', content.value.location)
  const live: Record<string, string> = { not_live: '普通视频', is_live: '直播中', is_upcoming: '即将直播', was_live: '直播回放', post_live: '直播结束，回放处理中' }
  const availability: Record<string, string> = { public: '公开', unlisted: '不公开列出', private: '私密', premium_only: '付费内容', subscriber_only: '订阅者专属', needs_auth: '需要登录' }
  push('直播状态', live[content.value.live_status] || content.value.live_status)
  push('访问范围', availability[content.value.availability] || content.value.availability)
  if (typeof content.value.age_limit === 'number') push('年龄限制', content.value.age_limit ? `${content.value.age_limit}+` : '无年龄限制')
  push('创作者', (content.value.creators || []).join('、'))
  push('艺人', (content.value.artists || []).join('、'))
  for (const [label, key] of [['曲目', 'track'], ['专辑', 'album'], ['系列', 'series'], ['分集', 'episode']]) push(label!, content.value[key!])
  push('季数', content.value.season_number)
  push('集数', content.value.episode_number)
  return rows
})

async function writeClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', 'true')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  document.execCommand('copy')
  area.remove()
}

async function copy(text: string | undefined, label: string) {
  const value = String(text || '').trim()
  if (!value) return
  try {
    await writeClipboard(value)
    message.success(`${label}已复制`)
  } catch {
    message.error('复制失败，请手动选择文本')
  }
}

function copySource() {
  const text = sourceRows.value.map(row => `${row.label}：${row.value}`).join('\n')
  void copy(text, '来源信息')
}
</script>

<template>
  <section class="video-info">
    <header class="workspace-head">
      <h3>视频资料</h3>
      <div class="toolbar">
        <n-tooltip><template #trigger><n-button size="small" secondary :disabled="!canRefresh || !!active" :loading="active === 'refresh'" @click="runAction('refresh')">刷新平台资料</n-button></template>只更新标题、简介等平台资料；不重新下载视频，不影响字幕、配音和成片，不调用 AI。</n-tooltip>
        <n-tooltip><template #trigger><n-button size="small" secondary :disabled="!canSummarize || !!active" :loading="active === 'summarize'" @click="runAction('summarize')">整理中文标题与概要</n-button></template>调用已配置的 LLM，根据平台资料生成中文标题与概要；不分析完整视频。</n-tooltip>
      </div>
    </header>
    <n-alert v-if="['refresh', 'comments', 'summarize'].includes(active)" type="info" :show-icon="false" class="notice">
      {{ active === 'refresh' ? '正在刷新平台资料…' : active === 'comments' ? '正在获取热门评论…' : '正在整理中文标题与概要…' }}
    </n-alert>
    <n-alert v-if="error" type="error" :show-icon="false" class="notice">{{ error }}</n-alert>
    <n-alert v-if="failed" type="warning" :show-icon="false" class="notice">资料读取失败。<n-button size="tiny" @click="loader.refresh(true)">重试</n-button></n-alert>

    <template v-if="info">
      <div class="hero">
        <a v-if="safeLink(content.thumbnail) && !coverBroken" :href="safeLink(content.thumbnail)" target="_blank" rel="noopener noreferrer" class="cover-link" title="查看原始封面">
          <img :src="coverPreview" :alt="title + ' · 平台封面'" loading="lazy" referrerpolicy="no-referrer" class="cover" @error="coverBroken = true" />
        </a>
        <div v-else class="cover-placeholder">
          <span>{{ coverBroken ? '封面加载失败' : '暂无封面' }}</span>
          <n-button v-if="coverBroken" size="tiny" quaternary @click="coverBroken = false">重试</n-button>
        </div>
        <div class="hero-content">
          <div class="eyebrow"><span>{{ source.platform }}</span><span v-if="source.uploader || source.channel">{{ source.uploader || source.channel }}</span></div>
          <div class="title-row"><h2>{{ title || '暂无标题' }}</h2><n-button size="tiny" quaternary @click="copy(title, '标题')">复制</n-button></div>
          <div v-if="originalTitle && originalTitle !== title" class="original-title">
            <span>{{ originalTitle }}</span><n-button size="tiny" quaternary @click="copy(originalTitle, '原标题')">复制原文</n-button>
          </div>
          <div class="meta-line"><span v-if="source.upload_date">{{ source.upload_date }}</span><span v-if="source.duration">{{ fmtDurCN(source.duration) }}</span>
            <a v-if="safeLink(source.url)" :href="safeLink(source.url)" target="_blank" rel="noopener noreferrer">视频原页 ↗</a>
            <n-tooltip><template #trigger><span class="updated" tabindex="0">更新于 {{ dateTime(info.collected_at || info.generated_at) }}</span></template>平台资料采集时间；播放量、点赞数等是此时的快照。</n-tooltip>
          </div>
        </div>
      </div>
      <n-alert v-if="info.summary_stale" type="warning" :show-icon="false" class="notice">平台资料已变化，中文标题与概要待更新。</n-alert>
      <section class="summary-card">
        <div class="section-head">
          <h4>内容概要 <n-tooltip><template #trigger><n-tag size="tiny" :bordered="false" tabindex="0">AI</n-tag></template>{{ info.summary_basis || '依据平台标题与简介，未读取完整视频或字幕正文。' }}<br />{{ statusText }} · {{ dateTime(info.summary_generated_at) }}</n-tooltip></h4>
          <n-button v-if="summary" size="tiny" quaternary @click="copy(summary, '内容概要')">复制</n-button>
        </div>
        <p v-if="summary" class="summary-text">{{ summary }}</p>
        <n-text v-else depth="3" :title="info.summary_note || '点击上方按钮生成中文概要'">暂无概要</n-text>
      </section>
      <div v-if="content.categories?.length || content.tags?.length" class="tags">
        <n-tag v-for="category in content.categories || []" :key="'c:' + category" size="small" type="info" :bordered="false">{{ category }}</n-tag>
        <n-tag v-for="tag in content.tags || []" :key="'t:' + tag" size="small" :bordered="false">{{ tag }}</n-tag>
      </div>
      <n-collapse class="details" :default-expanded-names="['description']">
        <n-collapse-item title="视频简介" name="description">
          <template #header-extra><n-button v-if="description" size="tiny" quaternary @click.stop="copy(description, '视频简介')">复制原文</n-button></template>
          <div v-if="description" class="description">{{ description }}</div><n-text v-else depth="3">暂无简介</n-text>
        </n-collapse-item>
        <n-collapse-item :title="'章节 · ' + (content.chapters?.length || 0)" name="chapters">
          <ol v-if="content.chapters?.length" class="chapters"><li v-for="(chapter, index) in content.chapters" :key="index"><span class="timecode">{{ timecode(chapter.start_time) }}</span>{{ chapter.title }}</li></ol>
          <n-text v-else depth="3">未获取到章节</n-text>
        </n-collapse-item>
        <n-collapse-item title="来源与统计" name="source">
          <template #header-extra><n-button size="tiny" quaternary @click.stop="copySource()">复制</n-button></template>
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item v-for="row in sourceRows" :key="row.label" :label="row.label"><a v-if="row.link && safeLink(row.value)" :href="safeLink(row.value)" target="_blank" rel="noopener noreferrer" class="source-link">{{ row.value }}</a><template v-else>{{ row.value }}</template></n-descriptions-item>
          </n-descriptions>
        </n-collapse-item>
        <n-collapse-item :title="'字幕语言 · 人工 ' + (content.captions?.manual?.length || 0) + ' / 自动 ' + (content.captions?.automatic?.length || 0)" name="captions">
          <n-tooltip><template #trigger><n-text depth="3" tabindex="0">可用语言清单 ⓘ</n-text></template>这里只登记语言，不下载字幕正文，也不会替换项目字幕。自动项可能包含平台翻译版本。</n-tooltip>
          <div v-for="group in [{ key: 'manual', label: '人工字幕' }, { key: 'automatic', label: '自动字幕 / 翻译' }]" :key="group.key" class="caption-group"><h4>{{ group.label }}</h4>
            <div class="tags"><n-tag v-for="caption in content.captions?.[group.key] || []" :key="caption.language" size="small" :title="caption.formats?.join(' / ')">{{ caption.name }} · {{ caption.language }}</n-tag></div>
            <n-text v-if="!content.captions?.[group.key]?.length" depth="3">未获取</n-text>
          </div>
        </n-collapse-item>
      </n-collapse>
    </template>
    <n-text v-else-if="loading" depth="3">正在读取资料…</n-text>
    <n-text v-else-if="!failed" depth="3">{{ canRefresh ? '暂无资料，点击“刷新平台资料”即可获取。' : '本地文件没有可刷新平台资料。' }}</n-text>
    <section v-if="canComments || comments" class="comments" aria-label="评论精选">
      <header class="workspace-head">
        <h3>评论精选 <n-tooltip><template #trigger><n-text depth="3" class="help-icon" tabindex="0">ⓘ</n-text></template>默认只采集评论数。可另取 1–500 条平台热门顶层评论，不含回复，不保证全站最高赞。重新获取会替换原文列表；已确认的上屏方案独立保留。</n-tooltip></h3>
        <div class="toolbar">
          <n-input-number :input-props="{ 'aria-label': '热门评论采集数量' }" v-model:value="commentLimit" :min="1" :max="500" :precision="0" :disabled="!!active || automation?.state==='running'" style="width:108px"><template #suffix>条</template></n-input-number>
          <n-button size="small" :disabled="!canComments || !!active || !validCommentLimit || automation?.state==='running'" :loading="active === 'comments'" @click="runAction('comments')">{{ comments ? '刷新热门评论' : '获取热门评论' }}</n-button>
        </div>
      </header>
      <CommentStudioPanel :uid="uid" :version="comments?.collected_at || ''" :automation="automation" :video-duration="Number(info?.source?.duration) || 0" :blocked="!!pending || !!busy" @busy-change="commentWork = $event" @show-diagnostics="emit('show-diagnostics')" />
    </section>
  </section>
</template>

<style scoped>
.video-info { border:1px solid rgba(128,128,128,.2); border-radius:12px; padding:20px; margin-top:14px; }
.workspace-head, .toolbar, .section-head, .title-row { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.workspace-head { flex-wrap:wrap; margin-bottom:18px; }
h3 { margin:0; font-size:16px; font-weight:650; } h4 { margin:0; font-size:13px; font-weight:600; }
.toolbar { flex-wrap:wrap; justify-content:flex-end; }
.hero { display:grid; grid-template-columns: minmax(170px, 240px) minmax(0,1fr); align-items:start; gap:22px; }
.hero-content { min-width:0; }
.cover-link { display:block; } .cover { display:block; width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:9px; background:#10151d; }
.cover-placeholder { aspect-ratio:16/9; display:flex; flex-direction:column; align-items:center; justify-content:center; background:rgba(128,128,128,.08); border-radius:9px; color:var(--text-muted,#93a0b0); font-size:12px; }
.eyebrow, .meta-line { display:flex; flex-wrap:wrap; align-items:center; gap:6px 14px; color:var(--text-muted,#93a0b0); font-size:12px; }
.title-row { align-items:flex-start; margin:8px 0; } .title-row h2 { font-size:22px; line-height:1.4; font-weight:650; margin:0; overflow-wrap:anywhere; }
.title-row .n-button { flex:none; }
.original-title { display:flex; align-items:flex-start; gap:8px; font-size:14px; line-height:1.5; color:var(--text-muted,#9eacbf); }
.original-title span { overflow-wrap:anywhere; } .original-title .n-button { flex:none; }
.meta-line { margin-top:14px; font-size:11px; } .updated { opacity:.7; }
.summary-card { border-left:3px solid rgba(83,161,208,.5); background:rgba(128,128,128,.045); border-radius:0 8px 8px 0; padding:12px 16px; margin-top:20px; }
.section-head { margin-bottom:7px; } .summary-text { white-space:pre-wrap; overflow-wrap:anywhere; font-size:14px; line-height:1.8; margin:0; }
.tags { display:flex; flex-wrap:wrap; gap:6px; max-height:130px; overflow:auto; margin-top:14px; }
.details { margin-top:20px; } .caption-group { margin-top:12px; }
.description { max-height:240px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; line-height:1.8; }
.source-link { overflow-wrap:anywhere; } .chapters { list-style:none; padding:0; margin:0; max-height:260px; overflow:auto; }
.chapters li { display:flex; gap:14px; padding:6px 0; } .timecode { flex:0 0 55px; font-variant-numeric:tabular-nums; color:var(--text-muted,#9fb0c3); }
.comments { border-top:1px solid rgba(128,128,128,.18); margin-top:24px; padding-top:20px; }
.help-icon { font-size:13px; cursor:help; } .notice { margin-bottom:12px; }
@media(max-width:700px) { .video-info { padding:14px; } .hero { grid-template-columns:1fr; gap:14px; } .cover-link,.cover-placeholder { max-width:360px; } .title-row h2 { font-size:19px; } .workspace-head { align-items:flex-start; } }
</style>
