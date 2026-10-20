import { formatTimecode } from './timecode.ts'

/** 所有声音报告共用指标解释；原片对照与处理前混音是两种不同参考。 */
const peakAdvice='默认 −1.5 dBTP，可调至 0 dBTP；高于 −1 dBTP 时，AAC 等有损编码后的峰值余量较小。上限不是必须达到的目标，实际峰值更低很正常。'
export const audioMetrics = [
  {key:'integrated_lufs',label:'综合响度',unit:'LUFS',help:'整段声音的总体听感音量，数值越接近 0 越响；不是播放器音量百分比。\n建议：不同项目使用相同成片目标，先从 −16 LUFS、真峰值上限 −1.5 dBTP 开始；觉得偏轻可试听 −14 LUFS（比 −16 高 2 LU），想温和一些可统一用 −18 LUFS。这是制作建议，不是 B 站官方标准。\n原片只用于对照，不作为自动追随的目标；单凭总体响度不能判断人声与背景是否协调。'},
  {key:'true_peak_db',label:'真峰值',unit:'dBTP',help:'声音最猛烈瞬间的峰值，包含采样点之间可能出现的峰值。数值接近或超过 0 时需要留意过载风险，不能仅凭它断定已经失真。\n'+peakAdvice+'统一响度会在必要时控制峰值；保留动态仅用固定增益，受峰值上限约束时可能达不到响度目标，甚至需要减小音量。'},
  {key:'loudness_range_lu',label:'响度范围',unit:'LU',help:'较轻和较响段落之间的典型起伏，数值大表示声音起伏更明显；不是最高峰减最低谷，也不代表音质。\n讲解配音通常保持自然变化即可，不需要刻意追求固定数值。少于 60 秒的片段仅作统计参考，请结合完整成片和试听判断。'},
] as const

export const isAudioNumber=(value:unknown):value is number=>typeof value==='number' && Number.isFinite(value)
export const audioValue=(value:unknown,unit:string)=>isAudioNumber(value)?`${value.toFixed(1)} ${unit}`:'未测得'
export const audioTime=(value:unknown)=>isAudioNumber(value)?formatTimecode(value,1):'—'
export const audioMode=(mode:unknown)=>({off:'保持混音音量',normalize:'保留动态（仅固定增益）',uniform:'统一响度（必要时控制峰值）'} as Record<string,string>)[String(mode)] || '已保存声音处理'

export function audioAssessment(report:any) {
  const explicit=report?.assessment?.target_status
  const status=explicit || (report?.has_audio===false || report?.processing?.silent ? 'unmeasurable' : report?.preview===true ? 'preview' : report?.settings?.mode==='off' ? 'off' : report?.target_reached===true ? 'reached' : report?.processing?.peak_limited && report?.target_reached===false ? 'peak_limited' : 'unmeasurable')
  const labels:Record<string,string>={reached:'接近统一目标',peak_limited:'受峰值限制',off:'保持混音音量',unmeasurable:'暂无法判断目标',preview:'试片局部测量',below:'低于统一目标',above:'高于统一目标'}
  return {status,label:labels[status] || '实际输出测量',type:status==='reached'?'success':['peak_limited','above','below'].includes(status)?'warning':'default'} as const
}

export function audioScope(report:any) {
  const scope=report?.scope
  if(!scope) return report?.preview===true?'当前试片；历史报告未记录源时间范围。':'当前输出文件；历史报告未记录对照范围。'
  const span=isAudioNumber(scope.source_start) && isAudioNumber(scope.source_end)?`源时间 ${audioTime(scope.source_start)}–${audioTime(scope.source_end)}`:''
  const cut=Array.isArray(scope.source_segments) && scope.source_segments.length>1?' · 已应用同一剪辑保留范围':''
  const duration=isAudioNumber(scope.output_duration)?` · 输出时长 ${audioTime(scope.output_duration)}`:''
  return `${report.preview===true?'当前试片':'完整成片'}${span?' · '+span:''}${cut}${duration}`
}

export function sourceComparison(report:any):{ready:boolean;measurement:Record<string,unknown>;note:string} {
  const source=report?.source_comparison
  if(source?.status==='ready') return {ready:true,measurement:source.measurement || {},note:'原片使用相同源时段与剪辑保留范围，仅用于对照；统一目标独立于原片。'}
  return {ready:false,measurement:{},note:source?.reason || (source?'原片对照暂不可用，成片测量仍可查看。':'此历史报告未记录原片对照；重新生成试片或成片后可查看。')}
}

export function audioProcessing(report:any) {
  const gain=report?.processing?.gain_db
  const description=report?.processing?.actual_method==='dynamic'
    ?'必要处动态控制峰值，瞬时增益会变化；不是整轨固定放大。'
    :isAudioNumber(gain)?`整轨实际增益 ${gain>0?'+':''}${gain.toFixed(1)} dB`:'未记录固定增益'
  const correction=audioAacPeakCorrection(report)
  return correction?`${description.replace(/。$/,'')}；${correction}`:description
}

/** 仅解释已保存的编码后衰减；不从主音轨或试片响度反推纠偏与达标。 */
export function audioAacPeakCorrection(report:any) {
  const processing=report?.processing,gain=processing?.aac_peak_correction_db
  if(report?.has_audio===false || processing?.silent || !isAudioNumber(gain) || gain>=0) return ''
  const attempts=processing?.aac_peak_verification?.attempts
  // 复测次数须来自完整过程，末次累计增益和峰值还应对应当前报告，不能借旧过程补数字。
  const recorded=Array.isArray(attempts) && attempts.length>0 && Array.from(attempts).every((row:any,index:number)=>
    row?.attempt===index+1 && isAudioNumber(row.gain_db) && row.gain_db<0 && isAudioNumber(row.true_peak_db)
    && (index===0 || row.gain_db<attempts[index-1].gain_db))
    && Math.abs(attempts.at(-1).gain_db-gain)<=.000001 && isAudioNumber(report.true_peak_db)
    && Math.abs(attempts.at(-1).true_peak_db-report.true_peak_db)<=.000001
  const attenuation=Math.abs(gain)<.1?`不足 0.1 dB`:` ${Math.abs(gain).toFixed(1)} dB`
  return `AAC 编码真峰值复核后衰减${attenuation}${recorded?`（复测 ${attempts.length} 次）`:''}；实际响度以上表测量为准。`
}

const signedGain=(gain:number)=>`${gain>0?'+':''}${gain.toFixed(1)} dB`
/** 编码前的整轨方案与实际增益分开；不从局部响度或名义增益推算处理结果。 */
export function audioStaticGainPlan(report:any) {
  if(report?.settings?.mode!=='normalize' || report?.processing?.actual_method==='dynamic' || report?.processing?.silent) return ''
  const processing=report?.processing || {},parts:string[]=[]
  if(isAudioNumber(processing.requested_gain_db)) parts.push(`目标所需增益 ${signedGain(processing.requested_gain_db)}`)
  if(isAudioNumber(processing.available_gain_db)) parts.push(`峰值允许增益上限 ${signedGain(processing.available_gain_db)}`)
  if(!parts.length) return ''
  const headroom=isAudioNumber(processing.headroom_db)?`（已计入 ${processing.headroom_db.toFixed(1)} dB 编码余量）`:''
  return `编码前整轨方案：${parts.join('；')}${headroom}。实际调整见上方，可能包含编码后的峰值修正。`
}

export function audioPeakConstraint(report:any):{message:string;shortfall:string}|null {
  if(report?.settings?.mode!=='normalize' || report?.processing?.actual_method==='dynamic' || !report?.processing?.peak_limited || report?.processing?.silent || report?.has_audio===false) return null
  const target=report.settings.target_lufs,measured=report.integrated_lufs
  // 只对当前完整输出的实测值计算差；不将试片局部或处理前混音冒充整片结果。
  const shortfall=report.preview===false && isAudioNumber(target) && isAudioNumber(measured) && target-measured>=.05
    ?`完整输出实测仍低于本次目标 ${(target-measured).toFixed(1)} LU。`:''
  return {message:'“保留动态”仅使用整轨固定增益；为遵守真峰值上限，本次增益受到限制，不保证达到目标响度。需要更接近统一目标时，可选择“统一响度”，生成试片试听峰值控制后的效果。',shortfall}
}

/** 参数本身生效才提示；编辑锁定不会改变它是否参与后续制作。 */
export function audioFieldNotice(field:{path?:string},value:unknown,values:Record<string,any>={}) {
  return field.path==='assemble.final_true_peak_db' && ['normalize','uniform'].includes(values['assemble.final_loudness_mode']) && isAudioNumber(value) && value>-1
    ?'高于 −1 dBTP，编码后的峰值余量较小；建议 −1.5 dBTP。':''
}

export function audioSettingGroups(fields:any[]) {
  const final=fields.filter(field=>String(field.path).startsWith('assemble.final_'))
  const balance=fields.filter(field=>!String(field.path).startsWith('assemble.final_'))
  return [{name:'人声与背景平衡',fields:balance},{name:'统一输出响度',fields:final}].filter(group=>group.fields.length)
}

const finalAudioFallbackHelp:Record<string,string>={
  'assemble.final_loudness_mode':'“保留动态”仅用整轨固定增益，峰值接近上限时可能无法达到目标，甚至需要减小音量。跨项目统一优先选“统一响度”，并用试片试听。原片仅作对照。',
  'assemble.final_true_peak_db':peakAdvice,
  'assemble.final_loudness_lufs':'−16 LUFS 是保守的产品默认；觉得偏轻可试听 −14 LUFS（比 −16 高 2 LU），不是 B 站要求。保留动态模式不保证达到目标。',
}
export function audioFieldHelp(field:any,values:Record<string,any>,scope:'version'|'configuration'='version') {
  // schema 负责核心说明；仅在缺失时兜底，避免不同入口重复拼接同一建议。
  const supplied=String(field.help ?? '').trim()
  let help=supplied || finalAudioFallbackHelp[field.path] || ''
  if(['assemble.final_loudness_lufs','assemble.final_true_peak_db'].includes(field.path) && values['assemble.final_loudness_mode']==='off') help+='\n当前保持混音音量，此参数暂不生效；先选择一种成片响度处理方式。'
  if(String(field.path).startsWith('assemble.final_') || ['assemble.voice_volume','assemble.bgm_volume','assemble.ducking','assemble.loudnorm','assemble.sentence_leveling','assemble.sentence_leveling_max_db'].includes(field.path)) help+='\n作用范围：'+(scope==='version'?'此版本重新生成的试片和成片':'保存后采用这些设置的试片和成片；不自动覆盖项目与版本专用值')+'；不修改已合成配音或旧成品。播放器监听音量只影响本机，快速配音试听不模拟完整混音处理。'
  return help
}
