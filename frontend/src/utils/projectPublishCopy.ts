import type { ConfigurationSource } from './configurationSource'

export const PROJECT_COPY_FIELDS = ['title', 'description', 'tags', 'dynamic', 'source']

export type ProjectCopyPreview = {
  project_uid: string
  revision: number
  fingerprint: string
  content_fingerprint: string
  content: Record<string, any>
  content_sources: Record<string, ConfigurationSource>
  parent_configured: string[]
  configured: string[]
  source_published_date?: string
}

// 自动基础只参与显示；不复制成明确覆盖，也不把显式空值误当成待填空项。
export function resolveProjectCopy(base: Record<string, any>, overrides: Record<string, any>,
  sources: Record<string, ConfigurationSource>, preview: ProjectCopyPreview | null) {
  const values = { ...base }, resolvedSources = { ...sources }
  if (!preview) return { values, sources: resolvedSources }
  const inherited = new Set(preview.parent_configured)
  for (const key of PROJECT_COPY_FIELDS) {
    if (Object.hasOwn(overrides, key) || inherited.has('publish.' + key)) continue
    if (Object.hasOwn(preview.content, key)) {
      values[key] = preview.content[key]
      resolvedSources[key] = preview.content_sources?.[key] || { kind: 'content' }
    }
  }
  return { values, sources: resolvedSources }
}

export function copyRestoreLabels(preview: ProjectCopyPreview | null) {
  const inherited = new Set(preview?.parent_configured || [])
  return Object.fromEntries(PROJECT_COPY_FIELDS.map(key => [key,
    preview && !inherited.has('publish.' + key) ? '恢复自动填充' : '恢复继承']))
}
