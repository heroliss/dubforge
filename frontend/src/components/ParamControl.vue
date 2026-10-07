<script setup lang="ts">
// 参数控件: 按 schema type 渲染对应 n-* 控件 (步骤页 / 设置页共用)
import { computed } from 'vue'
import { NButton, NInput, NInputNumber, NSelect, NSwitch, NSlider, NColorPicker } from 'naive-ui'
import FilePathLink from './FilePathLink.vue'
import LlmThinkingDetails from './LlmThinkingDetails.vue'
const props = defineProps<{ field: any, modelValue: any, disabled?: boolean, values?: Record<string, any> }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: any): void }>()
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
  <n-switch v-else-if="field.type === 'bool' && field.path?.endsWith('source_style_enabled')" v-model:value="val" :disabled="disabled" :aria-label="field.label">
    <template #checked>独立设置</template><template #unchecked>跟随译文</template>
  </n-switch>
  <n-switch v-else-if="field.type === 'bool'" v-model:value="val" :disabled="disabled" :aria-label="field.label" />
  <n-color-picker v-else-if="field.type === 'color'" v-model:value="val" :disabled="disabled" :show-alpha="false" :modes="['hex']" :aria-label="field.label" />
  <div v-else-if="field.path?.endsWith('width_percent')" class="background-opacity">
    <n-slider v-model:value="val" :min="0" :max="100" :step="1" :disabled="disabled" :aria-label="field.label" :format-tooltip="value => value === 0 ? '自动' : `${value}% 画面宽度`" />
    <n-input-number v-model:value="val" :min="0" :max="100" :show-button="false" :disabled="disabled" :input-props="{ 'aria-label': field.label }"><template #suffix>%</template></n-input-number>
    <n-button size="tiny" quaternary :disabled="disabled || val === 0" :title="field.help" @click="val = 0">{{ val === 0 ? '自动' : '设为自动' }}</n-button>
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
  <n-select v-else-if="field.type === 'select'" v-model:value="val" :options="field.options" :disabled="disabled"
            filterable :tag="!!field.editable" :aria-label="field.label" />
  <n-input-number v-else-if="field.type === 'int' || field.type === 'float'" :disabled="disabled"
                  v-model:value="val" :min="field.min" :max="field.max"
                  :step="field.step ?? (field.type === 'int' ? 1 : 0.1)"
                  style="width: 150px" :input-props="{ 'aria-label': field.label }" />
  <n-input v-else-if="field.type === 'text'" v-model:value="val" type="textarea" :rows="2" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
  <n-input v-else-if="field.type === 'password'" v-model:value="val" type="password" :disabled="disabled" :input-props="{ 'aria-label': field.label }"
           show-password-on="click" :aria-label="field.label" />
  <div v-else-if="/(?:_path|project_dir|timbre_audio|emo_audio)$/.test(field.path || '')" class="path-control">
    <n-input v-model:value="val" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
    <FilePathLink v-if="val" :path="val" label="定位" />
  </div>
  <n-input v-else v-model:value="val" :disabled="disabled" :input-props="{ 'aria-label': field.label }" />
</template>

<style scoped>
.thinking-control { width:100%; min-width:0; }
.path-control { display:flex; align-items:center; gap:10px; min-width:0; }.path-control :deep(.n-input) { flex:1; min-width:0; }.path-control :deep(.file-path-link) { flex-shrink:0; font-size:12px; }
.emotion-vector { width: 100%; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(250px, 100%), 1fr)); gap: 12px 24px; }
.emotion-item { display: flex; align-items: center; gap: 12px; min-width: 0; }
.emotion-item > span { width: 30px; flex-shrink: 0; }
.emotion-item :deep(.n-slider) { flex: 1; min-width: 50px; }
.emotion-item :deep(.n-input-number) { width: 64px; flex-shrink: 0; }
.background-opacity { display:flex; align-items:center; gap:10px; width:100%; min-width:0; }
.background-opacity :deep(.n-slider) { flex:1; min-width:55px; }
.background-opacity :deep(.n-input-number) { width:70px; flex:none; }
</style>
