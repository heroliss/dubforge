<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { STEP_LABELS } from '../stores/nav'
const props = defineProps<{ plan: any }>()
// 兼容旧后端或浏览器缓存中的原始路径；新接口也会返回同一套中文标签。
const SETTING_LABELS: Record<string, string> = {
  'run.mode': '执行终点', 'assembly.versions': '成片版本',
  'publish.account_id': 'B站账号', 'publish.title': '标题', 'publish.description': '简介',
  'publish.tags': '标签', 'publish.tid': '分区（兼容字段）', 'publish.human_type2': '分区',
  'publish.copyright': '稿件类型', 'publish.source': '原视频来源', 'publish.declaration_id': '创作声明',
  'publish.visibility': '可见范围', 'publish.watermark': '水印', 'publish.close_comments': '关闭评论',
  'publish.close_danmaku': '关闭弹幕', 'publish.selected_comments': '精选评论',
  'publish.dynamic': '粉丝动态', 'publish.publish_at': '定时发布', 'publish.season_id': '合集',
  'publish.section_id': '合集小节', 'publish.topic_id': '活动/话题', 'publish.topic_name': '活动/话题名称',
  'publish.mission_id': '活动任务', 'publish.ai_copy': '自动补全文案', 'publish.cover_source': '封面来源',
  'publish.cover_id': '指定封面', 'publish.version_ids': '发布分P', 'publish.part_titles': '分P名称',
}
function settingLabel(change: any) {
  const path = String(change?.path || '')
  const label = String(change?.label || '').trim()
  if (label && label !== path) return label
  return SETTING_LABELS[path] || (path.startsWith('publish.') ? '发布设置（高级）' : label || '设置项')
}
function displayValue(value: unknown) {
  if (value === undefined) return '未记录'
  if (value === '') return '（空文本）'
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
}
const metadataByAccount = ref<Record<string, any>>({})
// 旧后端在热更新前可能没有返回 publication_context。只有目录中恰好
// 一个账号时才采用回退值，避免把平台名称误归到错误账号。
const metadataFallbackAccount = ref('')
const metadataLoading = ref(false)
const metadataFailed = ref(false)
let metadataAbort: AbortController | undefined
let disposed = false
function idValue(value: unknown): number | null {
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}
function categoryTitle(rows: any[], id: number): string {
  for (const row of rows || []) {
    if (idValue(row?.id) === id) return String(row?.name || row?.title || '').trim()
    const nested = categoryTitle(Array.isArray(row?.children) ? row.children : [], id)
    if (nested) return nested
  }
  return ''
}
function accountMetadata(project: any, side: 'before' | 'after') {
  const account = project?.publication_context?.[side]?.account_id || metadataFallbackAccount.value
  return account ? metadataByAccount.value[String(account)] : undefined
}
function seasonFor(project: any, side: 'before' | 'after') {
  const row = (project?.change_details || []).find((item: any) => item.path === 'publish.season_id')
  const id = idValue(project?.publication_context?.[side]?.season_id ?? row?.[side])
  const meta = accountMetadata(project, side)
  return id && Array.isArray(meta?.seasons) ? meta.seasons.find((item: any) => idValue(item?.id) === id) : undefined
}
function platformValue(project: any, change: any, side: 'before' | 'after') {
  const value = change?.[side]
  const id = idValue(value)
  const path = String(change?.path || '')
  const meta = accountMetadata(project, side)
  if (path === 'publish.human_type2' && id) {
    const title = categoryTitle(meta?.human_categories || [], id)
    return title ? `${title}（ID ${id}）` : `ID ${id}`
  }
  if (path === 'publish.season_id' && id) {
    const season = Array.isArray(meta?.seasons) ? meta.seasons.find((item: any) => idValue(item?.id) === id) : undefined
    return season?.title ? `${season.title}（ID ${id}）` : `ID ${id}`
  }
  if (path === 'publish.section_id' && id) {
    const season = seasonFor(project, side)
    const section = season?.sections?.find((item: any) => idValue(item?.id) === id)
    if (section?.title) return `${section.title}（ID ${id}）`
    return `ID ${id}`
  }
  if (path === 'publish.declaration_id' && id) {
    const declaration = (meta?.declarations || []).find((item: any) => idValue(item?.value) === id)
    return declaration?.label ? `${declaration.label}（ID ${id}）` : `ID ${id}`
  }
  if (path === 'publish.copyright') {
    return ({ 1: '原创', 2: '转载', 3: '其他创作声明' } as Record<number, string>)[Number(value)] || displayValue(value)
  }
  if (path === 'publish.visibility') return value === 'public' ? '公开可见' : value === 'self' ? '仅自己可见' : displayValue(value)
  if (path === 'publish.cover_source') return value === 'platform' ? '平台封面' : value === 'custom' ? '指定封面' : displayValue(value)
  return displayValue(value)
}
async function accountIds() {
  const ids = new Set<string>()
  for (const project of props.plan?.projects || []) {
    for (const side of ['before', 'after'] as const) {
      const id = project?.publication_context?.[side]?.account_id
      if (id) ids.add(String(id))
    }
  }
  // 兼容旧后端的预览响应：只有单账号时才能安全确定元数据来源。
  if (!ids.size && (props.plan?.projects || []).length) {
    try {
      const response = await fetch('/api/publishing/catalog')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const accounts = (await response.json())?.accounts
      if (Array.isArray(accounts) && accounts.length === 1 && accounts[0]?.id) {
        metadataFallbackAccount.value = String(accounts[0].id)
        ids.add(metadataFallbackAccount.value)
      }
    } catch {
      // 名称读取是预览增强；失败时继续展示 ID，不阻断应用设置。
    }
  }
  return [...ids]
}
async function loadMetadata() {
  const ids = await accountIds()
  if (!ids.length || disposed) return
  metadataAbort?.abort()
  const controller = new AbortController(); metadataAbort = controller
  metadataLoading.value = true; metadataFailed.value = false
  const results = await Promise.all(ids.map(async account => {
    try {
      const response = await fetch(`/api/publishing/accounts/${encodeURIComponent(account)}/metadata`, { method: 'POST', signal: controller.signal })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json()
      return [account, data] as const
    } catch {
      return null
    }
  }))
  if (disposed || controller.signal.aborted) return
  const next: Record<string, any> = {}
  let failed = false
  for (const result of results) {
    if (result) next[result[0]] = result[1]
    else failed = true
  }
  metadataByAccount.value = next
  metadataFailed.value = failed
  metadataLoading.value = false
}
onMounted(() => { void loadMetadata() })
onBeforeUnmount(() => { disposed = true; metadataAbort?.abort() })
</script>

<template>
  <div class="apply-preview">
    <p>更新所选项目的基础设置，保留项目单独设置。已有文件保留，不会自动制作或投稿；实际制作参数变化后，相关步骤才提示需更新。</p>
    <p v-if="metadataLoading" class="metadata-note">正在读取B站分区、合集和小节名称…</p>
    <p v-else-if="metadataFailed" class="metadata-note">部分B站名称读取失败，相关值已保留为 ID；不影响应用设置。</p>
    <section v-for="project in plan.projects" :key="project.uid" class="apply-project">
      <header><strong>{{ project.title || project.uid }}</strong><small>{{ project.changes.length }} 项生效值变化 · 保留 {{ project.preserved }} 项项目设置</small></header>
      <p v-if="project.affected_steps?.length" class="affected">可能需更新：{{ project.affected_steps.map((step: string) => STEP_LABELS[step] || step).join('、') }}</p>
      <p v-else class="muted">没有制作步骤需要因本次应用更新。</p>
      <details v-if="project.change_details?.length" :open="plan.projects.length === 1">
        <summary>查看具体变化</summary>
        <div class="change-table" role="table" aria-label="项目基础设置变化">
          <div class="change-heading" role="row"><span role="columnheader">设置项</span><span role="columnheader">当前项目值</span><span role="columnheader">应用后的值</span></div>
          <div v-for="change in project.change_details" :key="change.path" class="change-row" role="row">
            <span role="cell">{{ settingLabel(change) }}</span><pre role="cell">{{ platformValue(project, change, 'before') }}</pre><pre role="cell">{{ platformValue(project, change, 'after') }}</pre>
          </div>
        </div>
      </details>
      <p v-else-if="!project.changes.length" class="muted">当前项目已使用相同设置；确认后只更新基础来源记录。</p>
    </section>
  </div>
</template>

<style scoped>
.apply-preview{font-size:12px;max-height:60vh;overflow:auto;line-height:1.7}.apply-preview>p{margin:0 0 16px}.apply-project{padding:14px 0;border-top:1px solid var(--df-border)}header{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}header strong{font-size:13px}small,.muted{color:var(--df-muted)}.metadata-note{color:var(--df-muted);font-size:11px}.affected{color:var(--df-warning,#dfb66f)}summary{cursor:pointer;color:var(--df-accent);padding:5px 0}.change-heading,.change-row{display:grid;grid-template-columns:minmax(110px,.8fr) minmax(0,1fr) minmax(0,1fr);gap:12px;padding:9px 0;border-bottom:1px solid var(--df-border)}.change-heading{color:var(--df-muted)}.change-row pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.change-row>span{overflow-wrap:anywhere}@media(max-width:500px){.change-heading,.change-row{gap:8px;grid-template-columns:minmax(75px,.7fr) minmax(0,1fr) minmax(0,1fr)}}
</style>
