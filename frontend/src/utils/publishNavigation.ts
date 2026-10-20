import type { RouteLocationNormalized, RouteLocationRaw } from 'vue-router'

// 旧入口只迁移导航；保留账号、BV、稿件编号等上下文，不触发发布操作。
export function legacyPublishTarget(to: Pick<RouteLocationNormalized, 'query' | 'hash'>): RouteLocationRaw {
  const workspace = String(to.query.manage || to.query.view || to.query.tab || to.query.workspace || to.query.section || to.hash.slice(1)).toLowerCase()
  if (['accounts', 'account', 'studio', 'bilibili'].includes(workspace)) {
    return { path: '/settings', query: { ...to.query, section: 'bilibili', ...(workspace === 'studio' ? { manage: 'studio' } : {}) }, hash: to.hash }
  }
  const publication = to.query.publication || to.query.id
  if (typeof publication === 'string' && publication) {
    return { path: `/publications/${encodeURIComponent(publication)}`, query: to.query, hash: to.hash }
  }
  return { path: '/publications', query: to.query, hash: to.hash }
}
