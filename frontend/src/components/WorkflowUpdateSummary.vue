<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NCheckbox, NSelect, NTag } from 'naive-ui'
import { publicationChapterScope } from '../utils/publicationChapters'
import { UPDATE_FIELD_LABELS, updateValue } from '../utils/publicationUpdate'
const props=withDefaults(defineProps<{plan:any;versionOptions?:{label:string;value:string}[];replaceVideos?:boolean;syncFields?:string[]|null;disabled?:boolean;readonly?:boolean;deliveryOnly?:boolean;deliveryMode?:string}>(),{versionOptions:()=>[],replaceVideos:true,syncFields:null,disabled:false,readonly:false,deliveryOnly:false,deliveryMode:'publish'})
const emit=defineEmits<{replacement:[cid:number|string,version:string|null];'replace-videos':[value:boolean];'sync-fields':[value:string[]]}>()
const options=computed(()=>props.plan?.scope_options || [
  ...Object.keys(props.plan?.values || {}).map(key=>({key,label:UPDATE_FIELD_LABELS[key] || '发布资料',selected:true,supported:true})),
  {key:'chapters',label:'视频章节',selected:props.plan?.chapters?.enabled===true,supported:true},
  {key:'collection',label:'合集与小节',selected:props.plan?.collection?.enabled===true,supported:true},
])
const supportedOptions=computed(()=>options.value.filter((option:any)=>option.supported!==false))
const unsupportedOptions=computed(()=>options.value.filter((option:any)=>option.supported===false))
const warnings=computed(()=>[...new Set((props.plan?.warnings || []).map((item:any)=>typeof item==='string'?item:item?.message).filter((item:any)=>typeof item==='string' && item.trim()).map((item:string)=>item.trim()))])
const selected=(key:string)=>props.syncFields===null ? options.value.some((item:any)=>item.key===key && item.selected) : props.syncFields.includes(key)
const readableTitle=(value:any)=>typeof value==='string' && value.trim()?value.trim():''
function originalTitle(part:any){
  const original=props.plan?.remote_parts?.find((item:any)=>String(item.cid)===String(part.old_cid))
  return readableTitle(original?.title) || readableTitle(part.old_title)
}
function targetTitle(part:any){
  if(part.added)return readableTitle(part.title)
  return readableTitle(props.plan?.part_titles?.[String(part.old_cid)]) || readableTitle(part.new_title)
}
function titleChange(part:any){
  const next=targetTitle(part),before=originalTitle(part)
  return !next?'分P名称待核对':before===next?`名称已一致：${next}`:`将改为：${next}`
}
const finalPartOrder=computed(()=>selected('part_order') && Array.isArray(props.plan?.part_order)
  ? props.plan.part_order.map((cid:number|string)=>{
    const part=props.plan.parts?.find((item:any)=>String(item.old_cid)===String(cid))
    const title=part ? part.added || selected('part_titles') ? targetTitle(part) : originalTitle(part) || readableTitle(part.title) : ''
    return {title:title || '分P名称待核对',added:part?.added===true}
  }):null)
function changeField(key:string,value:boolean){
  if(props.disabled || props.readonly || props.deliveryOnly)return
  const current=options.value.filter((item:any)=>item.supported!==false && selected(item.key)).map((item:any)=>item.key)
  emit('sync-fields',value?[...new Set([...current,key])]:current.filter((item:string)=>item!==key))
}
function fieldValue(key:string,value:any){
  const option=options.value.find((item:any)=>item.key===key)
  if(option?.value_display)return option.value_display
  if(key==='copyright')return Number(value)===1?'自制':Number(value)===2?'转载':updateValue(value)
  if(['tid','human_type2'].includes(key))return `${props.plan?.settings_display?.category_title || '分区名称未载入'}（ID ${value}）`
  if(key==='declaration_id' && value)return `创作声明（ID ${value}）`
  return updateValue(value)
}
</script>
<template>
  <section v-if="plan" class="workflow-update-target" aria-label="本次更新的原稿与分P">
    <header><div><h3>更新原稿件 <n-tag size="small" type="info" :bordered="false">保留原BV</n-tag></h3><strong>{{ plan.title || '原稿件' }}</strong></div><a v-if="plan.bvid" :href="`https://www.bilibili.com/video/${encodeURIComponent(plan.bvid)}`" target="_blank" rel="noopener noreferrer">{{ plan.bvid }} ↗</a></header>
    <p v-if="deliveryOnly" class="muted">账号：{{ plan.account_name || '账号名称待核对' }}。继续已确认的原稿更新方案，复用已完成的成片、上传和同步结果；本次不重新合成，也不采用后来修改的项目参数。</p>
    <p v-else class="muted">账号：{{ plan.account_name || '账号名称待核对' }}。先执行本次选定的制作范围，再同步下方勾选内容；输入未变化的有效结果会复用。</p>
    <section v-if="readonly && !deliveryOnly" class="sync-options" aria-label="已保存的原稿更新范围"><p>视频文件：{{ replaceVideos?'按下方分P映射替换':'保留原视频' }}</p><p>同步内容：{{ supportedOptions.filter((option:any)=>selected(option.key)).map((option:any)=>option.label).join('、') || '无' }}</p></section>
    <section v-else-if="!deliveryOnly" class="sync-options" aria-label="原稿更新范围">
      <n-checkbox :checked="replaceVideos" :disabled="disabled||readonly" @update:checked="emit('replace-videos',$event)">替换视频文件</n-checkbox>
      <p class="muted">关闭后保留现有视频，可单独同步标题、简介、章节等设置。视频替换按下方分P映射执行。</p>
      <div class="sync-field-options"><div v-for="option in supportedOptions" :key="option.key" class="sync-field"><n-checkbox :checked="selected(option.key)" :disabled="disabled||readonly" @update:checked="changeField(option.key,$event)">{{ option.label }}</n-checkbox><small v-if="option.reason">{{ option.reason }}</small></div></div>
      <p class="muted">同步内容取自项目已保存的发布设置。未勾选的内容保留B站当前值。</p>
      <details v-if="unsupportedOptions.length" class="sync-limitations"><summary>其他平台设置与能力限制（{{ unsupportedOptions.length }}）</summary><div class="sync-field-options"><div v-for="option in unsupportedOptions" :key="option.key" class="sync-field"><n-checkbox :checked="selected(option.key)" disabled>{{ option.label }}</n-checkbox><small>{{ option.reason || '当前不支持自动修改，请到B站官网处理。' }}</small></div></div></details>
    </section>
    <div v-for="(part,index) in plan.parts || []" :key="part.old_cid" class="update-part"><div><strong>P{{ index+1 }} · {{ originalTitle(part) || readableTitle(part.title) || '分P名称待核对' }}</strong><small>{{ part.added ? '新增分P · '+(part.version_name || '所选成片版本') : !replaceVideos ? '保留原视频文件' : part.version_name || (part.version_id ? '已选择制作版本' : '保留原视频（未选择替换版本）') }}</small><p v-if="selected('part_titles') && !part.added" class="part-title-change">{{ titleChange(part) }}</p></div><n-select v-if="!readonly && !part.added && (replaceVideos || selected('part_titles') || selected('part_order'))" :value="part.version_id || null" :options="versionOptions" :disabled="disabled" clearable :aria-label="replaceVideos ? `P${index+1}更新制作版本` : `P${index+1}对应项目版本`" :placeholder="replaceVideos ? '保留原视频；选择新版本以替换' : '仅用于分P名称和顺序，保留原视频'" @update:value="emit('replacement',part.old_cid,$event)" /></div>
    <section v-if="finalPartOrder" class="part-order-preview" aria-label="更新后分P顺序"><h4>更新后分P顺序</h4><ol><li v-for="(part,index) in finalPartOrder" :key="index"><strong>P{{ index+1 }}</strong><span>{{ part.title }}</span><n-tag v-if="part.added" size="small" type="info" :bordered="false">新增分P</n-tag></li></ol></section>
    <p class="update-scope">{{ publicationChapterScope({chapters_enabled:plan.chapters?.enabled,chapters_translate:plan.chapters?.translate,replace_videos:replaceVideos},true) }}</p>
    <p v-if="plan.collection?.enabled" class="update-scope">合集：{{ plan.collection.season_title || plan.settings_display?.season_title || (plan.collection.season_id ? '按项目已保存目标同步' : '移出当前合集') }}<template v-if="plan.collection.section_title || plan.settings_display?.section_title"> · {{ plan.collection.section_title || plan.settings_display?.section_title }}</template></p>
    <details v-if="Object.keys(plan.values || {}).length" class="copy-scope"><summary>{{ deliveryOnly ? '已确认' : '将同步' }} {{ Object.keys(plan.values).length }} 项项目发布资料</summary><div v-for="(value,key) in plan.values" :key="key"><strong>{{ UPDATE_FIELD_LABELS[key] || '发布资料' }}</strong><p>{{ fieldValue(String(key),value) }}</p></div></details>
    <n-alert v-if="!deliveryOnly && warnings.length" type="warning" :show-icon="false"><ul class="update-warnings"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul></n-alert>
    <p v-if="deliveryOnly" class="muted">仅继续未完成的交付步骤；已经完成的视频上传、原稿提交和设置同步不会重复执行。原BV保持不变。</p>
    <p v-else-if="deliveryMode==='draft'" class="muted">确认后完成所需制作并准备本地更新方案，等待你后续处理上传与提交。</p>
    <p v-else-if="deliveryMode==='upload'" class="muted">确认后完成所需制作并上传替换视频，然后等待你在发布页确认提交原稿更新。</p>
    <p v-else class="muted">确认后连续完成本次制作和原稿更新；需要更换视频时自动上传并等待平台处理，再同步章节等设置。未列出的分P与设置保留，原BV保持不变。</p>
  </section>
</template>
<style scoped>
.workflow-update-target{display:grid;gap:12px;padding:16px;border:1px solid var(--df-border);border-radius:12px;background:var(--df-panel)}header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}h3{display:flex;gap:8px;align-items:center;margin:0 0 8px;font-size:14px}header strong{font-size:13px}a{color:var(--df-accent);font-size:12px;white-space:nowrap}p{margin:0;font-size:12px;line-height:1.8}.muted,small{color:var(--df-muted)}.update-part{display:grid;grid-template-columns:minmax(0,1fr) minmax(160px,1fr);gap:16px;padding-top:12px;border-top:1px solid var(--df-border);align-items:center}.update-part strong{font-size:12px;overflow-wrap:anywhere}.update-part small{display:block;margin-top:5px;font-size:11px}.update-scope{color:var(--df-text)}@media(max-width:520px){header{flex-wrap:wrap}.update-part{grid-template-columns:1fr;gap:8px}}
.sync-options{display:grid;gap:10px;padding:12px 0;border-block:1px solid var(--df-border)}.sync-field-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 20px}.sync-field{display:grid;gap:3px;align-content:start}.sync-field small{font-size:11px;line-height:1.7;overflow-wrap:anywhere}@media(max-width:520px){.sync-field-options{grid-template-columns:1fr}}
.copy-scope{font-size:12px;line-height:1.8}.copy-scope summary{cursor:pointer}.copy-scope>div{display:grid;gap:4px;padding-top:10px}.copy-scope p{white-space:pre-wrap;overflow-wrap:anywhere}
.sync-limitations{border-top:1px solid var(--df-border);padding-top:10px;color:var(--df-muted);font-size:12px;line-height:1.8}.sync-limitations summary{cursor:pointer;width:fit-content}.sync-limitations summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}.sync-limitations>.sync-field-options{margin-top:12px}
.update-warnings{margin:0;padding-left:18px;font-size:12px;line-height:1.8}.update-warnings li+li{margin-top:6px}
.part-title-change{margin-top:6px;color:var(--df-text);overflow-wrap:anywhere}.part-order-preview{padding-top:12px;border-top:1px solid var(--df-border)}.part-order-preview h4{margin:0 0 10px;font-size:12px;font-weight:600}.part-order-preview ol{list-style:none;display:grid;gap:8px;margin:0;padding:0}.part-order-preview li{display:flex;align-items:baseline;gap:10px;font-size:12px;line-height:1.8}.part-order-preview strong{flex:none;color:var(--df-muted);font-weight:500}.part-order-preview span{overflow-wrap:anywhere}
</style>
