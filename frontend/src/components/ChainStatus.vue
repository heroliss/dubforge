<script setup lang="ts">
// 自动续跑链状态条 (ADR-0072): 显示剩余计划与挂起原因, 提供 继续/放弃 操作。
// active: 链在自动续跑; held: 计划保留待用户显式恢复 (失败/停止/中断)。
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { NButton } from 'naive-ui'
import { useMessage } from 'naive-ui'
import { friendlyError } from '../utils/http'
import { STEP_LABELS, useNavStore } from '../stores/nav'
import { chainKind, chainSequence, holdDescription, type ChainInfo } from '../utils/chainStatus'

const props = defineProps<{ uid: string; chain: ChainInfo | null }>()
const nav = useNavStore()
const message = useMessage()
const busy = ref(false)

const kind = computed(() => chainKind(props.chain))
const held = computed(() => props.chain?.state === 'held')
const sequence = computed(() => props.chain ? chainSequence(props.chain, STEP_LABELS) : '')
const holdText = computed(() => props.chain ? holdDescription(props.chain, STEP_LABELS) : '')
const holdStep = computed(() => props.chain?.hold?.step || props.chain?.remaining[0] || '')

async function call(path: string, okText: string) {
  if (busy.value) return
  busy.value = true
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(props.uid)}/chain/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    const d = await r.json()
    if (!r.ok) throw new Error(d.message || '操作失败')
    message.success(okText)
    void nav.poll()
  } catch (e: any) {
    message.error(friendlyError(e))
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div v-if="chain" class="chain-status" :class="held ? 'is-held' : 'is-active'" data-testid="chain-status">
    <template v-if="held">
      <span class="chain-icon" aria-hidden="true">⏸</span>
      <div class="chain-text">
        <strong>自动运行已挂起</strong>
        <span class="muted">{{ holdText || '等待确认' }}</span>
        <span class="seq">剩余：{{ sequence }}</span>
      </div>
      <div class="chain-actions">
        <RouterLink v-if="holdStep" :to="`/projects/${uid}/steps/${holdStep}`" class="chain-link">查看该步骤 →</RouterLink>
        <NButton size="tiny" type="primary" :loading="busy" :disabled="nav.busy || nav.dirMissing"
          @click="call('resume', '已继续自动运行')">重试并继续</NButton>
        <NButton size="tiny" quaternary :disabled="busy" @click="call('stop', '已放弃剩余计划')">放弃计划</NButton>
      </div>
    </template>
    <template v-else>
      <span class="chain-icon" aria-hidden="true">⛓</span>
      <div class="chain-text">
        <strong>自动运行中</strong>
        <span class="seq">剩余：{{ sequence }}</span>
      </div>
      <div class="chain-actions">
        <NButton size="tiny" quaternary :disabled="busy" @click="call('stop', '已停止后续自动运行; 当前步骤不受影响')">停止后续自动运行</NButton>
      </div>
    </template>
  </div>
</template>
<style scoped>
.chain-status { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:10px 12px; border-radius:8px;
  border:1px solid var(--df-border); background:var(--df-panel); font-size:12px; }
.chain-status.is-held { border-color:#efbd7066; }
.chain-icon { font-size:14px; }
.chain-text { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; min-width:0; }
.chain-text strong { font-size:12px; }
.muted { color:var(--df-muted); }
.seq { color:var(--df-muted); }
.chain-actions { display:flex; align-items:center; gap:8px; margin-left:auto; flex-wrap:wrap; }
.chain-link { font-size:12px; color:var(--df-accent); text-decoration:none; }
</style>
