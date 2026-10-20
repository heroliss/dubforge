import { versionSpec } from './assemblyVersions.ts'
import { formatTimecode } from './timecode.ts'

// 只比较冻结快照中的已知设置；不把“表单相同”误报为上游素材也完全一致。
export function stablePreviewValue(value:any):string {
  if(Array.isArray(value))return `[${value.map(stablePreviewValue).join(',')}]`
  if(value && typeof value==='object')return `{${Object.keys(value).sort().filter(key=>value[key]!==undefined).map(key=>`${JSON.stringify(key)}:${stablePreviewValue(value[key])}`).join(',')}}`
  return JSON.stringify(value) ?? 'null'
}
export function previewRange(start:number,length:number,duration:number){
  const safeStart=Math.max(0,Math.min(Number.isFinite(start)?start:0,Math.max(0,duration-.2)))
  return {start:safeStart,end:Math.min(duration,safeStart+Math.max(.2,Number.isFinite(length)?length:10))}
}
export function previewSettingsMatch(item:any,version:any,values:any,range:{start:number;end:number},cuts:any,overlayRevision?:string){
  if(item?.metadata_status!=='ready' || !item.snapshot || !item.range)return false
  const snapshot=item.snapshot
  if(stablePreviewValue(snapshot.version_spec)!==stablePreviewValue(versionSpec(version)))return false
  if(stablePreviewValue(snapshot.effective_values)!==stablePreviewValue(values))return false
  if(Math.abs(item.range.start-range.start)>.02 || Math.abs(item.range.end-range.end)>.02)return false
  if(stablePreviewValue(snapshot.output_cuts?.ranges || [])!==stablePreviewValue(cuts?.ranges || []))return false
  return !version.include_comments || typeof snapshot.overlay_revision==='string' && !!snapshot.overlay_revision
    && snapshot.overlay_revision===overlayRevision
}
export function previewTaskOwns(task:any,versionId:string,jobId?:number|null){
  return task?.scope==='preview' && Array.isArray(task.version_ids) && task.version_ids.includes(versionId)
    && (jobId==null || String(task.job_id)===String(jobId))
}
export function previewTime(value:number){
  return formatTimecode(value, 1)
}
export function previewCreated(value?:string){
  if(!value || !Number.isFinite(Date.parse(value)))return '生成时间未记录'
  return new Date(value).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'})
}
