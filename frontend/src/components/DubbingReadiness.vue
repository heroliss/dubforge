<script setup lang="ts">
defineProps<{ summary: { needed: boolean; known?: boolean; ready?: number; required?: number; pending?: number; original?: number; silent?: number } }>()
</script>
<template>
  <div class="dubbing-readiness" :data-pending="summary.pending || 0">
    <strong>配音检查</strong>
    <span v-if="!summary.needed">所选版本使用原声，不运行配音</span>
    <span v-else-if="!summary.known">原文与译文就绪后，逐句检查并补齐配音</span>
    <template v-else>
      <span>已就绪 {{ summary.ready }} / {{ summary.required }} 句</span>
      <span v-if="summary.pending" class="pending">待补 {{ summary.pending }} 句</span>
      <span v-else>无需补配</span>
      <small>{{ summary.pending ? '复用有效配音，仅补缺失或失效的句子。' : '复用已有配音和明确的声源选择，跳过配音步骤。' }}<template v-if="summary.original"> 已选原声 {{ summary.original }} 句。</template><template v-if="summary.silent"> 已设静音 {{ summary.silent }} 句。</template></small>
    </template>
  </div>
</template>
<style scoped>
.dubbing-readiness { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; font-size:13px; padding:12px 14px; border:1px solid var(--df-border); border-radius:8px; }
.dubbing-readiness strong { font-size:12px; }.dubbing-readiness small { flex-basis:100%; color:var(--df-muted); line-height:1.6; }.pending { color:var(--df-accent); font-weight:600; }
</style>
