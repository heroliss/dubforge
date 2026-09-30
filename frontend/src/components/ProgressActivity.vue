<script setup lang="ts">
import type { ProgressActivity } from '../utils/progressActivity'
defineProps<{ activity?: ProgressActivity | null }>()
</script>
<template>
  <div v-if="activity" class="progress-activity" :class="{ unavailable: activity.unavailable }" role="status" aria-live="polite" aria-atomic="true">
    <span class="activity-label">{{ activity.label }}<time v-if="activity.time"> · {{ activity.time }}</time></span>
    <span class="activity-message" :title="activity.message">{{ activity.message }}</span>
  </div>
</template>
<style scoped>
.progress-activity { display:flex; align-items:baseline; flex-wrap:wrap; gap:3px 8px; min-width:0; max-width:100%; margin-top:6px; font-size:12px; line-height:1.6; }
.activity-label { flex:none; color:var(--df-muted); font-size:11px; } time { font-variant-numeric:tabular-nums; }
.activity-message { min-width:0; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; color:var(--df-text); }
.unavailable .activity-message { color:var(--df-muted); }
</style>
