<script setup lang="ts">
import { computed } from 'vue'
import { useThemeVars } from 'naive-ui'
import { dubbingDurationReference, type DurationReferenceLine } from '../utils/dubbingDurationReference'

const props = defineProps<{ line?: DurationReferenceLine; configuredFactor?: unknown }>()
const theme = useThemeVars()
const colors = computed(() => ({
  '--reference-text': theme.value.textColor1,
  '--reference-muted': theme.value.textColor3,
  '--reference-border': theme.value.borderColor,
  '--reference-surface': theme.value.actionColor,
  '--reference-accent': theme.value.primaryColor,
  '--reference-warning': theme.value.warningColor,
}))
const reference = computed(() => dubbingDurationReference(props.line, props.configuredFactor))
const adjusted = computed(() => reference.value.used !== null && reference.value.requested !== null
  && reference.value.used !== reference.value.requested)
const display = (value: number | null) => value === null ? reference.value.generated ? '未记录' : '尚未生成' : String(value)
</script>

<template>
  <section class="duration-reference" :style="colors" aria-label="配音时长因子生成记录">
    <div class="reference-heading">
      <span class="reference-title">时长因子 <span class="reference-kind">生成记录</span></span><span class="reference-version">{{ reference.take === null ? '无配音版本' : `配音 ${reference.take}` }} · 只读</span>
    </div>
    <dl class="reference-values" :class="{ adjusted }">
      <div><dt>合成起始因子</dt><dd><output aria-label="合成起始因子">{{ display(reference.requested) }}</output></dd></div>
      <div class="used-factor"><dt>本版本实际因子</dt><dd><output aria-label="本版本实际因子">{{ display(reference.used) }}</output></dd></div>
    </dl>
    <p v-if="reference.configured !== null" class="reference-note">当前设置 {{ reference.configured }} · 重新合成使用当前设置</p>
    <p v-if="reference.retries" class="reference-note" :class="{ warning: reference.retryFailed }">自动缩时重试 {{ reference.retries }} 次<template v-if="reference.retryFailed"> · 重试失败，已保留有效音频</template></p>
    <p v-if="reference.stale" class="reference-note warning">此配音版本的文字或时间已更新，需重新生成。</p>
  </section>
</template>

<style scoped>
.duration-reference { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 12px; border: 1px solid var(--reference-border); border-radius: 8px; color: var(--reference-text); }
.reference-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 4px 10px; font-size: 11px; color: var(--reference-muted); }
.reference-title { color: var(--reference-text); font-size: 12px; font-weight: 500; }
.reference-kind { margin-left: 6px; color: var(--reference-muted); font-size: 11px; font-weight: 400; }
.reference-version { white-space: nowrap; }
.reference-values { position: relative; display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 28px; margin: 10px 0 0; padding: 10px 12px; border-radius: 6px; background: var(--reference-surface); }
.reference-values::after { content: '→'; position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); color: var(--reference-muted); font-size: 16px; }
.reference-values > div { min-width: 0; }
dt { color: var(--reference-muted); font-size: 11px; line-height: 1.5; }
dd { margin: 4px 0 0; font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.4; overflow-wrap: anywhere; }
.adjusted .used-factor dd { color: var(--reference-accent); }
.reference-note { margin: 8px 0 0; color: var(--reference-muted); font-size: 11px; line-height: 1.6; }
.reference-note.warning { color: var(--reference-warning); }
</style>
