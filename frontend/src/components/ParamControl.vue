<script setup lang="ts">
// 参数控件: 按 schema type 渲染对应 n-* 控件 (步骤页 / 设置页共用)
import { computed, useId } from 'vue'
import { NButton, NInput, NInputNumber, NSelect, NSwitch, NSlider, NColorPicker } from 'naive-ui'
import FilePathLink from './FilePathLink.vue'
import LlmThinkingDetails from './LlmThinkingDetails.vue'
import IndexTtsReferenceSentence from './IndexTtsReferenceSentence.vue'
import ReferenceFilePreview from './ReferenceFilePreview.vue'
import { isIndexTtsReferenceSentence } from '../utils/indexTtsReferenceSentence'
import { audioFieldNotice } from '../utils/audioQuality'
import { referencePreviewMaxSeconds } from '../utils/referenceAudioPreview'
const props = defineProps<{ field: any, modelValue: any, disabled?: boolean, values?: Record<string, any>, projectUid?: string, referenceFollowsCurrent?: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: any): void }>()
const noticeId=useId()
const audioNotice=computed(()=>audioFieldNotice(props.field,props.modelValue,props.values))
const referenceMode = computed(() => props.values?.[String(props.field.path).replace('_reference_line_id', '_mode')])
const referencePreviewEnabled = computed(() => referenceMode.value === 'stable' || referenceMode.value === 'dynamic' && props.referenceFollowsCurrent === true)
const referenceMaxSeconds = computed(() => props.referenceFollowsCurrent
  ? referencePreviewMaxSeconds(props.values?.['tts.indextts25.dynamic_reference_max_seconds']) : 15)
const referenceFile = computed(() => ['tts.indextts25.timbre_audio', 'tts.indextts25.emo_audio'].includes(props.field.path))
const val = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
const emotions = ['喜', '怒', '哀', '惧', '厌恶', '低落', '惊喜', '平静']
function setEmotion(index: number, value: number | null) {
  const vector = Array.isArray(props.modelValue) ? [...props.modelValue] : Array(8).fill(0)
  vector[index] = value
  emit('update:modelValue', vector)
}
</script>

<template>
  <div v-if="field.type === 'vector'" class="emotion-vector">
    <label v-for="(label, i) in emotions" :key="label" class="emotion-item">
      <span>{{ label }}</span>
      <n-slider :value="val?.[i] ?? 0" :min="0" :max="1" :step="0.05" :disabled="disabled"
                :aria-label="`情感向量：${label}`" @update:value="setEmotion(i, $event)" />
      <n-input-number :value="val?.[i] ?? 0" :min="0" :max="1" :step="0.05" :show-button="false"
                      :disabled="disabled" :input-props="{ 'aria-label': `${label}强度` }" @update:value="setEmotion(i, $event)" />
    </label>
  </div>
  <div v-else-if="field.type === 'bool'" class="parameter-toggle">
    <n-switch v-model:value="val" :disabled="disabled" :aria-disabled="disabled || undefined" :aria-label="field.label">
      <template v-if="field.path?.endsWith('source_style_enabled')" #checked>独立设置</template>
      <template v-if="field.path?.endsWith('source_style_enabled')" #unchecked>跟随译文</template>
    </n-switch>
  </div>
  <n-color-picker v-else-if="field.type === 'color'" v-model:value="val" :disabled="disabled" :show-alpha="false" :modes="['hex']" :aria-label="field.label" />
  <div v-else-if="field.path?.endsWith('width_percent')" class="background-opacity">
    <n-slider v-model:value="val" :min="0" :max="100" :step="1" :disabled="disabled" :aria-label="field.label" :format-tooltip="value => value === 0 ? '自动' : `${value}% 画面宽度`" />
    <n-input-number v-model:value="val" :min="0" :max="100" :show-button="false" :disabled="disabled" :input-props="{ 'aria-label': field.label }"><template #suffix>%</template></n-input-number>
    <n-button size="tiny" quaternary :disabled="disabled || val === 0" :title="field.help" @click="val = 0">{{ val === 0 ? '自动' : '设为自动' }}</n-button>
  </div>
  <div v-else-if="field.path?.endsWith('height_percent')" class="background-opacity">
    <n-slider v-model:value="val" :min="field.min ?? 1" :max="field.max ?? 100" :step="1" :disabled="disabled" :aria-label="field.label" :format-tooltip="value => `${value}% 画面高度（可扩展）`" />
    <n-input-number v-model:value="val" :min="field.min ?? 1" :max="field.max ?? 100" :show-button="false" :disabled="disabled" :input-props="{ 'aria-label': field.label }"><template #suffix>%</template></n-input-number>
  </div>
  <div v-else-if="field.path?.endsWith('background_opacity')" class="background-opacity">
    <n-slider v-model:value="val" :min="0" :max="100" :step="1" :disabled="disabled" :aria-label="field.label" :format-tooltip="value => value === 0 ? '完全透明' : `${value}% 不透明`" />
    <n-input-number v-model:value="val" :min="0" :max="100" :show-button="false" :disabled="disabled" :input-props="{ 'aria-label': field.label }"><template #suffix>%</template></n-input-number>
    <n-button size="tiny" quaternary :disabled="disabled || val === 0" @click="val = 0">透明</n-button>
  </div>
  <div v-else-if="field.path?.endsWith('.thinking_mode')" class="thinking-control">
    <n-select v-model:value="val" :options="field.options" :disabled="disabled" :aria-label="field.label" />
    <LlmThinkingDetails :mode="val" :profile="values?.[field.path.replace('thinking_mode', 'llm_profile')] || ''" :profiles="field.thinking_profiles || []" />
  </div>
  <IndexTtsReferenceSentence v-else-if="isIndexTtsReferenceSentence(field.path)" v-model="val" :label="field.label" :project-uid="projectUid" :disabled="disabled" :follow-current="referenceFollowsCurrent" :preview-enabled="referencePreviewEnabled" :max-seconds="referenceMaxSeconds" />
  <n-select v-else-if="field.type === 'select'" v-model:value="val" :options="field.options" :disabled="disabled"
            filterable :tag="!!field.editable" :aria-label="field.label" />
  <div v-else-if="field.path === 'assemble.final_true_peak_db'" class="peak-limit-control">
    <n-input-number v-model:value="val" :min="field.min" :max="field.max" :step="field.step ?? 0.1" :disabled="disabled"
                    :input-props="{ 'aria-label': field.label, 'aria-describedby': audioNotice ? noticeId : undefined }" />
    <small v-if="audioNotice" :id="noticeId" class="parameter-audio-note" role="note" aria-live="polite">{{ audioNotice }}</small>
  </div>
  <n-input-number v-else-if="field.type === 'int' || field.type === 'float'" :disabled="disabled"
                  v-model:value="val" :min="field.min" :max="field.max"
                  :step="field.step ?? (field.type === 'int' ? 1 : 0.1)"
                  style="width: 150px" :input-props="{ 'aria-label': field.label }" />
  <n-input v-else-if="field.type === 'text'" v-model:value="val" type="textarea" :rows="2" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
  <n-input v-else-if="field.type === 'password'" v-model:value="val" type="password" :disabled="disabled" :input-props="{ 'aria-label': field.label }"
           show-password-on="click" :aria-label="field.label" />
  <div v-else-if="/(?:_path|project_dir|timbre_audio|emo_audio)$/.test(field.path || '')" class="path-control" :class="{ 'reference-file-control': referenceFile }">
    <n-input v-model:value="val" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
    <FilePathLink v-if="val" :path="val" label="定位" />
    <ReferenceFilePreview v-if="referenceFile" :path="val" :label="field.label" />
  </div>
  <n-input v-else v-model:value="val" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
</template>

<style scoped>
.parameter-toggle { display:flex; align-items:center; min-height:28px; justify-self:start; }
.peak-limit-control { min-width:0; width:100%; }.peak-limit-control :deep(.n-input-number) { width:150px; max-width:100%; }.parameter-audio-note { display:block; max-width:30em; margin-top:5px; color:var(--df-warning); font-size:11px; line-height:1.6; overflow-wrap:anywhere; }
.thinking-control { width:100%; min-width:0; }
.path-control { display:flex; align-items:center; gap:10px; min-width:0; }.path-control :deep(.n-input) { flex:1; min-width:0; }.path-control :deep(.file-path-link) { flex-shrink:0; font-size:12px; }
.reference-file-control { flex-wrap:wrap; gap:6px; }.reference-file-control :deep(.n-input) { flex-basis:0; }
.emotion-vector { width: 100%; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(250px, 100%), 1fr)); gap: 12px 24px; }
.emotion-item { display: flex; align-items: center; gap: 12px; min-width: 0; }
.emotion-item > span { width: 30px; flex-shrink: 0; }
.emotion-item :deep(.n-slider) { flex: 1; min-width: 50px; }
.emotion-item :deep(.n-input-number) { width: 64px; flex-shrink: 0; }
.background-opacity { display:flex; align-items:center; gap:10px; width:100%; min-width:0; }
.background-opacity :deep(.n-slider) { flex:1; min-width:55px; }
.background-opacity :deep(.n-input-number) { width:70px; flex:none; }
</style>
