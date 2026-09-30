export type ProgressActivity = { message: string; label: string; time?: string; unavailable?: boolean }

export function stoppingLabel(step: { stop_reason?: string | null }) {
  return step.stop_reason === 'timeout' ? '超时停止中' : '正在停止'
}

/** 只使用当前任务有标识的日志；重跑时绝不显示上一轮的成功、失败或进度。 */
export function latestJobActivity(lines: unknown, jobId: number): ProgressActivity | null {
  if (!Array.isArray(lines) || !Number.isSafeInteger(jobId) || jobId <= 0) return null
  for (const raw of [...lines].reverse()) {
    if (typeof raw !== 'string') continue
    const match = raw.replace(/\[[0-9;]*m/g, '').match(/^\[(\d{2}:\d{2}:\d{2})\]\s*\[任务#(\d+)\]\s*(.+)$/)
    if (!match || Number(match[2]) !== jobId) continue
    let message = match[3].trim()
    // 配置快照、时限和结构化传输数据不是当前工作内容。
    if (/^(🔒|⏱|DUBFORGE_|收尾清理:|🧹)/.test(message)) continue
    message = message.replace(/\u001b\[[0-9;]*m/g, '')
      .replace(/(https?:\/\/)[^\s/]+@/gi, '$1[已隐藏凭据]@')
      .replace(/(https?:\/\/[^\s?#]+)[?#][^\s]+/gi, '$1?[已隐藏参数]')
      .replace(/\bsynthesized\b/g, '配音已生成')
    // 大段缓存路径不占满摘要；完整内容仍在步骤日志中。
    message = message.replace(/\s*→\s*[A-Za-z]:\\[^；\n]+/, '')
    return { message: message.slice(0, 600), label: '最新动态', time: match[1] }
  }
  return null
}

export function stepActivity(step: any, log?: ProgressActivity | null): ProgressActivity | null {
  if (!step || !['running', 'queued'].includes(step.status)) return null
  if (step.cancelling) return { label: '任务状态', message: `${stoppingLabel(step)}，等待当前处理安全收尾` }
  if (step.pause_state === 'paused') return { label: '任务状态', message: '已暂停，等待继续' }
  if (step.pause_state === 'requested' || step.pausing) return { label: '任务状态', message: '正在暂停，等待当前处理完成' }
  if (step.status === 'queued') return { label: '任务状态', message: `等待执行${step.label || '当前步骤'}` }
  if (log) return log
  const task = step.synthesis_task
  return { label: '当前任务', message: step.progress_phase || (task?.total
    ? `正在生成配音 · 已完成 ${task.completed || 0} / ${task.total} 句`
    : `正在执行${step.label || '当前步骤'}，等待新的处理动态`) }
}

/** 使用运行时冻结计划，不把用户后来修改的默认规则说成本次计划。 */
export function workflowComments(run: any, plan: any): { state: string; message: string } | null {
  if (!run || !plan || plan.id !== run.id) return null
  const index = plan.steps?.indexOf('comments') ?? -1
  if (index < 0) return { state: 'none', message: '本次流程未安排自动采集评论；使用已保存方案或版本未启用评论叠加。' }
  const cursor = Number(run.cursor) || 0
  if (cursor > index || run.status === 'completed') return { state: 'done', message: '本次流程的评论已准备完成，可在下方查看。' }
  if (['failed', 'paused', 'attention', 'stopping', 'cancelled'].includes(run.status)) return {
    state: 'stopped', message: cursor === index ? '评论准备尚未完成，流程已暂停或需要处理。' : '流程已暂停或需要处理，尚未执行评论准备。',
  }
  if (cursor === index && run.status === 'running') return { state: 'running', message: '正在自动准备评论：采集、评分并翻译，完成后用于合成。' }
  const before = plan.steps?.[index - 1], label = plan.labels?.[before] || '前序步骤'
  return { state: 'upcoming', message: `${label}完成后自动准备评论：最多采集 ${plan.recipe?.comment_limit ?? '所设数量的'} 条，精选最多 ${plan.recipe?.comment_count ?? '所设数量的'} 条上屏。` }
}

export function publicationActivity(row: any): ProgressActivity | null {
  if (!row) return null
  const title = row.snapshot?.settings?.title || row.draft?.overrides?.title
  const subject = title ? `「${title}」` : '本次稿件'
  const count = row.snapshot?.parts?.length
  const states: Record<string, string> = {
    uploading: `正在上传${subject}${count ? ` · 共 ${count} 个分P` : ''}，上传后处理封面`,
    paused: `上传已暂停 · ${subject}，已完成文件会在继续时复用`,
    uploaded: row.snapshot?.publish_after_upload ? `${subject}文件已上传，正在衔接投稿` : `${subject}文件已上传，等待确认投稿`,
    queued: `正在准备${subject}的上传任务`, submitting: `正在提交${subject}的标题、简介与分P资料`,
    joining: `正在把${subject}加入目标合集`, checking: `正在向B站查询${subject}的状态`,
  }
  return states[row.status] ? { label: '当前任务', message: states[row.status] } : null
}
