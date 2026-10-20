<script setup lang="ts">
import { NAlert, NTag } from 'naive-ui'
import { computed } from 'vue'
import { STEP_LABELS, STATUS_TAG, STATUS_TEXT } from '../stores/nav'
import { FRESHNESS_CATEGORIES, type FreshnessReport, type FreshnessStep, type FreshnessChange } from '../utils/freshness'
const props=defineProps<{report:FreshnessReport}>()
const warnings=computed(()=>props.report.warnings)
const totalChanges=computed(()=>props.report.steps.reduce((count,entry)=>count+entry.changes.length,0))
const pendingSteps=computed(()=>props.report.steps.filter(entry=>entry.fresh===false).length)
const stepName=(step:{key:string;label:string})=>step.label || STEP_LABELS[step.key] || '步骤结果'
const legacyWarning='缺少可核对的上次生成输入快照，无法还原具体旧值；只显示当前能证实的原因。'
// 历史说明已独立展示，只去掉这条完全相同的通用警告。
const stepWarnings=(entry:FreshnessStep)=>entry.warnings.filter(warning=>entry.history_available!==false || warning!==legacyWarning)
function currentValue(change:FreshnessChange) {
  if(change.after!==null)return change.after
  return change.category==='missing' ? '缺少有效文件或音频，请看具体原因'
    : change.category==='upstream' ? '需更新，请看具体原因' : '未提供'
}
</script>
<template>
  <div class="freshness-report">
    <n-alert v-for="warning in warnings" :key="warning" type="warning" :show-icon="false" class="report-warning">{{ warning }}</n-alert>
    <div v-if="report.steps.length>1" class="report-overview">
      <div><span class="overview-kicker">本次检查</span><strong>{{ report.steps.length }} 个步骤的更新依据</strong></div>
      <div class="overview-counts"><span>{{ totalChanges ? `${totalChanges} 条变化与诊断` : '暂无差异明细' }}</span><span v-if="pendingSteps">{{ pendingSteps }} 个结果需核对</span></div>
    </div>
    <p v-if="!report.steps.length" class="empty-reasons">当前没有需更新或待补齐的旧结果。状态可能已变化，请以最新检查为准。</p>
    <section v-for="(entry,stepIndex) in report.steps" :key="entry.key" class="freshness-step">
      <header v-if="report.steps.length>1 || entry.freshness_label || entry.fresh===true || entry.settings_changed || entry.upstream_changed || entry.key==='tts' || entry.status && !['done','stale'].includes(entry.status)" class="step-header">
        <div class="step-heading"><span v-if="report.steps.length>1" class="step-number" aria-hidden="true">{{ String(stepIndex+1).padStart(2,'0') }}</span><h3>{{ stepName(entry) }}</h3></div>
        <div class="step-status"><n-tag v-if="entry.status && !['done','stale'].includes(entry.status)" size="small" :type="STATUS_TAG[entry.status] || 'default'" :bordered="false">{{ STATUS_TEXT[entry.status] || '状态待确认' }}</n-tag><n-tag v-if="entry.settings_changed" size="small" type="warning" :bordered="false">本步设置已改</n-tag><n-tag v-if="entry.upstream_changed" size="small" type="warning" :bordered="false">上游产物已变</n-tag><n-tag v-if="entry.fresh===true" size="small" type="success" :bordered="false">{{ entry.freshness_label || '当前结果已是最新' }}</n-tag><n-tag v-else-if="entry.freshness_label || entry.key==='tts'" size="small" type="warning" :bordered="false">{{ entry.freshness_label || '配音完整性待核对' }}</n-tag></div>
      </header>
      <div class="step-body">
        <p class="summary">{{ entry.summary || entry.freshness_reason || '尚未取得更具体的变化说明。' }}</p>
        <div v-if="entry.history_available===false" class="history-note"><strong>历史依据有限</strong><p>缺少可对应这份结果的历史输入快照，无法还原具体旧值；这里只显示有证据的当前原因。</p></div>
        <n-alert v-for="warning in stepWarnings(entry)" :key="warning" type="warning" :show-icon="false" class="report-warning">{{ warning }}</n-alert>
        <div v-if="entry.changes.length" class="changes-list">
          <article v-for="(change,index) in entry.changes" :key="`${change.key}:${index}`" class="change-card">
            <header class="change-heading"><strong>{{ change.label || change.key || '未记录名称' }}</strong><span class="change-category">{{ FRESHNESS_CATEGORIES[change.category] || '其他变化' }}</span></header>
            <div class="value-comparison">
              <div class="value-cell previous-value"><small class="value-label">上次生成时</small><span :class="{unavailable:!change.before_known}">{{ change.sensitive ? '敏感值已隐藏' : !change.before_known ? '旧值未记录' : change.before ?? '未提供' }}</span><small v-if="change.origin_before" class="value-origin">{{ change.origin_before.label }}</small></div>
              <span class="comparison-arrow" aria-hidden="true">→</span>
              <div class="value-cell current-value"><small class="value-label">当前</small><span>{{ currentValue(change) }}</span><small v-if="change.origin_after" class="value-origin">{{ change.origin_after.label }}</small></div>
            </div>
            <div class="change-reason"><span>具体原因</span><p>{{ change.message || '接口未提供进一步说明' }}</p></div>
          </article>
        </div>
        <p v-else-if="entry.fresh!==true" class="empty-reasons">没有可还原的参数差异明细；请按上述状态说明核对文件、上游结果或配音选择。</p>
      </div>
      <footer v-if="entry.affected_steps.length" class="affected-steps"><div class="affected-heading"><span>依赖本步的后续步骤</span><small>列出依赖范围，不表示这些步骤都必须重跑。</small></div><div class="affected-tags"><n-tag v-for="affected in entry.affected_steps" :key="affected.key" size="small" :bordered="false">{{ stepName(affected) }}</n-tag></div></footer>
    </section>
  </div>
</template>
<style scoped>
.freshness-report{display:grid;gap:20px;color:var(--df-text)}
.report-overview{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:4px 2px 0}
.report-overview>div:first-child{display:grid;gap:5px}.overview-kicker{font-size:11px;letter-spacing:.08em;color:var(--df-muted)}
.report-overview strong{font-size:15px;font-weight:600}.overview-counts{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;font-size:11px;color:var(--df-muted)}
.overview-counts span{padding:5px 9px;border-radius:6px;background:rgba(154,167,184,.07)}
.freshness-step{border:1px solid rgba(154,167,184,.16);border-radius:12px;overflow:hidden;background:rgba(16,19,24,.18)}
.step-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:18px 20px;background:rgba(154,167,184,.035);border-bottom:1px solid rgba(154,167,184,.1)}
.step-heading,.step-status{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.step-number{display:grid;place-items:center;width:27px;height:27px;flex:none;border-radius:7px;background:var(--df-accent-soft);color:var(--df-accent);font-size:11px;font-variant-numeric:tabular-nums}
.step-heading h3{font-size:15px;font-weight:600;margin:0}.step-status{gap:7px}.step-body{padding:18px 20px 20px}
.summary{font-size:13px;line-height:1.85;margin:0 0 18px;color:#c6d1df;overflow-wrap:anywhere}
.history-note{padding:12px 14px;margin:0 0 16px;border-radius:8px;background:rgba(239,189,112,.065);border-left:2px solid rgba(239,189,112,.45);font-size:12px;line-height:1.8;color:var(--df-muted)}
.history-note strong{display:block;font-size:11px;font-weight:500;color:#d9b77f;margin-bottom:3px}.history-note p{margin:0;overflow-wrap:anywhere}
.report-warning{font-size:12px;line-height:1.8}.step-body .report-warning{margin-bottom:14px}.report-warning :deep(.n-alert-body){overflow-wrap:anywhere}
.changes-list{display:grid;gap:12px}.change-card{padding:16px;border:1px solid rgba(154,167,184,.12);border-radius:9px;background:rgba(154,167,184,.025)}
.change-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:13px}.change-heading strong{font-size:12px;font-weight:550;line-height:1.7;overflow-wrap:anywhere}.change-category{flex:none;font-size:10px;line-height:1.7;padding:2px 7px;border-radius:4px;background:rgba(154,167,184,.075);color:var(--df-muted)}
.value-comparison{display:grid;grid-template-columns:minmax(0,1fr) 30px minmax(0,1fr);align-items:stretch;gap:4px}
.value-cell{display:flex;flex-direction:column;align-items:flex-start;gap:5px;padding:11px 12px;border-radius:7px;min-width:0;font-size:12px;line-height:1.7;overflow-wrap:anywhere;white-space:pre-wrap}
.previous-value{background:rgba(154,167,184,.045);color:#bdc7d5}.current-value{background:var(--df-accent-soft);color:#c5ddff}
.value-label{font-size:10px;color:var(--df-muted);white-space:normal}.current-value .value-label{color:#9fbee4}.value-cell>span{font-variant-numeric:tabular-nums}.value-origin{font-size:10px;color:var(--df-muted);line-height:1.6}.unavailable{font-style:italic;color:var(--df-muted)}
.comparison-arrow{display:grid;place-items:center;color:#6f86a2;font-size:18px;line-height:1}.change-reason{display:flex;align-items:flex-start;gap:12px;margin-top:13px;padding-top:12px;border-top:1px solid rgba(154,167,184,.09);font-size:11px;line-height:1.8;color:var(--df-muted)}
.change-reason>span{flex:none;color:#aebdce}.change-reason p{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.empty-reasons{font-size:12px;line-height:1.85;margin:0;padding:14px;color:var(--df-muted);border-radius:8px;background:rgba(154,167,184,.04);overflow-wrap:anywhere}
.affected-steps{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:15px 20px;border-top:1px solid rgba(154,167,184,.1);background:rgba(154,167,184,.025)}
.affected-heading{display:grid;gap:4px;font-size:11px;color:#bcc8d7}.affected-heading small{font-size:10px;line-height:1.7;color:var(--df-muted)}.affected-tags{display:flex;flex-wrap:wrap;gap:6px}
@media(max-width:640px){.freshness-report{gap:16px}.report-overview{align-items:flex-start;flex-direction:column;gap:10px}.overview-counts{justify-content:flex-start}.step-header{padding:15px}.step-body{padding:15px}.change-card{padding:12px}.change-heading{flex-wrap:wrap;gap:6px}.value-comparison{grid-template-columns:minmax(0,1fr) 20px minmax(0,1fr);gap:2px}.value-cell{padding:9px;font-size:11px}.change-reason{flex-direction:column;gap:3px}.affected-steps{padding:13px 15px}}
@media(max-width:380px){.value-comparison{grid-template-columns:minmax(0,1fr)}.comparison-arrow{height:18px;transform:rotate(90deg)}}
</style>
