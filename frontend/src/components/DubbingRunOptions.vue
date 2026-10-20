<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { RouterLink } from 'vue-router'
import { dubbingRunPolicy, dubbingPolicyLabel, dubbingPolicyError, type DubbingRunPolicy, type DubbingRunSummary } from '../utils/dubbingRunPolicy'

const props = defineProps<{ modelValue?: DubbingRunPolicy; summary?: DubbingRunSummary | null; disabled?: boolean; readonly?: boolean; uid?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: DubbingRunPolicy] }>()
const expanded = ref(props.modelValue?.mode !== undefined && props.modelValue.mode !== 'fill')
const controlId = useId()
const policy = computed(() => dubbingRunPolicy(props.modelValue || props.summary?.policy))
// 当前执行策略只认预检结果；旧字段仅在历史只读摘要中解释，不用于新请求。
const repair = computed(() => props.summary?.repair_settings ?? (props.readonly && typeof props.summary?.policy?.repair_timing === 'boolean'
  ? { enabled: props.summary.policy.repair_timing, configured: props.summary.policy.repair_timing, legacy: true } : null))
const repairDescription = computed(() => !repair.value ? props.uid ? '按项目已保存的配音设置' : '按各项目已保存的配音设置'
  : repair.value.enabled ? '自动修复' : '仅排布并提示')
const validation = computed(() => dubbingPolicyError(policy.value))
const modes = [
  { value: 'fill', label: '继续已有配音', description: '补齐缺失或内容失效的句子，时间冲突按下方策略处理。参数变化不会触发全量覆盖。' },
  { value: 'changed', label: '按当前内容与设置更新', description: '补齐缺失配音，并更新朗读内容、时间或设置变化的句子，其他有效音频继续使用。' },
  { value: 'all', label: '重新生成全部配音', description: '按本次参数来源重新生成配音范围内的全部句子。' },
] as const
const targetLabel = computed(() => policy.value.target === 'new' ? '共同新配音槽'
  : props.summary?.target_label || (policy.value.target === 'current' ? '各句当前槽' : `配音槽 ${policy.value.slot ?? '未选择'}`))
function update(patch: Partial<DubbingRunPolicy>) {
  if (props.disabled || props.readonly) return
  emit('update:modelValue', dubbingRunPolicy({ ...policy.value, ...patch }))
}
</script>

<template>
  <section class="dubbing-run-options" data-testid="dubbing-run-options" aria-label="本次配音方案">
    <div class="dubbing-plan-heading"><strong>本次配音</strong><span>{{ summary?.needed === false ? '所选版本使用原声，无需配音' : dubbingPolicyLabel(policy) }}</span>
      <button v-if="!readonly && summary?.needed !== false" type="button" :disabled="disabled" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起选项' : '调整配音方案' }}</button>
    </div>
    <template v-if="summary?.needed !== false">
      <p v-if="summary?.deferred" class="muted">原文与译文就绪后计算实际句数；本次配音策略已确定，进入配音时固定目标槽，恢复继续使用该槽。</p>
      <div v-else-if="summary && summary.generate !== undefined" class="plan-counts" aria-label="配音处理范围">
        <span>生成 <strong>{{ summary.generate }}</strong> 句</span><span v-if="summary.repair">自动修复 <strong>{{ summary.repair }}</strong> 句</span><span>沿用 <strong>{{ summary.reuse || 0 }}</strong> 句</span>
        <span v-if="summary.customized">{{ policy.line_settings === 'project' ? '本次忽略' : '保留' }}逐句精调 {{ summary.customized }} 句</span>
        <span v-if="summary.original">原声 {{ summary.original }} 句</span><span v-if="summary.silent">静音 {{ summary.silent }} 句</span>
      </div>
      <p v-if="policy.mode === 'fill'" class="muted">沿用已选槽，保留逐句精调及原声、静音选择。新参数用于待补句子。</p>
      <p v-else class="muted">生成到<strong>{{ targetLabel }}</strong>，成功后选用目标槽；失败保留原音频与选择。{{ policy.line_settings === 'project' ? '本次统一使用项目参数，已保存的逐句设置仍保留。' : '使用已保存参数，逐句精调优先。' }}</p>
      <p v-if="policy.mode !== 'fill' && policy.include_manual_sources" class="settings-notice">本次也包含已手动选择原声或静音的句子；需要合成的句子成功后改用目标配音槽。</p>
      <p v-if="policy.mode !== 'fill' && policy.target === 'new'" class="muted">进入配音时统一分配并保存新槽，恢复沿用原槽<template v-if="summary?.new_slot">；当前候选为配音槽 {{ summary.new_slot }}</template>。</p>
      <p v-if="summary?.resumed" class="muted">继续同一轮配音，保留已完成 {{ summary.completed || 0 }} 句；剩余句子使用当前已保存的参数，仍写入本轮目标槽。</p>
      <p v-else-if="summary?.resume_reset" class="settings-notice">配音策略或上游内容已变化，将开始新一轮；上一轮已完成的音频仍保留。</p>
      <p v-if="policy.mode === 'fill' && summary?.settings_changed" class="settings-notice">另有 {{ summary.settings_changed }} 句可用配音尚未应用当前设置；需要一并更新时选择「按当前内容与设置更新」。文字或时间已失效的句子仍会补齐。</p>
      <p v-if="policy.mode !== 'fill' && summary?.overwrite" class="overwrite-notice">将覆盖目标槽中的 {{ summary.overwrite }} 句音频<template v-if="summary.confirmed">（含 {{ summary.confirmed }} 句已满意）</template>；被覆盖槽需重新确认满意。</p>
      <div class="repair-summary" aria-label="已保存的配音时间冲突处理设置"><span class="muted">时间冲突处理</span><span :title="repair?.enabled ? '自动缩时适用于 IndexTTS；旧声音需要可复现生成快照。先排布，仍放不下才尝试缩时。' : undefined">{{ repairDescription }}</span><small v-if="repair" class="muted">{{ repair.legacy ? '历史运行策略' : '使用已保存配音设置' }}</small><RouterLink v-if="uid && !disabled" :to="`/projects/${encodeURIComponent(uid)}/steps/tts#dubbing-parameters`">调整配音设置 →</RouterLink></div>
      <p v-if="repair?.enabled && summary?.repair_ids?.length" class="muted">待修复：第 {{ summary.repair_ids.slice(0, 12).join('、') }} 句{{ summary.repair_ids.length > 12 ? '等' : '' }}。</p>
      <details v-if="summary?.repair_blocked?.length" class="repair-details settings-notice"><summary>另 {{ summary.repair_blocked.length }} 句需在运行后重新检查</summary><p v-for="item in summary.repair_blocked" :key="item.id">第 {{ item.id }} 句：{{ item.reason }}</p></details>
      <div v-if="expanded && !readonly" class="dubbing-controls">
        <fieldset :disabled="disabled"><legend>如何处理已有配音</legend><div class="mode-options">
          <label v-for="mode in modes" :key="mode.value" :class="{ selected: policy.mode === mode.value }"><input type="radio" :name="`${controlId}-dubbing-run-mode`" :value="mode.value" :checked="policy.mode === mode.value" @change="update({ mode: mode.value })"><span><strong>{{ mode.label }}<small v-if="mode.value === 'fill'">默认</small></strong><small>{{ mode.description }}</small></span></label>
        </div></fieldset>
        <template v-if="policy.mode !== 'fill'">
          <fieldset :disabled="disabled"><legend>生成到哪个配音槽</legend><div class="target-options">
            <label><input type="radio" :name="`${controlId}-dubbing-run-target`" :checked="policy.target === 'new'" @change="update({ target: 'new', slot: null })">新配音槽 · 保留旧音频</label>
            <label><input type="radio" :name="`${controlId}-dubbing-run-target`" :checked="policy.target === 'current'" @change="update({ target: 'current', slot: null })">各句当前槽 · 覆盖选中版本</label>
            <label><input type="radio" :name="`${controlId}-dubbing-run-target`" :checked="policy.target === 'slot'" @change="update({ target: 'slot', slot: policy.slot || summary?.slots?.[0] || 1 })">指定配音槽</label>
          </div><label v-if="policy.target === 'slot'" class="slot-input">目标槽<input type="number" min="1" max="10000" :value="policy.slot ?? ''" aria-label="本次目标配音槽" @input="update({ slot: ($event.target as HTMLInputElement).value ? Number(($event.target as HTMLInputElement).value) : null })"><span v-if="summary?.slots?.length" class="muted">已有槽：{{ summary.slots.join('、') }}</span></label></fieldset>
          <fieldset :disabled="disabled"><legend>参数与声源</legend>
            <label class="check-option"><input type="checkbox" :checked="policy.line_settings === 'project'" @change="update({ line_settings: ($event.target as HTMLInputElement).checked ? 'project' : 'preserve' })"><span>本次统一使用项目参数，忽略逐句精调<small>仅影响本次合成，不删除保存的句子设置。</small></span></label>
            <label class="check-option"><input type="checkbox" :checked="policy.include_manual_sources" @change="update({ include_manual_sources: ($event.target as HTMLInputElement).checked })"><span>也为已手动选择原声或静音的句子生成配音<small>成功后这些句子会改用目标配音槽。</small></span></label>
          </fieldset>
        </template>
        <p class="muted">调整方案后需要重新检查。只影响本次运行，不修改全局或项目默认参数。</p>
      </div>
      <p v-if="validation" role="alert" class="overwrite-notice">{{ validation }}</p>
    </template>
  </section>
</template>

<style scoped>
.dubbing-run-options{display:grid;gap:9px;padding:14px 16px;border:1px solid var(--df-border);border-radius:10px;background:var(--df-panel);font-size:12px;line-height:1.7}.dubbing-plan-heading{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px}.dubbing-plan-heading>strong{font-size:13px}.dubbing-plan-heading>button{margin-left:auto;border:0;padding:2px 0;background:transparent;color:var(--df-accent);font:inherit;cursor:pointer}.dubbing-plan-heading>button:disabled{opacity:.5;cursor:default}.dubbing-run-options p{margin:0}.muted{color:var(--df-muted)}.plan-counts{display:flex;flex-wrap:wrap;gap:4px 14px}.settings-notice,.overwrite-notice{color:var(--df-warning,#dfb66f)}.dubbing-controls{display:grid;gap:16px;border-top:1px solid var(--df-border);padding-top:14px}.dubbing-controls fieldset{border:0;margin:0;padding:0;min-width:0}.dubbing-controls legend{font-weight:600;margin-bottom:8px}.mode-options{display:grid;gap:8px}.mode-options label{display:flex;align-items:flex-start;gap:9px;padding:10px 12px;border:1px solid var(--df-border);border-radius:8px;cursor:pointer}.mode-options label.selected{border-color:var(--df-accent);background:var(--df-bg)}.mode-options label>span{display:grid;gap:3px}.mode-options small,.check-option small{display:block;color:var(--df-muted);font-size:11px}.mode-options strong>small{display:inline;margin-left:8px;color:var(--df-accent);font-weight:400}.dubbing-controls input[type=radio],.dubbing-controls input[type=checkbox]{accent-color:var(--df-accent);margin:5px 0 0;flex-shrink:0}.target-options{display:flex;gap:8px 18px;flex-wrap:wrap}.target-options label{display:flex;gap:7px;align-items:flex-start;cursor:pointer}.slot-input{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px}.slot-input input{width:90px;box-sizing:border-box;background:var(--df-bg);color:var(--df-text);border:1px solid var(--df-border);border-radius:6px;padding:5px 8px;font:inherit}.check-option{display:flex;gap:9px;align-items:flex-start;cursor:pointer;margin-top:8px}.dubbing-controls fieldset:disabled{opacity:.6}.dubbing-controls :focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}
.repair-summary{display:flex;align-items:center;flex-wrap:wrap;gap:3px 9px}.repair-summary small{font-size:11px}.repair-summary a{color:var(--df-accent);text-decoration:none;margin-left:auto}.repair-summary a:hover{text-decoration:underline}.repair-details summary{cursor:pointer}.repair-details[open]>p{margin-top:5px}.repair-summary a:focus-visible,.repair-details summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:3px}
</style>
