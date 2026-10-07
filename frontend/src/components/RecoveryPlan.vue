<script setup lang="ts">
import { computed } from 'vue'
import { NTag } from 'naive-ui'
const props = defineProps<{ recovery: any }>()
const hasReused = computed(() => (props.recovery.rows || []).some((row: any) => row.action === 'reuse'))
const actions: Record<string, { label: string; tone: 'success' | 'warning' | 'info' }> = {
  reuse: { label: '复用', tone: 'success' }, update: { label: '需更新', tone: 'warning' },
  retry: { label: '重试', tone: 'warning' }, create: { label: '生成 / 补齐', tone: 'info' }, run: { label: '运行 / 补齐', tone: 'info' },
}
</script>
<template>
  <section class="recovery-plan" data-testid="recovery-plan" aria-label="本次恢复方案">
    <p v-if="recovery.previous_label || recovery.previous_step">上次停在：<strong>{{ recovery.previous_label || recovery.previous_step }}</strong></p>
    <p class="recovery-start">本次从：<strong>{{ recovery.start_label || recovery.start_step || '制作已就绪，继续后续流程' }}</strong></p>
    <p v-if="recovery.reason">{{ recovery.reason }}</p>
    <p v-if="hasReused" class="reuse-hint">有效步骤直接复用。</p>
    <ol aria-label="本次恢复步骤">
      <li v-for="row in recovery.rows || []" :key="row.key" :data-step="row.key" :data-action="row.action" :class="{ 'is-reused': row.action === 'reuse' }" :title="row.action === 'reuse' ? row.reason : undefined">
        <div><strong>{{ row.label || row.key }}</strong><n-tag size="small" :type="actions[row.action]?.tone || 'info'">{{ actions[row.action]?.label || '运行' }}</n-tag></div>
        <p v-if="row.action !== 'reuse'">{{ row.reason }}</p>
        <span v-else-if="row.reason" class="visually-hidden">{{ row.reason }}</span>
      </li>
    </ol>
  </section>
</template>
<style scoped>
.recovery-plan { display:grid; gap:8px; padding:14px; border:1px solid var(--df-border); border-radius:8px; background:var(--df-panel); }
p { margin:0; font-size:12px; line-height:1.7; color:var(--df-muted); } strong { color:var(--df-text); }
.recovery-start { color:var(--df-text); font-size:13px; }.recovery-start strong { font-size:14px; }.reuse-hint { font-size:11px; }
ol { list-style:none; padding:0; margin:2px 0 0; display:grid; gap:6px; } li { border-top:1px solid var(--df-border); padding-top:10px; }.is-reused { padding-top:4px; }
li>div { display:flex; gap:12px; align-items:center; justify-content:space-between; } li strong { font-size:12px; } li p { margin-top:4px; }
.visually-hidden { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
</style>
