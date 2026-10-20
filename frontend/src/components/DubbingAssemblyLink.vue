<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { NButton, NSelect } from 'naive-ui'
import { createLatestRefresh } from '../utils/preview'
import { commentFailure, commentWorkspaceLink } from '../utils/commentWorkflow'
import { assemblyComparisonKnown, assemblyReviewCandidates, assemblyDraftScope, saveAssemblySentenceDraft, type AssemblySentenceComparison } from '../utils/assemblySentenceLink'

const props = defineProps<{ uid: string; revision: string; selected: number[]; disabled?: boolean }>()
const emit = defineEmits<{ comparison: [value: AssemblySentenceComparison | null]; filter: []; 'reading-change': [value: 'loading' | 'ready' | 'error'] }>()
const router = useRouter(), value = ref<AssemblySentenceComparison | null>(null)
const versionId = ref<string | null>(null), busy = ref(false), error = ref('')
const commentProblem = ref(false)
let disposed = false
const options = computed(() => (value.value?.baselines || []).map(row => ({ label: row.name, value: row.version_id })))
const changes = computed(() => (value.value?.sentences || []).filter(row => row.status === 'changed'))
const pending = computed(() => (value.value?.sentences || []).filter(row => row.dubbing_pending).length)
const reviewCandidates = computed(() => assemblyReviewCandidates(value.value))
const known = computed(() => assemblyComparisonKnown(value.value))
const unknownCount = computed(() => (value.value?.sentences || []).filter(row => row.status === 'unknown').length)
const canSuggest = computed(() => !busy.value && !error.value && known.value && !!value.value?.suggestion?.can_partial && changes.value.length > 0)
const reader = createLatestRefresh(async signal => {
  const uid = props.uid, version = versionId.value
  const response = await fetch(`/api/projects/${encodeURIComponent(uid)}/assembly-versions/sentence-changes${version ? `?version_id=${encodeURIComponent(version)}` : ''}`, {
    cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
  })
  if (response.status === 404) throw new Error('成片对照需要重启后端后使用。')
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.message || '读取成片对照失败，请重试。'), { code: data.code })
  return { uid, version, data: data as AssemblySentenceComparison }
}, result => {
  if (disposed || props.uid !== result.uid || versionId.value !== result.version) return
  value.value = result.data; error.value = ''; commentProblem.value = false; emit('comparison', result.data); emit('reading-change', 'ready')
}, (e: any) => { error.value = e.name === 'TimeoutError' ? '读取成片对照超时，请重试。' : e.message; commentProblem.value = commentFailure(e); emit('reading-change', 'error') })
let reading = 0
async function refresh() {
  if (disposed) return
  const token = ++reading; busy.value = true; error.value = ''; commentProblem.value = false; emit('reading-change', 'loading')
  await reader.refresh(true)
  if (token === reading) busy.value = false
}
function chooseVersion(id: string) { versionId.value = id; void refresh() }
function openRevision(automatic: boolean) {
  if (busy.value || error.value || props.disabled || !value.value?.baseline || value.value.baseline.file_exists === false || automatic && !canSuggest.value) return
  const ids = automatic ? value.value.suggestion!.line_ids : props.selected
  if (!ids.length && !(automatic && value.value.suggestion?.ranges.length)) return
  try {
    const scope = assemblyDraftScope(value.value, ids, automatic)
    const target = saveAssemblySentenceDraft(sessionStorage, { uid: props.uid, token: crypto.randomUUID(), created: Date.now(),
      baseline: value.value.baseline.version_id, artifactId: value.value.baseline.artifact_id, artifactHash: value.value.baseline.artifact_hash, ...scope })
    void router.push(target)
  } catch (e: any) { error.value = e?.message?.includes('修订范围') ? e.message : '浏览器暂时无法传递修订范围，请在合成成片页手动选择这些句子。' }
}
watch(() => props.revision, () => { void refresh() })
watch(() => props.uid, () => { reader.cancel(); value.value = null; versionId.value = null; emit('comparison', null); void refresh() })
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; reader.cancel() })
</script>
<template>
  <section class="assembly-link" aria-label="句子与成片对照" :aria-busy="busy">
    <div class="comparison-heading">
      <strong>与成片对照</strong>
      <n-select v-if="options.length" :value="versionId || value?.baseline?.version_id" :options="options" :disabled="busy || disabled" size="small" aria-label="对照成片版本" @update:value="chooseVersion" />
      <n-button size="tiny" quaternary :loading="busy" :disabled="busy" @click="refresh">刷新对照</n-button>
    </div>
    <div v-if="error" class="comparison-error" role="alert"><strong>成片对照暂不可用</strong><p>{{ error }}</p><RouterLink v-if="commentProblem" :to="commentWorkspaceLink(uid)">前往上屏评论检查</RouterLink></div>
    <p v-else-if="!value" class="comparison-note" role="status">正在读取已有成片…</p>
    <template v-else-if="value.baseline">
      <div class="comparison-summary" role="status">
        <span v-if="busy" class="pending-note">正在重新核对字幕、声音与成片，旧对照暂不作为本次结果。</span>
        <template v-else>
          <button v-if="changes.length" type="button" class="changed-count" @click="emit('filter')">{{ changes.length }} 句成片待更新</button>
          <span v-else-if="known && value.baseline.file_exists !== false">句子内容与此成片一致</span>
          <span v-else-if="unknownCount" class="pending-note">{{ unknownCount }} 句缺少可比较的历史快照，尚未确认差异</span>
          <button v-if="reviewCandidates.length" type="button" class="changed-count" @click="emit('filter')">{{ reviewCandidates.length }} 句成片待核对（手工修改候选）</button>
          <span v-if="pending" class="pending-note">{{ pending }} 句配音待更新，重做后再核对成片</span>
        </template>
      </div>
      <p v-if="value.baseline.file_exists === false" class="comparison-note">此成片文件已丢失，请重新生成或选择其他对照版本。</p>
      <p v-if="!busy && !known" class="comparison-note">{{ value.coverage === 'partial' ? '此成片仅部分范围有逐句快照，其他范围需手动核对。' : '此旧成片没有逐句快照，暂不能自动识别历史差异；可勾选需要修订的句子。' }}</p>
      <p v-if="!busy && reviewCandidates.length" class="comparison-note">“成片待核对”只列出已保存的手工修改候选，不代表其余未知句与成片一致。请查看原成片后决定修订范围。</p>
      <p v-if="!busy && value.global_changes?.length" class="comparison-note">整片设置有变化：{{ value.global_changes.join('；') }}。完整应用这些变化需重新合成整片。</p>
      <div class="comparison-actions">
        <n-button v-if="changes.length" size="tiny" secondary :disabled="disabled || busy || !canSuggest" :title="value.suggestion?.reason" @click="openRevision(true)">修订变化片段（{{ changes.length }} 句）…</n-button>
        <n-button v-if="selected.length" size="tiny" :disabled="disabled || busy || value.baseline.file_exists === false" @click="openRevision(false)">修订勾选片段（{{ selected.length }} 句）…</n-button>
        <RouterLink v-if="!busy && !canSuggest && value.global_changes?.length" :to="`/projects/${encodeURIComponent(uid)}/steps/assemble`">前往合成成片 →</RouterLink>
      </div>
      <p v-if="changes.length && !canSuggest && value.suggestion?.reason" class="comparison-note">{{ value.suggestion.reason }}</p>
    </template>
    <p v-else class="comparison-note">尚无成片；当前句子内容会用于首次合成。</p>
  </section>
</template>
<style scoped>
.comparison-error p{margin:6px 0;line-height:1.7}.comparison-error a{color:var(--dub-info);text-underline-offset:3px}
.assembly-link{margin:12px 0;padding:12px 0;border-top:1px solid var(--dub-border);border-bottom:1px solid var(--dub-border);min-width:0;font-size:12px}.comparison-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.comparison-heading strong{font-size:12px}.comparison-heading :deep(.n-select){flex:1 1 180px;min-width:0;max-width:300px}.comparison-heading>.n-button{margin-left:auto}.comparison-summary{display:flex;gap:8px 16px;align-items:center;flex-wrap:wrap;margin-top:10px}.comparison-note,.pending-note{color:var(--dub-muted);line-height:1.7}.comparison-note{margin:8px 0 0;max-width:75ch}.comparison-error{color:var(--dub-error);margin:8px 0 0}.changed-count{border:0;padding:0;background:transparent;color:var(--dub-info);font:inherit;font-weight:600;cursor:pointer;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:3px}.changed-count:focus-visible{outline:2px solid var(--dub-info);outline-offset:3px}.changed-count:disabled{opacity:.6}.comparison-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:10px}.comparison-actions:empty{display:none}.comparison-actions a{color:var(--dub-info);font-size:12px;text-underline-offset:3px}@media(max-width:600px){.comparison-heading :deep(.n-select){max-width:none;flex-basis:160px}.comparison-actions{align-items:flex-start}.comparison-heading{gap:8px}}
</style>
