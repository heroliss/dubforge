import { createRouter, createWebHistory } from 'vue-router'

// 路由按里程碑逐步充实: M4 起补 管线详情/新建项目 等页面
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'projects', component: () => import('./views/ProjectsView.vue') },
    { path: '/projects/:uid', name: 'pipeline', component: () => import('./views/PipelineView.vue') },
    { path: '/projects/:uid/steps/:key', name: 'step', component: () => import('./views/StepView.vue') },
    { path: '/projects/:uid/workbench', name: 'workbench-p', component: () => import('./views/WorkbenchView.vue') },
    { path: '/workbench', name: 'workbench', component: () => import('./views/WorkbenchView.vue') },
    { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue') },
    { path: '/about', name: 'about', component: () => import('./views/AboutView.vue') },
    { path: '/help', name: 'help', component: () => import('./views/HelpView.vue') },
    // 兜底: 未知路径 (如手输 /projects) 回到项目列表, 避免 router-view 空白
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
