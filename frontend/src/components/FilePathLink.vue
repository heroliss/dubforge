<script setup lang="ts">
// 全工程统一的文件定位入口；文件不在时定位父目录，不执行文件。
import { ref } from 'vue'
import { useMessage } from 'naive-ui'
const props = defineProps<{ path?: string | null; label?: string }>()
const message = useMessage(), busy = ref(false)
async function reveal() {
  if (!props.path || busy.value) return
  busy.value = true
  try {
    const response = await fetch('/api/files/reveal', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: props.path }) })
    const result = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(result.message || '无法定位文件')
    if (result.missing) message.warning(result.message || '文件已缺失，已打开所在目录')
  } catch (e: any) { message.error(e.message || '无法连接 DubForge，请稍后重试') }
  finally { busy.value = false }
}
</script>

<template>
  <button v-if="path" type="button" class="file-path-link" :disabled="busy"
          :title="`在资源管理器中定位：${path}`" :aria-label="`定位 ${label || path}`" @click.stop.prevent="reveal">
    <slot>{{ label || path }}</slot>
  </button><span v-else class="empty-path">—</span>
</template>

<style scoped>
.file-path-link { display:inline; max-width:100%; padding:0; border:0; background:none; color:var(--df-accent,#63b3ed); font:inherit; text-align:left; overflow-wrap:anywhere; cursor:pointer; }
.file-path-link:hover { text-decoration:underline; }.file-path-link:focus-visible { outline:2px solid currentColor; outline-offset:3px; border-radius:2px; }.file-path-link:disabled { cursor:wait; opacity:.6; }.empty-path { color:var(--df-muted); }
</style>
