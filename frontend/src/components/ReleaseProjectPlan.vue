<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { NTag } from 'naive-ui'
import { releaseActionLabel, releaseProductionSummary } from '../utils/releaseBatch'
import { versionSummary } from '../utils/assemblyVersions'
import RecoveryPlan from './RecoveryPlan.vue'
import DubbingRunOptions from './DubbingRunOptions.vue'
import WorkflowUpdateSummary from './WorkflowUpdateSummary.vue'
const props=defineProps<{item:any;index:number}>()
const production=computed(()=>props.item.production || props.item.plan?.recovery)
const summary=computed(()=>releaseProductionSummary(props.item))
const reuseOriginal=computed(()=>props.item.publication_action==='reuse_existing' || props.item.publication?.reuse_existing)
const continuation=computed(()=>props.item.publication_action==='continue')
const updating=computed(()=>props.item.recipe?.mode==='update' || props.item.publication_action==='update_existing')
const targetDriven=computed(()=>props.item.recipe?.target_driven===true)
const action=computed(()=>updating.value?'更新原稿 · 保留原BV':reuseOriginal.value ? '保留原BV，仅核对并排序' : props.item.publication?.bvid ? '核对原BV，不重新投稿'
  : props.item.publication_action ? releaseActionLabel(props.item.publication_action) : props.item.publication_id ? '继续已有草稿' : '制作并准备新稿')
const versions=computed(()=>props.item.plan?.version_specs || [])
const publicationVersions=computed(()=>targetDriven.value?(props.item.recipe?.version_ids || []).map((id:string)=>versions.value.find((version:any)=>version.id===id) || {id,name:id}):versions.value)
const parts=computed(()=>publicationVersions.value.map((version:any)=>props.item.recipe?.part_titles?.[version.id] || version.name).join('、'))
</script>
<template>
  <article class="release-project-plan" :data-project="item.project_uid">
    <header><span class="plan-number">{{ index+1 }}</span><div><strong>{{ item.title }}</strong><small>{{ item.source_date || '原视频日期未知' }}</small></div><n-tag size="small" :bordered="false" :type="reuseOriginal ? 'success':'info'">{{ action }}</n-tag></header>
    <p v-if="reuseOriginal" class="production-summary">{{ item.publication?.bvid || '原稿件' }} · 核对审核与合集顺序，沿用原稿视频</p>
    <p v-else-if="summary.hasProduction" class="production-summary">{{ summary.execute ? '从「'+summary.start+'」开始' : summary.start }}<span>执行 {{ summary.execute }} 步</span><span>复用 {{ summary.reuse }} 步</span></p>
    <p v-else-if="continuation" class="production-summary">继续原上传清单，复用已完成分P</p>
    <p v-if="targetDriven && versions.length" class="part-summary">制作与复用目标：{{ versions.map((version:any)=>version.name).join('、') }}</p>
    <p v-if="parts && !reuseOriginal && !updating" class="part-summary">{{ publicationVersions.length }} 个分P · {{ parts }}</p>
    <p v-if="item.phase && !reuseOriginal" class="part-summary">当前进度：{{ item.phase }}</p>
    <DubbingRunOptions v-if="item.plan?.dubbing && !reuseOriginal && !continuation" :uid="item.project_uid" :summary="item.plan.dubbing" readonly style="margin-top:10px" />
    <ul v-if="item.plan?.blockers?.length" class="plan-blockers"><li v-for="blocker in item.plan.blockers" :key="blocker">{{ blocker }}</li></ul>
    <details class="plan-details"><summary>{{ reuseOriginal ? '查看原稿件说明' : '查看步骤与分P详情' }}</summary>
      <WorkflowUpdateSummary v-if="updating && item.plan?.update_plan" :plan="item.plan.update_plan" :replace-videos="item.recipe.update?.replace_videos!==false" :sync-fields="item.recipe.update?.sync_fields" :delivery-mode="item.recipe.delivery_mode" readonly />
      <div v-for="(version,partIndex) in updating?[]:publicationVersions" :key="version.id" class="preview-version">P{{ partIndex+1 }} · {{ item.recipe?.part_titles?.[version.id] || version.name }} · {{ versionSummary(version) }}</div>
      <RecoveryPlan v-if="production?.rows?.length && !reuseOriginal" :recovery="production" :pending-steps="item.plan?.pending_production_steps" class="production-plan" />
      <p v-if="item.publication_action==='prepare'" class="preparation-note">制作完成后重新绑定本次版本的最新成片并核对审查；旧成片引用不会直接上传。</p>
      <p v-if="reuseOriginal" class="preparation-note">本地重新制作不会替换原BV视频；需要替换时请到B站创作中心编辑原稿件。</p>
      <RouterLink v-if="item.publication_id" class="preview-publication" :to="{path:'/projects/'+item.project_uid+'/publish',query:{publication:item.publication_id}}">核对所选稿件资料 →</RouterLink>
    </details>
  </article>
</template>
<style scoped>
.release-project-plan{padding:14px 16px;border:1px solid var(--df-border);border-radius:10px;background:var(--df-panel)}header{display:flex;align-items:center;gap:10px}header>div{flex:1;min-width:0}strong{font-size:13px;line-height:1.6;overflow-wrap:anywhere}small{display:block;font-size:11px;color:var(--df-muted);margin-top:2px}.plan-number{display:grid;place-items:center;width:25px;height:25px;flex-shrink:0;background:var(--df-bg);border-radius:7px;font-size:12px;color:var(--df-muted)}.production-summary,.part-summary{font-size:12px;line-height:1.7;margin:9px 0 0;overflow-wrap:anywhere}.production-summary{color:var(--df-text)}.production-summary span{margin-left:14px;color:var(--df-muted);white-space:nowrap}.part-summary{color:var(--df-muted);margin-top:4px}.plan-details{margin-top:10px}.plan-details summary{cursor:pointer;font-size:11px;color:var(--df-accent);line-height:1.6}.production-plan{margin-top:12px}.preview-version{font-size:12px;color:var(--df-muted);margin-top:8px}.preparation-note{font-size:12px;color:var(--df-muted);line-height:1.8;margin:10px 0 0}.preview-publication{display:inline-block;font-size:12px;color:var(--df-accent);margin-top:10px}.plan-blockers{font-size:12px;color:var(--df-warning);line-height:1.8;margin:8px 0 0;padding-left:20px}@media(max-width:600px){header{align-items:flex-start;flex-wrap:wrap}header>.n-tag{margin-left:35px}.production-summary span{margin-left:10px}}
</style>
