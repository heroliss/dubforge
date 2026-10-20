export type ReleaseItem = {
  project_uid: string; title: string; source_date?: string; status?: string
  phase?: string; error?: string; failed_step?: string; error_code?: string; suggestion?: string
  failure?: { step?: string; code?: string; suggestion?: string }
  workflow_id?: string; publication_id?: string
  recipe?: any; plan?: any; production?: any; publication_action?: ReleasePublicationAction
  target_recipe?:any; project_goal?:any; saved_scope?:any; blockers?:string[]
  versions?: { id:string; name:string; dubbing:boolean; burn_subtitles:boolean; include_comments:boolean }[]
  selected_version_ids?: string[]
  publication_choices?: { id:string; title:string; status:string; bvid?:string; is_review_revision?:boolean; created_at?:string }[]
}
export type ReleasePublicationAction = 'prepare' | 'continue' | 'reuse_existing' | 'update_existing'

// 稿件是否已有平台身份决定处理方式，不能因本地成片更新而偷偷另投或换视频。
export function releasePublicationAction(item: ReleaseItem, publicationId?: string): ReleasePublicationAction {
  const row = item.publication_choices?.find(row => row.id === publicationId)
  if (row) return row.bvid ? 'reuse_existing' : row.status === 'draft' ? 'prepare' : 'continue'
  return item.publication_action || (item.publication_id ? 'continue' : 'prepare')
}
export function releaseActionLabel(action: ReleasePublicationAction): string {
  return { prepare:'制作并更新未投稿草稿', continue:'继续已确认的上传与投稿', reuse_existing:'保留原BV，仅核对并排序', update_existing:'按已保存范围更新原BV' }[action]
}
export function releaseVersionSelection(item: ReleaseItem): string[] {
  // 明确保存为空代表尚未选择；不擅自扩大范围。
  return item.selected_version_ids != null ? [...item.selected_version_ids] : (item.versions || []).map(version => version.id)
}
export function releaseProductionSummary(item:any): {start:string; execute:number; reuse:number; hasProduction:boolean} {
  const production=item.production || item.plan?.recovery || {}, rows=production.rows || []
  const pending=new Set(item.plan?.pending_production_steps || [])
  const recheck=rows.filter((row:any)=>row.action==='reuse' && pending.has(row.key)).length
  const execute=(Array.isArray(production.execute_steps) ? production.execute_steps.length : rows.filter((row:any)=>row.action!=='reuse').length)+recheck
  const reuse=Math.max(0,(Array.isArray(production.reusable_steps) ? production.reusable_steps.length : rows.filter((row:any)=>row.action==='reuse').length)-recheck)
  const first=rows.find((row:any)=>row.action!=='reuse' || pending.has(row.key))
  return {start:production.start_label || first?.label || (execute ? '继续制作' : '制作已就绪'),execute,reuse,hasProduction:rows.length>0}
}

// 未知日期不借用导入时间；相同日期维持用户当前次序。
export function chronologicalItems(items: ReleaseItem[], descending = false): ReleaseItem[] {
  return items.map((item, index) => ({ item, index })).sort((a, b) => {
    const x = a.item.source_date || '', y = b.item.source_date || ''
    if (!x || !y) return x ? -1 : y ? 1 : a.index - b.index
    return (descending ? y.localeCompare(x) : x.localeCompare(y)) || a.index - b.index
  }).map(({ item }) => item)
}

export function moveReleaseItem(items: ReleaseItem[], index: number, offset: number): ReleaseItem[] {
  const target = index + offset
  if (index < 0 || index >= items.length || target < 0 || target >= items.length) return items
  const result = [...items], [item] = result.splice(index, 1)
  result.splice(target, 0, item)
  return result
}

export function batchStage(status: string): { label: string; tone: 'default'|'info'|'warning'|'error'|'success' } {
  const values: Record<string, [string, 'default'|'info'|'warning'|'error'|'success']> = {
    ready: ['待确认开始', 'default'], running: ['顺序执行中', 'info'], pausing: ['本项完成后暂停', 'warning'],
    paused: ['已暂停', 'warning'], attention: ['需要处理', 'error'], failed: ['需要处理', 'error'], done: ['顺序发布完成', 'success'],
    stopped: ['已停止', 'warning'], queued: ['等待前项完成', 'default'], monitoring: ['等待审核与合集核对', 'info'],
    stopping: ['正在停止当前项', 'warning'],
  }
  const [label, tone] = values[status] || [status || '等待执行', 'default']
  return { label, tone }
}
export function releaseBatchControls(status:string) {
  return {pause:status==='running',stop:['running','pausing'].includes(status),resume:['ready','paused','attention','failed','stopped'].includes(status),
    hint:status==='running' ? '暂停：完成本项目后等待。停止：请求终止当前任务，保留产物与回执。'
      : status==='pausing' ? '已请求本项完成后暂停；本项目仍在执行，也可请求停止当前任务。'
      : status==='stopping' ? '正在停止当前任务并等待已发送操作的回执；完成后才能检查并继续。'
      : ['paused','attention','failed','stopped'].includes(status) ? '已完成项目和回执保留。处理问题后检查并继续，从断点恢复；结果不明时先核对原稿件。'
      : status==='ready' ? '方案已保存，检查并确认后才开始制作与发布。' : ''}
}

/** 项目入口的统一锁定说明；批次中的排队项目与当前项目都不能并行写入。 */
export function releaseBatchLockMessage(batch:any): string {
  if (!batch || !['running','pausing','stopping'].includes(batch.status)) return ''
  const position = Number.isInteger(batch.position) && batch.position > 0 && Number.isInteger(batch.total) && batch.total > 0
    ? `第 ${batch.position}/${batch.total} 项` : '当前项目'
  const queued = batch.lock_scope === 'queued' || batch.phase === '等待前项完成' || batch.item_status === 'queued'
  const stage = queued ? '等待前项完成' : batch.status === 'pausing' ? '本项完成后暂停'
    : batch.status === 'stopping' ? '正在停止当前项' : '正在处理'
  const phase = batch.phase && batch.phase !== stage ? `，${batch.phase}` : ''
  return `项目已锁定在顺序发布批次中（${position}，${stage}${phase}），暂不能单独运行步骤、自动流程或修改制作资料；可查看批次进度，批次暂停或停止后再操作。`
}
export function releaseItemStage(status?:string) {
  const stage=batchStage(status || '')
  return {...stage,label:({done:'已完成',running:'正在处理',queued:'待执行',monitoring:'等待审核与合集核对',attention:'需处理',failed:'需处理'} as Record<string,string>)[status || ''] || stage.label}
}
export function releaseCurrentProgress(current:any) {
  if (!current || typeof current!=='object') return null
  const numeric=typeof current.percentage==='number' && Number.isFinite(current.percentage)
  const percentage=current.indeterminate || !numeric ? null : Math.max(0,Math.min(100,current.percentage))
  const totalSteps=Number.isSafeInteger(current.total_steps) && current.total_steps>0 ? current.total_steps : null
  const completedSteps=totalSteps!==null && Number.isSafeInteger(current.completed_steps) && current.completed_steps>=0
    ? Math.min(totalSteps,current.completed_steps) : null
  return {...current,percentage,completedSteps,totalSteps,
    estimated:current.estimated===true,position:Number.isSafeInteger(current.position) && current.position>0 ? current.position : null}
}

export function releaseScopeKey(items: ReleaseItem[], scope: any, confirmed: boolean, publications: Record<string,string> = {}, versions: Record<string,string[]> = {}, actions: Record<string,ReleasePublicationAction> = {}): string {
  return JSON.stringify({ project_uids: items.map(item => item.project_uid), scope, order_confirmed: confirmed,
    publication_ids:items.filter(item => publications[item.project_uid]).map(item=>[item.project_uid,publications[item.project_uid]]),
    version_ids:items.filter(item=>Object.hasOwn(versions,item.project_uid)).map(item=>[item.project_uid,versions[item.project_uid]]),
    publication_actions:items.filter(item=>actions[item.project_uid]).map(item=>[item.project_uid,actions[item.project_uid]]) })
}
