<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NTag } from 'naive-ui'
import { conversationRows, conversationLabels } from '../utils/llmConversation'

const props=defineProps<{uid:string}>()
const action=ref('filter'), events=ref<any[]>([]), lines=ref<string[]>([]), job=ref<any>(null)
const error=ref(''), fetching=ref(false), exists=ref(false), truncated=ref(false)
const logBox=ref<HTMLElement|null>(null), historyBox=ref<HTMLElement|null>(null), follow=ref(true)
const rows=computed(()=>conversationRows(events.value))
const stateLabel=computed(()=>({running:'处理中',done:'已完成',partial:'部分完成',failed:'失败',cancelled:'已停止',interrupted:'已中断'}[job.value?.status as string]||'暂无记录'))
let cursor=0,traceId='',generation=0,disposed=false,mounted=false,timer:ReturnType<typeof setTimeout>|undefined,controller:AbortController|undefined

function reset(){
  generation++;controller?.abort();clearTimeout(timer);cursor=0;traceId='';events.value=[];lines.value=[];job.value=null
  error.value='';exists.value=false;fetching.value=false;follow.value=true
  if(mounted)void load()
}
function selectAction(value:string){if(value!==action.value){action.value=value;reset()}}
function scroll(){
  const el=historyBox.value
  if(el)follow.value=el.scrollHeight-el.clientHeight-el.scrollTop<45
}
async function bottom(){
  follow.value=true;await nextTick()
  if(historyBox.value)historyBox.value.scrollTop=historyBox.value.scrollHeight
}
async function load(){
  if(disposed||fetching.value)return
  if(typeof document!=='undefined'&&document.hidden){timer=setTimeout(load,2000);return}
  const ticket=generation, oldCursor=cursor
  fetching.value=true;clearTimeout(timer);controller=new AbortController()
  let more=false
  try{
    const params=new URLSearchParams({action:action.value,after:String(cursor),trace_id:traceId})
    const response=await fetch(`/api/projects/${encodeURIComponent(props.uid)}/comments/diagnostics?${params}`,{cache:'no-store',signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)])})
    const value=await response.json()
    if(disposed||ticket!==generation)return
    if(!response.ok)throw new Error(value.message||'评论诊断读取失败')
    error.value='';job.value=value.job;lines.value=value.lines||[];truncated.value=!!value.truncated
    if(value.retry)return
    if(value.reset||!value.exists){events.value=[];cursor=0;traceId='';follow.value=true}
    exists.value=!!value.exists
    events.value.push(...(value.events||[]));cursor=value.cursor??cursor;traceId=value.trace_id||traceId
    more=!!value.more&&cursor!==oldCursor
    const atLogEnd=!logBox.value||logBox.value.scrollHeight-logBox.value.clientHeight-logBox.value.scrollTop<45
    await nextTick()
    if(disposed||ticket!==generation)return
    if(follow.value&&historyBox.value)historyBox.value.scrollTop=historyBox.value.scrollHeight
    if(atLogEnd&&logBox.value)logBox.value.scrollTop=logBox.value.scrollHeight
  }catch(e:any){if(!disposed&&ticket===generation)error.value=e.name==='TimeoutError'?'诊断读取超时，已显示内容保留':e.message||'诊断读取失败'}
  finally{
    if(!disposed&&ticket===generation){fetching.value=false;if(mounted)timer=setTimeout(load,more?50:2000)}
  }
}
watch(()=>props.uid,reset,{flush:'sync'})
onMounted(()=>{mounted=true;void load()})
onBeforeUnmount(()=>{disposed=true;generation++;controller?.abort();clearTimeout(timer)})
</script>
<template>
  <section class="comment-diagnostics" aria-label="评论 LLM 对话与日志">
    <div class="diagnostic-toolbar">
      <div class="action-tabs"><button v-for="item in [{id:'filter',label:'评论评分'},{id:'translate',label:'评论翻译'}]" :key="item.id" :aria-pressed="action===item.id" @click="selectAction(item.id)">{{ item.label }}</button></div>
      <n-tag size="small" :type="job?.status==='failed'?'error':job?.status==='running'?'info':'default'">{{ stateLabel }}</n-tag>
      <span v-if="job" class="muted">本次 {{ job.processed }} / {{ job.total }} 条<span v-if="job.cached"> · 复用 {{ job.cached }} 条</span></span>
      <n-button size="small" quaternary :loading="fetching" @click="load">刷新记录</n-button>
    </div>
    <n-alert v-if="error" type="warning" :show-icon="false">{{ error }}</n-alert>
    <n-alert v-if="job?.error" :type="job.status==='failed'?'error':'warning'" :show-icon="false"><strong>{{ job.code }}</strong> {{ job.error }}<p v-if="job.suggestion">{{ job.suggestion }}</p><details v-if="job.detail"><summary>诊断详情</summary><pre>{{ JSON.stringify(job.detail,null,2) }}</pre></details></n-alert>
    <n-alert v-if="job?.diagnostics_warning" type="warning">{{ job.diagnostics_warning }}</n-alert>
    <n-alert v-if="job?.warning" type="warning" :show-icon="false">{{ job.warning }}</n-alert>
    <div class="diagnostic-columns">
      <section class="diagnostic-pane"><h3>评论运行日志</h3><small v-if="truncated" class="muted">仅显示末尾日志，完整内容保存在项目 logs 目录。</small><pre ref="logBox" class="diagnostic-log">{{ lines.join('\n') || '暂无记录；下次评论处理时会自动记录。' }}</pre></section>
      <section class="diagnostic-pane"><div class="conversation-heading"><h3>LLM 对话</h3><n-button v-if="!follow" text size="tiny" @click="bottom">回到最新</n-button></div>
        <div ref="historyBox" class="conversation-history" @scroll="scroll">
          <p v-if="!rows.length" class="muted">{{ exists?'等待模型请求，或本次全部复用了缓存。':'此前未记录的对话无法恢复；下次运行后可查看。' }}</p>
          <article v-for="row in rows" :key="row.id" :class="['conversation-row',row.role]">
            <header><strong>{{ conversationLabels[row.role]||row.role }}</strong><time>{{ row.ts?.slice(11,19) }}</time><small v-if="row.profile || row.model">{{ row.profile }}<template v-if="row.profile && row.model"> / </template>{{ row.model }}</small><small v-if="row.code">{{ row.code }}</small><small v-if="row.attempt && row.attempt>1">第 {{ row.attempt }} 次尝试</small></header>
            <details v-if="['system','reasoning','history'].includes(row.role)"><summary>展开查看 · {{ row.content.length }} 字</summary><pre v-text="row.content"/></details>
            <pre v-else v-text="row.content"/>
          </article>
        </div>
      </section>
    </div>
  </section>
</template>
<style scoped>
.comment-diagnostics{display:grid;gap:14px;margin-top:14px}.diagnostic-toolbar,.action-tabs,.conversation-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.diagnostic-toolbar>.n-button{margin-left:auto}.action-tabs button{background:transparent;border:1px solid var(--df-border);border-radius:6px;padding:7px 12px;color:var(--df-muted);cursor:pointer}.action-tabs button[aria-pressed=true]{color:var(--df-accent);background:var(--df-accent-soft);border-color:var(--df-accent)}.muted,time,small{color:var(--df-muted);font-size:12px}.diagnostic-columns{display:grid;grid-template-columns:minmax(250px,1fr) minmax(0,1.6fr);gap:16px}.diagnostic-pane{min-width:0;background:var(--df-panel);border:1px solid var(--df-border);border-radius:10px;padding:14px}.diagnostic-pane h3{font-size:13px;margin:0 0 12px}.conversation-heading{justify-content:space-between}.diagnostic-log,.conversation-history{height:480px;overflow:auto;margin:0;scrollbar-gutter:stable}.diagnostic-log,pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.65;font-family:var(--font-mono,monospace)}.conversation-row{padding:12px;border:1px solid var(--df-border);border-radius:7px;margin-bottom:10px}.conversation-row header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px}.conversation-row pre{margin:8px 0 0}.conversation-row summary{font-size:12px;color:var(--df-muted);cursor:pointer;margin-top:8px}.conversation-row.user{border-left:3px solid var(--df-accent)}.conversation-row.assistant{border-left:3px solid #77bda4}.conversation-row.error,.conversation-row.provider_error,.conversation-row.validation_error{border-left:3px solid #df9d79}.conversation-row.retry_strategy,.conversation-row.json_repair{border-left:3px solid #c9b576}@media(max-width:1000px){.diagnostic-columns{grid-template-columns:1fr}.diagnostic-log{height:220px}.conversation-history{height:460px}}
</style>
