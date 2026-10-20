<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NSpin, NTag } from 'naive-ui'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import { assemblySettingValue, type AssemblyInheritanceSummary } from '../utils/assemblyInheritance'

const props = defineProps<{ uid: string; summary?: AssemblyInheritanceSummary; disabled?: boolean }>()
const emit = defineEmits<{ applied: [value: any]; 'busy-change': [value: boolean] }>()
const show = ref(false), loading = ref(false), applying = ref(false), plan = ref<any>(null), error = ref('')
const current = computed<AssemblyInheritanceSummary>(() => plan.value?.inheritance || plan.value || props.summary || {})
const source = computed(() => props.summary?.source ? { ...props.summary.source, frozen: true } : undefined)
const changes = computed<any[]>(() => Array.isArray(plan.value?.changes) ? plan.value.changes : [])
const versions = computed<any[]>(() => Array.isArray(plan.value?.versions) ? plan.value.versions : [])
const available = computed(() => Math.max(0, Number(props.summary?.changed_count) || 0))
const pending = computed(() => loading.value || applying.value)
const canApply = computed(() => !pending.value && !props.disabled && !!plan.value?.fingerprint && !current.value.error)
let disposed = false, generation = 0, controller: AbortController | undefined
function currentContext(uid: string, ticket: number) { return !disposed && uid === props.uid && ticket === generation }
async function request(path: string, uid: string, signal: AbortSignal, body?: any) {
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/${path}`, {
    method: body ? 'POST' : 'GET', cache: 'no-store', signal,
    headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.message || '读取或应用最新合成设置失败')
  return data
}
async function inspect() {
  if (pending.value || props.disabled || disposed) return
  const uid = props.uid, ticket = ++generation, read = new AbortController()
  controller?.abort(); controller = read; loading.value = true; error.value = ''; plan.value = null; show.value = true
  try {
    const data = await request('defaults-plan', uid, AbortSignal.any([read.signal, AbortSignal.timeout(15000)]))
    if (currentContext(uid, ticket) && !read.signal.aborted) plan.value = data
  } catch (e: any) { if (currentContext(uid, ticket) && !read.signal.aborted) error.value = e.name === 'TimeoutError' ? '读取设置变化超时，请重试；当前设置保持不变。' : e.message }
  finally { if (currentContext(uid, ticket)) { loading.value = false; controller = undefined } }
}
async function apply() {
  if (!canApply.value) return
  const uid = props.uid, ticket = generation, write = new AbortController(), fingerprint = plan.value.fingerprint
  controller = write; applying.value = true; error.value = ''
  try {
    const data = await request('apply-defaults', uid, AbortSignal.any([write.signal, AbortSignal.timeout(30000)]), { fingerprint, confirmed: true })
    if (currentContext(uid, ticket) && !write.signal.aborted) { emit('applied', data); show.value = false; plan.value = null }
  } catch (e: any) {
    if (currentContext(uid, ticket) && !write.signal.aborted) {
      error.value = e.name === 'TimeoutError' ? '应用超时，请重新读取当前设置确认结果；不会自动重试。' : e.message
      plan.value = null
    }
  } finally { if (currentContext(uid, ticket)) { applying.value = false; controller = undefined } }
}
function close() {
  if (applying.value) return
  generation++; controller?.abort(); controller = undefined; show.value = false; loading.value = false; plan.value = null
}
watch(pending, value => emit('busy-change', value), { flush: 'sync' })
watch(() => props.uid, () => { generation++; controller?.abort(); controller = undefined; loading.value = false; applying.value = false; show.value = false; plan.value = null; error.value = '' })
onBeforeUnmount(() => { disposed = true; generation++; controller?.abort(); emit('busy-change', false) })
// 同页的项目默认参数入口复用这里的差异检查与确认，不另建应用协议。
defineExpose({ inspect })
</script>

<template>
  <section class="assembly-defaults" aria-label="合成设置基础来源">
    <div class="defaults-heading"><span class="base-caption">项目保存的基础值</span><ConfigurationSourceBadge :source="source"/><strong>{{ summary?.source_name || summary?.source?.name || (summary?.source?.kind==='global'?'全局默认':'来源读取中') }}</strong><n-tag v-if="summary?.error" type="warning" size="small">需核对基础设置</n-tag><n-tag v-else-if="available" type="info" size="small">上级有 {{ available }} 项更新</n-tag><span v-else-if="summary" class="muted">参数无差异</span><n-button size="small" :disabled="disabled || pending" :loading="pending" @click="inspect">查看并应用最新合成设置</n-button></div>
    <p v-if="summary?.error" class="base-note">{{ summary.message || summary.error }} 可打开差异检查查看具体原因，当前项目设置保留。</p>
    <p v-else class="base-note">“恢复项目基础值”使用创建或上次应用时保存的值；要读取当前全局或模板，请查看并应用最新合成设置。<span v-if="summary?.version_list_fixed">当前整份版本清单已设为项目独立值，包含尚未修改的版本，应用时整单保留；“恢复项目基础清单”才改用项目保存的基础版本。</span><span v-else>版本定义沿用项目基础清单；各版本专用参数仍优先于共同样式。</span></p>
  </section>
  <n-drawer :show="show" width="min(820px,100vw)" :mask-closable="!applying" :close-on-esc="!applying" @update:show="value=>{if(!value)close()}">
    <n-drawer-content title="应用最新合成设置" :closable="!applying">
      <div class="defaults-review">
        <p class="review-note">更新本项目的合成基础值，保留项目单独设置；各版本专用值的变化与保留以下方预览为准。只更新设置，已有成片保留，制作与投稿都需另行启动。</p>
        <n-spin v-if="loading" size="small" description="正在比较基础值与各版本的实际变化…" />
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <template v-if="plan">
          <div class="review-counts"><span>基础更新 <strong>{{ Number(current.changed_count)||0 }}</strong> 项</span><span>当前配置变化 <strong>{{ Number(current.effective_count)||0 }}</strong> 项</span><span>保留项目设置 <strong>{{ Number(current.preserved_count)||0 }}</strong> 项</span></div>
          <n-alert v-if="current.version_list_fixed" type="info" :show-icon="false">版本清单保持项目独立设置；不会用上级清单覆盖它。共同样式更新只作用于仍跟随项目默认的参数。</n-alert>
          <section v-for="version in versions" :key="version.id" class="version-diff"><header><strong>{{ version.name }}</strong><small>{{ version.changes?.length || 0 }} 项实际变化 · 保留 {{ version.preserved_overrides?.length || 0 }} 项专用值</small></header><div v-if="version.changes?.length" class="diff-table" role="table" :aria-label="`${version.name}实际参数变化`"><div class="diff-row diff-head" role="row"><span>参数</span><span>当前值</span><span>应用后</span></div><div v-for="change in version.changes" :key="change.path" class="diff-row" role="row"><span>{{ change.label || '版本参数' }}</span><pre>{{ assemblySettingValue(change.before, change.path) }}</pre><pre>{{ assemblySettingValue(change.after, change.path) }}</pre></div></div><p v-else class="muted">此版本的生效参数没有变化。</p><details v-if="version.preserved_overrides?.length"><summary>保留的版本专用值</summary><div v-for="field in version.preserved_overrides" :key="field.path" class="preserved-row"><span>{{ field.label || '版本参数' }}</span><div><pre>{{ assemblySettingValue(field.value, field.path) }}</pre><ConfigurationSourceBadge v-if="field.source" :source="field.source"/></div></div></details></section>
          <details v-if="changes.length" class="base-diff"><summary>查看基础值变化（包含被项目设置保留的项目）</summary><div class="diff-table" role="table" aria-label="合成基础值变化"><div class="diff-row diff-head" role="row"><span>设置项</span><span>当前基础值</span><span>最新基础值</span></div><div v-for="change in changes" :key="change.path" class="diff-row" role="row"><div>{{ change.label || '合成设置' }}<small v-if="change.overridden">保留项目单独值<template v-if="Object.hasOwn(change, 'effective_after')">：{{ assemblySettingValue(change.effective_after, change.path) }}</template></small></div><div><pre>{{ assemblySettingValue(change.before, change.path) }}</pre><ConfigurationSourceBadge v-if="change.before_source" :source="change.before_source"/></div><div><pre>{{ assemblySettingValue(change.after, change.path) }}</pre><ConfigurationSourceBadge v-if="change.after_source" :source="change.after_source"/></div></div></div></details>
          <p v-if="!changes.length" class="muted">基础值没有变化；确认后只更新本次检查对应的来源记录。</p>
        </template>
        <p v-if="disabled" class="muted">请先保存或放弃当前草稿，并等待相关操作完成后再应用。</p>
      </div>
      <template #footer><div class="review-actions"><n-button :disabled="applying" @click="close">返回</n-button><n-button :disabled="disabled || pending" @click="inspect">重新检查</n-button><n-button type="primary" :disabled="!canApply" :loading="applying" @click="apply">确认应用合成设置</n-button></div></template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.assembly-defaults{padding:13px 15px;margin-bottom:16px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-panel-raised);min-width:0}.defaults-heading,.review-counts,.version-diff header,.review-actions{display:flex;align-items:center;flex-wrap:wrap;gap:9px 12px}.base-caption,.base-note,.muted,.version-diff small,.review-note,.diff-head,.base-diff small{font-size:12px;color:var(--df-muted);line-height:1.75}.defaults-heading strong{font-size:12px;font-weight:500}.defaults-heading>.n-button{margin-left:auto}.base-note{margin:8px 0 0;font-size:11px}.defaults-review{display:grid;gap:18px;min-width:0;padding:12px 0}.review-note{margin:0}.review-counts{font-size:12px;padding:12px;background:var(--df-panel-raised);border-radius:8px;gap:12px 22px}.review-counts strong{color:var(--df-text);font-weight:600;font-variant-numeric:tabular-nums}.version-diff{border:1px solid var(--df-border);border-radius:8px;padding:14px;min-width:0}.version-diff header{justify-content:space-between;font-size:13px;margin-bottom:8px}.version-diff p{margin:7px 0}.diff-row{display:grid;grid-template-columns:minmax(100px,.8fr) minmax(0,1fr) minmax(0,1fr);gap:12px;border-bottom:1px solid var(--df-border);padding:9px 0;font-size:12px;align-items:start;overflow-wrap:anywhere}.diff-row pre,.preserved-row pre{margin:0;font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}.diff-row .configuration-source{margin-top:5px}.diff-row small{display:block;font-size:11px;margin-top:4px}.preserved-row{display:grid;grid-template-columns:minmax(100px,.8fr) minmax(0,2fr);gap:12px;padding:7px 0;font-size:12px;border-bottom:1px solid var(--df-border)}summary{font-size:12px;cursor:pointer;color:var(--df-muted);line-height:1.8}.version-diff details{margin-top:12px}.review-actions{justify-content:flex-end}@media(max-width:600px){.defaults-heading>.n-button{margin-left:0}.diff-row{grid-template-columns:minmax(76px,.8fr) minmax(0,1fr) minmax(0,1fr);gap:8px}.version-diff{padding:11px}.review-counts{gap:8px 14px}}
</style>
