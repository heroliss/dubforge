<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ mode: string; profile: string; profiles: any[] }>()
const selected = computed(() => props.profiles.filter(p => p.enabled !== false && (!props.profile || p.name === props.profile)))
function detail(p: any) {
  if (!p.params_valid) return '自定义 JSON 格式有误，须填写对象'
  if (props.mode === 'low' || props.mode === 'fast') {
    if (p.supports_low) return `实际请求：low${p.effort !== '服务商默认' ? ` · 覆盖 JSON 的 ${p.effort}` : ''}${p.has_thinking ? ' · 移除 thinking' : ''}`
    return `保留 JSON：${p.effort} · 自动 low 未适配，仅提示词要求尽快完成${p.has_thinking ? ` · thinking=${p.thinking_type}` : ''}`
  }
  return `跟随 JSON：${p.effort}${p.has_thinking ? ` · thinking=${p.thinking_type}` : ''}`
}
</script>

<template>
  <div class="thinking-details" role="status" aria-live="polite">
    <div v-for="p in selected" :key="p.name"><span class="profile-name">{{ p.name }}</span>{{ detail(p) }}</div>
    <div v-if="!selected.length">{{ profile ? '未找到已启用的模型配置，请检查选择' : '暂无已启用的模型配置' }}</div>
    <small>仅预览本步骤下一次请求；运行中的任务使用启动时配置。全局 JSON 保持不变。</small>
  </div>
</template>

<style scoped>
.thinking-details { margin-top:8px; font-size:12px; line-height:1.65; color:var(--df-muted, #9aa7b8); overflow-wrap:anywhere; }
.profile-name { margin-right:8px; font-weight:500; color:var(--df-text, #e6ebf2); }
small { display:block; margin-top:4px; color:var(--df-muted, #9aa7b8); }
</style>
