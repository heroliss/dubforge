<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCheckbox, NTag } from 'naive-ui'
import ParameterHelp from './ParameterHelp.vue'
import { audioMetrics, audioValue, audioTime, audioMode, audioAssessment, audioScope, sourceComparison, audioProcessing, audioStaticGainPlan, audioPeakConstraint, isAudioNumber } from '../utils/audioQuality'
const props=defineProps<{uid:string;report?:any;outdated?:boolean;compact?:boolean}>()
const expanded=ref(false),onlyAdjusted=ref(true),limit=ref(40),silenceLimit=ref(40)
const numeric=isAudioNumber,value=audioValue,time=audioTime
const rows=computed<any[]>(()=>Array.isArray(props.report?.line_report)?props.report.line_report:[])
const filtered=computed(()=>onlyAdjusted.value?rows.value.filter(row=>row.gain_stale || Number(row.gain_db) || Number(row.leveling_gain_db) || row.issues?.length):rows.value)
const silenceRanges=computed<any[]>(()=>Array.isArray(props.report?.silence_ranges)?props.report.silence_ranges:[])
const visible=computed(()=>filtered.value.slice(0,limit.value))
const mode=computed(()=>audioMode(props.report?.settings?.mode))
const assessment=computed(()=>audioAssessment(props.report))
const source=computed(()=>sourceComparison(props.report))
const scope=computed(()=>audioScope(props.report))
const shortLra=computed(()=>props.report?.assessment?.lra_reliable===false || (numeric(props.report?.scope?.output_duration) && props.report.scope.output_duration<60))
const delta=computed(()=>source.value.ready && props.report?.comparison?.available!==false && numeric(props.report?.comparison?.integrated_delta_lu)?props.report.comparison.integrated_delta_lu:null)
function target(metric:string) {
  if(metric==='loudness_range_lu') return '保持自然起伏'
  if(!props.report?.settings?.mode) return '未记录'
  if(props.report.settings.mode==='off') return '未启用'
  return metric==='integrated_lufs'?value(props.report.settings.target_lufs,'LUFS'):`≤ ${value(props.report.settings.true_peak_db,'dBTP')}`
}
const peakConstraint=computed(()=>audioPeakConstraint(props.report))
const gainPlan=computed(()=>audioStaticGainPlan(props.report))
const issues=computed(()=>(Array.isArray(props.report?.issues)?props.report.issues:[]).filter((issue:any)=>!peakConstraint.value || issue.kind!=='peak_limited'))
const referenceNote=computed(()=>props.report?.preview===true && props.report?.settings?.mode==='uniform'
  ?'先完成完整成片声音处理再截取试片；这里测量的是短片实际响度，不要求等于整片目标。':props.report?.reference?.scope!=='full_output_mix'?'':props.report.preview===true
  ?'沿用完整成片混音增益；这里测量的是短片实际响度，不要求等于整片目标。':props.report.preview===false
    ?'声音处理以完整成片混音为准；这里显示正式成片的实际测量结果。':'声音处理参考完整混音；这里显示当前输出文件的实际测量结果。')
const hasEffectiveRms=(row:any)=>Object.hasOwn(row,'effective_rms_db')
const rmsValue=(row:any)=>hasEffectiveRms(row)?row.effective_rms_db:row.active_rms_db
watch(()=>[props.uid,props.report],()=>{limit.value=40;silenceLimit.value=40;expanded.value=false})
watch(onlyAdjusted,()=>{limit.value=40})
</script>
<template>
  <section class="audio-quality-report" :class="{compact}" aria-label="成片声音测量">
    <header><strong>声音测量与原片对照</strong><n-tag v-if="report" size="small" :bordered="false" :type="outdated?'warning':assessment.type">{{ outdated?(report.preview===true?'历史试片报告':'历史成片报告'):assessment.label }}</n-tag></header>
    <p v-if="!report" class="report-note">暂无测量报告，生成试片或成片后可查看实际响度与峰值。</p>
    <template v-else>
      <p v-if="outdated" class="report-note">报告对应上次生成文件；当前调整需重新生成试片或成片后核对。</p>
      <p class="report-note report-scope">{{ scope }}</p>
      <p v-if="referenceNote" class="report-note">{{ referenceNote }}</p>
      <div class="audio-comparison-scroll"><table class="audio-comparison" aria-label="原片与输出声音指标对照">
        <thead><tr><th scope="col">声音指标</th><th scope="col">原片同范围</th><th scope="col">{{ report.preview===true?'当前试片':'当前成片' }}</th><th scope="col">统一目标</th></tr></thead>
        <tbody><tr v-for="metric in audioMetrics" :key="metric.key">
          <th scope="row"><ParameterHelp :label="metric.label" :help="metric.help"><span class="metric-label" tabindex="0">{{ metric.label }}</span></ParameterHelp></th>
          <td><ParameterHelp :show-icon="false" :label="`原片${metric.label}`" :help="metric.help+'\n这是相同源时段和剪辑范围内的原片测量，仅用于对照，不会作为自动响度目标。'"><span class="metric-value source-value" tabindex="0">{{ source.ready?value(source.measurement[metric.key],metric.unit):'暂无对照' }}</span></ParameterHelp></td>
          <td class="output-cell"><ParameterHelp :show-icon="false" :label="`输出${metric.label}`" :help="metric.help+(report.preview===true?'\n这是局部试片的测量，不要求等于完整成片目标。':'\n这是已生成文件的实际测量；修改设置后需要重新生成。')"><strong class="metric-value" tabindex="0">{{ value(report[metric.key],metric.unit) }}</strong></ParameterHelp></td>
          <td><ParameterHelp :show-icon="false" :label="`${metric.label}目标`" :help="metric.help+'\n目标沿用本次生成时保存的设置；版本专用值优先于项目默认。'"><span class="metric-value target-value" tabindex="0">{{ target(metric.key) }}</span></ParameterHelp></td>
        </tr></tbody>
      </table></div>
      <p class="report-note">{{ source.note }}<template v-if="delta!==null"> 综合响度差 {{ delta>0?'+':'' }}{{ Number(delta).toFixed(1) }} LU（成片 − 原片）。</template></p>
      <p v-if="shortLra" class="report-note">短于 60 秒的片段，响度范围仅作统计参考；请结合完整成片判断声音起伏。</p>
      <p class="report-note processing-note">{{ mode }} · {{ audioProcessing(report) }}</p>
      <p v-if="gainPlan" class="report-note gain-plan">{{ gainPlan }}</p>
      <p class="report-note">先核对人声与背景的比例，再用统一目标校准总体音量；LUFS 相近不代表人声一定清楚，仍需试听。</p>
      <details v-if="report.preview===true && report.full_output_measurement"><summary>整片处理基准（编码前）</summary><p class="report-note">这是完整成片音轨在截取与视频编码前的测量，不是当前短片，也不与上方原片同范围直接比较。</p><div class="full-output-metrics"><ParameterHelp v-for="metric in audioMetrics" :key="metric.key" :label="`整片${metric.label}`" :help="metric.help"><span>{{ metric.label }}</span><strong>{{ value(report.full_output_measurement[metric.key],metric.unit) }}</strong></ParameterHelp></div></details>
      <n-alert v-if="report.has_audio===false" type="info" :show-icon="false">此输出没有音轨。</n-alert>
      <n-alert v-else-if="report.processing?.silent" type="info" :show-icon="false">此输出为静音，无法测得有效响度。</n-alert>
      <n-alert v-else-if="peakConstraint" type="info" :show-icon="false">{{ peakConstraint.message }}<span v-if="peakConstraint.shortfall" class="constraint-shortfall">{{ peakConstraint.shortfall }}</span></n-alert>
      <n-alert v-else-if="assessment.status==='peak_limited'" type="info" :show-icon="false">当前输出受峰值上限约束，请结合本次目标与试听结果核对。</n-alert>
      <n-alert v-for="(issue,index) in issues" :key="index" :type="issue.severity==='error'?'error':issue.severity==='warning'?'warning':'info'" :show-icon="false">{{ issue.message }}</n-alert>
      <details v-if="silenceRanges.length || numeric(report.trailing_silence_start)"><summary>成品静音区间（不一定异常）</summary><p class="report-note">以下使用生成文件的成品时间，可能与剪辑前源时间不同。</p><p v-for="(range,index) in silenceRanges.slice(0,silenceLimit)" :key="index" class="report-note">{{ time(range.start) }}–{{ time(range.end) }}</p><p v-if="numeric(report.trailing_silence_start)" class="report-note">{{ time(report.trailing_silence_start) }} 起持续静音至文件末尾</p><n-button v-if="silenceLimit<silenceRanges.length" size="small" quaternary @click="silenceLimit+=40">继续显示静音区间</n-button></details>
      <details v-if="rows.length && !compact" :open="expanded" @toggle="expanded=($event.target as HTMLDetailsElement).open"><summary>逐句音量明细 · {{ rows.length }} 句</summary>
        <template v-if="expanded"><n-checkbox v-model:checked="onlyAdjusted" size="small">仅看有调整或需核对的句子</n-checkbox><p v-if="!filtered.length" class="report-note">没有逐句增益调整或失效记录。</p><div v-else class="line-report-table"><div class="line-report-row line-report-head"><span>句子 / 源时间</span><ParameterHelp label="手动增益" help="本句人工调整的音量，0 dB 表示不变；正值放大，负值减小。只在重新合成成片时应用，不改变配音 WAV。"><span>手动增益</span></ParameterHelp><ParameterHelp label="人声能量" help="本句的平均信号能量（dBFS），用于对照明显的句间差异；不是 LUFS，不能直接与成片响度比较。整轨校准和背景混音还会继续改变最终结果。"><span>人声能量</span></ParameterHelp><ParameterHelp label="自动平衡" help="保守均衡句间音量产生的修正，保留轻声和语气变化。0 dB 表示没有修正，不代表没有人声。"><span>自动平衡</span></ParameterHelp></div><div v-for="row in visible" :key="row.id" class="line-report-row"><RouterLink :to="{path:'/projects/'+uid+'/steps/tts',query:{line:String(row.id)}}">第 {{ row.id }} 句<small>{{ time(row.start) }}–{{ time(row.end) }}</small><small v-for="(issue,index) in row.issues || []" :key="index" class="line-issue">{{ issue.message }}</small></RouterLink><span>{{ value(row.gain_db,'dB') }}<small v-if="row.gain_stale" class="stale-gain">声音已变，旧增益未应用</small></span><span :title="hasEffectiveRms(row)?'本句后期增益与自动均衡后、整轨校准前':'旧报告仅记录本句增益调整前的能量'">{{ value(rmsValue(row),'dBFS') }}<small>{{ hasEffectiveRms(row)?'调整后 · 整轨校准前':'调整前' }}</small></span><span>{{ value(row.leveling_gain_db,'dB') }}</span></div></div><n-button v-if="visible.length<filtered.length" size="small" quaternary @click="limit+=40">继续显示（{{ visible.length }}/{{ filtered.length }}）</n-button></template>
      </details>
    </template>
  </section>
</template>
<style scoped>
.audio-quality-report{padding:14px;border:1px solid var(--df-border);border-radius:10px;background:var(--df-panel);font-size:12px;min-width:0}.audio-quality-report header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.audio-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:13px 0}.audio-metrics small,.audio-metrics strong{display:block}.audio-metrics small{color:var(--df-muted);margin-bottom:5px}.audio-metrics strong{font-size:15px;font-weight:500;font-variant-numeric:tabular-nums}.report-note{color:var(--df-muted);line-height:1.7;margin:9px 0 0}.audio-quality-report :deep(.n-alert){margin-top:10px}.audio-quality-report summary{color:var(--df-accent);cursor:pointer;margin:12px 0}.line-report-table{overflow:auto;margin:10px 0}.line-report-row{display:grid;grid-template-columns:minmax(120px,1.3fr) repeat(3,minmax(80px,1fr));gap:12px;border-bottom:1px solid var(--df-border);padding:9px 0;min-width:430px}.line-report-row small{display:block;margin-top:4px;color:var(--df-muted);line-height:1.5}.line-report-head{color:var(--df-muted);font-size:11px}.line-report-row .stale-gain,.line-report-row .line-issue{color:var(--df-warning)}.line-report-row a{color:var(--df-accent)}@media(max-width:500px){.audio-metrics{gap:8px}.audio-metrics strong{font-size:12px}}
.audio-comparison-scroll{overflow:auto;margin-top:13px;border:1px solid var(--df-border);border-radius:8px}.audio-comparison{width:100%;border-collapse:collapse;text-align:left;min-width:460px;table-layout:fixed}.audio-comparison th,.audio-comparison td{padding:12px 10px;border-bottom:1px solid var(--df-border);vertical-align:middle;font-weight:400}.audio-comparison thead{color:var(--df-muted);font-size:11px;background:var(--df-accent-soft)}.audio-comparison thead th:first-child{width:24%}.audio-comparison tr:last-child>*{border-bottom:0}.audio-comparison .output-cell{background:var(--df-accent-soft)}.metric-label{font-weight:500;cursor:help}.metric-value{display:block;font-size:13px;font-variant-numeric:tabular-nums;cursor:help;overflow-wrap:anywhere}.output-cell strong{font-weight:600;color:var(--df-text)}.source-value,.target-value{color:var(--df-muted)}.report-scope{font-variant-numeric:tabular-nums}.processing-note{color:var(--df-text)}.full-output-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:12px 0}.full-output-metrics strong{font-weight:500;font-variant-numeric:tabular-nums}
.constraint-shortfall{display:block;margin-top:6px;font-variant-numeric:tabular-nums}.gain-plan{font-variant-numeric:tabular-nums}
</style>
