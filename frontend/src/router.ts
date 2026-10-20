import { createRouter, createWebHistory } from 'vue-router'
import { legacyPublishTarget } from './utils/publishNavigation'

// 路由按里程碑逐步充实: M4 起补 管线详情/新建项目 等页面
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'projects', component: () => import('./views/ProjectsView.vue') },
    { path: '/publish', name: 'publish', redirect: legacyPublishTarget },
    { path: '/publications', name: 'publications', component: () => import('./views/PublicationRecordsView.vue') },
    { path: '/publications/:id', name: 'publication-record', component: () => import('./views/PublishView.vue'), props: { recordOnly: true } },
    { path: '/projects/:uid/publish', name: 'project-publish', component: () => import('./views/PublishView.vue') },
    { path: '/projects/:uid/comments', name: 'project-comments', component: () => import('./views/CommentsView.vue') },
    { path: '/projects/:uid/steps/comments', redirect: to => ({path:`/projects/${to.params.uid}/comments`,query:to.query,hash:to.hash}) },
    { path: '/projects/:uid/steps/publish', redirect: to => ({ path: `/projects/${to.params.uid}/publish`, query: to.query, hash: to.hash }) },
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

// 原来藏在获取视频页中的评论工作区和诊断链接继续可用。
router.beforeEach(to=>{
  if(to.name==='step' && to.params.key==='download'
    && (to.hash==='#comments-studio' || to.hash==='#llm-conversation' || to.query.comment_action))
    return {path:`/projects/${to.params.uid}/comments`,query:to.query,hash:to.hash==='#llm-conversation'?'#llm-conversation':''}
})
