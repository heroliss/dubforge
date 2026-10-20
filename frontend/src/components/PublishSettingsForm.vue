<script setup lang="ts">
import { computed } from 'vue'
import { NInput, NSelect, NDynamicTags, NSwitch, NDatePicker, NButton, useMessage } from 'naive-ui'
import { categoryOptions, collectionAvailability, retainSelectedOption, withSourcePublishedDate } from '../utils/publishing'
import PublishCollectionCreate from './PublishCollectionCreate.vue'
import PublishPlatformAssist from './PublishPlatformAssist.vue'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'
import ParameterResetButton from './ParameterResetButton.vue'
import type { ConfigurationSource } from '../utils/configurationSource'
const props = withDefaults(defineProps<{ values: any; overrides: any; sourcePublishedDate?: string; sources?: Record<string, ConfigurationSource>; metadata?: any; metadataLoading?: boolean; metadataError?: string; metadataReady?: boolean; metadataAccount?: string; metadataAccountName?: string; coverId?: string; contextKey?: string; hasAiDubbing?: boolean; disabled?: boolean; template?: boolean; fieldKeys?: string[]; restoreLabels?: Record<string, string>; resetLabel?: string; resetHint?: string; inheritance?: boolean; showMore?: boolean; showPlatformAssist?: boolean; grouped?: boolean }>(), { metadata: () => ({}), sources: () => ({}), restoreLabels: () => ({}), resetLabel: '恢复继承值', inheritance: true, showMore: true, showPlatformAssist: true, grouped: false })
const emit = defineEmits<{ change: [key: string, value: any]; inherit: [key: string]; collectionCreated: [value: any]; refreshMetadata: [] }>()
const message = useMessage()
const datedDescription = computed(() => withSourcePublishedDate(props.values.description || '', props.sourcePublishedDate || ''))
const needsSourceDate = computed(() => datedDescription.value !== (props.values.description || ''))
function insertPublishedDate() {
  if (props.disabled || !needsSourceDate.value) return
  if (datedDescription.value.length > 2000) { message.warning('补入日期后简介将超过2000字，请先精简正文；现有内容未修改。'); return }
  emit('change', 'description', datedDescription.value)
}
const categories = computed(() => categoryOptions(props.metadata?.human_categories || []))
const collectionState = computed(() => collectionAvailability(props.values, props.metadata,
  {account:props.metadataAccount, ready:props.metadataReady, loading:props.metadataLoading, error:props.metadataError}))
const seasons = computed(() => retainSelectedOption((props.metadata?.seasons || []).map((s: any) => ({ label: s.title, value: s.id })),
  props.values.season_id, `合集 · ID ${props.values.season_id} · ${collectionState.value.status === 'missing' ? '已不可用' : '待核对'}`))
const sections = computed(() => retainSelectedOption(((props.metadata?.seasons || []).find((s: any) => s.id === props.values.season_id)?.sections || []).map((s: any) => ({ label: s.title, value: s.id })),
  props.values.section_id, `小节 · ID ${props.values.section_id} · ${collectionState.value.status === 'section_missing' ? '已不可用' : '待核对'}`))
function emptyOptions(kind: string) {
  if (!props.metadataAccount) return '请先选择账号'
  if (props.metadataLoading) return '正在读取账号选项…'
  if (props.metadataError) return '读取失败，请刷新账号选项'
  if (kind === 'season' && props.metadata?.collection_error) return '合集读取失败，请刷新重试'
  if (!props.metadataReady) return '账号选项尚未加载'
  return kind === 'season' ? '此账号暂无可用合集，可先在B站创建' : kind === 'section' ? '所选合集暂无可用小节' : '平台未返回可用选项，请刷新重试'
}
const labels: Record<string, string> = { title: '标题', description: '简介', tags: '标签', human_type2: '分区', copyright: '稿件类型', source: '原视频来源', declaration_id: '创作声明', visibility: '可见范围', season_id: '合集', section_id: '合集小节', publish_at: '定时发布', dynamic: '粉丝动态',
  chapters_enabled: '投稿后自动设置视频章节', chapters_translate: '自动翻译章节标题为中文', chapters_permanent: 'B站App默认展示章节', chapters_instruction: '章节标题自定义要求（可选）' }
const chapterHelp: Record<string, string> = {
  chapters_enabled: '使用源视频章节，按各分P成片的剪辑调整时间；源视频无章节时跳过。保存设置不会立即修改B站。',
  chapters_translate: '开启时将标题翻译为中文，关闭时保留原语言。两种模式都会按B站规则合并相邻主题、精简标题并校验数量和时长；不会改动视频字幕或配音。',
  chapters_permanent: '设置章节时同时指定B站App默认展开章节，实际展示由B站客户端决定。',
}
function changeChapter(key: string, value: boolean) {
  if (props.disabled || key !== 'chapters_enabled' && props.values.chapters_enabled === false) return
  emit('change', key, value)
}
const fields = computed(() => Object.keys(labels).filter(key => (!props.fieldKeys || props.fieldKeys.includes(key))
  && (!props.template || !['title', 'description', 'source', 'dynamic', 'publish_at'].includes(key))))
const fieldGroups = computed(() => {
  if (!props.grouped) return [{ title: '', description: '', keys: fields.value }]
  return [
    { title: '稿件文案', description: '标题、简介和标签由所有分P共用；分P名称在视频清单中单独填写。', keys: ['title', 'description', 'tags', 'dynamic'] },
    { title: '投稿属性', description: '按实际素材权利核对稿件类型、原视频来源与创作声明。', keys: ['human_type2', 'copyright', 'source', 'declaration_id'] },
    { title: '发布安排', description: '核对可见范围、提交时间及目标合集；同一稿件的所有分P共用安排。', keys: ['visibility', 'publish_at', 'season_id', 'section_id'] },
    { title: '视频章节', description: '章节在投稿取得回执后独立处理；失败可单独重试，不会重新上传或投稿。', keys: ['chapters_enabled', 'chapters_translate', 'chapters_permanent', 'chapters_instruction'] },
  ].map(group => ({ ...group, keys: group.keys.filter(key => fields.value.includes(key)) }))
    .filter(group => group.keys.length)
})
const switches = { watermark: '添加B站水印', close_comments: '关闭评论', close_danmaku: '关闭弹幕', selected_comments: '开启精选评论' }
function resetText(key: string) { return props.restoreLabels[key] || props.resetLabel }
function reset(key: string) {
  if (props.disabled || !(key in props.overrides)) return
  emit('inherit', key)
}
function select(key: string, value: any) {
  emit('change', key, value)
  if (key === 'season_id') {
    const available = props.metadata?.seasons?.find((s: any) => s.id === value)?.sections || []
    // 唯一小节无需额外选择；多小节时不替用户猜测。
    emit('change', 'section_id', available.length === 1 ? available[0].id : null)
  }
}
</script>
<template>
  <div class="publish-settings-fields" :class="{ grouped }">
    <section v-for="group in fieldGroups" :key="group.title || 'all'" class="publish-field-group" :aria-label="group.title || undefined">
      <header v-if="grouped" class="field-group-heading"><h3>{{ group.title }}</h3><p>{{ group.description }}</p></header>
      <div class="publish-fields">
    <div v-for="key in group.keys" :key="key" class="publish-field" role="group" :aria-label="labels[key]" :class="{ wide: ['title', 'description', 'tags', 'dynamic', 'source', 'chapters_instruction'].includes(key) }">
      <span class="field-heading"><span>{{ labels[key] }}</span><span class="origin"><n-button v-if="key === 'description' && needsSourceDate && !disabled" text size="tiny" :title="`从平台资料补入 ${sourcePublishedDate}，仅修改当前简介，不会自动保存或投稿`" @click="insertPublishedDate">补入原视频日期</n-button><ConfigurationSourceBadge :source="sources[key]" :inherited="inheritance && !(key in overrides)"/><ParameterResetButton :label="labels[key]" :action="resetText(key)" :help="resetHint" :disabled="disabled || !(key in overrides)" @reset="reset(key)"/></span></span>
      <template v-if="key === 'chapters_instruction'"><n-input :value="values[key] || ''" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :maxlength="2000" show-count :disabled="disabled || values.chapters_enabled === false" :input-props="{ 'aria-label': labels[key] }" placeholder="例如：标题用简短名词短语；保留模型英文名；避免夸张表述。" @update:value="value => { if (!disabled && values.chapters_enabled !== false) emit('change',key,value) }" /><small class="chapter-help">可补充内容审查、格式、表述与术语等要求；留空仍按准确、客观原则整理。B站数量、时长与标题长度等硬规则固定校验，不能由自定义要求覆盖。</small></template>
      <n-input v-else-if="['title', 'description', 'source', 'dynamic'].includes(key)" :value="values[key]" :disabled="disabled" :input-props="{ 'aria-label': labels[key] }" :type="['description', 'dynamic'].includes(key) ? 'textarea' : 'text'" :autosize="{ minRows: 3, maxRows: 8 }" :maxlength="key === 'title' ? 80 : key === 'description' ? 2000 : key === 'source' ? 200 : 233" show-count @update:value="emit('change', key, $event)" />
      <n-dynamic-tags v-else-if="key === 'tags'" :value="values.tags" :disabled="disabled" :max="10" @update:value="emit('change', key, $event)" />
      <n-select v-else-if="key === 'human_type2'" :value="values.human_type2 || null" :options="categories" :disabled="disabled" :loading="metadataLoading" filterable placeholder="选择分区" @update:value="select(key, $event || 0)"><template #empty>{{ emptyOptions('category') }}</template></n-select>
      <n-select v-else-if="key === 'copyright'" :value="values.copyright" :options="[{ label: '转载 · 保留原作者与来源', value: 2 }, { label: '原创 · 需拥有相应权利', value: 1 }, { label: '其他创作声明 · 仍需素材授权', value: 3 }]" :disabled="disabled" @update:value="select(key, $event)" />
      <template v-else-if="key === 'declaration_id'">
        <n-select :value="values.declaration_id" :options="metadata?.declarations || []" :disabled="disabled" :loading="metadataLoading" placeholder="选择创作声明" @update:value="select(key, $event)"><template #empty>{{ emptyOptions('declaration') }}</template></n-select>
        <small v-if="hasAiDubbing" class="declaration-hint">配音版本 · 建议核对AI声明 <a href="https://www.bilibili.com/opus/1106496554576904197" target="_blank" rel="noopener noreferrer" title="如使用AI合成配音，建议选择对应AI声明。同一稿件的所有分P共用声明；由你核对选择，软件不强制改选，平台仍可能要求补标。声明不代表获得原视频授权。查看B站官方说明。" aria-label="查看B站AI创作声明说明">ⓘ</a></small>
        <small v-if="metadata?.declaration_source?.startsWith('adapter')" class="origin" title="平台未返回声明枚举，当前使用已核对的上传组件映射；如与官网不一致，请在官网处理。">选项来自组件适配表 ⓘ</small>
        <small v-if="metadataReady && !metadataLoading && !metadata?.declarations?.length" class="origin">平台未返回声明选项，暂不能投稿；请核对官方页面后再适配。</small>
      </template>
      <n-select v-else-if="key === 'visibility'" :value="values.visibility" :options="[{ label: '公开可见', value: 'public' }, { label: '仅自己可见（仍会投稿）', value: 'self' }]" :disabled="disabled" @update:value="select(key, $event)" />
      <template v-else-if="key === 'season_id'">
        <div class="season-picker"><n-select :value="values.season_id" :options="seasons" :disabled="disabled" :loading="metadataLoading" clearable filterable placeholder="不加入合集" @update:value="select(key, $event)"><template #empty>{{ emptyOptions('season') }}</template></n-select><PublishCollectionCreate :account-id="metadataAccount || ''" :account-name="metadataAccountName" :cover-id="coverId" :context-key="contextKey || ''" :season-id="values.season_id" :seasons="metadata?.seasons" :disabled="disabled" @created="emit('collectionCreated', $event)" @refresh="emit('refreshMetadata')" /></div>
        <small v-if="collectionState.message" role="status" class="collection-status" :class="{ warning: ['missing', 'section_missing'].includes(collectionState.status) }" :title="collectionState.status === 'valid' ? '合集按 ID 关联，B站改名不影响绑定，本地模板名称保持独立。' : ''">{{ collectionState.message }}</small>
      </template>
      <n-select v-else-if="key === 'section_id'" :value="values.section_id" :options="sections" :disabled="disabled || !values.season_id" :loading="metadataLoading" placeholder="选择目标小节" @update:value="select(key, $event)"><template #empty>{{ emptyOptions('section') }}</template></n-select>
      <n-date-picker v-else-if="key === 'publish_at'" :value="values.publish_at ? values.publish_at * 1000 : null" type="datetime" clearable :disabled="disabled" placeholder="留空则立即提交审核" @update:value="emit('change', key, $event ? Math.floor($event / 1000) : null)" />
      <template v-else-if="key in chapterHelp"><n-switch :aria-label="labels[key]" :value="values[key] !== false" :disabled="disabled || key !== 'chapters_enabled' && values.chapters_enabled === false" @update:value="value => changeChapter(key,value)" /><small class="chapter-help">{{ chapterHelp[key] }}</small></template>
    </div>
      </div>
    </section>
  </div>
  <PublishPlatformAssist v-if="!template && showPlatformAssist" :account-id="metadataAccount || ''" :values="values" :disabled="disabled" @change="(key,value) => emit('change',key,value)" />
  <details v-if="showMore" class="publish-more"><summary>更多设置 · 水印与互动</summary><div class="switch-grid">
    <div v-for="(label, key) in switches" :key="key" class="switch-row" role="group" :aria-label="label"><span>{{ label }}<small class="switch-source"><ConfigurationSourceBadge :source="sources[key]" :inherited="inheritance && !(key in overrides)"/></small><small v-if="key === 'watermark' && values.copyright === 2">转载稿件不提交此选项</small></span><n-switch :aria-label="label" :value="key === 'watermark' && values.copyright === 2 ? false : values[key]" :disabled="disabled || (key === 'watermark' && values.copyright === 2)" @update:value="emit('change', key, $event)" /><ParameterResetButton :label="label" :action="resetText(key)" :help="resetHint" :disabled="disabled || !(key in overrides)" @reset="reset(key)"/></div>
  </div></details>
</template>
<style scoped>
.season-picker { display:flex; align-items:center; gap:8px; min-width:0; }.season-picker :deep(.n-select) { min-width:0; flex:1; }
.collection-status { color:var(--df-muted); font-size:11px; line-height:1.6; }.collection-status.warning { color:var(--df-warning, #e8b86d); }
.publish-settings-fields,.publish-field-group { min-width:0; }
.grouped .publish-field-group + .publish-field-group { margin-top:26px; padding-top:22px; border-top:1px solid var(--df-border); }
.field-group-heading { margin-bottom:16px; }.field-group-heading h3 { margin:0 0 7px; color:var(--df-text); font-size:14px; font-weight:600; }
.field-group-heading p { margin:0; color:var(--df-muted); font-size:12px; line-height:1.75; overflow-wrap:anywhere; }
.publish-fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }
.publish-field { display:flex; flex-direction:column; gap:7px; min-width:0; }.wide { grid-column:1/-1; }
.field-heading { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px 10px; font-size:13px; }
.origin { display:flex; align-items:center; flex-wrap:wrap; gap:8px; color:var(--df-muted); font-size:10px; }.origin :deep(button) { font-size:10px; }
.switch-source { margin-top:5px; }
.declaration-hint { color:var(--df-muted); font-size:11px; }.declaration-hint a { color:var(--df-accent, #70a8ff); text-decoration:none; }
.chapter-help { color:var(--df-muted); font-size:11px; line-height:1.7; }
.publish-more { margin-top:24px; padding-top:16px; border-top:1px solid var(--df-border); } summary { cursor:pointer; color:var(--df-muted); font-size:12px; }
.switch-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; padding-top:18px; }.switch-row { display:flex; align-items:center; gap:10px; font-size:12px; }.switch-row>span { flex:1; }.switch-grid small { display:block; color:var(--df-muted); }
@media(max-width:700px) { .publish-fields,.switch-grid { grid-template-columns:1fr; } }
</style>
