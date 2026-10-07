<script setup lang="ts">
// 当前组合用于试片；加入清单后的每种成品独立保存三个维度，空清单不生成。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NRadioButton, NRadioGroup, NTag, NTooltip, useMessage, useDialog } from 'naive-ui'
import { useCommentOverlay } from '../composables/useCommentOverlay'
import { useNavStore } from '../stores/nav'
import { finalVideoKind, finalVideoLabel, newestVideoFirst } from '../utils/preview'
import { PUBLISH_ACTIVE } from '../utils/publishing'
import { fmtSize } from '../utils/format'
import { OUTPUT_LABELS, selectedOutputVariants, outputChoice, outputKey, choiceLabel, type AssemblyChoice } from '../utils/actionScope'
import DependencyNotice from './DependencyNotice.vue'
import FilePathLink from './FilePathLink.vue'
import { subtitleSettingsLink } from '../utils/subtitleWorkspace'
import { refreshInBackground } from '../utils/backgroundRefresh'
const props = defineProps<{ uid: string; artifacts: any[]; sourcePath: string; subtitlePath: string; subtitleName: string; disabled?: boolean; historyOnly?: boolean }>()
const emit = defineEmits<{ 'choice-change': [value: AssemblyChoice]; 'recycle-active': [value: boolean] }>()
const nav = useNavStore(), message = useMessage(), dialog = useDialog()
let disposed = false
let recycleDialog: ReturnType<typeof dialog.warning> | undefined
onBeforeUnmount(() => { disposed = true; recycleDialog?.destroy() })
const selected = ref<string[]>([]), choice = ref<AssemblyChoice>(outputChoice('src'))
const confirming = ref(false), submitting = ref(false), planBlockers = ref<any[]>([])
const refreshing = ref(false), recycling = ref(false), recycleConfirming = ref(false)
const { count: commentCount, revision: commentRevision, failed: commentsFailed, refresh: refreshComments } = useCommentOverlay(() => props.uid, () => !props.historyOnly)
const running = computed(() => ['running', 'queued'].includes(nav.stepMap.assemble?.status))
const locked = computed(() => !!props.disabled || running.value || submitting.value || confirming.value || recycling.value || recycleConfirming.value)
const recycleLocked = computed(() => locked.value || PUBLISH_ACTIVE.includes(nav.publication?.status))
const currentKey = computed(() => outputKey(choice.value))
const currentLabel = computed(() => choiceLabel(choice.value))
const canSubmit = computed(() => selected.value.length > 0 && !locked.value && (!selected.value.some(key => outputChoice(key).include_comments) || (commentCount.value > 0 && !commentsFailed.value)))
const taskLabel = computed(() => nav.stepMap.assemble?.assembly_task?.scope === 'preview' ? '正在生成试片' : '正在生成正式成品')
const taskSummary = computed(() => {
  const step = nav.stepMap.assemble, task = step?.assembly_task
  if (task?.scope === 'preview') return `正在试合成 ${task.start}–${task.end} 秒，不生成正式成品。`
  const labels = task?.version_names?.length ? task.version_names.join('；') : task?.scope === 'outputs' ? (task.variants ?? []).map((key: string) => OUTPUT_LABELS[key]).filter(Boolean).join('；') : step?.default_output_label
  return `当前正式合成任务：${labels || '读取方案中'}。${task?.comments_count ? `评论版使用 ${task.comments_count} 条已确认评论。` : '不叠加评论。'}当前任务以提交时的方案为准，不由页面勾选控制。`
})
const history = computed(() => [
  ...(props.artifacts || []).filter(item => item.version_id || finalVideoKind(item.name) === 'version').map(item => ({key:item.version_id || item.name,label:item.version_name || '独立成片版本',files:[item]})),
  ...Object.entries(OUTPUT_LABELS).map(([key, label]) => ({ key, label,
    files: (props.artifacts || []).filter(item => !item.version_id && finalVideoKind(item.name) === key).sort(newestVideoFirst),
  })).filter(group => group.files.length),
])
const oldFiles = computed(() => [...history.value.flatMap(group => group.files.slice(1)),
  ...(props.artifacts || []).filter(item => finalVideoKind(item.name) === 'legacy')].sort(newestVideoFirst))
const existingKeys = computed(() => new Set(history.value.filter(group => group.files.some(file => file.exists !== false)).map(group => group.key)))
watch(choice, value => { planBlockers.value = []; emit('choice-change', { ...value }) }, { deep: true, immediate: true })
watch(() => props.uid, () => { recycleDialog?.destroy(); recycleConfirming.value = false; selected.value = []; choice.value = outputChoice('src'); planBlockers.value = [] })
function setDimension(key: keyof AssemblyChoice, value: boolean) { choice.value = { ...choice.value, [key]: value } }
function toggle(key: string, value: boolean) {
  planBlockers.value = []
  selected.value = value ? selectedOutputVariants([...selected.value, key]) : selected.value.filter(item => item !== key)
}
function addCurrent() { if (currentKey.value !== 'src' && !locked.value) toggle(currentKey.value, true) }
function reuse(key: string) { if (!locked.value) { choice.value = outputChoice(key); toggle(key, true) } }
async function reveal(stepKey: string, name: string) {
  const response = await fetch(`/api/projects/${props.uid}/steps/${stepKey}/artifacts/${encodeURIComponent(name)}/reveal`, { method: 'POST' })
  if (!response.ok) message.error('定位失败（文件可能已不在磁盘）')
}
async function refreshFiles(notify = true) {
  if (refreshing.value) return
  const uid = props.uid
  refreshing.value = true
  try {
    await nav.refreshSteps()
    if (disposed || uid !== props.uid) return
    if (nav.stepsError) message.error(nav.stepsError)
    else if (notify) message.success('已按磁盘现状刷新成品列表')
  } catch { if (!disposed && uid === props.uid) message.error('刷新失败，请重试') }
  finally { refreshing.value = false }
}
function recycle(file: any) {
  if (recycleLocked.value || !file.id || !file.file_revision || file.exists === false) return
  confirmRemoval(file, false)
}
function forget(file: any) {
  if (recycleLocked.value || !file.id || !file.record_revision || file.exists !== false) return
  confirmRemoval(file, true)
}
function confirmRemoval(file: any, missing: boolean) {
  const uid = props.uid, name = file.name
  const body = { artifact_id: file.id, file_revision: file.file_revision, record_revision: file.record_revision, confirmed: true }
  recycleConfirming.value = true
  let accepted = false
  const confirmation = dialog.warning({ title: missing ? '删除这份缺失成品的记录？' : '将这份成品移到回收站？',
    content: `${finalVideoLabel(name, file.created_at)}（${fmtSize(file.disk_size ?? file.size)}）\n${name}\n` + (missing
      ? '仅删除本地成品记录，不删除任何磁盘文件或B站稿件。删除后，这条记录无法再通过“刷新列表”恢复；发布草稿中如有引用，需重新选择本地成品。'
      : '仅回收这一个文件，不删除原片、字幕、配音或B站稿件。发布草稿中的对应本地文件将暂不可用；从回收站还原到原位置后，刷新即可找回。'),
    positiveText: missing ? '仅删除记录' : '移到回收站', negativeText: '取消', maskClosable: false, closeOnEsc: false,
    onAfterLeave: () => { if (recycleDialog === confirmation) recycleConfirming.value = false },
    onNegativeClick: () => {},
    onPositiveClick: async () => {
      if (disposed || uid !== props.uid || recycling.value || accepted) return false
      recycling.value = true
      confirmation.loading = true
      confirmation.closable = false
      confirmation.negativeButtonProps = { disabled: true }
      emit('recycle-active', true)
      try {
        await nextTick()
        if (disposed || uid !== props.uid) return false
        const response = await fetch(`/api/projects/${uid}/steps/assemble/artifacts/${encodeURIComponent(name)}/${missing ? 'forget' : 'recycle'}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        })
        const result = await response.json().catch(() => ({}))
        if (!response.ok) throw new Error(result.message || (missing ? '无法删除记录，请刷新后重试' : '无法移到回收站，文件未被永久删除'))
        accepted = true
        if (!disposed && uid === props.uid) {
          nav.outputRemoved(uid, {id:body.artifact_id, name}, missing)
          message.success(result.message || (missing ? '已删除缺失成品记录' : '已移到回收站'))
          refreshInBackground(() => refreshFiles(false))
        }
      } catch (e: any) {
        if (!disposed && uid === props.uid) {
          message.error(e.message || '操作失败，请刷新后重试')
          refreshInBackground(() => refreshFiles(false))
        }
        return false
      } finally {
        recycling.value = false
        confirmation.loading = false; confirmation.closable = true; confirmation.negativeButtonProps = { disabled: false }
        if (!disposed && uid === props.uid) emit('recycle-active', false)
      }
    },
  })
  recycleDialog = confirmation
}
async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  const uid = props.uid, variants = selectedOutputVariants(selected.value)
  const payload = { outputs: variants.map(outputChoice), ...(variants.some(key => outputChoice(key).include_comments) ? { overlay_revision: commentRevision.value } : {}) }
  try {
    const response = await fetch(`/api/projects/${uid}/steps/assemble/output_plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.message || '无法检查成品方案')
    if (disposed || uid !== props.uid) return
    if (data.blockers?.length) { planBlockers.value = data.blockers; message.warning('部分方案依赖未就绪，请查看下方提示'); return }
    confirming.value = true
    let accepted = false
    dialog.info({ title: `确认生成 ${variants.length} 种完整成品`,
      content: `本次仅生成：${variants.map(key => OUTPUT_LABELS[key]).join('；')}。${data.comments_count ? `仅评论版叠加 ${data.comments_count} 条已确认评论。` : '不叠加评论。'}处理整段视频，使用已保存参数。${data.replacements?.length ? `将覆盖已有成品：${data.replacements.map((key: string) => OUTPUT_LABELS[key]).join('；')}。` : ''}同组合只保留一个当前成品，成功后替换；失败或取消保留原文件。`,
      positiveText: `生成这 ${variants.length} 种成品`, negativeText: '取消', maskClosable: false, closeOnEsc: false,
      onNegativeClick: () => { confirming.value = false }, onClose: () => { confirming.value = false },
      onPositiveClick: async () => {
        if (disposed || uid !== props.uid || submitting.value || accepted) return false
        submitting.value = true
        try {
          const result = await fetch(`/api/projects/${uid}/steps/assemble/outputs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
          const value = await result.json().catch(() => ({}))
          if (!result.ok) throw new Error(value.message || '合成启动失败')
          accepted = true
          nav.stepStarted(uid, 'assemble', value)
          message.success(`已开始生成 ${variants.length} 种成品`)
          refreshInBackground(() => nav.poll())
        } catch (e: any) { message.error(e.message || '合成启动失败') }
        finally { submitting.value = false; confirming.value = false }
      },
    })
  } catch (e: any) { message.error(e.message || '合成启动失败') }
  finally { submitting.value = false }
}
</script>

<template>
  <n-card size="small" :title="historyOnly ? '成品文件' : '成品组合'" class="assemble-outputs-panel">
    <template v-if="!historyOnly" #header-extra><n-tooltip><template #trigger><span class="help" tabindex="0">ⓘ</span></template>三项独立选择。当前组合用于下方短片试合成；加入清单后才会参与批量生成，不改变流程默认参数。</n-tooltip></template>
    <n-alert v-if="running" type="info" :show-icon="false" class="notice">{{ taskSummary }}</n-alert>
    <template v-if="!historyOnly">
    <div class="dimensions" aria-label="当前成品的三个独立维度">
      <section><h4>声音</h4><n-radio-group :value="choice.dubbing ? 'dub' : 'src'" :disabled="locked" aria-label="是否使用配音" @update:value="setDimension('dubbing', $event === 'dub')"><n-radio-button value="src">保留原声</n-radio-button><n-radio-button value="dub">使用配音</n-radio-button></n-radio-group></section>
      <section><h4>对话字幕</h4><n-radio-group :value="choice.burn_subtitles ? 'on' : 'off'" :disabled="locked" aria-label="是否烧录字幕" @update:value="setDimension('burn_subtitles', $event === 'on')"><n-radio-button value="off">不烧录</n-radio-button><n-radio-button value="on">烧录字幕</n-radio-button></n-radio-group></section>
      <section><h4>观众评论 <small>{{ commentCount }} 条已确认</small></h4><n-radio-group :value="choice.include_comments ? 'on' : 'off'" :disabled="locked" aria-label="是否叠加评论" @update:value="setDimension('include_comments', $event === 'on')"><n-radio-button value="off">不叠加</n-radio-button><n-radio-button value="on" :disabled="!commentCount || commentsFailed">叠加评论</n-radio-button></n-radio-group></section>
    </div>
    <div class="related-links"><RouterLink :to="subtitleSettingsLink(uid)">调整字幕 →</RouterLink><RouterLink :to="`/projects/${uid}/steps/download#comments-studio`">编辑评论 →</RouterLink><n-button size="tiny" quaternary :disabled="locked" @click="refreshComments">{{ commentsFailed ? '评论读取失败，重试' : '刷新评论方案' }}</n-button></div>
    <div class="current-combination"><div><small>当前组合 · 下方可试合成</small><strong>{{ currentLabel }} <n-tag v-if="existingKeys.has(currentKey)" size="small" :bordered="false" type="warning">已有成品 · 再生成将覆盖</n-tag></strong></div>
      <n-button v-if="currentKey !== 'src'" size="small" secondary :disabled="locked || selected.includes(currentKey) || (choice.include_comments && (!commentCount || commentsFailed))" @click="addCurrent">{{ selected.includes(currentKey) ? '已加入清单' : '加入本次清单' }}</n-button>
      <n-button v-else size="small" :disabled="!sourcePath" @click="reveal('download', 'video.mp4')">查看原片</n-button>
    </div>
    </template>
    <section class="existing-outputs" aria-label="已有组合成品">
      <div class="existing-heading"><h4>已有成品 <small>{{ historyOnly ? '同版本再次生成将替换自身' : '同组合再次生成将覆盖' }}</small></h4><n-button size="tiny" quaternary :loading="refreshing" :disabled="refreshing || recycling" title="检查磁盘文件；从回收站还原到原位置后，也可在这里刷新" @click="refreshFiles()">刷新列表</n-button></div>
      <p v-if="!history.length" class="empty-outputs">暂无组合成品</p>
      <div v-for="group in history" :key="group.key" class="existing-row">
        <div class="existing-info"><strong>{{ group.label }}</strong><small>{{ fmtSize(group.files[0].disk_size ?? group.files[0].size) }}</small><n-tag v-if="group.files[0].exists === false" size="small" :bordered="false" type="warning" title="恢复文件到原位置后，刷新列表即可重新使用">磁盘上缺失</n-tag><FilePathLink class="output-path" :path="group.files[0].path" /></div>
        <div class="existing-actions"><FilePathLink :path="group.files[0].path" :label="group.files[0].exists === false ? '定位原目录' : '定位文件'" /><n-button v-if="group.files[0].exists === false" size="tiny" quaternary type="error" :disabled="recycleLocked || !group.files[0].record_revision" :aria-label="`删除记录${group.label}`" @click="forget(group.files[0])">删除记录</n-button><n-button v-else size="tiny" quaternary type="error" :disabled="recycleLocked || !group.files[0].file_revision" :aria-label="`回收${group.label}`" @click="recycle(group.files[0])">移到回收站</n-button><n-button v-if="!historyOnly && !group.files[0].version_id" size="tiny" secondary :disabled="locked || selected.includes(group.key)" @click="reuse(group.key)">{{ selected.includes(group.key) ? '已加入清单' : '加入重生成' }}</n-button></div>
      </div>
    </section>
    <template v-if="!historyOnly">
    <div class="batch-heading"><h4>本次生成清单 <span>{{ selected.length }} 种</span></h4><small>调整上方选项不会修改已加入的方案</small></div>
    <div v-if="!selected.length" class="empty-list">尚未加入方案，不会生成任何成品。</div>
    <div v-for="key in selected" :key="key" class="batch-row"><span>{{ OUTPUT_LABELS[key] }} <n-tag size="tiny" :bordered="false" :type="existingKeys.has(key) ? 'warning' : 'info'">{{ existingKeys.has(key) ? '覆盖已有' : '新生成' }}</n-tag></span><n-button size="tiny" quaternary :disabled="locked" :aria-label="`移除${OUTPUT_LABELS[key]}`" @click="toggle(key, false)">移除</n-button></div>
    <div class="outputs-bar"><span>同组合成功后替换，失败保留原成品</span><n-button size="small" type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">{{ running ? taskLabel : `生成清单成品（${selected.length} 种）` }}</n-button></div>
    <DependencyNotice :uid="uid" :items="planBlockers" />
    </template>
    <div class="related-links"><RouterLink :to="`/projects/${uid}/publish`">选择成片，发布到B站 →</RouterLink></div>
    <details class="history"><summary>输入文件与旧版历史</summary>
      <div v-if="sourcePath" class="file-row"><span>原片 · 无需合成</span><FilePathLink :path="sourcePath" /></div>
      <div v-if="subtitlePath" class="file-row"><span>字幕输入</span><FilePathLink :path="subtitlePath" /></div>
      <div v-for="file in oldFiles" :key="file.name" class="file-row"><div class="existing-heading"><span>{{ finalVideoLabel(file.name, file.created_at) }} · 历史文件 <n-tag v-if="file.exists === false" size="small" :bordered="false" type="warning">磁盘上缺失</n-tag></span><n-button v-if="file.exists === false" size="tiny" quaternary type="error" :disabled="recycleLocked || !file.record_revision" @click="forget(file)">删除记录</n-button><n-button v-else size="tiny" quaternary type="error" :disabled="recycleLocked || !file.file_revision" :aria-label="`回收历史文件${file.name}`" @click="recycle(file)">移到回收站</n-button></div><FilePathLink :path="file.path" /><span>{{ fmtSize(file.disk_size ?? file.size) }}</span></div>
    </details>
  </n-card>
</template>

<style scoped>
.assemble-outputs-panel { margin-top:14px; } .notice { margin-bottom:14px; } .help { cursor:help; color:var(--df-muted); }
.existing-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }.existing-heading h4 { margin:0; }.empty-outputs { color:var(--df-muted); font-size:12px; }.existing-info { min-width:0; flex:1 1 280px; }.existing-info :deep(.n-tag) { margin-left:10px; }.output-path { display:block; margin-top:5px; font-size:11px; }.existing-actions { align-items:center; flex-wrap:wrap; }
.dimensions { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; } h4 { margin:0 0 10px; font-size:13px; } h4 small { color:var(--df-muted); font-weight:400; font-size:11px; }
.dimensions section { background:rgba(128,128,128,.04); padding:14px; border-radius:8px; }
.related-links { display:flex; align-items:center; justify-content:flex-end; gap:18px; flex-wrap:wrap; margin:10px 0 16px; font-size:12px; }
.current-combination { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; padding:14px 16px; border:1px solid var(--df-border); border-radius:8px; background:var(--df-accent-soft); }
.current-combination strong { display:block; margin-top:4px; font-size:14px; } small,.outputs-bar,.empty-list { color:var(--df-muted); font-size:12px; }
.batch-heading { display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; margin:24px 0 8px; } .batch-heading h4 { margin:0; } .batch-heading span { margin-left:5px; color:var(--df-muted); font-weight:400; }
.batch-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:9px 12px; border-bottom:1px solid var(--df-border); font-size:13px; }
.existing-outputs { margin-top:22px; } .existing-row { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 0; border-bottom:1px solid var(--df-border); flex-wrap:wrap; font-size:12px; }
.existing-row strong { font-weight:500; } .existing-row small { margin-left:10px; } .existing-actions { display:flex; gap:8px; flex-shrink:0; } .current-combination :deep(.n-tag) { margin-left:8px; vertical-align:middle; }
.empty-list { padding:16px; border:1px dashed var(--df-border); border-radius:8px; }
.outputs-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-top:16px; }
.history { margin-top:20px; font-size:12px; border-top:1px solid var(--df-border); padding-top:14px; } summary { cursor:pointer; color:var(--df-muted); }
.file-row { display:flex; flex-direction:column; gap:6px; padding:12px 0; border-bottom:1px solid var(--df-border); } .path-button { background:none; border:0; color:var(--df-muted); padding:0; text-align:left; overflow-wrap:anywhere; cursor:pointer; font-size:11px; } .path-button:hover { color:var(--df-accent); }
@media(max-width:1050px) { .dimensions { grid-template-columns:1fr; gap:8px; } .dimensions section { display:flex; justify-content:space-between; align-items:center; gap:10px; } .dimensions h4 { margin:0; } }
@media(max-width:470px) { .dimensions section { align-items:flex-start; flex-direction:column; } }
</style>
