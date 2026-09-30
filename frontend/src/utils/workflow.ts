import { publicationStage } from './publishing.ts'

export const WORKFLOW_ENDS = [
  { label: '仅制作成片', value: 'produce' },
  { label: '准备发布草稿', value: 'draft' },
  { label: '自动投稿', value: 'publish' },
]
export const WORKFLOW_BUSY = ['queued', 'running', 'stopping']
export const WORKFLOW_OPEN = [...WORKFLOW_BUSY, 'monitoring']
export function workflowModeLabel(mode?: string) {
  return WORKFLOW_ENDS.find(item => item.value === mode)?.label || '正在读取'
}
export function workflowRunStage(run: any) {
  const states: Record<string, [string, 'info' | 'warning' | 'success' | 'error']> = {
    queued: ['等待执行', 'info'], running: ['执行中', 'info'], stopping: ['正在停止', 'warning'],
    monitoring: ['等待平台审核', 'info'], paused: ['已暂停', 'warning'], failed: ['需处理', 'error'],
    attention: ['需处理', 'warning'], ready: ['草稿已就绪', 'success'], completed: ['流程已完成', 'success'],
  }
  const [text, tone] = states[run?.status] || ['尚未运行', 'info']
  return { text, tone }
}

// 总览和侧栏共用：执行策略不冒充稿件状态，确认任务不被后来改动的默认终点改写。
export function workflowPublication(summary: any, publication: any, error = '') {
  if (error) return { text: '状态读取失败', tone: 'warning' as const, dot: 'stale', note: error }
  const run = summary?.run
  const mode = run && WORKFLOW_OPEN.includes(run.status) ? run.mode : summary?.mode
  const note = mode === 'publish' ? '已启用自动投稿 · 确认完整流程后自动上传并投稿'
    : mode === 'draft' ? '自动流程准备草稿，投稿前由你确认'
    : mode === 'produce' ? '自动流程仅制作成片，发布可单独操作' : '正在读取执行终点'
  const current = run?.publication || (run && run.mode !== 'produce' && WORKFLOW_BUSY.includes(run.status) ? null : publication)
  if (current?.status === 'uploaded' && run?.mode === 'publish' && WORKFLOW_BUSY.includes(run.status)) return {
    text: '上传完成 · 准备投稿', tone: 'info' as const, dot: 'running', note: '按本次确认继续投稿，无需再次点击',
  }
  if (current?.id && current.status !== 'pending') return { ...publicationStage(current.status), note }
  if (run && run.mode !== 'produce') {
    if (WORKFLOW_BUSY.includes(run.status)) return {
      text: run.status === 'stopping' ? '正在停止' : run.cursor >= run.total ? '准备发布资料' : '等待制作完成',
      tone: 'info' as const, dot: 'running', note: `本次执行到：${workflowModeLabel(run.mode)}`,
    }
    if (['failed', 'paused', 'attention'].includes(run.status)) return {
      text: '流程待继续', tone: 'warning' as const, dot: 'stale', note: '请在上方处理当前流程；尚未进入投稿',
    }
  }
  return { text: '尚未开始', tone: 'default' as const, dot: 'pending', note }
}
