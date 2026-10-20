<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NForm, NFormItem, NInput, NInputNumber, NRadioButton, NRadioGroup, NSelect, NSpace, NText, useMessage, useThemeVars } from 'naive-ui'
import DubbingDurationReference from './DubbingDurationReference.vue'
import DubbingAdjustmentBadges from './DubbingAdjustmentBadges.vue'
import { dubbingTimingView, playbackDraftError, timeRange, preciseTime } from '../utils/dubbingTimingPresentation'

const props = defineProps<{ uid: string; line: any; disabled?: boolean; lineSettingsSupported?: boolean }>()
const emit = defineEmits<{ close: []; saved: []; 'dirty-change': [value: boolean]; 'edit-parameters': [id: number] }>()
const message = useMessage(), saving = ref(false), error = ref('')
const theme = useThemeVars()
const editorColors = computed(() => ({
  '--editor-text': theme.value.textColor1, '--editor-muted': theme.value.textColor3,
  '--editor-border': theme.value.borderColor, '--editor-surface': theme.value.actionColor,
  '--editor-primary': theme.value.primaryColor,
}))
const activePage = ref<'playback' | 'source'>('playback')
const initial = Object.fromEntries(['source_text', 'text', 'start', 'end'].map(k => [k, props.line[k]]))
const draft = ref({ ...initial, ...(props.line.saved_edit ?? {}) })
const timing = computed(() => dubbingTimingView(props.line))
const playbackMode = ref(timing.value.manual === null ? 'automatic' : 'manual')
const manualStart = ref<number | null>(timing.value.manual ?? timing.value.effective.start)
const manualEnd = ref<number | null>(timing.value.reservedEnd)
const audioHistory = computed(() => props.line.wav?.adjustment_history)
const audioRecords = computed(() => Array.isArray(audioHistory.value?.entries) ? audioHistory.value.entries : [])
const audioRecord = ref<string | null>(audioHistory.value?.current_id ?? null)
const audioDirty = computed(() => audioRecords.value.length > 1 && audioRecord.value !== audioHistory.value?.current_id)
const audioOptions = computed(() => audioRecords.value.map((entry: any) => ({ value: entry.id,
  label: `${entry.label}${typeof entry.factor === 'number' ? ` · 因子 ${entry.factor}` : ''}${typeof entry.seconds === 'number' ? ` · ${entry.seconds.toFixed(2)} 秒` : ''}${entry.id === audioHistory.value?.current_id ? '（当前）' : ''}` })))
const sourceDirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(initial))
const playbackDirty = computed(() => playbackMode.value === 'automatic' ? timing.value.manual !== null
  : timing.value.manual === null || manualStart.value !== timing.value.manual || manualEnd.value !== timing.value.reservedEnd)
const dirty = computed(() => sourceDirty.value || playbackDirty.value || audioDirty.value)
const minimumEnd = computed(() => manualStart.value != null && timing.value.seconds != null
  ? Math.ceil((manualStart.value + timing.value.seconds) * 1000) / 1000 : null)
const playbackError = computed(() => playbackMode.value === 'manual'
  ? playbackDraftError(manualStart.value, manualEnd.value, timing.value.seconds) : '')
const currentDirty = computed(() => activePage.value === 'playback' ? playbackDirty.value : sourceDirty.value)
const canSave = computed(() => !audioDirty.value && (activePage.value === 'playback'
  ? (playbackDirty.value || timing.value.stale) && !playbackError.value && (playbackMode.value === 'automatic' || timing.value.editable)
  : sourceDirty.value || !!props.line.edit_stale))
watch(dirty, value => emit('dirty-change', value), { immediate: true })
function changePage(value: 'playback' | 'source') {
  if (saving.value) return
  if (currentDirty.value || audioDirty.value) { message.warning('请先保存，或点击放弃修改，再切换编辑内容'); return }
  activePage.value = value; error.value = ''
}
function close() {
  if (saving.value) return
  if (dirty.value) { message.warning('请先保存，或点击放弃修改'); return }
  emit('close')
}
function discard() { emit('dirty-change', false); emit('close') }
function editParameters() {
  if (dirty.value) { message.warning('请先保存，或点击放弃修改，再调整配音参数'); return }
  emit('close'); emit('edit-parameters', props.line.id)
}
async function saveSource(reset = false) {
  if (saving.value || props.disabled) return
  saving.value = true; error.value = ''
  try {
    const r = await fetch(`/api/projects/${props.uid}/workbench/lines/${props.line.id}/text`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision: props.line.edit_revision, values: draft.value, reset}),
    })
    const data = await r.json()
    if (!r.ok) throw new Error(data.message || '保存失败，请重试')
    emit('dirty-change', false); emit('saved'); emit('close')
    message.success(reset ? '已恢复上游源稿；人工播放位置仍保留，待核对时可恢复自动排布' : '已保存源稿；字幕与配音状态将按修改内容更新')
  } catch (e: any) { error.value = e.message || '保存失败，请重试' }
  finally { saving.value = false }
}
async function savePlayback() {
  if (saving.value || props.disabled || !canSave.value) return
  saving.value = true; error.value = ''
  try {
    const reset = playbackMode.value === 'automatic'
    const body = reset ? { revision: timing.value.revision, reset: true }
      : { revision: timing.value.revision, start: manualStart.value, ...(manualEnd.value == null ? {} : { end: manualEnd.value }) }
    const response = await fetch(`/api/projects/${props.uid}/workbench/lines/${props.line.id}/timing`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '播放排布保存失败，请重试')
    emit('dirty-change', false); emit('saved'); emit('close')
    message.success(reset ? '已恢复自动排布；配音音频和源稿保留' : '已保存人工播放位置；原文、译文和配音同步更新，音频保留')
  } catch (e: any) { error.value = e.message || '播放排布保存失败，请重试' }
  finally { saving.value = false }
}
async function applyAudioRecord() {
  if (saving.value || props.disabled || !audioDirty.value || playbackDirty.value || sourceDirty.value) return
  saving.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${props.uid}/workbench/lines/${props.line.id}/audio-adjustment`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: audioHistory.value?.revision, entry_id: audioRecord.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message || '声音记录应用失败，请重试')
    emit('dirty-change', false); emit('saved'); emit('close')
    message.success('已在当前槽应用声音记录，并重新排布；试听后请重新确认满意')
  } catch (e: any) { error.value = e.message || '声音记录应用失败，请重试' }
  finally { saving.value = false }
}
</script>

<template>
  <n-drawer show :width="620" style="max-width:100vw" :mask-closable="!dirty && !saving" @update:show="!$event && close()">
    <n-drawer-content :title="`第 ${line.id} 句 · 文字与时间`" closable class="sentence-editor" :style="editorColors">
      <n-radio-group :value="activePage" size="small" class="page-tabs" aria-label="编辑内容" :disabled="saving" @update:value="changePage">
        <n-radio-button value="playback">播放排布</n-radio-button><n-radio-button value="source">源稿校对</n-radio-button>
      </n-radio-group>
      <n-alert v-if="error" type="error" class="editor-notice">{{ error }}</n-alert>
      <section v-if="activePage === 'playback'" aria-label="本句播放排布">
        <div class="playback-summary">
          <div class="summary-heading"><span>当前最终播放</span><span class="position-source">{{ timing.mode === 'manual' ? '人工调整' : timing.mode === 'source' ? '源时间' : '自动排布' }}</span></div>
          <p class="effective-time">{{ timeRange(timing.effective) }}</p>
          <DubbingAdjustmentBadges :line="line" />
          <p class="summary-note">原文、译文与配音共用此时间。</p>
        </div>
        <section class="timing-reference" aria-label="源稿与自动排布参考">
          <div class="group-heading"><h3>时间依据</h3><span>只读参考</span></div>
          <dl class="timing-layers">
            <div><dt>原始时间 <span>上游源稿</span></dt><dd>{{ timeRange(timing.source) }}</dd></div>
            <div v-if="timing.source.start !== line.start || timing.source.end !== line.end"><dt>校对源时间 <span>已保存校对</span></dt><dd>{{ timeRange(timing.current) }}</dd></div>
            <div><dt>自动排布 <span>{{ line.timing?.automatic_ready === false ? line.timing?.needs_scale ? '尚未容纳 · 源时间回退' : '未应用 · 源时间参考' : '不叠加人工位置' }}</span></dt><dd>{{ timeRange(timing.automatic) }}</dd></div>
          </dl>
        </section>
        <n-alert v-if="timing.stale" type="warning" class="editor-notice" :show-icon="false">本句内容或所选音频已变化，旧人工位置当前未采用。请核对完整音频范围后重新保存，或恢复自动排布。</n-alert>
        <n-alert v-if="line.timing?.needs_scale || line.timing?.manual_conflict" type="warning" class="editor-notice" :show-icon="false">本句仍放不下或人工位置与邻句冲突。请调整位置、恢复自动排布，或缩短声音后重新生成。源时间回退不是已解决冲突。</n-alert>
        <section class="editor-group position-group" aria-label="调整本句播放位置">
          <div class="group-heading"><h3>调整播放位置</h3><span>保留声音</span></div>
          <p class="group-intro">先利用附近空位，再按需人工调整；不重做音频、不新建配音槽。</p>
          <n-form label-placement="top" :disabled="disabled || saving || audioDirty" class="playback-form editor-form">
            <n-form-item label="本句位置来源">
              <n-radio-group v-model:value="playbackMode" aria-label="本句位置来源" class="position-options">
                <n-radio-button value="automatic">自动排布</n-radio-button><n-radio-button value="manual" :disabled="!timing.editable">人工调整</n-radio-button>
              </n-radio-group>
            </n-form-item>
            <template v-if="playbackMode === 'manual'">
              <div class="times">
                <n-form-item label="播放开始（秒）"><n-input-number v-model:value="manualStart" :disabled="!timing.editable" :min="0" :step="0.1" :precision="3" aria-label="播放开始（秒）" /></n-form-item>
                <n-form-item label="字幕结束 / 预留到（秒）"><n-input-number v-model:value="manualEnd" :disabled="!timing.editable" :min="minimumEnd ?? 0" :step="0.1" :precision="3" clearable placeholder="自动跟随完整配音" aria-label="字幕结束 / 预留到（秒）" /></n-form-item>
              </div>
              <div v-if="minimumEnd !== null" class="audio-length"><span>完整配音 <strong>{{ timing.seconds!.toFixed(3) }} 秒</strong></span><span>声音结束 <strong>{{ preciseTime(minimumEnd) }}</strong><small>{{ minimumEnd.toFixed(3) }} 秒</small></span></div>
              <n-alert v-if="playbackError" type="warning" class="editor-notice" :show-icon="false">{{ playbackError }}</n-alert>
              <n-text depth="3" class="note">结束留空时跟随完整声音，也可延长字幕停留；不能裁短声音。保存后重新检查邻句与视频边界。</n-text>
            </template>
            <n-text v-else depth="3" class="note">按已保存设置计算。切回自动并保存可撤销人工位置，保留源稿、配音槽和满意标记。</n-text>
          </n-form>
          <n-text v-if="!timing.editable" depth="3" class="note availability-note">选择有效配音后可人工调整；原声、静音或缺失音频仍按源稿时间处理。</n-text>
        </section>
        <section v-if="line.wav?.exists || line.wav?.duration_retry" class="editor-group generation-section" aria-label="当前声音生成记录">
          <div class="group-heading"><h3>声音与时长</h3><span>{{ line.selection?.take == null ? '当前声音' : `当前配音槽 ${line.selection.take}` }}</span></div>
          <DubbingDurationReference :line="line" />
          <div v-if="audioRecords.length > 1" class="history-group">
            <div class="history-heading">自动缩时记录</div>
            <div class="history-controls">
              <n-select v-model:value="audioRecord" :options="audioOptions" :disabled="disabled || saving || playbackDirty || sourceDirty" aria-label="当前槽自动缩时记录" />
              <n-button size="small" :loading="saving" :disabled="disabled || saving || !audioDirty || playbackDirty || sourceDirty" @click="applyAudioRecord">应用此声音记录</n-button>
            </div>
            <p class="note">应用会更新当前槽的声音并重新排布，不调用模型、不新建槽。试听后请重新确认满意；其他槽保留。</p>
          </div>
          <div class="generation-actions">
            <p class="note">声音的变化不能仅改时间还原。新因子需重新合成，旧声音可从记录或历史槽恢复。</p>
            <n-button v-if="lineSettingsSupported" size="small" :disabled="disabled || saving || dirty" @click="editParameters">调整本句合成参数…</n-button>
          </div>
        </section>
      </section>
      <section v-else aria-label="本句源稿校对">
        <p class="section-intro">修订文字与源稿边界；仅移动声音时，请切换到“播放排布”。</p>
        <n-alert v-if="line.edit_stale" type="warning" class="editor-notice">上游内容已变化，旧修改尚未全部应用。请核对后保存，或恢复上游内容。</n-alert>
        <n-form label-placement="top" :disabled="disabled || saving" class="editor-form source-form">
          <div class="editor-group">
            <div class="group-heading"><h3>双语文字</h3><span>校对当前句</span></div>
            <n-form-item label="原文"><n-input v-model:value="draft.source_text" type="textarea" :autosize="{minRows:2,maxRows:8}" aria-label="编辑原文" /></n-form-item>
            <n-form-item label="译文"><n-input v-model:value="draft.text" type="textarea" :autosize="{minRows:2,maxRows:8}" :disabled="!line.translation_available" placeholder="完成翻译后可在这里校对译文" aria-label="编辑译文" /></n-form-item>
          </div>
          <div class="editor-group">
            <div class="group-heading"><h3>源稿时间</h3><span>校对与原声参考</span></div>
            <div class="times">
              <n-form-item label="源稿开始（秒）"><n-input-number v-model:value="draft.start" :min="0" :step="0.1" :precision="3" aria-label="源稿开始（秒）" /></n-form-item>
              <n-form-item label="源稿结束（秒）"><n-input-number v-model:value="draft.end" :min="0" :step="0.1" :precision="3" aria-label="源稿结束（秒）" /></n-form-item>
            </div>
            <n-text depth="3" class="note">源句不能重叠。朗读跟随译文时，修改译文需重新配音；自定义朗读独立保留。保存后请核对排布和成片状态。</n-text>
          </div>
        </n-form>
        <details v-if="line.edit_stale" class="upstream"><summary>查看当前上游内容</summary><p>{{ line.base.source_text }}</p><p>{{ line.base.text }}</p><p>{{ line.base.start }} — {{ line.base.end }} 秒</p></details>
      </section>
      <template #footer>
        <div class="editor-footer">
          <n-text v-if="dirty" type="warning" class="footer-state">未保存</n-text>
          <n-space justify="end" align="center">
            <n-button v-if="activePage === 'source' && line.edited" quaternary :disabled="disabled || saving" @click="saveSource(true)">恢复上游源稿</n-button>
            <n-button v-if="dirty" quaternary :disabled="saving" @click="discard">放弃修改</n-button>
            <n-button v-else quaternary :disabled="saving" @click="close">关闭</n-button>
            <n-button type="primary" :loading="saving" :disabled="disabled || !canSave" @click="activePage === 'playback' ? savePlayback() : saveSource()">{{ activePage === 'playback' ? playbackMode === 'automatic' && timing.manual !== null ? '恢复自动并保存' : '保存播放排布' : '保存源稿校对' }}</n-button>
          </n-space>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.sentence-editor { color:var(--editor-text); }
.page-tabs { margin-bottom:20px; }
.section-intro,.group-intro { color:var(--editor-muted);font-size:12px;line-height:1.7; }
.section-intro { margin:0 0 18px; }
.group-intro { margin:0 0 14px; }
.editor-notice { margin-bottom:16px; }
.playback-summary { padding:16px 18px;border:1px solid color-mix(in srgb,var(--editor-primary) 26%,var(--editor-border));border-radius:10px;background:color-mix(in srgb,var(--editor-primary) 4%,transparent); }
.summary-heading { display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;font-weight:500; }
.position-source { padding:2px 7px;border:1px solid var(--editor-border);border-radius:5px;background:var(--editor-surface);color:var(--editor-muted);font-size:11px;font-weight:400; }
.effective-time { margin:12px 0 10px;font:500 22px/1.4 ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums;letter-spacing:-.4px; }
.summary-note { margin:10px 0 0;color:var(--editor-muted);font-size:11px;line-height:1.6; }
.timing-reference { margin:20px 0; }
.group-heading { display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px; }
.group-heading h3 { margin:0;font-size:13px;font-weight:600;line-height:1.5; }
.group-heading > span { color:var(--editor-muted);font-size:11px;font-weight:400; }
.timing-layers { margin:0; }
.timing-layers > div { display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0; }
.timing-layers > div + div { border-top:1px solid var(--editor-border); }
.timing-layers dt { min-width:0;font-size:12px;line-height:1.5; }
.timing-layers dt span { display:block;margin-top:3px;color:var(--editor-muted);font-size:11px; }
.timing-layers dd { flex-shrink:0;margin:0;font:12px/1.6 ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums; }
.editor-group { margin-top:20px;padding-top:20px;border-top:1px solid var(--editor-border); }
.position-group { padding:16px;border:1px solid var(--editor-border);border-radius:9px; }
.times { display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px; }
.times :deep(.n-input-number) { width:100%; }
.playback-form :deep(.n-form-item) { margin-bottom:0; }
.editor-form :deep(.n-form-item-label) { font-size:12px; }
.position-options { display:inline-flex; }
.audio-length { display:flex;flex-wrap:wrap;gap:10px 20px;margin:0 0 12px;padding:10px 12px;border-radius:6px;background:var(--editor-surface);color:var(--editor-muted);font-size:11px;line-height:1.6;font-variant-numeric:tabular-nums; }
.audio-length > span { display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 7px; }
.audio-length strong { color:var(--editor-text);font-size:12px;font-weight:500; }
.audio-length small { font-size:10px; }
.note { display:block;margin:8px 0 0;color:var(--editor-muted);font-size:12px;line-height:1.7; }
.availability-note { margin-top:12px; }
.generation-section :deep(.duration-reference) { margin-top:0; }
.history-group { margin-top:16px; }
.history-heading { margin-bottom:9px;font-size:12px;font-weight:500; }
.history-controls { display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px; }
.generation-actions { display:flex;flex-direction:column;align-items:flex-start;gap:12px;margin-top:14px; }
.generation-actions .note { margin:0; }
.source-form > .editor-group:first-child { margin-top:0; }
.source-form .editor-group { padding-top:16px; }
.upstream { margin-top:20px;padding:12px 14px;border:1px solid var(--editor-border);border-radius:8px;color:var(--editor-muted);font-size:12px;line-height:1.7; }
.upstream summary { cursor:pointer;color:var(--editor-text); }
.upstream p { margin:8px 0 0;overflow-wrap:anywhere; }
.editor-footer { display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:10px; }
.footer-state { margin-right:auto;font-size:11px; }
@media(max-width:480px) { .times { grid-template-columns:1fr;gap:0; }.timing-layers > div { align-items:flex-start;flex-direction:column;gap:6px; }.playback-summary { padding:14px; }.effective-time { font-size:18px; }.history-controls { grid-template-columns:1fr; }.history-controls > :last-child { justify-self:start; }.position-group { padding:14px; } }
</style>
