<script setup lang="ts">
import { ref } from 'vue'
import { NAlert, NButton } from 'naive-ui'

const props = defineProps<{ reason: string; refresh: () => Promise<unknown> }>()
const refreshing = ref(false)

// 后台轮询只更新权限，用户主动核对才显示加载，避免每轮检查改变提示布局。
async function recheck() {
  if (refreshing.value) return
  refreshing.value = true
  try { await props.refresh() }
  finally { refreshing.value = false }
}
</script>

<template>
  <n-alert type="info" :show-icon="false" class="configuration-lock-notice" role="status">
    <div class="notice-row">
      <span class="notice-message">{{ reason }}<slot /></span>
      <n-button size="small" quaternary class="notice-action" :loading="refreshing"
                :disabled="refreshing" @click="recheck">重新核对</n-button>
    </div>
  </n-alert>
</template>

<style scoped>
.notice-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.notice-message { flex: 1 1 280px; min-width: 0; overflow-wrap: anywhere; line-height: 1.8; }
.notice-action { flex: 0 0 108px; width: 108px; height: 32px; }
</style>
