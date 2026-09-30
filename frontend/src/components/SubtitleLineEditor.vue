<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NForm, NFormItem, NInput, NInputNumber, NSpace, NText, useMessage } from 'naive-ui'

const props = defineProps<{ uid: string; line: any; disabled?: boolean }>()
const emit = defineEmits<{ close: []; saved: []; 'dirty-change': [value: boolean] }>()
const message = useMessage(), saving = ref(false), error = ref('')
const initial = Object.fromEntries(['source_text', 'text', 'start', 'end'].map(k => [k, props.line[k]]))
const draft = ref({ ...initial, ...(props.line.saved_edit ?? {}) })
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(initial))
watch(dirty, value => emit('dirty-change', value), { immediate: true })
function close() {
  if (saving.value) return
  if (dirty.value) { message.warning('请先保存，或点击放弃修改'); return }
  emit('close')
}
async function save(reset = false) {
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
    message.success(reset ? '已恢复上游文字和时间' : '已保存文字和时间；字幕导出将使用新内容')
  } catch (e: any) { error.value = e.message || '保存失败，请重试' }
  finally { saving.value = false }
}
</script>

<template>
  <n-drawer show :width="620" style="max-width:100vw" :mask-closable="!dirty && !saving" @update:show="!$event && close()">
    <n-drawer-content :title="`第 ${line.id} 句 · 文字与时间`" closable>
      <n-alert v-if="line.edit_stale" type="warning" style="margin-bottom:16px">上游内容已变化，旧修改尚未全部应用。请核对后保存，或恢复上游内容。</n-alert>
      <n-alert v-if="error" type="error" style="margin-bottom:16px">{{ error }}</n-alert>
      <n-form label-placement="top" :disabled="disabled || saving">
        <n-form-item label="原文"><n-input v-model:value="draft.source_text" type="textarea" :autosize="{minRows:2,maxRows:8}" aria-label="编辑原文" /></n-form-item>
        <n-form-item label="译文"><n-input v-model:value="draft.text" type="textarea" :autosize="{minRows:2,maxRows:8}" :disabled="!line.translation_available" placeholder="完成翻译后可在这里校对译文" aria-label="编辑译文" /></n-form-item>
        <div class="times">
          <n-form-item label="开始时间（秒）"><n-input-number v-model:value="draft.start" :min="0" :step="0.1" :precision="3" aria-label="开始时间（秒）" /></n-form-item>
          <n-form-item label="结束时间（秒）"><n-input-number v-model:value="draft.end" :min="0" :step="0.1" :precision="3" aria-label="结束时间（秒）" /></n-form-item>
        </div>
      </n-form>
      <n-text depth="3" class="note">保存后同步用于字幕、试听和成片。只改时间会移动已有配音的位置，不改变音频长度；修改译文后，对应配音需重新合成。相邻句子的时间不能重叠。</n-text>
      <details v-if="line.edit_stale" class="upstream"><summary>查看当前上游内容</summary><p>{{ line.base.source_text }}</p><p>{{ line.base.text }}</p><p>{{ line.base.start }} — {{ line.base.end }} 秒</p></details>
      <template #footer>
        <n-space justify="end" align="center">
          <n-button v-if="line.edited" quaternary :disabled="disabled || saving" @click="save(true)">恢复上游内容</n-button>
          <n-button v-if="dirty" quaternary :disabled="saving" @click="emit('dirty-change', false); emit('close')">放弃修改</n-button>
          <n-button type="primary" :loading="saving" :disabled="disabled || (!dirty && !line.edit_stale)" @click="save()">保存本句</n-button>
        </n-space>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.times { display:grid;grid-template-columns:1fr 1fr;gap:16px; }
.times :deep(.n-input-number) { width:100%; }
.note,.upstream { font-size:12px;line-height:1.7; }
.upstream { margin-top:16px; }
</style>
