<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { WORKFLOW_ENDS, workflowModeLabel } from '../utils/workflow'
import { NAlert,NButton,NDrawer,NDrawerContent,NSelect } from 'naive-ui'
import PublicationRightsHint from './PublicationRightsHint.vue'
const props=defineProps<{uids:string[]}>(),emit=defineEmits<{'started':[]}>()
const show=ref(false),busy=ref(false),error=ref(''),items=ref<any[]>([]),results=ref<any[]>([]),mode=ref<string|null>(null)
const ready=computed(()=>items.value.filter(item=>!item.blockers.length)),targetUids=ref<string[]>([])
const publishCount=computed(()=>ready.value.filter(item=>item.plan.recipe.mode==='publish').length)
const confirmLabel=computed(()=>publishCount.value
  ? `确认运行 ${ready.value.length} 个项目并投稿 ${publishCount.value} 个`
  : `确认运行 ${ready.value.length} 个项目（不投稿）`)
let requestId=''
async function api(path:string,body:any){const r=await fetch('/api/configuration/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(d.message||'批量运行失败');return d}
async function inspect(){if(busy.value)return;busy.value=true;error.value='';items.value=[];results.value=[];try{items.value=(await api('batch-plan',{uids:targetUids.value,mode:mode.value})).items;requestId=crypto.randomUUID()}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function open(){if(busy.value||!props.uids.length)return;targetUids.value=[...props.uids];show.value=true;mode.value=null;await inspect()}
async function start(){if(busy.value||!ready.value.length)return;busy.value=true;error.value='';try{results.value=(await api('batch-start',{confirmed:true,request_id:requestId,items:ready.value.map(item=>({uid:item.uid,recipe:item.plan.recipe,fingerprint:item.plan.fingerprint}))})).items;items.value=[];emit('started')}catch(e:any){error.value=e.message}finally{busy.value=false}}
</script>
<template>
  <n-button :disabled="!uids.length||busy" size="small" type="primary" @click="open">按项目设置批量运行…</n-button>
  <n-drawer :show="show" width="min(780px,100vw)" :mask-closable="!busy" :close-on-esc="!busy" @update:show="show=$event">
    <n-drawer-content title="批量运行检查" :closable="!busy">
      <div class="batch-form">
        <p class="muted">按各项目已保存设置执行，不受手动步骤勾选影响；本次终点不会写回设置。</p>
        <label>本次执行终点<n-select v-model:value="mode" clearable placeholder="分别沿用每个项目的设置" :disabled="busy" :options="WORKFLOW_ENDS" @update:value="inspect"/></label>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <div v-for="item in items" :key="item.uid" class="batch-row">
          <strong>{{ item.title }}</strong><small>{{ item.plan?workflowModeLabel(item.plan.recipe.mode):'检查未完成' }}</small>
          <p v-if="item.blockers.length" class="blocked">{{ item.blockers.join('；') }}</p>
          <p v-else>{{ item.plan.version_specs.map((v:any,i:number)=>(item.plan.recipe.mode==='produce'?'版本 ':'P')+(i+1)+' '+(item.plan.recipe.part_titles[v.id]||v.name)).join('、') }} · 检查通过</p>
          <p v-if="item.plan?.account_name" class="muted">{{ item.plan.account_name }} · {{ item.plan.settings.season_id?'合集 ID '+item.plan.settings.season_id:'不加入合集' }} · {{ item.plan.settings.visibility==='self'?'仅自己可见':'公开可见' }}</p>
        </div>
        <div v-for="item in results" :key="item.uid" class="batch-row"><RouterLink :to="`/projects/${item.uid}`">{{ item.uid }}</RouterLink><p>{{ item.error||'已交给后台执行' }}</p></div>
      </div>
      <template #footer>
        <PublicationRightsHint v-if="publishCount"/>
        <div class="batch-actions"><n-button :disabled="busy" :loading="busy" @click="inspect">重新检查</n-button><n-button type="primary" :disabled="busy||!ready.length" @click="start">{{ confirmLabel }}</n-button></div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>.batch-form{display:grid;gap:20px;padding:20px 0}.batch-form label{display:grid;gap:8px;font-size:12px}.batch-row{border-bottom:1px solid var(--df-border);padding:12px 0}.batch-row small{float:right;color:var(--df-muted)}.batch-row p{font-size:12px;margin-bottom:0}.blocked{color:var(--df-warning,#dfb66f)}.batch-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:12px;margin-top:12px}</style>
