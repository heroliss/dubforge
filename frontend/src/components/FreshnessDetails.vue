<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NPopover, NSpin } from 'naive-ui'
import FreshnessReport from './FreshnessReport.vue'
import { freshnessPreview, normalizeFreshnessReport, type FreshnessReport as Report } from '../utils/freshness'

const props=defineProps<{uid:string;stepKey?:string;label?:string;statusText?:string;summary?:string;buttonText?:string}>()
const show=ref(false), hovered=ref(false), busy=ref(false), error=ref(''), report=ref<Report|null>(null)
const title=computed(()=>`${props.label ? `${props.label} · ` : ''}更新原因`)
const accessibleName=computed(()=>`${props.label ? `${props.label} · ` : ''}查看更新原因`)
const badgeText=computed(()=>props.statusText || props.buttonText || '需更新')
const preview=computed(()=>freshnessPreview(report.value))
let controller:AbortController|undefined, pending:Promise<void>|undefined, epoch=0, disposed=false, loadedAt=0
const cacheCurrent=()=>!!report.value && Date.now()-loadedAt<30000

function cancel() {
  epoch++; controller?.abort(); controller=undefined; pending=undefined; busy.value=false
}
function close() { show.value=false; hovered.value=false; cancel() }
function resetContext() { close(); report.value=null; error.value=''; loadedAt=0 }
watch(()=>`${props.uid}:${props.stepKey || ''}`,resetContext)
function invalidateInput() {
  report.value=null; loadedAt=0; cancel()
  if(show.value || hovered.value)void load()
}
watch(()=>[props.statusText,props.summary],invalidateInput)

async function load() {
  if(disposed || !(show.value || hovered.value))return
  // 悬停与点击共享同一读取，摘要未返回时也允许立即打开详情。
  if(busy.value)return pending
  const ticket=++epoch, uid=props.uid, key=props.stepKey
  const active=new AbortController()
  controller=active; busy.value=true; error.value=''
  pending=(async()=>{
    try {
      const response=await fetch(`/api/projects/${encodeURIComponent(uid)}/freshness${key?`?step=${encodeURIComponent(key)}`:''}`,
        {method:'GET',cache:'no-store',signal:active.signal})
      const data=await response.json()
      if(!response.ok)throw new Error(data.message || '读取更新原因失败')
      const normalized=normalizeFreshnessReport(data)
      if(!normalized || normalized.project_uid && normalized.project_uid!==uid)throw new Error('更新原因报告不完整，请刷新后重试')
      if(key && (normalized.steps.length!==1 || normalized.steps[0].key!==key))throw new Error('未取得当前步骤的更新原因，请刷新后重试')
      if(disposed || ticket!==epoch || !(show.value || hovered.value) || props.uid!==uid || props.stepKey!==key)return
      report.value=normalized; loadedAt=Date.now()
    } catch(e:any) {
      if(!disposed && ticket===epoch && !active.signal.aborted)error.value=e.message || '读取更新原因失败，请重试'
    } finally {
      if(ticket===epoch){busy.value=false;controller=undefined;pending=undefined}
    }
  })()
  return pending
}
async function previewShown(value:boolean) {
  hovered.value=value && !show.value
  if(hovered.value && !cacheCurrent())await load()
  else if(!hovered.value && !show.value && busy.value)cancel()
}
async function open() {
  if(show.value || disposed)return
  show.value=true; hovered.value=false
  if(!cacheCurrent())await load()
}
onBeforeUnmount(()=>{disposed=true;cancel()})
</script>

<template>
  <n-popover trigger="hover" placement="top-start" :delay="280" :duration="120" :show="hovered && !show"
    :show-arrow="false" style="max-width:min(360px,calc(100vw - 32px));padding:0" @update:show="previewShown">
    <template #trigger>
      <button type="button" class="freshness-badge" :aria-label="accessibleName" aria-haspopup="dialog" :aria-expanded="show"
        @click.stop="open" @focus="previewShown(true)" @blur="previewShown(false)">
        <span class="badge-dot" aria-hidden="true"></span><span>{{ badgeText }}</span>
        <svg class="badge-chevron" aria-hidden="true" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="m6 4 4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>
      </button>
    </template>
    <div class="freshness-preview">
      <div class="preview-heading"><strong>更新摘要</strong><span>{{ label || '制作结果' }}</span></div>
      <ul v-if="preview.items.length"><li v-for="item in preview.items" :key="item">{{ item }}</li></ul>
      <p v-else>{{ error ? '暂时无法读取摘要，点击标签重试。' : summary || (busy ? '正在核对输入与设置…' : '点击标签查看具体变化。') }}</p>
      <small v-if="preview.remaining" class="preview-more">另有 {{ preview.remaining }} 项变化</small>
      <div class="preview-footer"><span v-if="busy" class="preview-loading">正在检查…</span><span v-else>点击标签查看完整对比</span><span aria-hidden="true">↗</span></div>
    </div>
  </n-popover>
  <n-modal :show="show" preset="card" :title="title" class="freshness-modal"
    style="width:min(960px,calc(100vw - 32px));max-width:calc(100vw - 32px);border-radius:16px"
    @update:show="value=>{if(!value)close()}">
    <p class="report-intro">对比上次成功生成与当前输入，查看具体变化和影响范围。</p>
    <div class="report-content">
      <n-alert v-if="error" type="error" :show-icon="false">{{ error }}<template #action><n-button size="small" :disabled="busy" @click="load">重试</n-button></template></n-alert>
      <n-spin :show="busy"><p v-if="busy && !report" class="report-loading" role="status" aria-live="polite">正在检查配置、上游结果和文件状态…</p><FreshnessReport v-if="report" :report="report" /></n-spin>
    </div>
    <template #footer><div class="report-actions"><span>仅查看，不会启动制作</span><n-button size="small" quaternary :disabled="busy" @click="load">重新检查</n-button><n-button size="small" secondary @click="close">关闭</n-button></div></template>
  </n-modal>
</template>

<style scoped>
.freshness-badge{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:3px 8px;border:1px solid rgba(239,189,112,.2);border-radius:6px;background:rgba(239,189,112,.08);color:#efbd70;font-family:inherit;font-size:11px;line-height:1.5;font-weight:500;white-space:nowrap;cursor:pointer;transition:background .15s,border-color .15s,box-shadow .15s;vertical-align:middle}
.freshness-badge:hover{background:rgba(239,189,112,.15);border-color:rgba(239,189,112,.4)}.freshness-badge:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}
.badge-dot{width:4px;height:4px;background:currentColor;border-radius:50%;flex:none;opacity:.8}.badge-chevron{opacity:.65;flex:none}.freshness-badge:hover .badge-chevron{opacity:1}
.freshness-preview{padding:15px 16px 0;font-size:12px;line-height:1.75;color:var(--df-text);width:320px;max-width:calc(100vw - 64px);box-sizing:border-box}.preview-heading{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:10px}.preview-heading strong{font-size:12px;font-weight:600}.preview-heading>span{font-size:10px;color:var(--df-muted);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.freshness-preview ul{list-style:none;margin:0;padding:0}.freshness-preview li{position:relative;padding:4px 0 4px 12px;overflow-wrap:anywhere}.freshness-preview li:before{content:'';position:absolute;left:0;top:13px;width:4px;height:4px;border-radius:50%;background:#efbd70;opacity:.7}.freshness-preview p{margin:0 0 10px;overflow-wrap:anywhere;color:var(--df-muted)}.preview-more{display:block;font-size:10px;color:var(--df-muted);margin:5px 0 9px}.preview-footer{display:flex;align-items:center;justify-content:space-between;margin-top:12px;padding:10px 0;border-top:1px solid var(--df-border);font-size:10px;color:var(--df-muted)}.preview-loading{color:var(--df-accent)}
.report-intro{font-size:12px;line-height:1.8;color:var(--df-muted);margin:0 0 20px}.report-content{max-height:65vh;overflow:auto;min-height:80px}.report-loading{padding:28px 0;text-align:center;color:var(--df-muted);font-size:12px}.report-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;border-top:1px solid var(--df-border);padding-top:16px}.report-actions>span{margin-right:auto;font-size:11px;color:var(--df-muted)}
@media(max-width:640px){.report-actions>span{display:none}.freshness-preview{width:300px}}@media(prefers-reduced-motion:reduce){.freshness-badge{transition:none}}
</style>
