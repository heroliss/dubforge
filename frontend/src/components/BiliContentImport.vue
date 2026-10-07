<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NInputNumber, NSelect, useDialog, useMessage } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
import { useConfigurationEditability } from '../utils/configurationEditability'
const props = defineProps<{accountId:string; projects:any[]; defaultProject?:string}>()
const emit = defineEmits<{busy:[value:boolean]}>()
const dialog=useDialog(), message=useMessage()
const bvid=ref(''),uid=ref(props.defaultProject || ''), limit=ref(50), busy=ref(false),error=ref(''),comments=ref<any>(null), subtitles=ref<any>(null), caption=ref<any>(null),cid=ref<number|null>(null),language=ref<string|null>(null)
const { locked: projectLocked, reason: projectLockReason, canEdit, observing } = useConfigurationEditability(() => uid.value ? 'project:' + uid.value : '')
let generation=0,disposed=false
const validBV=computed(() => /^BV[0-9A-Za-z]{10}$/.test(bvid.value.trim()))
watch(() => props.accountId,reset)
watch(bvid,reset)
watch(cid,() => {language.value=null;caption.value=null;if(subtitles.value)subtitles.value={...subtitles.value,items:[]}})
watch(language,()=>{caption.value=null})
function reset(){generation++;comments.value=null;subtitles.value=null;caption.value=null;cid.value=null;language.value=null;error.value=''}
async function run(action:()=>Promise<any>){if(busy.value || !props.accountId)return;busy.value=true;emit('busy',true);error.value='';try{await action()}catch(e:any){if(!disposed)error.value=e.message}finally{if(!disposed){busy.value=false;emit('busy',false)}}}
async function fetchContent(kind:string){const ticket=generation,account=props.accountId
  await run(async()=>{const body:any={bvid:bvid.value.trim()};if(kind==='comments')body.limit=limit.value
    if(kind!=='comments')body.cid=cid.value || 0
    if(kind==='subtitle')body.subtitle_id=language.value
    const data=await publishApi(`/accounts/${account}/studio/read/${kind}`,body)
    if(disposed || ticket!==generation)return
    if(kind==='comments')comments.value=data
    else if(kind==='subtitles')subtitles.value=data
    else caption.value=data
  })
}
function importData(data:any,kind:string){if(!uid.value || projectLocked.value || busy.value)return
  const account=props.accountId,target=uid.value,project=props.projects.find(p=>p.uid===target)
  dialog.warning({title:kind==='comments'?'导入评论到项目':'保存平台字幕来源',
    content:kind==='comments'?`将 ${data.items.length} 条评论合并到「${project?.title || target}」，按来源ID去重，不覆盖已有翻译和上屏方案。`
      :`将 ${data.lines.length} 句 ${data.language} 字幕保存到「${project?.title || target}」。请核对BV、分P和语言与项目一致；此操作不切换原文来源，也不运行制作。`,
    positiveText:'确认导入',negativeText:'取消',onPositiveClick:()=>run(async()=>{
      if(disposed || props.accountId!==account || uid.value!==target || projectLocked.value)return
      if(observing.value && !(await canEdit())) { error.value=projectLockReason.value || '项目正在运行，请完成后再导入';return }
      if(disposed || props.accountId!==account || uid.value!==target)return
      const result=await publishApi(`/accounts/${account}/studio/import-content`,{import_key:data.import_key,project_uid:target,confirmed:true})
      if(!disposed)message.success(result.message || `已导入 ${result.count} 条评论；前往项目的评论工作区翻译、评分和选用`,{duration:8000})
    })})
}
onBeforeUnmount(()=>{disposed=true;generation++})
</script>
<template>
  <section class="imports"><h3>评论与字幕导入</h3><div class="toolbar"><n-input v-model:value="bvid" placeholder="来源BV号（可与当前稿件不同）" :disabled="busy" maxlength="12" /><n-select v-model:value="uid" :options="projects.map(p=>({label:p.title,value:p.uid}))" filterable placeholder="导入到哪个项目" :disabled="busy" /></div>
    <n-alert v-if="error" type="warning">{{ error }}</n-alert>
    <n-alert v-if="projectLocked && uid" type="info" :show-icon="false">{{ projectLockReason }}；平台评论和字幕仍可读取，暂不能保存到此项目。</n-alert>
    <div class="columns"><div><h4>热门评论</h4><div class="toolbar"><n-input-number v-model:value="limit" :min="1" :max="200" :precision="0" :disabled="busy" /><n-button :disabled="!validBV || busy || !accountId" @click="fetchContent('comments')">读取评论</n-button></div>
      <template v-if="comments"><p class="muted">已获取 {{ comments.items.length }} 条 · 平台总数 {{ comments.total }}（可能含回复）</p><div class="preview"><p v-for="row in comments.items" :key="row.id"><strong>{{ row.author }}</strong><small> · 👍 {{ row.like_count }}</small><br/>{{ row.text }}</p></div><n-button :disabled="!uid || projectLocked || busy || !comments.items.length" @click="importData(comments,'comments')">合并到项目评论工作区</n-button></template>
    </div><div><h4>已有字幕</h4><n-button :disabled="!validBV || busy || !accountId" @click="fetchContent('subtitles')">{{ cid ? '读取此分P的字幕语言' : '读取分P列表' }}</n-button>
      <n-select v-if="subtitles" v-model:value="cid" :options="subtitles.parts.map((p:any,i:number)=>({label:`P${i+1} · ${p.title}`,value:p.cid}))" :disabled="busy" placeholder="选择与项目对应的分P" />
      <n-select v-if="subtitles?.items.length" v-model:value="language" :options="subtitles.items.map((s:any)=>({label:`${s.name} · ${s.language}`,value:s.id}))" :disabled="busy" placeholder="选择语言" />
      <p v-if="subtitles && cid && !subtitles.items.length" class="muted">选择分P后点击读取字幕语言；无字幕时可继续使用语音识别。</p>
      <n-button v-if="language" :disabled="busy" @click="fetchContent('subtitle')">预览所选字幕</n-button>
      <template v-if="caption"><div class="preview"><p v-for="(line,i) in caption.lines" :key="i"><small>{{ line.start.toFixed(1) }}–{{ line.end.toFixed(1) }}s</small><br/>{{ line.text }}</p></div><n-button :disabled="!uid || projectLocked || busy" @click="importData(caption,'subtitle')">保存字幕来源</n-button><p class="muted">随后在「语音转文字 → 原文获取方式」选择已导入的平台字幕并运行。</p></template>
    </div></div>
  </section>
</template>
<style scoped>
.imports{border-top:1px solid var(--df-border);padding-top:20px}.toolbar{display:flex;gap:10px;margin:12px 0;align-items:center}.toolbar>*{min-width:0}.toolbar :deep(.n-input-number){max-width:140px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:26px}.columns>div{min-width:0}.columns :deep(.n-select){margin:12px 0}.preview{max-height:300px;overflow:auto;margin:12px 0;padding:0 14px;border:1px solid var(--df-border);border-radius:8px}.preview p{font-size:12px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px solid var(--df-border);padding-bottom:12px}.muted,small{font-size:11px;color:var(--df-muted)}h3{font-size:16px}h4{font-size:13px}@media(max-width:850px){.columns{grid-template-columns:1fr}.toolbar{flex-wrap:wrap}}
</style>
