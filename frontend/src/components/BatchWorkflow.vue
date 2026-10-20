<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { workflowModeLabel } from '../utils/workflow'
import { NAlert,NButton,NDrawer,NDrawerContent } from 'naive-ui'
import PublicationRightsHint from './PublicationRightsHint.vue'
import DubbingRunOptions from './DubbingRunOptions.vue'
import { publicationChapterScope } from '../utils/publicationChapters'
import PublicationTargetSummary from './PublicationTargetSummary.vue'
import WorkflowUpdateSummary from './WorkflowUpdateSummary.vue'
import { publicationVisibility } from '../utils/publishing'
const props=defineProps<{uids:string[];hiddenTrigger?:boolean}>(),emit=defineEmits<{'started':[]}>()
const show=ref(false),busy=ref(false),error=ref(''),items=ref<any[]>([]),results=ref<any[]>([])
const ready=computed(()=>items.value.filter(item=>!item.blockers.length)),targetUids=ref<string[]>([])
const publicationParts=(plan:any)=>(plan.recipe.version_ids || []).map((id:string,index:number)=>`P${index+1} ${plan.recipe.part_titles?.[id] || plan.version_specs?.find((v:any)=>v.id===id)?.name || id}`).join('、')
const publishCount=computed(()=>ready.value.filter(item=>(item.plan.recipe.delivery_mode || item.plan.recipe.mode)==='publish').length)
const confirmLabel=computed(()=>publishCount.value
  ? `确认运行 ${ready.value.length} 个项目并投稿 ${publishCount.value} 个`
  : `确认运行 ${ready.value.length} 个项目（不投稿）`)
let requestId='', startRequest:any=null
async function api(path:string,body:any){const r=await fetch('/api/configuration/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(d.message||'批量运行失败');return d}
async function inspect(){if(busy.value)return;error.value='';items.value=[];results.value=[];startRequest=null;if(error.value)return;busy.value=true;try{items.value=(await api('batch-plan',{uids:targetUids.value,target_driven:true})).items;requestId=crypto.randomUUID()}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function open(){if(busy.value||!props.uids.length)return;targetUids.value=[...props.uids];show.value=true;await inspect()}
async function start(){
  if(busy.value||!ready.value.length)return
  if (!startRequest) startRequest=JSON.parse(JSON.stringify({confirmed:true,request_id:requestId,
    items:ready.value.map(item=>({uid:item.uid,recipe:item.plan.recipe,fingerprint:item.plan.fingerprint}))}))
  busy.value=true;error.value=''
  try{
    const data=await api('batch-start',startRequest), accepted=new Set(data.items.filter((r:any)=>r.run_id).map((r:any)=>r.uid))
    const resultTitles = new Map(items.value.map(item=>[item.uid,item.title]))
    results.value=[...results.value.filter((r:any)=>!data.items.some((next:any)=>next.uid===r.uid)),
      ...data.items.map((item:any)=>({...item,title:resultTitles.get(item.uid) || item.title || '未命名项目'}))]
    items.value=items.value.filter(item=>!accepted.has(item.uid))
    startRequest={...startRequest,items:startRequest.items.filter((item:any)=>!accepted.has(item.uid))}
    if (accepted.size) emit('started')
  }catch(e:any){error.value=e.message+'；可重试本批，已启动的项目不会重复启动。'}
  finally{busy.value=false}
}
defineExpose({open})
</script>
<template>
  <n-button v-if="!hiddenTrigger" :disabled="!uids.length||busy" size="small" type="primary" @click="open">按项目设置批量运行…</n-button>
  <n-drawer :show="show" width="min(780px,100vw)" :mask-closable="!busy" :close-on-esc="!busy" @update:show="show=$event">
    <n-drawer-content title="批量运行检查" :closable="!busy">
      <div class="batch-form">
        <p class="muted">各项目按成片目标和交付设置分别检查。需要修改时，请进入对应项目的步骤页保存后重新检查。</p>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <div v-for="item in items" :key="item.uid" class="batch-row">
          <strong>{{ item.title }}</strong><small>{{ item.plan?workflowModeLabel(item.plan.recipe.delivery_mode || item.plan.recipe.mode):'检查未完成' }}</small>
          <p v-if="item.blockers.length" class="blocked">{{ item.blockers.join('；') }}</p>
          <p v-else>制作与复用目标：{{ item.plan.version_specs?.map((v:any)=>v.name).join('、') || '无需制作' }} · 检查通过</p><p v-if="item.plan && !['produce','update'].includes(item.plan.recipe.mode)">发布分P：{{ publicationParts(item.plan) }}</p>
          <p><RouterLink :to="`/projects/${item.uid}/steps/assemble`">成片目标</RouterLink> · <RouterLink :to="`/projects/${item.uid}/publish#project-rules`">交付设置</RouterLink></p>
          <WorkflowUpdateSummary v-if="item.plan?.update_plan" :plan="item.plan.update_plan" :replace-videos="item.plan.recipe.update?.replace_videos!==false" :sync-fields="item.plan.recipe.update?.sync_fields" :delivery-mode="item.plan.recipe.delivery_mode" readonly />
          <PublicationTargetSummary v-if="!['produce','update'].includes(item.plan?.recipe.mode) && item.plan?.settings" :plan="item.plan" />
          <p v-if="item.plan?.recipe.mode!=='produce' && item.plan?.settings" class="muted">{{ publicationVisibility(item.plan.settings.visibility) }}</p>
          <DubbingRunOptions v-if="item.plan?.dubbing" :uid="item.uid" :summary="item.plan.dubbing" readonly />
          <p v-if="item.plan?.recipe.mode==='publish'" class="muted">{{ publicationChapterScope(item.plan) }}</p>
        </div>
        <div v-for="item in results" :key="item.uid" class="batch-row"><RouterLink :to="`/projects/${item.uid}`">{{ item.title || '未命名项目' }}</RouterLink><p :class="{blocked:item.error}">{{ item.error||'已交给后台执行' }}</p></div>
      </div>
      <template #footer>
        <PublicationRightsHint v-if="publishCount"/>
        <div class="batch-actions"><n-button :disabled="busy" :loading="busy" @click="inspect">重新检查</n-button><n-button type="primary" :disabled="busy||!ready.length" @click="start">{{ confirmLabel }}</n-button></div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>.batch-form{display:grid;gap:20px;padding:20px 0}.batch-form label{display:grid;gap:8px;font-size:12px}.batch-row{border-bottom:1px solid var(--df-border);padding:12px 0}.batch-row small{float:right;color:var(--df-muted)}.batch-row p{font-size:12px;margin-bottom:0}.blocked{color:var(--df-warning,#dfb66f)}.batch-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:12px;margin-top:12px}</style>
