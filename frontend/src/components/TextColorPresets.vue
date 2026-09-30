<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NPopover } from 'naive-ui'
import { TEXT_COLOR_GROUPS, TEXT_COLOR_PRESETS, colorPairContrast, matchingTextColorPreset, normalizeTextColor } from '../utils/textColorPresets'

const props = defineProps<{ color?: unknown; outlineColor?: unknown; outline?: number; disabled?: boolean; kind: string;
  sourceEnabled?: boolean; sourceColor?: unknown; sourceOutlineColor?: unknown; sourceOutline?: number }>()
const emit = defineEmits<{ select: [id: string, source: boolean]; 'enable-source': [] }>()
const open = ref(false)
const source = ref(false)
watch(() => props.sourceEnabled, value => { if (!value) source.value = false })
const currentColor = computed(() => source.value ? props.sourceColor : props.color)
const currentOutlineColor = computed(() => source.value ? props.sourceOutlineColor : props.outlineColor)
const currentOutline = computed(() => source.value ? props.sourceOutline : props.outline)
const group = ref('all')
const visiblePresets = computed(() => TEXT_COLOR_PRESETS.filter(preset => group.value === 'all' || preset.group === group.value))
const selected = computed(() => matchingTextColorPreset(currentColor.value, currentOutlineColor.value))
function sampleStyle(color: unknown, outlineColor: unknown) {
  return { '--sample-color': normalizeTextColor(color) || '#F7F3E8', '--sample-outline': normalizeTextColor(outlineColor) || '#202631' }
}
function choose(id: string) {
  if (props.disabled || source.value && !props.sourceEnabled || !TEXT_COLOR_PRESETS.some(preset => preset.id === id)) return
  emit('select', id, source.value); open.value = false
}
function selectTarget(original: boolean) {
  if (props.disabled) return
  // 只开启本页草稿的独立样式，不覆盖原文备用值，也不自动保存。
  if (original && !props.sourceEnabled) emit('enable-source')
  source.value = original
  open.value = false
}
</script>

<template>
  <div class="text-color-presets" :aria-label="`${kind}配色预设`">
    <div v-if="sourceEnabled !== undefined" class="palette-target" role="group" :aria-label="`${kind}配色对象`">
      <button type="button" :aria-pressed="!source" :disabled="disabled" @click="selectTarget(false)">译文{{ sourceEnabled ? '' : ' · 通用' }}</button>
      <button type="button" :aria-pressed="source" :disabled="disabled"
        :title="sourceEnabled ? '选择原文配色，不影响译文' : '开启原文独立样式；字体、字号和配色可分别调整，保存后生效'"
        @click="selectTarget(true)">原文 · {{ sourceEnabled ? '独立配色' : '单独设置' }}</button>
    </div>
    <n-popover v-model:show="open" trigger="click" placement="bottom-start" :disabled="disabled" :show-arrow="false"
      style="width:460px;max-width:calc(100vw - 32px);box-sizing:border-box">
      <template #trigger>
        <n-button secondary :disabled="disabled" class="palette-trigger" :aria-label="`${kind}配色预设：${selected?.name || '自定义'}`"
          title="只替换文字与描边颜色；不改其他设置，保存后用于成片">
          <span class="trigger-sample" :style="sampleStyle(currentColor, currentOutlineColor)" aria-hidden="true">字</span>
          <span>配色预设 · {{ selected?.name || '自定义' }}</span><span class="chevron" aria-hidden="true">⌄</span>
        </n-button>
      </template>
      <div class="palette-picker" :aria-label="`${kind}配色列表`">
        <div class="palette-heading"><strong>{{ source ? '原文' : sourceEnabled ? '译文' : '通用' }}配色</strong><small>只改文字与描边色 · 待保存</small></div>
        <div class="palette-groups" role="group" aria-label="按配色风格筛选">
          <button type="button" :aria-pressed="group === 'all'" :disabled="disabled" @click="group = 'all'">全部 {{ TEXT_COLOR_PRESETS.length }}</button>
          <button v-for="category in TEXT_COLOR_GROUPS" :key="category.id" type="button" :aria-pressed="group === category.id"
            :disabled="disabled" @click="group = category.id">{{ category.name }}</button>
        </div>
        <div class="palette-grid">
          <button v-for="preset in visiblePresets" :key="preset.id" type="button" class="palette-option"
            :class="{ selected: selected?.id === preset.id }" :disabled="disabled" :aria-pressed="selected?.id === preset.id"
            :aria-label="`${preset.name}：${preset.scene}`"
            :title="`${preset.rationale}\n文字 ${preset.color} / 描边 ${preset.outline_color}\n色对对比 ${colorPairContrast(preset.color, preset.outline_color)?.toFixed(1)}:1；以视频预览为准`"
            @click="choose(preset.id)">
            <span class="palette-sample" :style="sampleStyle(preset.color, preset.outline_color)" aria-hidden="true">美好时刻 Aa</span>
            <span class="palette-name">{{ preset.name }}<span v-if="selected?.id === preset.id" aria-hidden="true">✓</span></span>
            <span class="palette-scene">{{ preset.scene }}</span>
          </button>
        </div>
        <details class="palette-basis"><summary>配色依据与使用提示</summary>
          <p>先区分色相、冷暖与场景，再筛选文字／描边的亮度对比 ≥ 7:1；不依赖同亮度撞色来辨认文字。场景是设计建议，不是审美定律。</p>
          <p>色卡是固定粗细示意，不代表成片对比达标。视频背景、抗锯齿和描边粗细都会影响阅读；小字或复杂画面请结合底色并查看实际预览。选择后仍可手动调色。</p>
          <a href="https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html" target="_blank" rel="noopener noreferrer">亮度对比依据</a> ·
          <a href="https://www.w3.org/TR/css-color-4/#ok-lab" target="_blank" rel="noopener noreferrer">OKLab / OKLCH</a>
        </details>
      </div>
    </n-popover>
    <small v-if="currentOutline === 0" class="outline-warning">描边已关闭；调大下方粗细后才会显示描边色。</small>
  </div>
</template>

<style scoped>
.text-color-presets { min-width:0; }
.palette-target { display:flex; gap:4px; margin-bottom:6px; }
.palette-target button { flex:1; border:0; border-radius:5px; padding:5px; font:inherit; font-size:11px; color:var(--df-muted); background:transparent; cursor:pointer; }
.palette-target button[aria-pressed="true"] { background:var(--df-hover,rgba(128,128,128,.1)); color:var(--df-accent); }
.palette-target button:disabled { opacity:.55; cursor:default; }
.palette-target button:focus-visible { outline:2px solid var(--df-accent); outline-offset:2px; }
.palette-trigger { width:100%; }
.palette-trigger :deep(.n-button__content) { width:100%; gap:8px; justify-content:flex-start; }
.chevron { margin-left:auto; opacity:.65; }
.trigger-sample { font-size:18px; font-weight:700; color:var(--sample-color); -webkit-text-stroke:2px var(--sample-outline); paint-order:stroke fill; }
.palette-picker { max-height:min(650px, 72vh); overflow:auto; overscroll-behavior:contain; }
.palette-heading { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; }
.palette-heading small,.palette-scene,.palette-basis { color:var(--df-muted); font-size:11px; }
.palette-groups { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }
.palette-groups button { border:1px solid var(--df-border); background:transparent; color:inherit; border-radius:12px; padding:3px 9px; font:inherit; font-size:11px; cursor:pointer; }
.palette-groups button[aria-pressed="true"] { border-color:var(--df-accent); color:var(--df-accent); }
.palette-groups button:focus-visible { outline:2px solid var(--df-accent); outline-offset:2px; }
.palette-groups button:disabled { cursor:not-allowed; opacity:.5; }
.palette-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.palette-option { min-width:0; display:flex; flex-direction:column; padding:0 0 9px; gap:5px; border:1px solid var(--df-border); border-radius:7px; background:var(--df-panel); color:inherit; text-align:left; cursor:pointer; overflow:hidden; }
.palette-option:hover,.palette-option.selected { border-color:var(--df-accent); }
.palette-option:focus-visible { outline:2px solid var(--df-accent); outline-offset:2px; }
.palette-option:disabled { opacity:.5; cursor:not-allowed; }
.palette-sample { display:flex; align-items:center; justify-content:center; align-self:stretch; min-height:54px; margin-bottom:2px; background:linear-gradient(105deg,#dddcd8 0 50%,#465365 50% 100%); color:var(--sample-color); -webkit-text-stroke:2px var(--sample-outline); paint-order:stroke fill; font:700 24px/1.3 'Microsoft YaHei',sans-serif; }
.palette-name { display:flex; justify-content:space-between; width:100%; box-sizing:border-box; padding:0 10px; font-size:12px; font-weight:600; }
.palette-scene { padding:0 10px; }
.palette-basis { margin-top:12px; line-height:1.7; } summary { cursor:pointer; } .palette-basis p { margin:7px 0; }
.palette-basis a { color:var(--df-accent); }
.outline-warning { display:block; margin-top:6px; font-size:11px; color:#e8ad77; }
@media (max-width:400px) { .palette-heading { align-items:flex-start; flex-direction:column; } .palette-sample { font-size:20px; } }
</style>
