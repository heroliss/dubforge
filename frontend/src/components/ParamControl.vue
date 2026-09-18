<script setup lang="ts">
// 参数控件: 按 schema type 渲染对应 n-* 控件 (步骤页 / 设置页共用)
import { computed } from 'vue'

const props = defineProps<{ field: any, modelValue: any }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: any): void }>()
const val = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <n-switch v-if="field.type === 'bool'" v-model:value="val" />
  <n-select v-else-if="field.type === 'select'" v-model:value="val" :options="field.options"
            filterable :tag="!!field.editable" />
  <n-input-number v-else-if="field.type === 'int' || field.type === 'float'"
                  v-model:value="val" :min="field.min" :max="field.max"
                  :step="field.step ?? (field.type === 'int' ? 1 : 0.1)"
                  style="width: 150px" />
  <n-input v-else-if="field.type === 'text'" v-model:value="val" type="textarea" :rows="2" />
  <n-input v-else-if="field.type === 'password'" v-model:value="val" type="password"
           show-password-on="click" />
  <n-input v-else v-model:value="val" />
</template>
