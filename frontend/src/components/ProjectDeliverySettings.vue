<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NSelect, NTag } from 'naive-ui'
import { publishApi } from '../utils/publishApi'

const props = defineProps<{ uid:string; values:Record<string,any>; disabled?:boolean; preferredPublication?:string;
  targetSelection?:{source:string;has_history:boolean;explicit:boolean;reason?:string} }>()
const emit = defineEmits<{ change:[path:string,value:any]; 'busy-change':[value:boolean] }>()
const records=ref<any[]>([]), listLoading=ref(false), contextLoading=ref(false), error=ref(''), context=ref<any>(null)
const choice=ref(''), loadedPublication=ref('')
let generation=0, listGeneration=0, disposed=false
let choosingOriginal=false
const target=computed(()=>props.values['publish.project_target'])
const firstPublishDefault=computed(()=>props.targetSelection?.source==='first_publish' && target.value?.kind==='new')
const targetWarning=computed(()=>props.targetSelection?.source==='invalid'
  ?'保存的稿件目标格式无效，请重新选择创建新稿或更新原稿后保存。'
  :props.targetSelection?.source==='explicit_unset'
  ?'稿件目标已清空，请选择创建新稿或更新原稿后保存。'
  :props.targetSelection?.has_history
    ?'项目已有发布或上传记录，请选择创建新稿或更新原稿后保存；不会自动选择最近的BV。'
    :'尚未保存明确的稿件目标；运行预检会提示补齐，不会自动选择最近的BV。')
const mode=computed(()=>props.values['run.mode'] || 'produce')
const needsDelivery=computed(()=>mode.value!=='produce')
const locked=computed(()=>!!props.disabled || contextLoading.value)
const modes=[{label:'仅保留成片',value:'produce'},{label:'准备发布草稿',value:'draft'},
  {label:'上传后等待确认',value:'upload'},{label:'上传并提交',value:'publish'}]
const modeHelp:Record<string,string>={produce:'完成项目目标成片后结束。',draft:'只准备本地稿件或更新方案，随后由你处理上传与提交。',
  upload:'上传选定视频后停止；提交新稿或原稿更新仍需另行确认。',publish:'按启动前确认的账号、目标与范围提交；原稿保留原BV，新稿创建新BV。'}
const recordOptions=computed(()=>records.value.filter(row=>row.receipt?.bvid && (!row.lifecycle || row.lifecycle==='active'))
  .map(row=>({value:String(row.id),label:`${row.receipt.bvid} · ${row.snapshot?.settings?.title || row.draft?.overrides?.title || '稿件'} · ${row.snapshot?.account_name || '账号待核对'}`})))
const versions=computed(()=>props.values['assembly.versions'] || [])
const versionOptions=computed(()=>versions.value.map((row:any)=>({label:row.name,value:row.id})))
const remoteParts=computed(()=>context.value?.remote?.videos || [])
const syncOptions=[{key:'title',label:'标题'},{key:'description',label:'简介'},{key:'tags',label:'标签'},
  {key:'dynamic',label:'粉丝动态'},{key:'declaration_id',label:'创作声明'},{key:'cover',label:'封面'},
  {key:'human_type2',label:'分区'},{key:'copyright',label:'稿件类型'},{key:'source',label:'转载来源'},
  {key:'chapters',label:'视频章节'},{key:'collection',label:'合集与小节'},
  {key:'part_titles',label:'分P名称'},{key:'part_order',label:'分P顺序'},{key:'add_parts',label:'追加分P'}]
const mappingOutdated=computed(()=>target.value?.kind==='existing' && loadedPublication.value===target.value.publication_id
  && (target.value.replacements || []).some((row:any)=>!remoteParts.value.some((part:any)=>Number(part.cid)===row.old_cid)))
const emptyUpdateScope=computed(()=>target.value?.kind==='existing' && !(target.value.replace_videos && target.value.replacements?.length)
  && !target.value.sync_fields?.length)
const selectedReplacement=(cid:number)=>target.value?.replacements?.find((row:any)=>row.old_cid===Number(cid))?.version_id || null
function setMode(value:string){if(!locked.value)emit('change','run.mode',value)}
function clearTargetContext(){generation++;context.value=null;loadedPublication.value='';contextLoading.value=false;emit('busy-change',false)}
function setChoice(value:string){
  if(locked.value)return
  choosingOriginal=value==='existing' && !!target.value
  choice.value=value;clearTargetContext()
  emit('change','publish.project_target',value==='new'?{kind:'new'}:null)
}
function selectPublication(value:string|null){
  if(locked.value)return
  choosingOriginal=!value && !!target.value;clearTargetContext()
  emit('change','publish.project_target',value?{kind:'existing',publication_id:value,replace_videos:false,replacements:[],sync_fields:[]}:null)
}
function changeTarget(patch:Record<string,any>){if(!locked.value && target.value?.kind==='existing')emit('change','publish.project_target',{...target.value,...patch})}
function replaceVideo(value:boolean){changeTarget({replace_videos:value,...(!value?{replacements:[]}: {})})}
function replacePart(cid:number,versionId:string|null){
  if(locked.value)return
  const replacements=(target.value?.replacements || []).filter((row:any)=>row.old_cid!==Number(cid))
  if(versionId)replacements.push({old_cid:Number(cid),version_id:versionId})
  changeTarget({replacements})
}
function syncField(key:string,value:boolean){const fields=target.value?.sync_fields || [];changeTarget({sync_fields:value?[...new Set([...fields,key])]:fields.filter((field:string)=>field!==key)})}
async function loadRecords(){
  if(listLoading.value || !props.uid)return
  const uid=props.uid,ticket=++listGeneration;listLoading.value=true;error.value=''
  try{const data=await publishApi(`/publications?project_uid=${encodeURIComponent(uid)}`)
    if(!disposed && uid===props.uid && ticket===listGeneration){if(!Array.isArray(data))throw new Error('稿件列表格式无效');records.value=data}}
  catch(e:any){if(!disposed && ticket===listGeneration)error.value=e.message || '稿件列表读取失败'}
  finally{if(!disposed && ticket===listGeneration)listLoading.value=false}
}
async function loadContext(){
  const id=target.value?.kind==='existing'?target.value.publication_id:''
  if(!id || contextLoading.value)return
  const uid=props.uid,ticket=++generation;contextLoading.value=true;error.value='';emit('busy-change',true)
  try{const data=await publishApi(`/publications/${encodeURIComponent(id)}/updates/context`)
    if(!disposed && uid===props.uid && ticket===generation && target.value?.publication_id===id){context.value=data;loadedPublication.value=id}}
  catch(e:any){if(!disposed && ticket===generation)error.value=e.message || '原稿分P读取失败'}
  finally{if(!disposed && ticket===generation){contextLoading.value=false;emit('busy-change',false)}}
}
function syncTargetChoice(){
  // 选择“更新原稿”后允许先清空新稿目标，再由用户选择具体稿件。
  choice.value=choosingOriginal && !target.value?'existing':target.value?.kind || ''
  choosingOriginal=false;clearTargetContext()
}
watch(()=>[target.value?.kind,target.value?.publication_id],syncTargetChoice,{immediate:true})
function resetProjectContext(){choosingOriginal=false;clearTargetContext();listGeneration++;records.value=[];listLoading.value=false;choice.value=target.value?.kind || '';void loadRecords()}
watch(()=>props.uid,resetProjectContext)
onMounted(()=>{void loadRecords()})
onBeforeUnmount(()=>{disposed=true;generation++;listGeneration++;emit('busy-change',false)})
</script>
<template>
  <section id="project-delivery-settings" class="publication-panel project-delivery" aria-label="项目交付方式与稿件目标">
    <header><h3>项目交付目标</h3><small>总览和批次运行都读取这里保存的设置。</small></header>
    <label class="delivery-field"><strong>交付方式</strong><n-select :value="mode" :options="modes" :disabled="locked" aria-label="项目交付方式" @update:value="setMode" /><small>{{ modeHelp[mode] }}</small></label>
    <template v-if="needsDelivery">
      <label class="delivery-field"><strong>稿件目标</strong><n-select :value="choice || null" :options="[{label:'创建新稿 · 生成新BV',value:'new'},{label:'更新已选原稿 · 保留原BV',value:'existing'}]" :disabled="locked" aria-label="项目稿件目标" placeholder="明确选择新稿或原稿" @update:value="setChoice" /></label>
      <p v-if="firstPublishDefault" class="delivery-note" role="status">首次发布 · 默认创建新稿。没有发布或上传记录的项目可直接按模板运行；产生记录后需明确选择后续稿件目标。</p>
      <p v-if="!target" class="delivery-warning" role="status">{{ targetWarning }}</p>
      <p v-if="choice==='new' && recordOptions.length" class="delivery-warning">项目已有BV；此目标会另发新稿，原稿保留。启动前将再次核对新稿范围。</p>
      <template v-if="choice==='existing'">
        <label class="delivery-field"><strong>更新哪条原稿</strong><n-select :value="target?.kind==='existing'?target.publication_id:null" :options="recordOptions" :disabled="locked || listLoading" :loading="listLoading" filterable clearable aria-label="项目原稿目标" placeholder="按BV与账号选择明确目标" @update:value="selectPublication" /><small v-if="preferredPublication">可对照刚查看的发布记录选择；不会自动改写已保存的目标。</small></label>
        <div class="delivery-tools"><n-button size="small" :disabled="listLoading || locked" :loading="listLoading" @click="loadRecords">刷新可选稿件</n-button><n-button size="small" :disabled="!target?.publication_id || locked" :loading="contextLoading" @click="loadContext">读取原稿分P</n-button><small>只读核对；保存或运行都不会自动猜测分P映射。</small></div>
        <template v-if="target?.kind==='existing'">
          <n-checkbox :checked="target.replace_videos" :disabled="locked" @update:checked="replaceVideo">替换视频文件</n-checkbox>
          <p class="delivery-note">关闭后只同步下面勾选的资料与章节，无需重新制作视频。未勾选内容保留B站当前值。</p>
          <template v-if="target.replace_videos">
            <p v-if="!context" class="delivery-warning">先读取原稿分P，再为要替换的分P选择项目成片版本。</p>
            <p v-if="!target.replacements.length" class="delivery-warning" role="status">尚未选择要替换的分P与版本。请先选择映射；运行预检会阻止空替换范围，不会自动替换原视频。</p>
            <n-alert v-if="mappingOutdated" type="warning" :show-icon="false">原稿分P已变化，保存的部分CID已不存在。请重新选择映射；运行时还会重新核对。</n-alert>
            <div v-for="(part,index) in remoteParts" :key="part.cid" class="target-part"><label><strong>P{{ index+1 }} · {{ part.title }}</strong><n-select :value="selectedReplacement(part.cid)" :options="versionOptions" :disabled="locked" clearable :aria-label="`项目P${index+1}替换版本`" placeholder="保留原视频" @update:value="replacePart(part.cid,$event)" /></label></div>
            <p v-if="target.replacements.length" class="delivery-note">已保存 {{ target.replacements.length }} 个分P替换映射。具体文件与远端CID在运行预检时冻结。</p>
          </template>
          <div class="sync-heading"><strong>原稿同步范围</strong><n-tag size="small" :bordered="false">{{ target.sync_fields.length }} 项</n-tag></div>
          <div class="sync-fields"><n-checkbox v-for="field in syncOptions" :key="field.key" :checked="target.sync_fields.includes(field.key)" :disabled="locked" @update:checked="syncField(field.key,$event)">{{ field.label }}</n-checkbox></div>
          <p class="delivery-note">同步值来自本页保存的文案、章节及平台设置。只同步文案或章节时无需启用“替换视频文件”。</p>
          <p v-if="emptyUpdateScope" class="delivery-warning" role="status">尚未选择原稿更新范围。请勾选要同步的资料或选择替换分P；运行预检会阻止空范围任务。</p>
        </template>
      </template>
    </template>
    <n-alert v-if="error" type="warning" :show-icon="false">{{ error }}；当前设置已保留，可稍后重新读取。</n-alert>
    <p class="delivery-note">修改后使用下方“保存修改”；保存不会启动制作、上传或提交。</p>
  </section>
</template>
<style scoped>
.project-delivery{display:grid;gap:14px;margin-bottom:18px}.project-delivery header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}.project-delivery h3{margin:0}.project-delivery small,.delivery-note{color:var(--df-muted);font-size:12px;line-height:1.7}.delivery-field,.target-part label{display:grid;gap:8px;max-width:780px}.delivery-field strong,.sync-heading strong,.target-part strong{font-size:13px}.delivery-tools,.sync-heading{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.delivery-warning{color:var(--df-warning,#e3b76c);font-size:12px;line-height:1.7;margin:0}.delivery-note{margin:0}.sync-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px 16px}.target-part{padding:12px;border:1px solid var(--df-border);border-radius:8px;min-width:0}
</style>
