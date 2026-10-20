import { commentStageDisplay, WORKFLOW_STAGES } from './workflowStages.ts'
import { freshnessStatus } from './freshness.ts'
import { isPublicationComplete, publicationStage } from './publishing.ts'
import { publicationUpdateProgress } from './publicationUpdate.ts'
import { projectFreshnessKnown } from './projectListProjection.ts'
import { stepDisplayStatus } from './stepFeedback.ts'

export type ProjectWorkflowStage = {
  key:string; label:string; number:number; kind:string; status:string; statusText:string
  detail:string; complete:boolean; [key:string]:any
}

/** 仅把有任务身份依据的主动停止与真实失败分开；产物是否有效仍由完整核验决定。 */
export function projectProductionDisplayStatus(project:any):string {
  if (project.status !== 'failed') return project.status
  const steps = project.steps || []
  if (steps.some((step:any) => ['running', 'queued'].includes(stepDisplayStatus(step)))) return 'processing'
  const failed = steps.filter((step:any) => ['failed', 'interrupted'].includes(step.status))
  return failed.length && failed.every((step:any) => stepDisplayStatus(step) === 'stopped') ? 'stopped' : project.status
}

/** 卡片展示同一组九个工作区；完成事实不改变本次制作范围与后台任务。 */
export function projectWorkflowStages(project:any):ProjectWorkflowStage[] {
  const tasks=new Map<string,any>((project.steps||[]).map((task:any)=>[task.key,task]))
  return WORKFLOW_STAGES.map((stage,index)=>{
    const identity={key:stage.key,label:stage.label,number:index+1,kind:stage.kind}
    if(stage.kind==='comments') {
      const display=commentStageDisplay(project.comments_workspace?{workspace:project.comments_workspace}:{})
      return {...identity,status:display.status,statusText:display.label,detail:display.detail,complete:display.status==='done',progress_indeterminate:true}
    }
    if(stage.kind==='publication') {
      const publication=project.publication,display=publicationStage(publication?.status)
      const update=publicationUpdateProgress(publication)
      // Replacing an existing BV is a delivery stage in its own right.  Keep
      // the original publication state for history, but surface the active
      // update first so project cards and the overview never look idle while
      // the replacement is uploading or waiting for platform processing.
      if (update && (update.active || update.status === 'failed' || update.status === 'unknown'
          || update.status === 'paused' || update.status === 'completed')) {
        const updateStatus = update.status === 'completed' ? 'done'
          : update.status === 'failed' ? 'failed'
          : update.status === 'unknown' ? 'attention'
          : update.status === 'paused' ? 'stale'
          : update.active ? 'running' : 'pending'
        return {...identity, status:updateStatus, statusText:update.text,
          detail:update.phase || String(update.task.error || ''), complete:update.status==='completed',
          progress:update.progress, progress_indeterminate:update.progress===null,
          update, publication}
      }
      const progress=publication?.status==='uploading'&&Number.isFinite(publication.progress)?Math.max(0,Math.min(100,publication.progress)):undefined
      return {...identity,status:display.dot,statusText:display.text,
        detail:typeof publication?.error==='string'?publication.error:'',complete:isPublicationComplete(publication?.status),
        progress,progress_indeterminate:progress===undefined}
    }
    // 字幕导出仍属于第六阶段：真实在途子任务可见，可选未导出不制造额外断点。
    const active=stage.taskKeys.map(key=>tasks.get(key)).find(task=>task&&['running','queued','stopping','pausing'].includes(stepDisplayStatus(task)))
    const task=active||tasks.get(stage.key)||{status:'pending',fresh:false}
    const displayStatus=stepDisplayStatus(task)
    if (!active && !projectFreshnessKnown(project)) {
      const history:Record<string,string>={failed:'上次执行失败',interrupted:'上次执行中断',stopped:'上次已停止',cancelled:'上次已停止'}
      const failure=history[displayStatus]
      return {...task,...identity,status:failure?displayStatus:'checking',
        statusText:failure?`${failure} · 产物待核对`:'产物待核对',
        detail:failure&&task.error?task.error:'登记状态已读取，尚未核对当前设置与磁盘产物。',complete:false}
    }
    const status=displayStatus==='done'&&task.fresh===false?'stale':displayStatus
    return {...task,...identity,status,statusText:status==='checking'?'任务已结束，产物核验中':status==='stopped'?'已停止':task.key==='subtitles'&&active?'字幕文件导出中':freshnessStatus(task)||'',
      detail:task.error||task.freshness_reason||'',complete:task.status==='done'&&task.fresh===true}
  })
}
