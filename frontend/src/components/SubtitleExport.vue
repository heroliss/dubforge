<script setup lang="ts">
import FilePathLink from './FilePathLink.vue'
import { computed, ref } from 'vue'
import { NAlert, NButton, NCard, NText, useMessage, useDialog } from 'naive-ui'
import DependencyNotice from './DependencyNotice.vue'
import { useNavStore } from '../stores/nav'
import { runPlannedSteps } from '../utils/runPlan'
import ExecutionProgress from './ExecutionProgress.vue'
import { executionForStep } from '../utils/executionProgress'
import { formatEta } from '../stores/nav'

const props = defineProps<{uid: string; state: any; disabled: boolean}>()
const emit = defineEmits<{refresh: []; preview: [name: string]}>()
const nav = useNavStore(), message = useMessage(), pending = ref(false)
const dialog = useDialog()
const names: Record<string,string> = {'source.srt':'原文字幕','trans.srt':'译文字幕','bilingual.srt':'双语字幕'}
const files = computed<any[]>(() => props.state?.files ?? [])
const running = computed(() => ['running','queued'].includes(props.state?.status))
const step = computed(() => nav.stepMap.subtitles || props.state)
const execution = computed(() => executionForStep({...step.value, label:'字幕导出'}))
const stopping = ref(false)
async function stopExport() {
  if (stopping.value || step.value?.cancelling || !running.value) return
  stopping.value = true
  try {
    const r = await fetch(`/api/projects/${props.uid}/steps/subtitles/cancel`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:step.value?.job_id ?? null})})
    const data = await r.json()
    if (!r.ok) throw new Error(data.message || '停止字幕导出失败')
    message.info(['stale_action','not_running'].includes(data.status) ? data.message || '字幕导出已经结束，无需停止' : '已请求停止字幕导出，等待当前处理安全收尾')
    emit('refresh'); await nav.poll(false)
  } catch(e:any) { message.error(e.message || '停止字幕导出失败') }
  finally { stopping.value = false }
}
async function exportFiles() {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再导出字幕。'); return }
  if (pending.value || props.disabled || running.value) return
  if (props.state?.unready_inputs?.length) {
    await runPlannedSteps(props.uid, ['subtitles'], true, message, dialog, async () => {},
      async () => { emit('refresh'); await nav.poll(false) }, () => nav.archived)
    return
  }
  pending.value = true
  try {
    const r = await fetch(`/api/projects/${props.uid}/steps/subtitles/run`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:false})})
    const data = await r.json()
    if (!r.ok) throw new Error(data.message || '字幕导出失败')
    message.success(data.status === 'skipped' ? '字幕已是最新' : '正在导出字幕')
    emit('refresh'); void nav.poll(false)
  } catch(e:any) { message.error(e.message || '字幕导出失败') }
  finally { pending.value = false }
}
async function reveal(name: string) {
  try {
    const r = await fetch(`/api/projects/${props.uid}/steps/subtitles/artifacts/${name}/reveal`, {method:'POST'})
    const data = await r.json()
    if (!r.ok) throw new Error(data.message || '无法定位文件')
  } catch(e:any) { message.error(e.message || '无法定位文件') }
}
</script>

<template>
  <n-card size="small" title="字幕文件" class="export-card">
    <template #header-extra><n-button size="small" type="primary" :loading="pending || running" :disabled="nav.archived || disabled || running" @click="exportFiles">{{ state?.unready_inputs?.length ? '准备依赖并仅导出字幕…' : state?.fresh ? '字幕已是最新' : '仅导出字幕文件' }}</n-button></template>
    <n-text depth="3" class="note">
      只导出字幕文件，使用已保存的文字、时间和导出设置；不生成配音、不生成视频，也不会把字幕烧进画面。
      依赖未就绪时先展示需更新的步骤，确认后再执行。
    </n-text>
    <DependencyNotice :uid="uid" :items="state?.unready_inputs ?? []" />
    <ExecutionProgress :execution="execution" :percentage="running ? step?.progress : null" :estimated="step?.progress_estimated" :indeterminate="step?.progress_indeterminate" :eta="running ? formatEta(step?.eta_seconds,step?.eta_scope) : ''" :activity="nav.activityFor('subtitles')" compact />
    <n-button v-if="running" size="small" secondary :loading="stopping" :disabled="step?.cancelling" @click="stopExport">{{ step?.cancelling ? '正在停止，等待收尾' : '停止字幕导出' }}</n-button>
    <n-alert v-if="state?.status === 'failed' && state?.error" type="error" class="spaced">{{ state.error }}</n-alert>
    <n-alert v-if="files.length && !state?.fresh && !running" type="warning" :show-icon="false" class="spaced">文字、时间或导出设置已变化，以下为上次导出的文件，请重新导出。</n-alert>
    <div v-for="item in files" :key="item.name" class="file-row">
      <span>{{ names[item.name] || item.name }}</span>
      <n-button size="tiny" quaternary :disabled="!state?.fresh || running" :aria-label="`预览${names[item.name] || item.name}`" @click="emit('preview', item.name)">预览</n-button>
      <a :href="item.url" :download="item.name">下载</a>
      <n-button size="tiny" quaternary @click="reveal(item.name)">定位文件</n-button>
      <FilePathLink class="file-path" :path="item.path" />
    </div>
  </n-card>
</template>

<style scoped>
.export-card { margin-top:16px; }
.note { font-size:12px; }
.spaced { margin-top:12px; }
.file-row { display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:12px;font-size:12px; }
.file-row a { color:inherit; }
.file-path { width:100%;opacity:.6;overflow-wrap:anywhere;font-size:11px; }
</style>
