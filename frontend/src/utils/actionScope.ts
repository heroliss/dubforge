// 同一套名称用于方案选择、试片与执行确认；空选择绝不补入默认方案。
export const OUTPUT_LABELS: Record<string, string> = {
  dub: '配音 · 不烧录字幕', dub_hardsub: '配音 · 烧录字幕', src_hardsub: '原声 · 烧录字幕',
  src_comments: '原声 · 仅叠加评论',
  dub_comments: '配音 · 不烧录字幕 · 叠加评论',
  dub_hardsub_comments: '配音 · 烧录字幕 · 叠加评论',
  src_hardsub_comments: '原声 · 烧录字幕 · 叠加评论',
}
export type AssemblyChoice = { dubbing: boolean; burn_subtitles: boolean; include_comments: boolean }
export function outputChoice(key: string): AssemblyChoice {
  return { dubbing: key.startsWith('dub'), burn_subtitles: key.includes('hardsub'), include_comments: key.endsWith('_comments') }
}
export function outputKey(choice: AssemblyChoice): string {
  return (choice.dubbing ? 'dub' : 'src') + (choice.burn_subtitles ? '_hardsub' : '') + (choice.include_comments ? '_comments' : '')
}
export function choiceLabel(choice: AssemblyChoice): string { return OUTPUT_LABELS[outputKey(choice)] || '原声 · 不烧录字幕 · 不叠加评论（原片）' }
export function selectedOutputVariants(selected: string[]): string[] {
  return [...new Set(selected)].filter(key => Object.hasOwn(OUTPUT_LABELS, key))
}
export function defaultOutputLabel(burn: boolean): string {
  return OUTPUT_LABELS[burn ? 'dub_hardsub' : 'dub']
}
export function runPlanDescription(plan: any): string {
  const order = (plan.steps ?? []).map((key: string) => plan.labels?.[key] || key).join(' → ')
  return `执行顺序：${order}。已有最新结果会跳过。使用已保存的参数，不使用未保存的草稿。`
    + (plan.effects?.length ? `\n${plan.effects.join('\n')}` : '')
}
export function hiddenSelectionCount(selected: Array<string | number>, visible: Array<string | number>): number {
  const shown = new Set(visible)
  return selected.filter(id => !shown.has(id)).length
}
