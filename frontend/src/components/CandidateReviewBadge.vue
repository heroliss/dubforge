<script setup lang="ts">
import { computed } from 'vue'
import { NTag } from 'naive-ui'
import { candidateReview, reviewRevisionDetails, type CandidateReviewSource } from '../utils/contentReview'
const props = defineProps<{ file?: CandidateReviewSource | null }>()
const review = computed(() => candidateReview(props.file))
const revisionDetails = computed(() => reviewRevisionDetails(props.file))
</script>
<template>
  <div v-if="review" class="candidate-review" role="status">
    <n-tag size="small" :bordered="false" :type="review.type">{{ review.label }}</n-tag>
    <span v-if="review.reason" class="review-reason">{{ review.reason }}</span>
    <details v-if="review.type === 'warning' && revisionDetails.length" class="review-revision-details"><summary>查看修订失效原因 · {{ revisionDetails.length }} 项</summary><div v-for="(reason, index) in revisionDetails" :key="`${reason.code}:${index}`"><p>{{ reason.message }}</p><dl v-if="reason.changes.length"><div v-for="(change, changeIndex) in reason.changes" :key="changeIndex"><dt>{{ change.label }}</dt><dd>生成时：{{ change.before }}<br />当前：{{ change.after }}</dd></div></dl></div></details>
  </div>
</template>
<style scoped>
.candidate-review{display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-width:0;font-size:11px;line-height:1.65}.review-reason{color:var(--df-muted);overflow-wrap:anywhere;flex:1 1 200px}
.review-revision-details{flex-basis:100%;color:var(--df-muted)}.review-revision-details summary{cursor:pointer}.review-revision-details p{margin:7px 0 3px}.review-revision-details dl{margin:4px 0 8px}.review-revision-details dl>div{display:grid;grid-template-columns:minmax(80px,auto) 1fr;gap:6px 12px;margin:5px 0}.review-revision-details dd{margin:0;overflow-wrap:anywhere}
</style>
