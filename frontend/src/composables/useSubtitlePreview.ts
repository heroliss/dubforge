import { computed, onScopeDispose, ref, watch } from 'vue'
import { createLatestRefresh } from '../utils/preview'
import { srtToVtt, subtitlesToVtt, type SubtitleLine, type SubtitleMode } from '../utils/subtitlePreview'

export function useSubtitlePreview(options: {
  lines: () => readonly SubtitleLine[]
  files: () => readonly { name: string; url: string }[]
  exportsReady: () => boolean
  sourceFirst: () => boolean
}) {
  const mode = ref('source'), url = ref<string | null>(null), error = ref('')
  const hasTranslation = computed(() => options.lines().some(line => !!line.text?.trim()))
  const exported = computed(() => mode.value.startsWith('export:'))
  let initialized = false
  const currentMode = () => hasTranslation.value ? 'bilingual' : 'source'
  watch([() => options.lines().length, hasTranslation, options.exportsReady, options.files], () => {
    if (!initialized && options.lines().length) { mode.value = currentMode(); initialized = true }
    if (!hasTranslation.value && ['target', 'bilingual'].includes(mode.value)) mode.value = 'source'
    if (exported.value && (!options.exportsReady() || !options.files().some(file => `export:${file.name}` === mode.value)))
      mode.value = currentMode()
  }, { immediate: true })
  function clear() {
    if (url.value) URL.revokeObjectURL(url.value)
    url.value = null
  }
  function show(text: string) {
    clear()
    url.value = URL.createObjectURL(new Blob([text], { type: 'text/vtt' }))
  }
  const fileUrl = computed(() => exported.value && options.exportsReady()
    ? options.files().find(file => `export:${file.name}` === mode.value)?.url : undefined)
  const loader = createLatestRefresh(async signal => {
    const response = await fetch(fileUrl.value!, { signal, cache: 'no-store' })
    if (!response.ok) throw new Error('读取字幕失败')
    return srtToVtt(await response.text())
  }, show, () => { clear(); error.value = '字幕文件读取失败，请重新选择预览类型' })
  const text = computed(() => exported.value || mode.value === 'off' ? ''
    : subtitlesToVtt(options.lines(), mode.value as SubtitleMode, options.sourceFirst()))
  // 只在字幕内容或文件版本变化时替换轨道，合成进度轮询不会重载字幕或视频。
  watch([mode, text, fileUrl], () => {
    loader.cancel(); clear(); error.value = ''
    if (fileUrl.value) void loader.refresh()
    else if (text.value) show(text.value)
  }, { immediate: true })
  onScopeDispose(() => { loader.cancel(); clear() })
  return { mode, url, error, hasTranslation, exported }
}
