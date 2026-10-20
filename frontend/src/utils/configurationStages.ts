import { WORKFLOW_STAGES, settingsStageKey, workflowStage } from './workflowStages.ts'

export type ConfigurationSection = { label?: string; fields: any[]; [key: string]: any }
export type ConfigurationStageSection = ConfigurationSection & {
  label: string; stageKey: string; stageNumber?: number; category: 'stage' | 'shared'
}
export type ConfigurationStageEntry = [string, ConfigurationStageSection]

/** 设置栏沿用稳定的 schema key；地址和导航使用统一的工作阶段 key。 */
export function configurationStageKey(key: string | undefined): string | undefined {
  if (!key) return undefined
  return workflowStage(key)?.key ?? key
}

export function configurationSettingsKey(key: string | undefined): string | undefined {
  return key ? settingsStageKey(key) : undefined
}

/** 兼容独立字幕栏的旧 schema；只重排显示，不改配置路径、来源或已保存值。 */
export function configurationStageSections(schema: Record<string, ConfigurationSection> = {},
  requested?: string[], paths?: string[]): ConfigurationStageEntry[] {
  const output: ConfigurationStageEntry[] = [], emitted = new Set<string>(), consumed = new Set<string>()
  const allowedPaths = paths ? new Set(paths) : null
  function fieldsFor(keys: readonly string[], stageKey: string) {
    return keys.flatMap(key => {
      const section = schema[key]
      if (!section) return []
      consumed.add(key)
      return section.fields.filter(field => {
        const selected = !requested || requested.includes(stageKey)
          || requested.some(wanted => wanted !== 'subtitles' && keys.includes(wanted))
          || requested.includes('subtitles') && field.path.startsWith('subtitles.')
        if (!selected || field.path === 'assemble.burn_subtitles' || allowedPaths && !allowedPaths.has(field.path)
          || emitted.has(field.path)) return false
        emitted.add(field.path)
        return true
      }).map(field => field.path.startsWith('subtitles.') ? { ...field, group: '字幕导出与时间' } : field)
    })
  }
  WORKFLOW_STAGES.forEach((stage, index) => {
    const keys = stage.settingsKeys as readonly string[]
    const fields = fieldsFor(keys, stage.key)
    if (!fields.length) return
    output.push([keys[0], { ...schema[keys[0]], fields, label: stage.label,
      stageKey: stage.key, stageNumber: index + 1, category: 'stage' }])
  })
  for (const [key, section] of Object.entries(schema)) {
    if (consumed.has(key) || key === 'dubtask' || requested && !requested.includes(key)) continue
    const fields = fieldsFor([key], key)
    if (fields.length) output.push([key, { ...section, fields, label: section.label || key,
      stageKey: key, category: 'shared' }])
  }
  return output
}
