import { finalVideoKind } from './preview.ts'
export type AssemblyVersion = { id: string; name: string; dubbing: boolean; burn_subtitles: boolean; include_comments: boolean; overrides: Record<string, any>; partial?: { plan_id: string }; update?: { plan_id: string }; timeline?: { plan_id: string } }
// 只发送版本定义，派生的状态、来源和生效值不能意外变成覆盖值。
export function versionSpec(v: any): AssemblyVersion {
  return { id: v.id, name: v.name, dubbing: v.dubbing, burn_subtitles: v.burn_subtitles, include_comments: v.include_comments, overrides: { ...v.overrides }, ...(v.partial ? { partial: { plan_id: v.partial.plan_id } } : {}), ...(v.update ? { update: { plan_id: v.update.plan_id } } : {}), ...(v.timeline ? { timeline: { plan_id: v.timeline.plan_id } } : {}) }
}
export function versionSummary(v: any, defaults: Record<string, any> = {}) {
  const values = { ...defaults, ...v.values, ...v.overrides }
  const subtitle = ({ source: '原文字幕', trans: '译文字幕', bilingual: '双语字幕' } as Record<string, string>)[values['assemble.subtitle_track']] || '跟随默认字幕'
  return `${v.dubbing ? '配音' : '原声'} · ${v.burn_subtitles ? subtitle : '无字幕'} · ${v.include_comments ? '叠加评论' : '无评论'}${v.timeline ? ' · 成片剪辑' : v.update ? ' · 所选更新' : v.partial ? ' · 局部修订' : ''}`
}
export function effectiveVersion(v: AssemblyVersion, defaults: Record<string, any>) { return { ...defaults, ...v.overrides } }
export function workflowVersionSelection(recipe: any, versions: AssemblyVersion[]) {
  if (recipe.version_ids != null) return [...recipe.version_ids]
  const matching = (recipe.variants || []).map((id: string) => `legacy-${id}`).filter((id: string) => versions.some(v => v.id === id))
  return matching.length ? matching : versions.map(v => v.id)
}

/** 仅监听已登记正式文件身份；执行秒数、试片与临时文件不触发目录扫描。 */
export function assemblyFormalOutputSignature(artifacts:any) {
  return JSON.stringify((Array.isArray(artifacts)?artifacts:[])
    .filter(row=>row && row.id!=null && finalVideoKind(String(row.name || '')))
    .map(row=>[row.id,row.name,row.sha256 ?? '',row.exists!==false,row.version_id ?? '',row.generated_at ?? row.created_at ?? ''])
    .sort((a,b)=>String(a[1]).localeCompare(String(b[1]))))
}

/** 有草稿时只合并同一已保存定义的提交事实，不改变草稿基线或提交版本号。 */
export function mergeAssemblyOutputMetadata(saved:any[],incoming:any[]) {
  const current=new Map(incoming.map(row=>[row.id,row]))
  return saved.map(row=>{
    const next=current.get(row.id)
    if(!next || JSON.stringify(versionSpec(row))!==JSON.stringify(versionSpec(next))) return row
    const metadata=Object.fromEntries(['artifact_id','fresh','status','audio_report']
      .filter(key=>Object.hasOwn(next,key)).map(key=>[key,next[key]]))
    return {...row,...metadata}
  })
}
