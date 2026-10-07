<script setup lang="ts">
import { computed } from 'vue'
import { NTag } from 'naive-ui'
import { candidateReview, type CandidateReviewSource } from '../utils/contentReview'
const props = defineProps<{ file?: CandidateReviewSource | null }>()
const review = computed(() => candidateReview(props.file))
</script>
<template>
  <span v-if="review" class="candidate-review" role="status">
    <n-tag size="small" :bordered="false" :type="review.type">{{ review.label }}</n-tag>
    <span v-if="review.reason" class="review-reason">{{ review.reason }}</span>
  </span>
</template>
<style scoped>
.candidate-review{display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-width:0;font-size:11px;line-height:1.65}.review-reason{color:var(--df-muted);overflow-wrap:anywhere;flex:1 1 200px}
</style>
