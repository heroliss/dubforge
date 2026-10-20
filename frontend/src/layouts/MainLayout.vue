<script setup lang="ts">
// 主布局: 左侧导航 (全局 + 项目两段) + 内容区
// - 全局入口永远固定在顶部；制作规则与系列模板统一放在设置中。
// - 打开过项目后, 项目导航区 (项目名 + 总览 + 管线步骤实时徽章)
//   常驻在全局项下方: 切到设置/帮助等全局页不会清掉; 只有打开另一个项目才替换
import { computed, defineAsyncComponent, h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useNavStore, STEP_LABELS, STATUS_TEXT } from '../stores/nav'
import { useProjectListStore } from '../stores/projectList'
import { WORKFLOW_STAGES, stagePath } from '../utils/workflowStages'
import { stepDisplayStatus } from '../utils/stepFeedback'
import AppIcon from '../components/AppIcon.vue'
import { NAlert, NButton } from 'naive-ui'
import { BACKEND_CONNECTION_NOTICE, friendlyError } from '../utils/http'
import { LLM_DIALOG_EVENT, openLlmConversations, type LlmConversationFilter } from '../utils/llmDialog'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const narrowScreen = ref(false)
const nav = useNavStore()
const listSnapshots = useProjectListStore()
const LlmConversationDrawer = defineAsyncComponent(() => import('../components/LlmConversationDrawer.vue'))
const llmOpen = ref(false), llmFilter = ref<LlmConversationFilter>({})
function showLlm(event: Event) { llmFilter.value = (event as CustomEvent).detail || {}; llmOpen.value = true }
const restoringArchive = ref(false)
const archiveFeedback = ref<{ uid: string; message: string; type: 'error' | 'success' } | null>(null)
const projectPage = computed(() => !!route.params.uid && route.params.uid === nav.uid && !nav.suspended)
async function restoreArchivedProject() {
  if (!projectPage.value || !nav.archived || restoringArchive.value) return
  const uid = nav.uid
  const cacheContext = listSnapshots.context
  restoringArchive.value = true
  archiveFeedback.value = null
  try {
    const response = await fetch(`/api/projects/${uid}/archive`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archived: false }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok || data.ok !== true || data.archived !== false) throw new Error(data.message || '恢复项目失败')
    if (cacheContext === listSnapshots.context) listSnapshots.changed()
    nav.archiveChanged(uid, false, null)
    archiveFeedback.value = { uid, type: 'success', message: '项目已恢复，可以继续编辑、制作和发布。' }
    if (nav.uid === uid) await nav.poll()
  } catch (error) {
    archiveFeedback.value = { uid, type: 'error', message: friendlyError(error, '恢复项目失败，请稍后重试') }
  } finally { restoringArchive.value = false }
}
const mobileOpen = ref(false)
const navigationContent = ref<HTMLElement | null>(null)
const navigationTrigger = ref<HTMLButtonElement | null>(null)
const navigationClose = ref<HTMLButtonElement | null>(null)
const currentPageLabel = ref<HTMLElement | null>(null)
let navigationFocusVersion = 0
const pageName = computed(() => route.name === 'step' ? STEP_LABELS[route.params.key as string]
  : ({ projects: '项目空间', pipeline: '项目总览', 'project-comments':'上屏评论',publications: '全部发布记录', 'publication-record': '发布记录', 'project-publish': '发布到B站', settings: '设置', help: '使用帮助', about: '关于 DubForge' } as Record<string, string>)[route.name as string] || '项目空间')
watch(() => route.fullPath, () => { void closeMobileNavigation(true) })

async function openMobileNavigation() {
  if (!narrowScreen.value) return
  const ticket = ++navigationFocusVersion
  mobileOpen.value = true
  await nextTick()
  if (ticket === navigationFocusVersion && narrowScreen.value && mobileOpen.value) navigationClose.value?.focus({ preventScroll: true })
}
async function closeMobileNavigation(routeChanged = false) {
  if (!mobileOpen.value) return
  const ticket = ++navigationFocusVersion
  mobileOpen.value = false
  await nextTick()
  if (ticket !== navigationFocusVersion || mobileOpen.value || !narrowScreen.value) return
  // 主动关闭返回触发按钮；导航完成后移交到新页面位置，不能把焦点留在 inert 侧栏中。
  ;(routeChanged ? currentPageLabel.value : navigationTrigger.value)?.focus({ preventScroll: true })
}
function onNavigationKeydown(event: KeyboardEvent) {
  if (!narrowScreen.value || !mobileOpen.value) return
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void closeMobileNavigation(); return }
  if (event.key !== 'Tab') return
  const controls = Array.from(navigationContent.value?.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])') || [])
    .filter(element => !element.closest('[inert]') && element.getClientRects().length > 0)
  const first = controls[0], last = controls.at(-1), active = document.activeElement
  if (!first) { event.preventDefault(); return }
  if (!controls.includes(active as HTMLElement) || event.shiftKey && active === first || !event.shiftKey && active === last) {
    event.preventDefault(); (event.shiftKey ? last : first)?.focus({ preventScroll: true })
  }
}
function checkedTime(value: string) {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : ''
}

function syncNarrowScreen() {
  const previous = narrowScreen.value
  narrowScreen.value = window.innerWidth <= 720
  if (!narrowScreen.value) { navigationFocusVersion++; mobileOpen.value = false }
  else if (!previous && navigationContent.value?.contains(document.activeElement)) void nextTick(() => navigationTrigger.value?.focus({ preventScroll: true }))
}
onMounted(() => {
  // 热更新会先创建新布局，再卸载旧布局；挂载完成后重新取得轮询，避免旧清理挂起新页面。
  if (route.params.uid) nav.enter(route.params.uid as string)
  else nav.suspend()
  syncNarrowScreen()
  window.addEventListener('resize', syncNarrowScreen)
  window.addEventListener(LLM_DIALOG_EVENT, showLlm)
})
onUnmounted(() => { navigationFocusVersion++; window.removeEventListener('resize', syncNarrowScreen); window.removeEventListener(LLM_DIALOG_EVENT, showLlm); nav.suspend() })

watch(() => route.params.uid, (u) => {
  if (u) { if (u !== nav.uid || nav.suspended) nav.enter(u as string) }
  else nav.suspend()
}, { immediate: true })

// 链接负责文字点击，菜单负责图标与留白；阻止一次点击触发两次导航及离开提醒。
const menuLinkClick = (event: MouseEvent) => event.stopPropagation()
const globalOptions = [
  { label: () => h(RouterLink, { to: '/', onClick: menuLinkClick }, { default: () => '项目空间' }), icon: () => h(AppIcon, { name: 'projects' }), key: 'projects' },
  { label: () => h(RouterLink, { to: '/settings', onClick: menuLinkClick }, { default: () => '设置' }), icon: () => h(AppIcon, { name: 'settings' }), key: 'settings' },
  { label: () => h(RouterLink, { to: '/help', onClick: menuLinkClick }, { default: () => '使用帮助' }), icon: () => h(AppIcon, { name: 'help' }), key: 'help' },
  { label: () => h(RouterLink, { to: '/about', onClick: menuLinkClick }, { default: () => '关于' }), icon: () => h(AppIcon, { name: 'about' }), key: 'about' },
]

const projectOptions = computed(() => [
  {
    label: () => h(RouterLink, { to: `/projects/${nav.uid}`, onClick: menuLinkClick },
      { default: () => '总览' }),
    icon: () => h(AppIcon, { name: 'overview' }),
    key: 'overview',
  },
  ...WORKFLOW_STAGES.map((stage, index) => {
    const k=stage.key
    const s = stage.taskKeys.map(key=>nav.stepMap[key]).find(row=>['running','queued'].includes(row?.status)) || nav.stepMap[k]
    const status = stage.kind==='comments'?(nav.commentsDisplay.status==='attention'?'stale':nav.commentsDisplay.status):stage.kind==='publication'?nav.publicationDisplay.dot
      :s ? (s.status === 'done' && !s.fresh ? 'stale' : stepDisplayStatus(s) || 'unknown')
        :nav.stepsError ? 'unavailable' : nav.stepsLoaded || nav.suspended ? 'unknown' : 'loading'
    const label=stage.kind==='comments'?nav.commentsDisplay.label:stage.kind==='publication'?nav.publicationDisplay.text
      :status==='loading'?'正在读取':status==='unavailable'?'状态读取失败':STATUS_TEXT[status]||'状态待核对'
    const hint=stage.kind==='comments'?`${nav.commentsDisplay.label} · ${nav.commentsDisplay.detail}`
      :stage.kind==='publication'?`${nav.publicationDisplay.text} · ${nav.publicationDisplay.note}`
        :nav.stepsError?`${label} · ${nav.stepsError}`
          :status==='loading'?`${label} · 正在核对步骤与已有结果。`
            :status==='unknown'?`${label} · ${nav.suspended?'进入项目后读取并核对步骤状态。':'当前响应尚未提供该步骤状态，可进入工作区查看。'}`:label
    return {
      label: () => h(RouterLink, {
        class: 'step-nav-link', title: `${stage.label} · ${hint}`,
        onClick: menuLinkClick,
        to: stagePath(nav.uid,k),
      }, { default: () => [h('span', stage.label), h('i', { class: `nav-status nav-status-${status}`, title: hint, 'aria-label': label, 'aria-busy':status==='loading'?'true':undefined })] }),
      icon: () => h('span', { class: 'step-menu-number', title: stage.label }, String(index + 1)),
      key: k,
    }
  }),
])

function navigateGlobal(key: string) {
  void router.push(key === 'projects' ? '/' : `/${key}`)
}
function navigateProject(key: string) {
  void router.push(key === 'overview' ? `/projects/${nav.uid}` : stagePath(nav.uid,key))
}

const GLOBAL_KEYS = ['projects', 'settings', 'help', 'about']
const globalActive = computed(() =>
  ['publications', 'publication-record'].includes(route.name as string) ? 'projects'
    : GLOBAL_KEYS.includes(route.name as string) ? route.name as string : null)
const projActive = computed(() => {
  if (route.name === 'step') return route.params.key as string
  if (route.name === 'pipeline') return 'overview'
  if (route.name === 'project-publish') return 'publish'
  if (route.name === 'project-comments') return 'comments'
  return null
})
</script>

<template>
  <n-layout has-sider class="app-shell" @keydown="onNavigationKeydown">
    <button v-if="narrowScreen && mobileOpen" class="mobile-scrim" aria-label="关闭导航" tabindex="-1" @click="closeMobileNavigation()" />
    <n-layout-sider
      class="workspace-sidebar" :class="{ 'mobile-open': mobileOpen }"
      :inert="narrowScreen && !mobileOpen"
      bordered
      collapse-mode="width"
      :collapsed="narrowScreen ? false : collapsed"
      :collapsed-width="64"
      :width="232"
      :show-trigger="!narrowScreen"
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div id="workspace-navigation" ref="navigationContent">
      <div class="brand">
        <span class="brand-icon"><AppIcon name="wave" :size="22" /></span>
        <span v-if="!collapsed || narrowScreen" class="brand-wordmark">DubForge<small>视频译制工作台</small></span>
        <button v-if="narrowScreen" ref="navigationClose" class="icon-button close-nav" aria-label="关闭导航" @click="closeMobileNavigation()"><AppIcon name="close" /></button>
      </div>
      <div v-if="!collapsed || narrowScreen" class="nav-caption">工作空间</div>
      <n-menu :options="globalOptions" :value="globalActive"
              @update:value="navigateGlobal"
              :collapsed="narrowScreen ? false : collapsed" :collapsed-width="64" :collapsed-icon-size="20" />
      <template v-if="nav.uid">
        <div v-if="!collapsed || narrowScreen" class="nav-caption project-caption">当前项目</div>
        <div v-if="!collapsed || narrowScreen" class="proj-title" :title="nav.title">
          {{ nav.title || (nav.projectError ? '项目信息加载失败' : '正在读取项目…') }}
        </div>
        <div v-if="nav.archived && (!collapsed || narrowScreen)" class="sidebar-archive">已归档</div>
        <div v-if="nav.suspended && (!collapsed || narrowScreen)" class="sidebar-hint snapshot-hint">显示最近状态 · 进入项目后刷新</div>
        <n-menu :options="projectOptions" :value="projActive"
                @update:value="navigateProject"
                :collapsed="narrowScreen ? false : collapsed" :collapsed-width="64" :collapsed-icon-size="20" />
      </template>
      <div v-if="!nav.uid && (!collapsed || narrowScreen)" class="sidebar-hint">打开一个项目，<br />在这里切换制作步骤。</div>
      </div>
    </n-layout-sider>
    <n-layout class="workspace-main" :native-scrollbar="false" :inert="narrowScreen && mobileOpen">
    <n-alert v-if="nav.connectionLost && !nav.suspended" type="warning" style="margin: 12px 16px 0" data-testid="connection-lost">
      {{ BACKEND_CONNECTION_NOTICE }}
      <p v-for="issue in nav.connectionIssues" :key="issue.source" class="connection-check">{{ issue.label }}未更新 · {{ issue.checkedAt ? `最后成功核对 ${checkedTime(issue.checkedAt)}` : '尚未取得可信状态' }}</p>
    </n-alert>
      <header class="workspace-topbar">
        <button v-if="narrowScreen" ref="navigationTrigger" class="icon-button" aria-label="打开导航" aria-controls="workspace-navigation" :aria-expanded="mobileOpen" @click="openMobileNavigation"><AppIcon name="menu" /></button>
        <nav class="breadcrumbs" aria-label="当前位置">
          <RouterLink to="/">工作空间</RouterLink><AppIcon name="chevron" :size="13" />
          <template v-if="route.params.uid"><RouterLink :to="`/projects/${nav.uid}`" class="breadcrumb-project">{{ nav.title || '当前项目' }}</RouterLink><AppIcon name="chevron" :size="13" /></template>
          <span ref="currentPageLabel" tabindex="-1" aria-current="page">{{ pageName }}</span>
        </nav>
        <div class="workspace-topbar-actions"><n-button size="small" quaternary @click="openLlmConversations({projectUid: route.params.uid ? String(route.params.uid) : ''})">LLM对话</n-button><span class="workspace-caption"><span /> 本地工作台</span></div>
      </header>
      <n-alert v-if="projectPage && nav.archived" type="info" class="archive-notice" data-testid="project-archived">
        <div class="archive-notice-content"><span>项目已归档，文件与记录完整保留。可查看已有内容，恢复后可继续编辑、制作和发布。</span>
          <n-button size="small" secondary :loading="restoringArchive" :disabled="restoringArchive" @click="restoreArchivedProject">恢复项目</n-button>
        </div>
      </n-alert>
      <n-alert v-if="projectPage && archiveFeedback?.uid === nav.uid && (archiveFeedback.type !== 'success' || !nav.archived)" :type="archiveFeedback.type" class="archive-notice" closable @close="archiveFeedback = null">{{ archiveFeedback.message }}</n-alert>
      <!-- 通过插槽把 key 放到实际页面组件上。
           直接给 router-view 加 key 会让路由容器本身反复重建，热更新/快速切换时
           旧页面可能短暂保留；实际组件 key 能保证内容区始终只有一个页面实例。 -->
      <router-view v-slot="{ Component }">
        <component :is="Component" :key="route.path" />
      </router-view>
    </n-layout>
    <LlmConversationDrawer v-if="llmOpen" :show="llmOpen" :project-uid="llmFilter.projectUid" :operation="llmFilter.operation" :owner-id="llmFilter.ownerId" @update:show="value => llmOpen=value" />
  </n-layout>
</template>

<style scoped>
.app-shell { height: 100vh; height: 100dvh; }
.workspace-main { min-width: 0; }
.connection-check { margin:4px 0 0; font-size:12px; font-variant-numeric:tabular-nums; }
.archive-notice { margin: 16px 32px 0; }
.archive-notice-content { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.archive-notice-content > span { flex: 1; min-width: 180px; }
.sidebar-archive { margin: 0 24px 6px; color: var(--df-muted); font-size: 11px; }
.workspace-topbar { min-height: 58px; padding: 12px 32px; display: flex; gap: 12px; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--df-border); background: #141920; }
.breadcrumbs { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--df-muted); font-size: 12px; }
.breadcrumbs a { color: var(--df-muted); text-decoration: none; }
.breadcrumbs a:hover { color: var(--df-accent); }
.breadcrumbs > span { color: var(--df-text); white-space: nowrap; }
.breadcrumb-project { max-width: 230px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-caption { display: flex; align-items: center; gap: 7px; white-space: nowrap; color: var(--df-muted); font-size: 11px; }
.workspace-caption > span { width: 6px; height: 6px; border-radius: 50%; background: #78c9a5; }
.workspace-topbar-actions{display:flex;align-items:center;gap:14px;margin-left:auto}
.icon-button { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; color: var(--df-text); background: transparent; border: 1px solid var(--df-border); border-radius: 8px; cursor: pointer; flex-shrink: 0; }
.close-nav { margin-left: auto; }
.nav-caption { margin: 14px 24px 8px; font-size: 10px; color: #78889d; letter-spacing: .12em; }
.project-caption { border-top: 1px solid var(--df-border); padding-top: 16px; margin-top: 18px; }
.sidebar-hint { margin: 26px 24px; padding-top: 20px; border-top: 1px solid var(--df-border); color: #7e8ca0; font-size: 12px; line-height: 1.9; }
:deep(.nav-status) { width: 6px; height: 6px; border-radius: 50%; background: #566174; flex: 0 0 auto; }
:deep(.nav-status-done) { background: #78c9a5; }
:deep(.nav-status-running), :deep(.nav-status-queued) { background: #83b5ff; box-shadow: 0 0 0 3px #83b5ff18; }
:deep(.nav-status-failed), :deep(.nav-status-interrupted) { background: #ef9299; }
:deep(.nav-status-stale) { background: #efbd70; }
:deep(.nav-status-unavailable) { background: #efbd70; }
:deep(.nav-status-loading) { background: transparent; border: 1px solid #8d9aab; box-sizing: border-box; }
.brand {
  height: 78px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-icon { display: flex; align-items: center; justify-content: center; width: 34px; height: 36px; background: #83b5ff; color: #122238; border-radius: 10px; }
.brand-wordmark small { display: block; color: var(--df-muted); font-size: 10px; font-weight: 400; letter-spacing: 2px; margin-top: 1px; }
.brand-mode {
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0;
  color: #63b3ed;
  border: 1px solid rgba(99, 179, 237, .45);
  border-radius: 4px;
  padding: 1px 4px;
}
.proj-title {
  padding: 2px 24px 6px;
  font-size: 12px;
  color: #c3cdd9;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
:deep(.step-nav-link) { display: flex; align-items: center; gap: 8px; min-width: 0; }
:deep(.step-nav-link > span) { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:deep(.n-menu-item-content-header) { min-width: 0; }
:deep(.step-menu-number) { font-size: 12px; opacity: .65; font-variant-numeric: tabular-nums; }
@media (max-width: 720px) {
  .workspace-sidebar { position: fixed !important; inset: 0 auto 0 0; z-index: 101; transform: translateX(-100%); transition: transform .2s ease; }
  .workspace-sidebar.mobile-open { transform: translateX(0); }
  .mobile-scrim { position: fixed; inset: 0; z-index: 100; border: 0; background: rgba(0,0,0,.65); }
  .brand { justify-content: flex-start; padding: 0 16px; }
  .workspace-topbar { padding: 10px 14px; justify-content: flex-start; }
  .archive-notice { margin: 12px 14px 0; }
  .workspace-caption, .breadcrumb-project, .breadcrumb-project + svg { display: none; }
  .breadcrumbs { gap: 6px; }
}
</style>
