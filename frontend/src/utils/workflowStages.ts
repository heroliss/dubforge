// 用户工作阶段共用此清单；任务键仍由执行器管理，不能把工作区键直接提交为制作任务。
export type WorkflowStage = {
  key: string; label: string; kind: 'step' | 'comments' | 'publication'
  settingsKeys: readonly string[]; taskKeys: readonly string[]
}
export const WORKFLOW_STAGES: readonly WorkflowStage[] = [
  { key:'download', label:'获取视频', kind:'step', settingsKeys:['download'], taskKeys:['download'] },
  { key:'preprocess', label:'音轨分离', kind:'step', settingsKeys:['preprocess'], taskKeys:['preprocess'] },
  { key:'transcribe', label:'语音转文字', kind:'step', settingsKeys:['transcribe'], taskKeys:['transcribe'] },
  { key:'split', label:'语义整理', kind:'step', settingsKeys:['split'], taskKeys:['split'] },
  { key:'translate', label:'翻译', kind:'step', settingsKeys:['translate'], taskKeys:['translate'] },
  { key:'tts', label:'字幕与配音', kind:'step', settingsKeys:['tts','subtitles'], taskKeys:['tts','subtitles'] },
  { key:'comments', label:'上屏评论', kind:'comments', settingsKeys:['comments'], taskKeys:[] },
  { key:'assemble', label:'合成成片', kind:'step', settingsKeys:['assemble'], taskKeys:['assemble'] },
  { key:'publish', label:'发布到B站', kind:'publication', settingsKeys:['publication'], taskKeys:[] },
]
export function workflowStage(key:string) {
  if(key==='dubtask' || key==='workbench') key='tts'
  return WORKFLOW_STAGES.find(stage => stage.key===key || stage.settingsKeys.includes(key) || stage.taskKeys.includes(key))
}
export function workflowStageSequence(keys:unknown) {
  const result:{key:string;label:string;indices:number[]}[]=[]
  // 损坏或尚未读取的范围整体视为未知，不能筛成部分有效步骤后伪造分母。
  if (!Array.isArray(keys) || !keys.every(key => typeof key === 'string' && key.trim())) return result
  keys.forEach((key,index)=>{
    const stage=workflowStage(key),id=stage?.key||key
    const entry=result.find(row=>row.key===id)
    if(entry)entry.indices.push(index)
    else result.push({key:id,label:stage?.label||key,indices:[index]})
  })
  return result
}
export function stageNumber(key:string) { const stage=workflowStage(key);return stage?WORKFLOW_STAGES.indexOf(stage)+1:0 }
export function stagePath(uid:string,key:string) {
  const stage=workflowStage(key),project=`/projects/${encodeURIComponent(uid)}`
  return stage?.kind==='comments'?`${project}/comments`:stage?.kind==='publication'?`${project}/publish`:`${project}/steps/${stage?.key||encodeURIComponent(key)}`
}
export function settingsStageKey(key:string) { return workflowStage(key)?.settingsKeys[0] || key }
export function stageSettingsPath(key:string,template?:string) {
  const stage=workflowStage(key)
  const query=new URLSearchParams({section:'production',...(template?{template}:{}),stage:stage?.key||key})
  return `/settings?${query}`
}
export function adjacentStage(key:string,direction:-1|1) {
  const current=stageNumber(key)
  return current?WORKFLOW_STAGES[current-1+direction]:undefined
}

export function commentStageDisplay(snapshot:any,error='') {
  const workspace=snapshot?.workspace
  if(error) return {status:'unknown',label:'状态未更新',detail:error,type:'default' as const}
  if(workspace) {
    const status=String(workspace.state||'pending')
    return {status,label:String(workspace.label||'尚未准备'),detail:String(workspace.detail||''),
      type:({running:'info',done:'success',attention:'warning',failed:'error'} as Record<string,'info'|'success'|'warning'|'error'>)[status]||'default' as const}
  }
  return {status:'unknown',label:snapshot?'状态待核对':'正在读取',detail:snapshot?'当前后端未提供工作阶段摘要，可进入评论工作区查看；重启更新后的后端后显示完整状态。':'正在读取评论任务与已保存方案。',type:'default' as const}
}
