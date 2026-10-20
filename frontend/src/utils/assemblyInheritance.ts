import type { ConfigurationSource } from './configurationSource'
import { audioMode } from './audioQuality.ts'
import { videoEncoderLabel } from './assemblyEncoding.ts'

export type AssemblyInheritanceSummary = {
  source?: ConfigurationSource; source_name?: string; changed_count?: number; effective_count?: number
  preserved_count?: number; version_list_fixed?: boolean; preserved_version_count?: number; fingerprint?: string
  error?: string; message?: string
}
type Version = { id?: string; name: string; overrides: Record<string, any>; sources?: Record<string, ConfigurationSource> }

/** 版本专用值也可能来自基础清单，不能一律误标成本项目精调。 */
export function assemblyVersionFieldSource(version: Version, path: string, defaults: Record<string, ConfigurationSource | undefined> = {},
  persisted?: Version, definitionSource?: ConfigurationSource): ConfigurationSource | undefined {
  const owns = Object.hasOwn(version.overrides, path), had = !!persisted && Object.hasOwn(persisted.overrides, path)
  if (!owns) return defaults[path] || persisted?.sources?.[path]
  if (!had || JSON.stringify(version.overrides[path]) !== JSON.stringify(persisted!.overrides[path])) {
    return { kind: 'version', name: version.name, version_name: version.name }
  }
  if (persisted?.sources?.[path]) return { ...persisted.sources[path], version_name: version.name }
  if (definitionSource && ['global', 'series'].includes(definitionSource.kind)) return { ...definitionSource, version_name: version.name }
  return { kind: 'version', name: version.name, version_name: version.name }
}

export function assemblySettingValue(value: unknown, path?: string): string {
  if (path === 'assemble.video_encoder' && ['software', 'nvenc'].includes(String(value))) return videoEncoderLabel(value)
  if (path === 'assemble.final_loudness_mode' && ['off', 'normalize', 'uniform'].includes(String(value))) return audioMode(value)
  if (value === undefined) return '未记录'
  if (value === null) return '跟随默认'
  if (value === true) return '开启'
  if (value === false) return '关闭'
  if (value === '') return '（留空）'
  if (Array.isArray(value) && value.every(row => row && typeof row === 'object' && 'name' in row)) return value.map(row => String(row.name || '未命名版本')).join('、') || '无版本'
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}
