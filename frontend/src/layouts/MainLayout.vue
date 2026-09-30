<script setup lang="ts">
// 主布局: 左侧导航 (全局 + 项目两段) + 内容区
// - 全局入口永远固定在顶部；制作规则与系列模板统一放在设置中。
// - 打开过项目后, 项目导航区 (项目名 + 总览 + 管线步骤实时徽章)
//   常驻在全局项下方: 切到设置/帮助等全局页不会清掉; 只有打开另一个项目才替换
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useNavStore, NAV_STEP_ORDER, STEP_LABELS, STATUS_TEXT } from '../stores/nav'
import { stepDisplayStatus } from '../utils/stepFeedback'
import AppIcon from '../components/AppIcon.vue'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const narrowScreen = ref(false)
const nav = useNavStore()
const mobileOpen = ref(false)
const pageName = computed(() => route.name === 'step' ? STEP_LABELS[route.params.key as string]
  : ({ projects: '项目空间', pipeline: '项目总览', publish: '发布中心', 'project-publish': '发布到B站', settings: '设置', help: '使用帮助', about: '关于 DubForge' } as Record<string, string>)[route.name as string] || '项目空间')
watch(() => route.fullPath, () => { mobileOpen.value = false })

function syncNarrowScreen() {
  narrowScreen.value = window.innerWidth <= 720
}
onMounted(() => {
  syncNarrowScreen()
  window.addEventListener('resize', syncNarrowScreen)
})
onUnmounted(() => window.removeEventListener('resize', syncNarrowScreen))

watch(() => route.params.uid, (u) => {
  if (u && u !== nav.uid) nav.enter(u as string)
}, { immediate: true })

// 链接负责文字点击，菜单负责图标与留白；阻止一次点击触发两次导航及离开提醒。
const menuLinkClick = (event: MouseEvent) => event.stopPropagation()
const globalOptions = [
  { label: () => h(RouterLink, { to: '/', onClick: menuLinkClick }, { default: () => '项目空间' }), icon: () => h(AppIcon, { name: 'projects' }), key: 'projects' },
  { label: () => h(RouterLink, { to: '/publish', onClick: menuLinkClick }, { default: () => '发布中心' }), icon: () => h(AppIcon, { name: 'publish' }), key: 'publish' },
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
  ...NAV_STEP_ORDER.map((k, index) => {
    const s = nav.stepMap[k]
    const status = s ? (s.status === 'done' && !s.fresh ? 'stale' : stepDisplayStatus(s)) : 'pending'
    return {
      label: () => h(RouterLink, {
        class: 'step-nav-link', title: s?.label ?? STEP_LABELS[k],
        onClick: menuLinkClick,
        to: `/projects/${nav.uid}/steps/${k}`,
      }, { default: () => [h('span', s?.label ?? STEP_LABELS[k]), h('i', { class: `nav-status nav-status-${status}`, title: STATUS_TEXT[status] || '待运行', 'aria-label': STATUS_TEXT[status] || '待运行' })] }),
      icon: () => h('span', { class: 'step-menu-number', title: s?.label ?? STEP_LABELS[k] }, String(index + 1)),
      key: k,
    }
  }),
  {
    label: () => h(RouterLink, { class: 'step-nav-link', to: `/projects/${nav.uid}/publish`, onClick: menuLinkClick },
      { default: () => [h('span', '发布到B站'), h('i', { class: `nav-status nav-status-${nav.publicationDisplay.dot}`, title: `${nav.publicationDisplay.text} · ${nav.publicationDisplay.note}`, 'aria-label': nav.publicationDisplay.text })] }),
    icon: () => h('span', { class: 'step-menu-number' }, String(NAV_STEP_ORDER.length + 1)),
    key: 'publish',
  },
])

function navigateGlobal(key: string) {
  void router.push(key === 'projects' ? '/' : `/${key}`)
}
function navigateProject(key: string) {
  void router.push(key === 'overview' ? `/projects/${nav.uid}` : key === 'publish' ? `/projects/${nav.uid}/publish` : `/projects/${nav.uid}/steps/${key}`)
}

const GLOBAL_KEYS = ['projects', 'publish', 'settings', 'help', 'about']
const globalActive = computed(() =>
  GLOBAL_KEYS.includes(route.name as string) ? route.name as string : null)
const projActive = computed(() => {
  if (route.name === 'step') return route.params.key as string
  if (route.name === 'pipeline') return 'overview'
  if (route.name === 'project-publish') return 'publish'
  return null
})
</script>

<template>
  <n-layout has-sider class="app-shell" @keydown.esc="mobileOpen = false">
    <button v-if="narrowScreen && mobileOpen" class="mobile-scrim" aria-label="关闭导航" @click="mobileOpen = false" />
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
      <div class="brand">
        <span class="brand-icon"><AppIcon name="wave" :size="22" /></span>
        <span v-if="!collapsed || narrowScreen" class="brand-wordmark">DubForge<small>视频译制工作台</small></span>
        <button v-if="narrowScreen" class="icon-button close-nav" aria-label="关闭导航" @click="mobileOpen = false"><AppIcon name="close" /></button>
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
        <n-menu :options="projectOptions" :value="projActive"
                @update:value="navigateProject"
                :collapsed="narrowScreen ? false : collapsed" :collapsed-width="64" :collapsed-icon-size="20" />
      </template>
      <div v-if="!nav.uid && (!collapsed || narrowScreen)" class="sidebar-hint">打开一个项目，<br />在这里切换制作步骤。</div>
    </n-layout-sider>
    <n-layout class="workspace-main" :native-scrollbar="false" :inert="narrowScreen && mobileOpen">
      <header class="workspace-topbar">
        <button v-if="narrowScreen" class="icon-button" aria-label="打开导航" :aria-expanded="mobileOpen" @click="mobileOpen = true"><AppIcon name="menu" /></button>
        <nav class="breadcrumbs" aria-label="当前位置">
          <RouterLink to="/">工作空间</RouterLink><AppIcon name="chevron" :size="13" />
          <template v-if="route.params.uid"><RouterLink :to="`/projects/${nav.uid}`" class="breadcrumb-project">{{ nav.title || '当前项目' }}</RouterLink><AppIcon name="chevron" :size="13" /></template>
          <span aria-current="page">{{ pageName }}</span>
        </nav>
        <span class="workspace-caption"><span /> 本地工作台</span>
      </header>
      <!-- 通过插槽把 key 放到实际页面组件上。
           直接给 router-view 加 key 会让路由容器本身反复重建，热更新/快速切换时
           旧页面可能短暂保留；实际组件 key 能保证内容区始终只有一个页面实例。 -->
      <router-view v-slot="{ Component }">
        <component :is="Component" :key="route.path" />
      </router-view>
    </n-layout>
  </n-layout>
</template>

<style scoped>
.app-shell { height: 100vh; height: 100dvh; }
.workspace-main { min-width: 0; }
.workspace-topbar { min-height: 58px; padding: 12px 32px; display: flex; gap: 12px; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--df-border); background: #141920; }
.breadcrumbs { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--df-muted); font-size: 12px; }
.breadcrumbs a { color: var(--df-muted); text-decoration: none; }
.breadcrumbs a:hover { color: var(--df-accent); }
.breadcrumbs > span { color: var(--df-text); white-space: nowrap; }
.breadcrumb-project { max-width: 230px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-caption { display: flex; align-items: center; gap: 7px; white-space: nowrap; color: var(--df-muted); font-size: 11px; }
.workspace-caption > span { width: 6px; height: 6px; border-radius: 50%; background: #78c9a5; }
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
  .workspace-caption, .breadcrumb-project, .breadcrumb-project + svg { display: none; }
  .breadcrumbs { gap: 6px; }
}
</style>
