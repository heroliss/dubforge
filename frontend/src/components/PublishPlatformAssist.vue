<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NTag, useDialog } from 'naive-ui'
import { publishApi } from '../utils/publishApi'
const props = defineProps<{accountId:string; values:any; disabled?:boolean}>()
const emit = defineEmits<{change:[key:string,value:any]}>()
const dialog = useDialog(), busy = ref(''), error = ref(''), tags = ref<any>(null), topics = ref<any>(null), keyword = ref(''), topicPage = ref(1)
let generation = 0, disposed = false
const signature = computed(() => JSON.stringify([props.accountId,props.values.title,props.values.description,props.values.tags,props.values.tid]))
watch(() => JSON.stringify([props.accountId,props.values.title,props.values.description,props.values.tid]), () => { generation++;tags.value=null;topics.value=null;busy.value='';error.value='' })
watch(() => JSON.stringify(props.values.tags), () => { generation++;if(tags.value)tags.value={...tags.value,checks:[]};busy.value='' })
async function load(action:string, page = 1) {
  if (busy.value || props.disabled || !props.accountId) return
  const ticket = ++generation, account = props.accountId, source = signature.value
  busy.value=action;error.value=''
  const body = action === 'tags' ? {title:props.values.title || '',description:props.values.description || '',tid:props.values.tid || 21,tags:props.values.tags || []}
    : {title:props.values.title || '',tid:props.values.tid || 21,keyword:keyword.value,page}
  try {
    const data = await publishApi(`/accounts/${account}/studio/read/${action}`,body)
    if (disposed || ticket !== generation || signature.value !== source) return
    if (action === 'tags') tags.value=data
    else { topics.value=data;topicPage.value=page }
  } catch(e:any) { if (!disposed && ticket === generation) error.value=e.message }
  finally { if (!disposed && ticket === generation) busy.value='' }
}
function add(tag:string) {
  if (props.disabled || (props.values.tags || []).includes(tag) || (props.values.tags || []).length >= 10) return
  emit('change','tags',[...(props.values.tags || []),tag])
}
function topic(item:any) {
  if (props.disabled) return
  const account = props.accountId
  dialog.warning({title:'确认选用话题',content:`${item.name}\n${item.conditions || item.description || '请在B站核对活动规则和参与条件。'}\n这里只写入草稿，正式投稿时才提交话题。`,
    positiveText:'选用此话题',negativeText:'取消',onPositiveClick:() => {
      if (disposed || props.disabled || props.accountId !== account) return
      emit('change','topic_id',item.id);emit('change','topic_name',item.name);emit('change','mission_id',item.mission_id || null)
    }})
}
function clearTopic() { emit('change','topic_id',null);emit('change','topic_name','');emit('change','mission_id',null) }
onBeforeUnmount(() => {disposed=true;generation++})
</script>
<template>
  <details class="platform-assist"><summary>平台建议 · 标签与话题</summary>
    <div class="toolbar"><n-button size="small" :disabled="disabled || !accountId || !!busy" :loading="busy === 'tags'" @click="load('tags')">推荐标签 / 校验现有标签</n-button><small>不会覆盖现有标签</small></div>
    <n-alert v-if="error" type="warning">{{ error }}</n-alert>
    <div v-if="tags" class="suggestions"><n-button v-for="tag in tags.suggestions" :key="tag" size="tiny" secondary :disabled="disabled || values.tags?.includes(tag) || values.tags?.length >= 10" @click="add(tag)">＋ {{ tag }}</n-button><span v-if="!tags.suggestions.length">暂无推荐标签</span></div>
    <div v-for="check in tags?.checks || []" :key="check.tag" class="check"><n-tag size="small" :type="check.valid === true ? 'success' : check.valid === false ? 'warning' : 'default'">{{ check.tag }}</n-tag><small>{{ check.valid === true ? '可用' : check.message || '当前不可用' }}</small></div>
    <div class="toolbar"><n-input v-model:value="keyword" size="small" placeholder="搜索话题；留空按标题推荐" :disabled="!!busy" maxlength="100" @keyup.enter="load('topics')" /><n-button size="small" :loading="busy === 'topics'" :disabled="disabled || !accountId || !!busy" @click="load('topics')">查找话题</n-button></div>
    <div v-if="values.topic_id" class="toolbar"><n-tag>{{ values.topic_name }}</n-tag><n-button text size="tiny" :disabled="disabled" @click="clearTopic">清除话题</n-button></div>
    <div v-for="item in topics?.items || []" :key="item.id" class="topic"><div><strong>{{ item.name }}</strong><p>{{ item.description }}</p><small>{{ item.activity }}</small><details v-if="item.conditions"><summary>活动条件</summary><p>{{ item.conditions }}</p></details></div><n-button size="small" :disabled="disabled || !item.id" @click="topic(item)">选用</n-button></div>
    <small v-if="topics && !topics.items.length">暂无匹配话题</small>
    <div v-if="topics" class="toolbar"><n-button size="tiny" :disabled="!!busy || topicPage <= 1" @click="load('topics',topicPage-1)">上一页</n-button><span>{{ topicPage }}</span><n-button size="tiny" :disabled="!!busy || !topics.more" @click="load('topics',topicPage+1)">下一页</n-button></div>
  </details>
</template>
<style scoped>
.platform-assist {margin-top:18px;border-top:1px solid var(--df-border);padding-top:14px;font-size:12px;}summary{cursor:pointer;color:var(--df-muted)}.toolbar,.suggestions,.check{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}.toolbar :deep(.n-input){max-width:400px}.topic{display:flex;justify-content:space-between;gap:18px;border-bottom:1px solid var(--df-border);padding:12px 0}.topic>div{min-width:0}.topic p{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0;line-height:1.7}small{color:var(--df-muted)}
</style>
