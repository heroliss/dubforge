<script setup lang="ts">
import { computed } from 'vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { releaseCurrentProgress } from '../utils/releaseBatch'
import { formatEta } from '../stores/nav'
const props=defineProps<{progress:any;total:number}>()
const current=computed(()=>releaseCurrentProgress(props.progress))
const eta=computed(()=>current.value ? formatEta(current.value.eta_seconds,'phase') : '')
const execution=computed(()=>{
  if(!current.value)return null
  // 准备快照中的空对象不属于执行诊断，仍要显示真实的等待阶段。
  if(current.value.execution?.schema===1)return current.value.execution
  return {schema:1,state:current.value.stage==='preparing'?'queued':['review','waiting','ordering'].includes(current.value.stage)?'waiting':'running',
    label:current.value.progress_label || current.value.phase || current.value.stage_label || '正在处理'}
})
</script>
<template>
  <section v-if="current" class="release-current-progress" aria-label="当前项目阶段进度">
    <div class="current-subject"><span>{{ current.position ? '当前项目 '+current.position+'/'+total : '批次收尾' }}</span><strong>{{ current.title || current.stage_label }}</strong></div>
    <div class="current-stage"><strong>{{ current.stage_label || '正在处理' }}</strong><span v-if="current.completedSteps!==null && current.totalSteps!==null">{{ current.completedSteps }}/{{ current.totalSteps }} 步已完成</span><span v-if="current.percentage===null" class="stage-percent">进度暂不可计算</span></div>
    <ExecutionProgress :execution="execution" :percentage="current.percentage" :estimated="current.estimated" :indeterminate="current.indeterminate" :eta="eta" compact />
  </section>
</template>
<style scoped>
.release-current-progress{margin:12px 0;padding:12px 14px;border:1px solid var(--df-border);border-radius:9px;background:var(--df-bg)}.current-subject,.current-stage{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;font-size:12px;line-height:1.8}.current-subject{margin-bottom:7px}.current-subject>span{font-size:11px;color:var(--df-accent);flex-shrink:0}.current-subject strong{min-width:0;font-weight:500;color:var(--df-text);overflow-wrap:anywhere}.current-stage{margin-bottom:7px;font-size:11px;color:var(--df-muted)}.current-stage strong{font-weight:500;color:var(--df-text)}.stage-percent{margin-left:auto;font-variant-numeric:tabular-nums;white-space:nowrap}
</style>
