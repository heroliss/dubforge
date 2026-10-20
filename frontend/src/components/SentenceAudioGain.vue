<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NInputNumber, NTag } from 'naive-ui'
import { useConfigurationEditability } from '../utils/configurationEditability'
const props=defineProps<{uid:string;line:any;disabled?:boolean}>()
const emit=defineEmits<{'saved':[value:any];'dirty-change':[value:boolean]}>()
const guard=useConfigurationEditability(()=>`project:${props.uid}`)
const state=ref<any>(null),gain=ref<number|null>(0),loading=ref(false),saving=ref(false),error=ref(''),conflict=ref(false)
const identity=computed(()=>JSON.stringify([props.uid,props.line?.id,props.line?.audio_gain_state?.binding,props.line?.selection,props.line?.wav?.url]))
const dirty=computed(()=>!!state.value && gain.value!==state.value.gain_db)
const locked=computed(()=>!!props.disabled || guard.locked.value || guard.isPathLocked('assemble.audio_line_gains') || saving.value || loading.value)
const valid=computed(()=>typeof gain.value==='number' && Number.isFinite(gain.value) && gain.value>=-12 && gain.value<=12)
const canSave=computed(()=>!locked.value && state.value?.available!==false && !conflict.value && valid.value && dirty.value)
let generation=0,disposed=false,controller:AbortController|undefined
function adopt(value:any){state.value=value;gain.value=value.gain_db ?? 0;conflict.value=false;error.value=''}
async function load(){
  controller?.abort();controller=new AbortController();const ticket=++generation,key=identity.value
  loading.value=true;error.value=''
  try {
    const r=await fetch(`/api/projects/${encodeURIComponent(props.uid)}/audio/lines/${props.line.id}`,{cache:'no-store',signal:controller.signal}),data=await r.json()
    if(!r.ok)throw new Error(data.message||'读取本句音量失败')
    if(!disposed && ticket===generation && key===identity.value)adopt(data)
  }catch(e:any){if(!disposed && ticket===generation && e.name!=='AbortError')error.value=e.message||'读取失败，请重试'}
  finally{if(ticket===generation)loading.value=false}
}
async function save(reset=false){
  if(locked.value || !state.value || conflict.value || !reset && (!valid.value || state.value.available===false))return
  const key=identity.value,ticket=++generation,uid=props.uid,id=props.line.id
  const binding=state.value.binding,revision=state.value.revision
  saving.value=true;error.value=''
  try{
    if(!(await guard.canEditPaths(['assemble.audio_line_gains'])) || props.disabled)throw new Error('当前成片声音参数不可修改，请稍后再保存；调音草稿已保留。')
    if(disposed || ticket!==generation || key!==identity.value)return
    const r=await fetch(`/api/projects/${encodeURIComponent(uid)}/audio/lines/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({binding,revision,...(reset?{reset:true}:{gain_db:gain.value})})}),data=await r.json()
    if(!r.ok){if(data.code==='AUDIO_GAIN_CONFLICT' && !disposed && ticket===generation && key===identity.value)conflict.value=true;throw new Error(data.message||'本句音量保存失败')}
    if(!disposed && ticket===generation && key===identity.value){adopt(data);emit('saved',data)}
  }catch(e:any){if(!disposed && ticket===generation)error.value=e.message||'保存失败，请重试'}
  finally{if(ticket===generation)saving.value=false}
}
function reuse(){if(!locked.value && state.value?.available!==false && state.value?.stale && Number.isFinite(state.value.saved_gain_db))gain.value=state.value.saved_gain_db}
watch(identity,()=>{
  generation++;controller?.abort();loading.value=false;saving.value=false
  if(dirty.value){conflict.value=true;error.value='选用声音已变化，调音草稿已保留；请重新载入后再应用。';return}
  if(props.line?.audio_gain_state)adopt(props.line.audio_gain_state)
  else{state.value=null;if(!disposed && typeof window!=='undefined')void load()}
},{immediate:true})
watch(()=>props.line?.audio_gain_state,value=>{if(value && !dirty.value && !saving.value)adopt(value)})
watch(dirty,value=>emit('dirty-change',value),{flush:'sync'})
onMounted(()=>{if(!state.value)void load()})
onBeforeUnmount(()=>{disposed=true;generation++;controller?.abort();emit('dirty-change',false)})
const db=(value:unknown)=>typeof value==='number' && Number.isFinite(value) ? `${value>0?'+':''}${value.toFixed(1)} dB` : '未记录'
</script>
<template>
  <section class="sentence-audio-gain" aria-label="本句后期音量">
    <header><strong>本句后期音量</strong><n-tag size="small" :bordered="false">仅影响所选声音</n-tag></header>
    <p>调整本句播放和成片音量，不重新生成配音。0 dB 保持原音量。</p>
    <p v-if="state?.available===false">本句尚无可用声音，生成或选择声音后可调整；已有旧调音仍可恢复 0 dB。</p>
    <n-alert v-if="state?.stale" type="warning" :show-icon="false">声音或槽位已更新，之前的 {{ db(state.saved_gain_db || 0) }} 未自动应用。<n-button text size="small" :disabled="locked || state.available===false" @click="reuse">填入之前的增益</n-button></n-alert>
    <div class="gain-control"><n-input-number v-model:value="gain" :min="-12" :max="12" :step="0.5" :disabled="locked || !state || state.available===false" :input-props="{'aria-label':'本句后期增益'}"><template #suffix>dB</template></n-input-number><n-button type="primary" size="small" :disabled="!canSave" :loading="saving" @click="save(false)">应用本句音量</n-button><n-button size="small" quaternary :disabled="locked || !state || !state.stale && !state.active && !dirty" @click="save(true)">恢复 0 dB</n-button></div>
    <p v-if="state" class="gain-state">当前生效 {{ db(state.effective_gain_db || 0) }}<span v-if="dirty"> · 未应用 {{ valid ? db(gain!) : '请输入 −12 至 +12 dB' }}</span></p>
    <n-alert v-if="error" type="warning" :show-icon="false">{{ error }} <n-button size="tiny" :disabled="saving || loading" @click="load">重新载入当前声音</n-button></n-alert>
  </section>
</template>
<style scoped>
.sentence-audio-gain{padding:14px;border:1px solid var(--df-border);border-radius:10px;background:var(--df-panel);font-size:12px}.sentence-audio-gain header,.gain-control{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.sentence-audio-gain p{margin:9px 0;color:var(--df-muted);line-height:1.7}.gain-control{margin-top:12px}.gain-control :deep(.n-input-number){width:135px}.sentence-audio-gain .gain-state{margin-bottom:0;font-variant-numeric:tabular-nums}.sentence-audio-gain :deep(.n-alert){margin-top:10px}
</style>
