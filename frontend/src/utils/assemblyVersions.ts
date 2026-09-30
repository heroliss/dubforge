export type AssemblyVersion = { id: string; name: string; dubbing: boolean; burn_subtitles: boolean; include_comments: boolean; overrides: Record<string, any> }
// 只发送版本定义，派生的状态、来源和生效值不能意外变成覆盖值。
export function versionSpec(v: any): AssemblyVersion {
  return { id: v.id, name: v.name, dubbing: v.dubbing, burn_subtitles: v.burn_subtitles, include_comments: v.include_comments, overrides: { ...v.overrides } }
}
export function versionSummary(v: any, defaults: Record<string, any> = {}) {
  const values = { ...defaults, ...v.values, ...v.overrides }
  const subtitle = ({ source: '原文字幕', trans: '译文字幕', bilingual: '双语字幕' } as Record<string, string>)[values['assemble.subtitle_track']] || '跟随默认字幕'
  return `${v.dubbing ? '配音' : '原声'} · ${v.burn_subtitles ? subtitle : '无字幕'} · ${v.include_comments ? '叠加评论' : '无评论'}`
}
export function effectiveVersion(v: AssemblyVersion, defaults: Record<string, any>) { return { ...defaults, ...v.overrides } }
export function workflowVersionSelection(recipe: any, versions: AssemblyVersion[]) {
  if (recipe.version_ids != null) return [...recipe.version_ids]
  const matching = (recipe.variants || []).map((id: string) => `legacy-${id}`).filter((id: string) => versions.some(v => v.id === id))
  return matching.length ? matching : versions.map(v => v.id)
}
