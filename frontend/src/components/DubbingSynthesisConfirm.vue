<script setup lang="ts">
import { NSpin, NText, useThemeVars } from 'naive-ui'
import { computed } from 'vue'
import type { DubbingSynthesisPlan, DubbingSynthesisTarget } from '../utils/dubbingSynthesis'

const props = defineProps<{ plan: DubbingSynthesisPlan; slots: { value: number; count: number }[];
  scopeLabel?: string; note?: string; sentenceText?: string; submitting?: boolean; submissionError?: string; appending?: boolean }>()
const emit = defineEmits<{ (event: 'update:target', target: DubbingSynthesisTarget): void }>()
function changeMode(mode: DubbingSynthesisTarget['mode']) {
  if (props.submitting) return
  emit('update:target', { mode, slot: props.plan.target.slot ?? props.slots[0]?.value })
}
const theme = useThemeVars()
const style = computed(() => ({ '--summary-border': theme.value.borderColor,
  '--summary-muted': theme.value.textColor3, '--summary-accent': theme.value.primaryColor,
  '--summary-input': theme.value.inputColor, '--summary-text': theme.value.textColor1 }))
</script>

<template>
  <div class="synthesis-confirm" :style="style">
    <div class="scope-summary">
      <strong v-if="plan.total === 1">第 {{ plan.ids[0] }} 句</strong>
      <strong v-else>{{ scopeLabel ?? (plan.all ? '全部' : '勾选') }} {{ plan.total }} 句</strong>
      <span>{{ appending ? '加入当前待配音列表' : plan.total === 1 ? '仅合成本句' : '按视频顺序依次合成' }}</span>
    </div>
    <div v-if="plan.total === 1" class="sentence-review" role="group" :aria-label="`待生成第 ${plan.ids[0]} 句`">
      <span>译文</span><p>{{ sentenceText || '尚无译文' }}</p>
    </div>
    <n-text v-if="note" depth="3" class="source-notice">{{ note }}</n-text>
    <div v-if="appending" class="queue-notice" role="note">不打断正在合成的句子。当前请求完成后，优先处理这些句子，再继续原任务。</div>
    <fieldset class="target-options" :disabled="submitting">
      <legend>生成到哪个配音槽</legend>
      <label class="target-choice" :class="{ selected: plan.target.mode === 'new' }">
        <input type="radio" name="dubbing-target" :checked="plan.target.mode === 'new'" @change="changeMode('new')">
        <span><strong>新配音槽<template v-if="plan.newSlot <= 10000"> {{ plan.newSlot }}</template></strong><span v-if="plan.total === 1">保留本句已有配音和满意标记；按本句现有及待生成槽编号新建</span><span v-else>所选句子生成到共同的新槽，保留已有配音和满意标记；按本次范围内现有及待生成槽编号新建</span></span>
      </label>
      <label class="target-choice" :class="{ selected: plan.target.mode === 'current' }">
        <input type="radio" name="dubbing-target" :checked="plan.target.mode === 'current'" @change="changeMode('current')">
        <span><strong>{{ plan.total === 1 ? '当前配音槽' : '各句当前配音槽' }}</strong><span>用于修正读音、语气等，替换当前选中的槽；{{ plan.total === 1 ? '未选槽时' : '未选槽的句子' }}<template v-if="plan.newSlot <= 10000">生成到新槽 {{ plan.newSlot }}</template><template v-else>需要新增槽，但槽号已达上限</template></span></span>
      </label>
      <label class="target-choice" :class="{ selected: plan.target.mode === 'slot', unavailable: !slots.length }">
        <input type="radio" name="dubbing-target" :checked="plan.target.mode === 'slot'" :disabled="!slots.length" @change="changeMode('slot')">
        <span><strong>指定配音槽</strong><span>{{ plan.total === 1 ? '本句使用所选槽号，已有配音会覆盖，空槽新增' : '所选句子使用同一槽号，已有配音会覆盖，其余句子新增' }}</span></span>
      </label>
      <div v-if="plan.target.mode === 'slot'" class="slot-picker">
        <label for="dubbing-target-slot">目标槽</label>
        <select id="dubbing-target-slot" :value="plan.target.slot ?? ''" @change="emit('update:target', { mode: 'slot', slot: Number(($event.target as HTMLSelectElement).value) })">
          <option v-for="slot in slots" :key="slot.value" :value="slot.value">配音槽 {{ slot.value }} · 本次 {{ slot.count }} 句已有配音</option>
        </select>
      </div>
    </fieldset>
    <n-text v-if="plan.invalidReason" type="error" role="alert">{{ plan.invalidReason }}</n-text>
    <div class="version-summary" aria-label="配音版本变化">
      <div v-if="plan.create"><strong>新增 {{ plan.create }} 句</strong><span>写入{{ plan.target.mode === 'current' ? `新配音槽 ${plan.newSlot}` : plan.destination }}</span></div>
      <div v-if="plan.overwrite"><strong>覆盖 {{ plan.overwrite }} 句</strong><span>替换目标槽中的音频<template v-if="plan.confirmed">，含 {{ plan.confirmed }} 句已满意</template></span></div>
      <div v-if="plan.refill"><strong>补齐 {{ plan.refill }} 句</strong><span>当前槽缺失的音频</span></div>
    </div>
    <p>{{ appending ? '使用当前任务的基础配音参数与已保存的本句设置，本句设置优先。' : '使用本项目已保存的配音参数。' }}<template v-if="plan.customized && !appending">其中 {{ plan.customized }} 句保留句子单独设置，单独设置优先。</template></p>
    <n-text v-if="plan.overwrite" type="warning" class="source-notice">覆盖成功后目标槽的旧音频会被替换，需重新试听确认满意；其他槽保留。</n-text>
    <p class="source-notice">每句合成成功后自动选用目标槽。<template v-if="plan.target.mode === 'new'">新槽保留旧配音，可在列表切回对比。</template><template v-if="plan.original">其中 {{ plan.original }} 句将从原声切换为配音。</template>合成失败的句子保留原来的音频与选择。</p>
    <details v-if="!plan.all && plan.total > 1" class="range-detail">
      <summary>查看句子范围</summary><div>第 {{ plan.range }} 句</div>
    </details>
    <p class="next-step">配音更新后，使用这些配音的成片需重新生成。</p>
    <div v-if="submitting" class="submission-status" role="status" aria-live="polite">
      <n-spin :size="16" /><span>正在核对配音参数与目标槽，接收后即开始排队…</span>
    </div>
    <n-text v-if="submissionError" type="error" class="source-notice" role="alert">{{ submissionError }}</n-text>
  </div>
</template>

<style scoped>
.synthesis-confirm { line-height: 1.7; }
.scope-summary { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 14px; margin: 8px 0 14px; }
.scope-summary strong { font-size: 18px; }
.scope-summary span { color: var(--summary-muted); font-size: 12px; }
.sentence-review { padding:10px 12px; margin:0 0 16px; border:1px solid var(--summary-border); border-radius:8px; background:var(--summary-input); }
.sentence-review > span { color:var(--summary-muted); font-size:11px; }
.sentence-review p { margin:4px 0 0!important; max-height:180px; overflow:auto; color:var(--summary-text); white-space:pre-wrap; overflow-wrap:anywhere; }
.target-options { border: 0; margin: 0 0 16px; padding: 0; min-width: 0; }
.target-options legend { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.target-choice { display: flex; align-items: flex-start; gap: 10px; border: 1px solid var(--summary-border); border-radius: 8px; padding: 10px 12px; cursor: pointer; margin-top: 8px; }
.target-choice.selected { border-color: var(--summary-accent); background: var(--summary-input); }
.target-choice:focus-within { outline: 2px solid var(--summary-accent); outline-offset: 2px; }
.target-choice input { margin: 5px 0 0; accent-color: var(--summary-accent); }
.target-choice > span { display: grid; gap: 2px; min-width: 0; }
.target-choice strong { font-size: 14px; }
.target-choice span span { font-size: 12px; color: var(--summary-muted); }
.target-choice small { margin-left: 8px; color: var(--summary-accent); font-size: 11px; font-weight: normal; }
.target-choice.unavailable { opacity: .5; cursor: not-allowed; }
.target-options:disabled .target-choice { cursor: wait; opacity: .7; }
.slot-picker { display: flex; align-items: center; gap: 12px; margin: 10px 0 0 28px; font-size: 13px; }
.slot-picker select { min-width: 0; flex: 1; color: var(--summary-text); background: var(--summary-input); border: 1px solid var(--summary-border); border-radius: 6px; padding: 6px 8px; }
.version-summary { display: flex; flex-wrap: wrap; gap: 12px; padding: 14px 0; border-top: 1px solid var(--summary-border); border-bottom: 1px solid var(--summary-border); }
.version-summary > div { flex: 1 1 120px; display: grid; gap: 2px; }
.version-summary strong { font-size: 16px; color: var(--summary-accent); }
.version-summary span { font-size: 12px; color: var(--summary-muted); }
.synthesis-confirm p { margin: 12px 0 0; }
.source-notice { display: block; margin-top: 10px; }
.queue-notice { padding: 9px 12px; border-radius: 8px; background: var(--summary-input); color: var(--summary-accent); margin-bottom: 14px; font-size: 13px; }
.range-detail { margin-top: 12px; font-size: 12px; }
.range-detail summary { cursor: pointer; }
.range-detail div { max-height: 80px; overflow: auto; overflow-wrap: anywhere; margin-top: 6px; color: var(--summary-muted); }
.next-step { color: var(--summary-muted); font-size: 12px; }
.submission-status { display: flex; align-items: center; gap: 8px; margin-top: 12px; color: var(--summary-accent); font-size: 12px; }
</style>
