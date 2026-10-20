<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { conversationRows, conversationLabels, formatLlmEventTime } from '../utils/llmConversation'
const props = withDefaults(defineProps<{ events?: any[]; rows?: any[]; active?: boolean; emptyText?: string; heading?: boolean }>(), { heading: true })
const rows = computed(() => props.rows || conversationRows(props.events || []))
const historyBox = ref<HTMLElement | null>(null), follow = ref(true)
function scroll() { const el = historyBox.value; if (el) follow.value = el.scrollHeight - el.clientHeight - el.scrollTop < 45 }
async function bottom() { follow.value = true; await nextTick(); if (historyBox.value) historyBox.value.scrollTop = historyBox.value.scrollHeight }
watch(() => rows.value.at(-1)?.content, () => { if (follow.value) void bottom() })
</script>

<template>
  <section class="llm-conversation-panel" aria-label="LLM对话过程">
    <div v-if="heading" class="conversation-heading"><h3>LLM 对话</h3><button v-if="!follow" type="button" @click="bottom">回到最新</button></div>
    <div ref="historyBox" class="conversation-history llm-conversation-history" @scroll="scroll">
      <p v-if="!rows.length" class="conversation-empty">{{ emptyText || (active ? '正在等待模型返回内容；收到的输入、回复及校验过程会显示在这里。' : '暂无已记录的模型对话；此前未记录的内容无法恢复。') }}</p>
      <article v-for="row in rows" :key="row.id" :class="['conversation-row','llm-message',row.role,`llm-message-${row.role}`]">
        <header class="llm-message-head"><slot name="header" :row="row"><strong>{{ conversationLabels[row.role] || row.role }}</strong><time>{{ formatLlmEventTime(row.ts) }}</time><small v-if="row.profile || row.model">{{ row.profile }}<template v-if="row.profile && row.model"> / </template>{{ row.model }}</small><small v-if="row.code">{{ row.code }}</small><small v-if="row.attempt || row.retryAttempt > 1">第 {{ row.attempt || row.retryAttempt }} 次尝试</small><small v-if="row.aborted">本次候选未采用</small></slot></header>
        <slot name="content" :row="row"><details v-if="['system','reasoning','history'].includes(row.role) || row.content.length > 4000"><summary>{{ row.role === 'reasoning' ? '展开模型返回的思考' : '展开查看' }} · {{ row.content.length }} 字符</summary><pre v-text="row.content" /></details><pre v-else v-text="row.content" /></slot>
      </article>
    </div>
    <p class="conversation-note">思考仅展示服务商实际返回的内容；未返回时不会推测或补写。重试保留同一时间线，重复携带的历史上下文仅展示一次。</p>
  </section>
</template>

<style scoped>
.llm-conversation-panel{min-width:0}.conversation-heading{display:flex;align-items:center;justify-content:space-between;gap:10px}.conversation-heading h3{margin:0 0 10px;font-size:13px}.conversation-heading button{border:0;background:transparent;color:var(--df-accent);font-size:11px;cursor:pointer}.conversation-history{max-height:560px;overflow:auto;scrollbar-gutter:stable;min-width:0}.conversation-empty,.conversation-note{color:var(--df-muted);font-size:11px;line-height:1.8}.conversation-row{border:1px solid var(--df-border);border-radius:8px;margin:9px 0;overflow:hidden}.conversation-row header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 9px;padding:8px 10px;background:rgba(128,150,180,.05);font-size:11px}.conversation-row strong{font-size:11px}.conversation-row time,.conversation-row small{font-size:10px;color:var(--df-muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.7;font-family:var(--font-mono,monospace);margin:0;padding:10px 12px;max-height:370px;overflow:auto}details summary{font-size:11px;color:var(--df-muted);cursor:pointer;padding:9px 12px}.user{border-left:3px solid var(--df-accent)}.assistant{border-left:3px solid #77bda4}.reasoning{border-left:3px solid #c6a962}.error,.provider_error,.validation_error,.request_aborted{border-left:3px solid #df9d79}.retry_strategy,.json_repair{border-left:3px solid #b5a0d4}
</style>
