<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NFormItem, NInput, NPopconfirm, NSpace, NSwitch, NTag, NText, useMessage } from 'naive-ui'
import { useConfigurationEditability } from '../utils/configurationEditability'

const props = defineProps<{ uid: string; disabled?: boolean }>()
const emit = defineEmits<{ 'dirty-change': [value: boolean]; 'busy-change': [value: boolean]; saved: [] }>()
const message = useMessage(), editability = useConfigurationEditability(() => `project:${props.uid}`)
const empty = () => ({ enabled: false, title: '', author: '', description: '', summary: '', terms: '' })
const state = ref<any>(null), draft = ref(empty()), baseline = ref(''), loading = ref(false), saving = ref(false), error = ref('')
const dirty = computed(() => !!baseline.value && JSON.stringify(draft.value) !== baseline.value)
const locked = computed(() => !!props.disabled || editability.locked.value
  || editability.isPathLocked('translation_context') || loading.value || saving.value)
const fields = [
  { key: 'title', label: '视频标题', placeholder: '原视频标题，辅助理解主题', rows: 1, max: 300 },
  { key: 'author', label: '作者 / 说话人', placeholder: '有依据的人物称谓；没有资料可留空', rows: 1, max: 200 },
  { key: 'description', label: '来源简介', placeholder: '已有平台简介；只保留与理解内容有关的资料', rows: 3, max: 4000 },
  { key: 'summary', label: '背景摘要', placeholder: '已有摘要或手工补充；不确定的内容可留空', rows: 3, max: 2000 },
  { key: 'terms', label: '项目术语', placeholder: '每行一个，例如：IndexTTS → IndexTTS（语音合成工具）', rows: 3, max: 4000 },
] as const
let abort: AbortController | null = null, generation = 0, disposed = false
watch(dirty, value => emit('dirty-change', value), { flush: 'sync' })
watch([loading, saving], () => emit('busy-change', loading.value || saving.value), { flush: 'sync' })

function apply(value: any) {
  state.value = value
  draft.value = { ...empty(), ...(value.has_saved ? value.context : value.source_fields), enabled: !!value.context.enabled }
  baseline.value = JSON.stringify(draft.value)
}
async function load(fill = false) {
  if (saving.value || fill && locked.value) return
  abort?.abort()
  const current = abort = new AbortController(), ticket = ++generation, uid = props.uid
  loading.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${uid}/steps/translate/context`, { cache: 'no-store', signal: current.signal })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '翻译背景读取失败')
    if (disposed || current.signal.aborted || ticket !== generation || uid !== props.uid) return
    if (fill) {
      // 刷新只是填入已有资料的草稿，不调用模型、不立即改变制作输入；手工术语保留。
      state.value = value
      if (!value.has_source) { message.info('暂无可填入的缓存资料，当前草稿已保留'); return }
      draft.value = { ...empty(), ...value.source_fields, enabled: draft.value.enabled, terms: draft.value.terms }
      message.info('已填入最新缓存资料，保存后生效；项目术语已保留')
    } else apply(value)
  } catch (e: any) {
    if (!disposed && !current.signal.aborted && ticket === generation) error.value = e.message || '翻译背景读取失败'
  } finally { if (ticket === generation) loading.value = false }
}
function discard() { if (state.value && !saving.value) apply(state.value) }
async function save() {
  if (!dirty.value || locked.value || !state.value) return
  const uid = props.uid, ticket = generation
  const submitted = { context: { ...draft.value }, revision: state.value.revision }
  saving.value = true; error.value = ''
  try {
    if (!await editability.canEdit()) { error.value = editability.reason.value || '运行期间不能修改背景资料'; return }
    if (disposed || uid !== props.uid || ticket !== generation) return
    const response = await fetch(`/api/projects/${uid}/steps/translate/context`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(submitted),
    })
    const value = await response.json()
    if (!response.ok) throw new Error(value.message || '翻译背景保存失败')
    if (disposed || uid !== props.uid || ticket !== generation) return
    apply(value); emit('saved'); message.success('翻译背景已保存，将用于下一次翻译')
  } catch (e: any) { if (!disposed) error.value = e.message || '翻译背景保存失败' }
  finally { saving.value = false }
}
watch(() => props.uid, () => { state.value = null; baseline.value = ''; draft.value = empty(); void load() }, { immediate: true })
onUnmounted(() => { disposed = true; generation++; abort?.abort() })
</script>

<template>
  <n-card size="small" title="翻译背景与术语" class="context-card" :aria-busy="loading || saving">
    <template #header-extra><n-tag size="small" :bordered="false" :type="dirty ? 'warning' : state?.context.enabled ? 'success' : 'default'">{{ dirty ? '未保存' : state?.context.enabled ? '已启用' : '未启用' }}</n-tag></template>
    <p class="context-description">复用已有视频资料，帮助理解指代和统一称谓。保存并启用后用于翻译，原文编号与事实仍以语义整理结果为准。</p>
    <n-alert v-if="error" type="error" class="context-notice">{{ error }}<template #action><n-button size="small" :disabled="dirty || saving" @click="load()">重新读取</n-button></template></n-alert>
    <n-alert v-if="editability.reason.value" type="info" :show-icon="false" class="context-notice">{{ editability.reason.value }}</n-alert>
    <n-alert v-if="state?.summary_stale" type="warning" :show-icon="false" class="context-notice">来源资料已有变化，旧自动摘要未填入。可在获取视频页面重新整理摘要，再在此填入。</n-alert>
    <div class="context-controls">
      <label><n-switch v-model:value="draft.enabled" :disabled="locked" />用于下一次翻译</label>
      <n-popconfirm v-if="state" :disabled="locked" @positive-click="load(true)">
        <template #trigger><n-button size="small" secondary :disabled="locked">填入最新资料</n-button></template>
        用最新缓存标题、作者、简介和摘要替换当前草稿，项目术语保留；保存后才生效。
      </n-popconfirm>
      <n-text v-if="state && !state.has_source" depth="3">暂无平台资料，可直接手工填写</n-text>
    </div>
    <div class="context-fields df-settings-grid">
      <n-form-item v-for="field in fields" :key="field.key" :label="field.label" class="df-setting" :class="{ wide: field.rows > 1 }">
        <n-input v-model:value="draft[field.key]" :type="field.rows > 1 ? 'textarea' : 'text'" :rows="field.rows"
          :maxlength="field.max" show-count :placeholder="field.placeholder" :disabled="locked" :input-props="{ 'aria-label': field.label }" />
      </n-form-item>
    </div>
    <n-text depth="3" class="context-footnote">填入资料只读取本项目的缓存，不额外请求 LLM。背景变化只影响翻译及其相关下游；禁用时不会参与制作。</n-text>
    <n-space class="context-actions">
      <n-button type="primary" :loading="saving" :disabled="!dirty || locked" @click="save">保存背景与术语</n-button>
      <n-button v-if="dirty" quaternary :disabled="saving || loading" @click="discard">放弃修改</n-button>
    </n-space>
  </n-card>
</template>

<style scoped>
.context-card { margin-top: 16px; }
.context-description { margin: 0 0 16px; color: var(--df-muted); font-size: 12px; line-height: 1.8; }
.context-notice { margin-bottom: 14px; }
.context-controls { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; font-size: 12px; }
.context-controls label { display: flex; align-items: center; gap: 10px; }
.context-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 18px; }
.context-fields .wide { grid-column: 1 / -1; }
.context-footnote { display: block; font-size: 12px; line-height: 1.8; }
.context-actions { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--df-border); }
@media (max-width: 650px) { .context-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
