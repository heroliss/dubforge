export const FRESHNESS_CATEGORIES:Record<string,string>={
  parameter:'制作参数',model:'模型设置',prompt:'提示词或程序升级',glossary:'术语表',
  text:'文字或时间修改',upstream:'上游结果变化',missing:'文件或音频缺失',unknown:'历史记录或其他原因',
}
export type FreshnessChange={category:string;key:string;label:string;before:string|null;after:string|null;before_known:boolean;
  origin_before:{kind:string;label:string}|null;origin_after:{kind:string;label:string}|null;message:string;sensitive:boolean}
export type FreshnessStep={key:string;label:string;status:string;fresh:boolean|null;freshness_label:string;freshness_reason:string;history_available:boolean|null;summary:string;
  changes:FreshnessChange[];warnings:string[];affected_steps:{key:string;label:string}[]}
export type FreshnessReport={project_uid:string;steps:FreshnessStep[];warnings:string[]}

const sensitivePath=/(?:^|[._-])(?:api[_-]?key|token|password|passwd|secret|cookie|authorization|credentials?)(?:$|[._-])/i
function text(value:unknown):string {
  if(typeof value!=='string')return ''
  return value.replace(/\b(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|[a-f0-9]{32}\.[A-Za-z0-9]{8})\b/g,'已隐藏')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi,'$1已隐藏@')
}
const texts=(value:unknown)=>Array.isArray(value)?value.filter(item=>typeof item==='string').map(text):[]
function origin(value:any) {return value && typeof value==='object' && typeof value.label==='string'?{kind:text(value.kind),label:text(value.label)}:null}
export function needsFreshnessDetails(step:any):boolean { return !!step && step.fresh!==true && (step.status==='stale' || step.status==='done' && step.fresh===false) }
export function freshnessStatus(step:any):string {
  if(['done','stale'].includes(step?.status))return step.freshness_label || (step.status==='done' && step.fresh ? '已是最新' : step.key==='tts' ? '配音待核对' : '需更新')
  return ''
}
export function normalizeFreshnessReport(value:any):FreshnessReport|null {
  if(!value || typeof value!=='object' || !Array.isArray(value.steps))return null
  const steps:FreshnessStep[]=value.steps.filter((step:any)=>step && typeof step.key==='string').map((step:any)=>({
    key:text(step.key),label:text(step.label),status:text(step.status),fresh:typeof step.fresh==='boolean'?step.fresh:null,
    freshness_label:text(step.freshness_label),freshness_reason:text(step.freshness_reason),
    history_available:typeof step.history_available==='boolean'?step.history_available:null,summary:text(step.summary),
    changes:(Array.isArray(step.changes)?step.changes:[]).filter((change:any)=>change && typeof change==='object').map((change:any)=>{
      const sensitive=change.sensitive===true || sensitivePath.test(String(change.key || ''))
      const known=change.before_known===true
      return {category:text(change.category)||'unknown',key:text(change.key),label:text(change.label),sensitive,
        before:sensitive?'敏感值已隐藏':typeof change.before==='string'?text(change.before):null,
        after:sensitive?'敏感值已隐藏':typeof change.after==='string'?text(change.after):null,before_known:known,
        origin_before:known?origin(change.origin_before):null,origin_after:origin(change.origin_after),
        message:sensitive?'敏感设置值已隐藏':text(change.message)}
    }),
    warnings:texts(step.warnings),affected_steps:(Array.isArray(step.affected_steps)?step.affected_steps:[])
      .filter((affected:any)=>affected && typeof affected.key==='string').map((affected:any)=>({key:text(affected.key),label:text(affected.label)})),
  }))
  if(value.steps.length && !steps.length)return null
  return {project_uid:text(value.project_uid),steps,warnings:texts(value.warnings)}
}

/** 悬停仅呈现已核实的变化；无历史时不用当前值猜测旧值。 */
export function freshnessPreview(report:FreshnessReport|null, limit=3):{items:string[];remaining:number} {
  if(!report)return {items:[],remaining:0}
  const items:string[]=[]
  const short=(value:string)=>value.length>46?value.slice(0,43)+'…':value
  for(const step of report.steps) {
    const prefix=report.steps.length>1?`${step.label || step.key} · `:''
    if(step.fresh===true) {
      items.push(prefix+(step.freshness_label || '当前结果已是最新'))
      continue
    }
    for(const change of step.changes) {
      const name=change.label || '输入'
      const values=['parameter','model'].includes(change.category) && change.before_known
        && !change.sensitive && change.before!==null && change.after!==null
      items.push(prefix+(values?`${name}：${short(change.before!)} → ${short(change.after!)}`
        :change.message || `${name}发生变化`))
    }
    if(!step.changes.length)items.push(prefix+(step.summary || step.freshness_reason || '输入需核对，点击查看详情'))
  }
  const unique=[...new Set(items)]
  return {items:unique.slice(0,limit),remaining:Math.max(0,unique.length-limit)}
}
