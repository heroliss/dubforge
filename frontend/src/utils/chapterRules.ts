// 发布候选与源视频资料分开；此处只做即时提示，提交仍由后端按实时规则校验。
export const DEFAULT_CHAPTER_RULES = {
  max_chapters: 10, max_boundaries: 9, title_max_length: 16,
  first_start: 0, min_first_duration: 1, min_duration: 5,
}

export function chapterRules(value: any = {}) {
  const result = { ...DEFAULT_CHAPTER_RULES }
  for (const key of Object.keys(result) as (keyof typeof result)[]) {
    const number = Number(value?.[key])
    if (value?.[key] != null && Number.isFinite(number) && number >= 0) result[key] = number
  }
  return result
}

export function chapterRuleText(value?: any) {
  const rules = chapterRules(value)
  return `每个分P最多 ${rules.max_chapters} 个章节（${rules.max_boundaries} 个分界点）；从视频开头开始，首个分界点至少 ${rules.min_first_duration} 秒，其余每章至少 ${rules.min_duration} 秒；标题最多 ${rules.title_max_length} 个字符。`
}

export function chapterIssues(items: any[], duration?: number | null, value?: any) {
  const rules = chapterRules(value), issues: string[] = []
  if (items.length > rules.max_chapters) issues.push(`共 ${items.length} 个章节，超过 ${rules.max_chapters} 个上限；请先用 AI 整理或手动合并`)
  items.forEach((row, index) => {
    const title = String(row?.title || '').trim(), rawStart = Number(row?.start_time)
    // B站网页按整数秒校验；标题input的maxlength按UTF-16字符位计算。
    const start = Math.round(rawStart)
    const next = items[index + 1]
    const end = Math.round(Number(row?.end_time ?? next?.start_time ?? duration))
    const minimum = index === 0 ? rules.min_first_duration : rules.min_duration
    if (!title) issues.push(`第 ${index + 1} 章未填写标题`)
    else if (title.length > rules.title_max_length) issues.push(`第 ${index + 1} 章标题超过 ${rules.title_max_length} 个字符`)
    if (!Number.isFinite(rawStart) || rawStart < 0) issues.push(`第 ${index + 1} 章开始时间无效`)
    else if (index === 0 && Math.abs(start - rules.first_start) > .001) issues.push(`首章应从 ${rules.first_start} 秒开始`)
    else if (!Number.isFinite(end) || end - start < minimum - .001) issues.push(`第 ${index + 1} 章需至少保留 ${minimum} 秒`)
    else if (next && end > Math.round(Number(next.start_time))) issues.push(`第 ${index + 1} 章与下一章时间重叠`)
    else if (duration != null && Number.isFinite(Number(duration)) && end > Math.round(Number(duration))) issues.push(`第 ${index + 1} 章超出当前分P时长`)
  })
  return issues
}

export function chapterSourceLabel(value: unknown, sourceName = '源章节') {
  if (!Array.isArray(value)) return ''
  const indices = [...new Set(value.filter(index => Number.isInteger(index) && index >= 0))].sort((a, b) => a - b)
  if (!indices.length) return ''
  const ranges: string[] = []
  for (let i = 0; i < indices.length; i++) {
    const start = indices[i]
    while (i + 1 < indices.length && indices[i + 1] === indices[i] + 1) i++
    ranges.push(start === indices[i] ? String(start + 1) : `${start + 1}–${indices[i] + 1}`)
  }
  return `${indices.length > 1 ? '合并自' : '来自'}${sourceName} ${ranges.join('、')}`
}

// 整理接口返回本次输入的索引；再次整理时展开已有来源，避免把第10个候选误称原视频第10章。
export function preserveChapterSources(items: any[], original: any[]) {
  return items.map(row => ({ ...row, ...(Array.isArray(row?.source_indices) ? {
    source_indices: [...new Set(row.source_indices.flatMap((index: number) =>
      Array.isArray(original[index]?.source_indices) ? original[index].source_indices : [index]))].sort((a: any, b: any) => a - b),
  } : {}) }))
}

export function preserveChapterAdaptation(value: any, original: any[]) {
  return value?.review?.findings ? { ...value, review: { ...value.review,
    findings: preserveChapterSources(value.review.findings, original) } } : value
}
