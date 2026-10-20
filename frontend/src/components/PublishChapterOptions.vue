<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { NAlert, NButton, NCheckbox, NInput, NInputNumber } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
import ChapterAdaptationSummary from './ChapterAdaptationSummary.vue'
import LlmConversationButton from './LlmConversationButton.vue'
import { chapterIssues, chapterSourceLabel, preserveChapterSources, preserveChapterAdaptation } from '../utils/chapterRules'

const props = defineProps<{ value?: any; source?: any[]; duration?: number; projectUid: string; disabled?: boolean }>()
const emit = defineEmits<{ change: [value: any]; 'busy-change': [value: boolean] }>()
const busy = ref(false), error = ref('')
const translatingCount = ref(0)
const adaptation = ref<any>(null)
let disposed = false
const options = computed(() => ({ enabled: false, translate: true, permanent: true, instruction: '', items: [], ...props.value }))
const issues = computed(() => chapterIssues(options.value.items, props.duration, adaptation.value?.rules))
function update(values: any) { if (!props.disabled && !busy.value) emit('change', { ...options.value, ...values }) }
function edit(index: number, key: string, value: any) {
  const items = options.value.items.map((row: any, i: number) => i === index ? { ...row, [key]: value } : { ...row })
  update({ items })
}
function load() { if (props.disabled || busy.value) return; adaptation.value = null; update({ items: (props.source || []).map(row => ({ ...row })) }) }
async function translate() {
  if (props.disabled || busy.value || !options.value.items.length) return
  busy.value = true; emit('busy-change', true); error.value = ''
  const original = options.value.items.map((row: any) => ({ ...row }))
  const projectUid = props.projectUid
  translatingCount.value = original.length
  try {
    const result = await publishApi(`/projects/${encodeURIComponent(projectUid)}/chapters/translate`, {
      items: original, ...(props.duration != null ? { duration: props.duration } : {}), translate: options.value.translate !== false,
      instruction: options.value.instruction || '',
    })
    if (disposed || props.disabled || props.projectUid !== projectUid) return
    if (JSON.stringify(props.value?.items || []) !== JSON.stringify(original)) { error.value = '整理期间章节已变化，请重新生成'; return }
    // 整理可合并相邻主题，必须整体接受时间与来源，不能按旧数组下标替换标题。
    const items = Array.isArray(result.items) ? result.items : []
    if (!items.length) throw new Error('模型未返回可用章节，原候选已保留')
    adaptation.value = preserveChapterAdaptation(result.adaptation, original) || null
    emit('change', { ...options.value, items: preserveChapterSources(items, original).map((row: any, index: number) => ({
      ...(row.start_time == null ? original[index] : {}), ...row,
    })) })
  } catch (e: any) { if (!disposed && props.projectUid === projectUid) error.value = e.message || '章节整理失败' }
  finally { busy.value = false; emit('busy-change', false) }
}
onBeforeUnmount(() => { disposed = true })
</script>

<template>
  <section class="chapter-options" aria-label="发布视频章节">
    <div class="chapter-heading"><div class="chapter-title-row"><h3>本稿件视频章节</h3><LlmConversationButton prominent :project-uid="projectUid" operation="chapters" title="查看当前项目章节整理的输入、模型回复和纠错过程" /></div><p>投稿后按平台审核进度自动设置章节，按各分P成片的剪辑调整时间。下方是本稿件的冻结选项；项目默认在「发布规则」中设置。</p></div>
    <n-checkbox class="chapter-enable" :checked="options.enabled" :disabled="disabled || busy" @update:checked="value => update({ enabled: value })">投稿后自动设置视频章节</n-checkbox>
    <template v-if="options.enabled">
      <div class="chapter-tools"><n-checkbox :checked="options.translate" :disabled="disabled || busy" @update:checked="value => update({ translate: value })">整理时将标题翻译为中文</n-checkbox><n-checkbox :checked="options.permanent" :disabled="disabled || busy" @update:checked="value => update({ permanent: value })">App默认展示章节</n-checkbox><n-button size="small" :disabled="disabled || busy || !source?.length" @click="load">载入源视频章节</n-button><n-button size="small" secondary :loading="busy" :disabled="disabled || busy || !options.items.length" @click="translate">{{ options.translate ? 'AI翻译并整理供预览' : 'AI整理章节供预览' }}</n-button></div>
      <p class="chapter-note">自动任务会合并相邻主题并改写标题以符合平台规则；关闭中文翻译时保留原语言。预览整理只修改当前草稿，不会写入B站；源视频资料保持完整。</p>
      <label class="chapter-review-label">章节标题自定义要求（本稿件，可选）<n-input :value="options.instruction" type="textarea" :autosize="{minRows:2,maxRows:5}" :maxlength="2000" show-count :disabled="disabled || busy" placeholder="例如：标题用简短名词短语；保留模型英文名；避免夸张表述。" @update:value="value => update({instruction:value})" /></label>
      <ChapterAdaptationSummary :value="adaptation" />
      <p v-if="issues.length" class="chapter-note chapter-warning">{{ issues[0] }}{{ issues.length > 1 ? `，另有 ${issues.length - 1} 项待调整` : '' }}。自动任务会再次整理和校验。</p>
      <p v-if="busy" class="chapter-note" role="status" aria-live="polite">正在整理 {{ translatingCount }} 条源章节，等待模型按主题合并并校验规则…</p>
      <n-alert v-if="error" type="warning" :show-icon="false">{{ error }}</n-alert>
      <p v-if="!options.items.length" class="chapter-empty">源视频没有章节，暂不会写入B站。可刷新平台资料后载入。</p>
      <ol v-else class="chapter-list"><li v-for="(item,index) in options.items" :key="index"><span>{{ index + 1 }}</span><n-input-number :value="item.start_time" :min="0" size="small" :disabled="disabled || busy" @update:value="value => edit(index,'start_time',value)"/><n-input :value="item.title" maxlength="16" show-count size="small" :disabled="disabled || busy" placeholder="章节标题（最多16个字符）" @update:value="value => edit(index,'title',value)"/><n-button size="tiny" quaternary :disabled="disabled || busy" @click="update({ items: options.items.filter((_:any,i:number) => i!==index) })">移除</n-button><small v-if="chapterSourceLabel(item.source_indices)" class="chapter-source">{{ chapterSourceLabel(item.source_indices) }}</small></li></ol>
      <p class="chapter-note">B站会按账号实时规则校验数量、时长和标题。已剪去的完整章节会移除，保留章节会前移；原片与剪辑成片分别处理。外部变速或添加片头仍须人工核对。保存资料不会写入B站。</p>
    </template>
  </section>
</template>

<style scoped>
.chapter-options{margin-top:18px;padding-top:18px;border-top:1px solid var(--df-border)}.chapter-heading,.chapter-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.chapter-heading h3{margin:0;font-size:13px}.chapter-heading p,.chapter-note,.chapter-empty{font-size:11px;line-height:1.7;color:var(--df-muted);margin:6px 0}.chapter-tools{justify-content:flex-start;margin:12px 0}.chapter-list{list-style:none;padding:0;margin:12px 0;max-height:340px;overflow:auto}.chapter-list li{display:grid;grid-template-columns:22px 100px minmax(140px,1fr) auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--df-border);font-size:11px;color:var(--df-muted)}@media(max-width:680px){.chapter-list li{grid-template-columns:22px 90px 1fr}.chapter-list li>:nth-child(3){grid-column:2/-1}.chapter-list li>:last-child{grid-column:3}}
.chapter-warning{color:var(--df-warning,#efbd70)}.chapter-source{grid-column:2/-1;color:var(--df-primary,#93b7ed);font-size:10px}
.chapter-review-label{display:grid;gap:8px;font-size:11px;color:var(--df-muted);margin:12px 0}
.chapter-heading{display:block}.chapter-title-row{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}.chapter-title-row :deep(.n-button){flex-shrink:0}.chapter-enable{margin-top:10px}
</style>
