import { createRouter, createWebHistory } from 'vue-router'

// 路由按里程碑逐步充实: M4 起补 管线详情/新建项目 等页面
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'projects', component: () => import('./views/ProjectsView.vue') },
    { path: '/publish', name: 'publish', component: () => import('./views/PublishView.vue') },
    { path: '/projects/:uid/publish', name: 'project-publish', component: () => import('./views/PublishView.vue') },
    { path: '/projects/:uid/steps/publish', redirect: to => `/projects/${to.params.uid}/publish` },
    { path: '/projects/:uid', name: 'pipeline', component: () => import('./views/PipelineView.vue') },
    { path: '/projects/:uid/steps/dubtask', redirect: to => `/projects/${to.params.uid}/steps/tts` },
    { path: '/projects/:uid/steps/workbench', redirect: to => `/projects/${to.params.uid}/steps/tts` },
    { path: '/projects/:uid/steps/subtitles', redirect: to => ({ path: `/projects/${to.params.uid}/steps/tts`, query: { panel: 'subtitles' } }) },
    { path: '/projects/:uid/steps/:key', name: 'step', component: () => import('./views/StepView.vue') },
    { path: '/projects/:uid/workbench', redirect: to => `/projects/${to.params.uid}/steps/tts` },
    { path: '/workbench', redirect: to => to.query.uid ? `/projects/${encodeURIComponent(String(to.query.uid))}/steps/tts` : '/' },
    { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue') },
    { path: '/templates', redirect: { path: '/settings', query: { section: 'production' } } },
    { path: '/about', name: 'about', component: () => import('./views/AboutView.vue') },
    { path: '/help', name: 'help', component: () => import('./views/HelpView.vue') },
    // 兜底: 未知路径 (如手输 /projects) 回到项目列表, 避免 router-view 空白
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
