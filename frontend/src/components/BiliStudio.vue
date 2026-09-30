<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NDynamicTags, NInput, NModal, NPagination, NSelect, NTag, useDialog, useMessage } from 'naive-ui'
import BiliContentImport from './BiliContentImport.vue'
import { publishApi } from '../utils/publishApi'

const props=defineProps<{accounts:any[];projects:any[];defaultProject?:string}>()
const emit=defineEmits<{guard:[value:boolean];refresh:[]}>()
const account=ref(props.accounts.length===1?props.accounts[0].id:''),keyword=ref(''),status=ref(''),page=ref(1),list=ref<any>(null)
const busy=ref(false),importBusy=ref(false),error=ref(''),detail=ref<any>(null),edit=ref<any>({}),order=ref<number[]>([]),baseline=ref(''),bv=ref('')
const listLoading=ref(false),templatesLoaded=ref(false)
const meta=ref<any>({}),season=ref<number|null>(null),section=ref<number|null>(null),uploads=ref<any[]>([]),upload=ref<string|null>(null)
const templates=ref<any[]>([]),templateFields=ref<string[]>(['tags','description']),changes=ref<any[]>([]),preview=ref<any>(null),modal=ref(false)
const message=useMessage(),dialog=useDialog()
const detailEl=ref<HTMLElement|null>(null)
let generation=0,disposed=false
const editState=computed(()=>JSON.stringify([edit.value,order.value]))
const dirty=computed(()=>!!detail.value && editState.value!==baseline.value)
const guarded=computed(()=>busy.value || importBusy.value || dirty.value || modal.value)
watch(guarded,value=>emit('guard',value))
const parts=computed(()=>order.value.map(cid=>detail.value?.videos.find((p:any)=>p.cid===cid)).filter(Boolean))
const sections=computed(()=>meta.value.seasons?.find((s:any)=>s.id===season.value)?.sections || [])
const accountName=computed(()=>props.accounts.find(a=>a.id===account.value)?.name || account.value)
const totals=computed(()=>Object.fromEntries(['view','like','favorite','reply'].map(k=>[k,(list.value?.items || []).reduce((sum:number,r:any)=>sum+(r.stats?.[k] || 0),0)])))
const labels:any={edit:'文案 / 分P排序',append:'追加分P',collection:'合集归属'}
const stateLabels:any={prepared:'待确认',committing:'结果待确认',unknown:'结果待核对',done:'已完成',failed:'未完成'}
const fieldLabels:any={title:'标题',description:'简介',tags:'标签',dynamic:'粉丝动态',declaration_id:'创作声明'}
const diffs=computed(()=>{
  if(!preview.value)return []
  const {before,after}=preview.value.preview
  return Object.keys(fieldLabels).filter(k=>after.settings && JSON.stringify(before.settings[k])!==JSON.stringify(after.settings[k]))
    .map(k=>({label:fieldLabels[k],before:before.settings[k],after:after.settings[k]}))
})
watch(account,()=>{generation++;list.value=null;detail.value=null;baseline.value='';meta.value={};changes.value=[];templates.value=[];templatesLoaded.value=false;uploads.value=[];error.value='';bv.value='';upload.value=null})
function selectSeason(value:number|null){season.value=value;const rows=meta.value.seasons?.find((s:any)=>s.id===value)?.sections || [];section.value=rows.length===1?rows[0].id:null}
async function call(path:string,body?:any){return publishApi(`/accounts/${account.value}/studio${path}`,body)}
async function run(action:()=>Promise<any>){if(busy.value || !account.value)return;busy.value=true;error.value='';try{await action()}catch(e:any){if(!disposed)error.value=e.message}finally{if(!disposed)busy.value=false}}
function setDetail(value:any){detail.value=value;edit.value={...value.settings,tags:[...(value.settings.tags || [])]};order.value=value.videos.map((p:any)=>p.cid);baseline.value=JSON.stringify([edit.value,order.value]);bv.value=value.bvid;selectSeason(value.collection?.id || null)}
function resetEdit(){if(detail.value)setDetail(detail.value)}
async function loadList(p=1){const ticket=generation
  await run(async()=>{listLoading.value=true;try{const result=await call('/read/archives',{keyword:keyword.value,status:status.value,page:p});if(!disposed && ticket===generation){list.value=result;page.value=p}}finally{listLoading.value=false}})
}
async function open(bvid=bv.value.trim()){
  if(dirty.value){message.warning('请先提交修改或放弃本地编辑');return}
  const ticket=generation
  await run(async()=>{const result=await call('/read/detail',{bvid});if(disposed || ticket!==generation)return;setDetail(result)
    await nextTick();detailEl.value?.scrollIntoView({behavior:'smooth',block:'start'})
    // 选项失败不能抹掉已成功读取的稿件。
    const results=await Promise.allSettled([publishApi(`/accounts/${account.value}/metadata`,{}),publishApi('/publications'),call('/changes')])
    if(disposed || ticket!==generation)return
    if(results[0].status==='fulfilled'){meta.value=results[0].value;selectSeason(result.collection?.id || null)}
    if(results[1].status==='fulfilled')uploads.value=results[1].value.filter((r:any)=>r.status==='uploaded' && r.snapshot.account_id===account.value)
    if(results[2].status==='fulfilled')changes.value=results[2].value
    if(results.some(r=>r.status==='rejected'))error.value='稿件已读取，部分辅助资料加载失败；可重新读取稿件重试。'
  })
}
function move(index:number,offset:number){if(busy.value || index+offset<0 || index+offset>=order.value.length)return;const rows=[...order.value];[rows[index],rows[index+offset]]=[rows[index+offset],rows[index]];order.value=rows}
async function prepare(kind:string){
  if(!detail.value)return
  if(kind!=='edit' && dirty.value){message.warning('请先提交或放弃文案和顺序修改');return}
  const body:any={kind,bvid:detail.value.bvid}
  if(kind==='edit'){
    body.values=Object.fromEntries(Object.keys(fieldLabels).filter(k=>JSON.stringify(edit.value[k])!==JSON.stringify(detail.value.settings[k])).map(k=>[k,edit.value[k]]))
    body.order=order.value
  }else if(kind==='append')body.publication_id=upload.value
  else body.values={season_id:season.value,section_id:section.value}
  await run(async()=>{preview.value=await call('/changes',body);modal.value=true})
}
async function commit(){const item=preview.value;if(!item || busy.value)return
  await run(async()=>{try{
      await call(`/changes/${item.id}/commit`,{confirmed:true});modal.value=false;message.success('B站已接收修改，可能重新审核');baseline.value=editState.value;emit('refresh')
      // 成功回执不依赖后续读取；读取失败时也不会再次提交。
      try{setDetail(await call('/read/detail',{bvid:item.bvid}))}catch{error.value='修改已接收，但详情暂未刷新，请稍后读取。'}
    }catch(e){modal.value=false;throw e}
    finally{try{changes.value=await call('/changes')}catch{}}
  })
}
async function loadChanges(){await run(async()=>{changes.value=await call('/changes')})}
async function reconcile(item:any,manual=false){
  const action=()=>run(async()=>{const result=await call(`/changes/${item.id}/reconcile`,manual?{confirmed_not_applied:true}:{})
    changes.value=await call('/changes');emit('refresh')
    message.info(result.status==='done'?'平台内容已匹配，回执已恢复':result.status==='failed'?'已解除保护；如需修改，请重新预览':'尚不能确认完整结果，请在官网核对；未重复提交')})
  if(!manual)return action()
  dialog.warning({title:'确认已在官网核对',content:'只有已确认本次修改未生效（或已在官网自行处理完成）时才能解除保护。不会重发旧请求；后续修改必须重新预览当前资料。',positiveText:'已核对，解除保护',negativeText:'取消',onPositiveClick:action})
}
async function loadTemplates(){await run(async()=>{templates.value=(await call('/read/templates',{})).items;templatesLoaded.value=true})}
function importTemplate(item:any){const selected=[...templateFields.value],id=account.value
  if(!selected.length)return
  dialog.info({title:'导入为本地模板',content:`「${item.name}」仅导入所选 ${selected.map(k=>fieldLabels[k]).join('、')}。不修改官网，不导入旧分区、版权或默认标记。`,positiveText:'确认导入',negativeText:'取消',onPositiveClick:()=>run(async()=>{
    if(account.value!==id || disposed)return
    await call('/import-template',{id:item.id,fields:selected,confirmed:true});emit('refresh');message.success('已导入到默认设置与合集模板')
  })})
}
onBeforeUnmount(()=>{disposed=true;generation++;emit('guard',false)})
</script>
<template>
  <section class="studio">
    <div class="toolbar"><n-select v-model:value="account" :options="accounts.map(a=>({label:`${a.name} · ${a.id}`,value:a.id}))" placeholder="选择B站账号" :disabled="guarded" /><span class="muted">手动读取；不会自动修改或投稿</span></div>
    <n-alert v-if="error" type="warning">{{ error }}</n-alert>
    <div class="toolbar"><n-input v-model:value="keyword" placeholder="按标题查找我的稿件" :disabled="busy" maxlength="100" @keyup.enter="loadList()" /><n-select v-model:value="status" :options="[{label:'全部状态',value:''},{label:'审核中',value:'is_pubing'},{label:'已通过',value:'pubed'},{label:'未通过',value:'not_pubed'}]" :disabled="busy" /><n-button :disabled="!account || busy" :loading="listLoading" @click="loadList()">读取我的投稿</n-button></div>
    <template v-if="list"><div class="stats"><span>本页 {{ list.items.length }} / 共 {{ list.total }} 条</span><span>播放 {{ totals.view }}</span><span>点赞 {{ totals.like }}</span><span>收藏 {{ totals.favorite }}</span><span>评论 {{ totals.reply }}</span></div><div class="archives">
      <button v-for="item in list.items" :key="item.bvid" :disabled="busy || dirty" class="archive" @click="open(item.bvid)"><strong>{{ item.title }}</strong><small>{{ item.bvid }} · {{ item.state_desc || `状态 ${item.state}` }}</small><span>播放 {{ item.stats.view }} · 点赞 {{ item.stats.like }} · 收藏 {{ item.stats.favorite }} · 评论 {{ item.stats.reply }}</span><p v-if="item.reject_reason" class="warning">{{ item.reject_reason }}</p></button>
      <p v-if="!list.items.length" class="muted">未找到匹配稿件</p></div><n-pagination :page="page" :page-size="20" :item-count="list.total" :disabled="busy" @update:page="loadList" /></template>
    <div class="toolbar"><n-input v-model:value="bv" placeholder="或直接填写自己稿件的BV号" maxlength="12" :disabled="busy || dirty" /><n-button :disabled="!account || busy || dirty || !/^BV[0-9A-Za-z]{10}$/.test(bv.trim())" @click="open()">读取稿件</n-button></div>
    <section v-if="detail" ref="detailEl" class="detail"><div class="heading"><h3>{{ detail.settings.title }}</h3><a :href="`https://www.bilibili.com/video/${detail.bvid}`" target="_blank" rel="noopener noreferrer">{{ detail.bvid }} ↗</a></div>
      <p class="muted">{{ detail.state_desc }} · {{ detail.collection ? `合集：${detail.collection.title}` : '未加入合集' }}</p><n-alert v-if="detail.reject_reason" type="warning">{{ detail.reject_reason }}</n-alert><n-alert v-if="detail.edit_limitation" type="warning">{{ detail.edit_limitation }}</n-alert>
      <div class="edit-grid"><label>标题<n-input v-model:value="edit.title" maxlength="80" show-count :disabled="busy" /></label><label>标签<n-dynamic-tags v-model:value="edit.tags" :max="10" :disabled="busy" /></label><label>简介<n-input v-model:value="edit.description" type="textarea" :autosize="{minRows:3,maxRows:8}" maxlength="2000" show-count :disabled="busy" /></label><label>粉丝动态<n-input v-model:value="edit.dynamic" type="textarea" maxlength="233" :disabled="busy" /></label><label v-if="detail.settings.copyright !== 2">创作声明<n-select v-model:value="edit.declaration_id" :options="meta.declarations || []" placeholder="保持原声明，或明确选择" :disabled="busy" /></label></div>
      <div class="parts"><div v-for="(part,i) in parts" :key="part.cid" class="part"><span>P{{ i+1 }}</span><div><strong>{{ part.title }}</strong><small>{{ part.state_desc || '平台未提供状态说明' }}<template v-if="part.xcode_state !== undefined"> · 转码状态码 {{ part.xcode_state }}</template></small><p v-if="part.reject_reason" class="warning">{{ part.reject_reason }}</p></div><n-button size="tiny" :disabled="busy || i===0" @click="move(i,-1)">上移</n-button><n-button size="tiny" :disabled="busy || i===parts.length-1" @click="move(i,1)">下移</n-button></div></div>
      <div class="toolbar"><n-button type="primary" :disabled="busy || !dirty" @click="prepare('edit')">预览文案 / 顺序修改</n-button><n-button :disabled="busy || !dirty" @click="resetEdit">放弃本地编辑</n-button><small>保留全部原分P，不删除、不替换</small></div>
      <details><summary>追加已上传的配音版</summary><p class="muted">先在「稿件与队列」中仅上传新版本。这里将新分P追加到当前BV末尾，保留原视频、封面、文案及创作声明；需要调整声明时，请单独预览修改。</p><div class="toolbar"><n-select v-model:value="upload" :options="uploads.map(r=>({label:`${r.snapshot.settings.title} · ${r.snapshot.parts.length} 个新分P`,value:r.id}))" :disabled="busy || dirty" placeholder="选择本账号已上传、尚未投稿的清单" /><n-button :disabled="busy || dirty || !upload" @click="prepare('append')">预览追加分P</n-button></div></details>
      <details><summary>调整合集归属</summary><div class="toolbar"><n-select :value="season" :options="(meta.seasons || []).map((s:any)=>({label:s.title,value:s.id}))" clearable :disabled="busy || dirty" placeholder="不加入合集 / 移出" @update:value="selectSeason" /><n-select v-if="season" v-model:value="section" :options="sections.map((s:any)=>({label:s.title,value:s.id}))" :disabled="busy || dirty" placeholder="选择小节" /><n-button :disabled="busy || dirty || (season && !section) || !meta.seasons" @click="prepare('collection')">{{ season ? '预览转入合集' : '预览移出合集' }}</n-button></div></details>
    </section>
    <details class="aux"><summary>官网投稿模板</summary><div class="toolbar"><n-button :disabled="!account || busy" @click="loadTemplates">读取官网模板</n-button><n-checkbox v-for="field in ['title','description','tags']" :key="field" :checked="templateFields.includes(field)" :disabled="busy" @update:checked="v=>templateFields=v?[...templateFields,field]:templateFields.filter(f=>f!==field)">{{ fieldLabels[field] }}</n-checkbox></div><p v-if="templatesLoaded && !templates.length" class="muted">当前账号暂无官网投稿模板，仍可在「默认设置与合集模板」中创建本地模板。</p><div v-for="item in templates" :key="item.id" class="template"><strong>{{ item.name }}</strong><p>{{ item.values.title }}</p><p>{{ item.values.description }}</p><small>{{ item.values.tags.join(' · ') }}</small><n-button size="small" :disabled="busy || !templateFields.length" @click="importTemplate(item)">导入所选字段为本地模板</n-button></div></details>
    <details class="aux"><summary>修改记录与异常恢复</summary><div class="toolbar"><n-button :disabled="!account || busy" @click="loadChanges">刷新修改记录</n-button><a href="https://member.bilibili.com/platform/home" target="_blank" rel="noopener noreferrer">官网核对 ↗</a></div><div v-for="item in changes" :key="item.id" class="change"><strong>{{ item.bvid }} · {{ labels[item.kind] }}</strong><n-tag size="small">{{ stateLabels[item.status] }}</n-tag><small>{{ new Date(item.created_at).toLocaleString() }}</small><p v-if="item.error" class="warning">{{ item.error }}</p><div v-if="['committing','unknown'].includes(item.status)" class="toolbar"><n-button size="small" :disabled="busy || item.busy" @click="reconcile(item)">读取并核对结果</n-button><n-button size="small" :disabled="busy || item.busy" @click="reconcile(item,true)">已在官网处理，解除保护</n-button></div></div><p v-if="!changes.length" class="muted">尚无已读取的修改记录</p></details>
    <BiliContentImport :account-id="account" :projects="projects" :default-project="defaultProject" @busy="importBusy=$event" />
    <n-modal v-model:show="modal" preset="card" title="确认B站稿件变更" style="width:min(860px,calc(100vw - 32px))" :mask-closable="!busy" :closable="!busy" :close-on-esc="!busy">
      <template v-if="preview"><p>{{ accountName }} · {{ preview.bvid }} · {{ labels[preview.kind] }}</p><n-alert type="warning">确认后会修改B站已有稿件，可能重新审核。不会创建另一条投稿。</n-alert>
        <p v-for="warning in preview.preview.warnings || []" :key="warning" class="warning" role="note">{{ warning }}</p>
        <div v-for="d in diffs" :key="d.label" class="diff"><strong>{{ d.label }}</strong><div><small>修改前</small><pre>{{ Array.isArray(d.before)?d.before.join('、'):d.before ?? '平台未提供' }}</pre></div><div><small>修改后</small><pre>{{ Array.isArray(d.after)?d.after.join('、'):d.after }}</pre></div></div>
        <div v-if="preview.preview.after.videos" class="diff"><strong>分P清单</strong><div><small>修改前</small><p v-for="(p,i) in preview.preview.before.videos" :key="p.cid">P{{ i+1 }} · {{ p.title }}</p></div><div><small>修改后</small><p v-for="(p,i) in preview.preview.after.videos" :key="p.cid">P{{ i+1 }} · {{ p.title }}</p></div></div>
        <p v-if="preview.kind==='collection'">{{ preview.preview.before.collection?.title || '未加入合集' }} → {{ preview.preview.after.collection?.title || '移出合集' }}<template v-if="preview.preview.after.collection"> · 小节 {{ preview.preview.after.collection.section_id }}</template></p>
        <div class="toolbar"><n-button :disabled="busy" @click="modal=false">返回编辑</n-button><n-button type="primary" :loading="busy" :disabled="busy" @click="commit">确认修改此BV</n-button></div>
      </template>
    </n-modal>
  </section>
</template>
<style scoped>
.archives{max-height:350px;overflow:auto;overscroll-behavior:contain}.detail{scroll-margin-top:20px}
.studio{padding-top:14px;max-width:1120px}.toolbar{display:flex;align-items:center;gap:12px;margin:14px 0;flex-wrap:wrap}.toolbar :deep(.n-select),.toolbar :deep(.n-input){min-width:180px;max-width:430px;flex:1}.muted,small{color:var(--df-muted);font-size:11px;line-height:1.8}.stats{display:flex;gap:20px;flex-wrap:wrap;font-size:12px;padding:12px;background:var(--df-accent-soft);border-radius:8px}.archives{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}.archive{text-align:left;border:1px solid var(--df-border);background:transparent;color:var(--df-text);border-radius:8px;padding:14px;cursor:pointer;min-width:0}.archive:hover{background:var(--df-accent-soft)}.archive strong,.archive small,.archive span{display:block;overflow-wrap:anywhere}.archive span{font-size:11px;margin-top:8px}.detail{border:1px solid var(--df-border);border-radius:12px;padding:22px;margin:20px 0}.heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.heading h3{font-size:17px;margin:0}.heading a{font-size:12px}.edit-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px}label{display:grid;gap:8px;font-size:12px}.parts{margin-top:20px}.part{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--df-border);font-size:12px}.part>div{flex:1;min-width:0}.part small{display:block}.warning{color:var(--df-warning,#efbd70);font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;font-size:12px;color:var(--df-muted)}details{padding:14px 0;border-top:1px solid var(--df-border)}.template,.change{padding:14px;border:1px solid var(--df-border);border-radius:8px;margin:10px 0;font-size:12px}.template p{white-space:pre-wrap;max-height:160px;overflow:auto}.template small{display:block}.change>strong{margin-right:12px}.change>small{margin-left:12px}.diff{display:grid;grid-template-columns:85px 1fr 1fr;gap:14px;padding:18px 0;border-bottom:1px solid var(--df-border);font-size:12px}.diff>div{min-width:0}.diff pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow:auto}@media(max-width:800px){.archives,.edit-grid{grid-template-columns:1fr}.diff{grid-template-columns:1fr}.heading{align-items:flex-start;flex-direction:column}.detail{padding:14px}}
</style>
