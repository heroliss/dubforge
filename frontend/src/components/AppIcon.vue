<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  projects: 'M3 7h7l2 2h9v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Zm0 0V5a1 1 0 0 1 1-1h6l2 3',
  publish: 'M12 16V3m-5 5 5-5 5 5M4 14v6h16v-6',
  settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
  help: 'M4 4h6l2 2 2-2h6v15h-6l-2 2-2-2H4V4Zm8 2v15',
  about: 'M12 16v-4m0-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  overview: 'M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z',
  menu: 'M4 6h16M4 12h16M4 18h16',
  close: 'm6 6 12 12M6 18 18 6',
  check: 'M5 12l4 4L19 6',
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  chevron: 'm9 5 7 7-7 7',
  wave: 'M3 10v4m4-8v12m5-15v18m5-15v12m4-8v4',
  plus: 'M12 5v14M5 12h14',
  search: 'm21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  locate: 'M12 3v3m0 12v3M3 12h3m12 0h3M18 12a6 6 0 1 1-12 0 6 6 0 0 1 12 0Zm-4 0a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
  refresh: 'M20 7v5h-5M4 17v-5h5m10-2a7 7 0 0 0-12-5L4 8m16 8-3 3a7 7 0 0 1-12-5',
  'skip-back': 'M5 4v16M19 5 9 12l10 7V5Z',
  bookmark: 'M6 3h12v18l-6-4-6 4V3Z',
  language: 'M3 5h12M9 3v2M6 6c1 5 4 8 8 10m-2-10c-1 6-4 9-9 12m12-9-4 12m4-12 4 12m-6-4h5',
  'arrow-up': 'M12 20V4m-6 6 6-6 6 6',
  'arrow-down': 'M12 4v16m-6-6 6 6 6-6',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path :d="paths[name] || paths.overview" /></svg>
</template>
