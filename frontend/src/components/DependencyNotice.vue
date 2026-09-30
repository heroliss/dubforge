<script setup lang="ts">
import { computed, ref } from 'vue'
import { NAlert, NButton, NText } from 'naive-ui'
import { useRouter } from 'vue-router'
import { dependencyTitle } from '../utils/runPlan'
const props = defineProps<{ uid: string; items: any[] }>()
const router = useRouter()
const expanded = ref(false)
const visibleItems = computed(() => expanded.value ? props.items : props.items.slice(0, 6))
</script>

<template>
  <n-alert v-if="items.length" type="warning" :title="dependencyTitle(items)" class="dependency-notice">
    <div v-for="item in visibleItems" :key="item.step + item.name" class="dependency">
      <div>
        <n-text strong>{{ item.name }}（{{ item.desc }}）</n-text>
        <n-text depth="3" class="reason">{{ item.reason }} · 来自「{{ item.label }}」</n-text>
      </div>
      <n-button size="tiny" quaternary type="primary"
                @click="router.push(`/projects/${uid}/steps/${item.step}`)">前往 →</n-button>
    </div>
    <n-button v-if="items.length > 6" text size="small" class="dependency-expand" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起明细' : `展开其余 ${items.length - 6} 项（共 ${items.length} 项）` }}</n-button>
    <slot />
  </n-alert>
</template>

<style scoped>
.dependency-notice { margin-top: 14px; }
.dependency { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 4px 0 8px; }
.dependency > div { min-width: 0; overflow-wrap: anywhere; }
.reason { display: block; font-size: 12px; }
.dependency-expand { margin: 4px 0 10px; }
</style>
