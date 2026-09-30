import { runPlanDescription } from './actionScope.ts'
import { refreshInBackground } from './backgroundRefresh.ts'
type Dependency = { state: string; step: string; label: string }

export function dependencyActionLabel(items: Dependency[]) {
  if (items.length && items.every(i => i.state === 'stale')) return '更新依赖并运行'
  if (items.length && items.every(i => ['missing', 'deleted'].includes(i.state))) return '补齐依赖并运行'
  return '准备依赖并运行'
}

export function dependencyTitle(items: Dependency[]) {
  if (items.length && items.every(i => i.state === 'stale')) return '依赖已过期，请先更新'
  if (items.length && items.every(i => ['missing', 'deleted'].includes(i.state))) return '缺少依赖产物'
  return '上游依赖未就绪'
}

// 先展示实际执行列表；缺失与过期由后端分别判定。
export async function runPlannedSteps(uid: string, steps: string[], includeDependencies: boolean,
  message: any, dialog: any, beforeRun: (steps: string[]) => Promise<void>, afterRun: () => Promise<void>) {
  try {
    const body = { steps, include_dependencies: includeDependencies }
    const response = await fetch(`/api/projects/${uid}/run_plan`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const plan = await response.json()
    if (!response.ok) { message.error(plan.message || '无法生成运行计划'); return }
    const blockers = plan.blockers ?? plan.missing ?? []
    if (blockers.length) {
      message.warning(blockers.map((i: any) => `${i.name}：${i.reason}（${i.label}）`).join('；'))
      return
    }
    let sending = false, accepted = false
    const execute = async () => {
      if (sending || accepted) return false
      sending = true
      try {
        await beforeRun(plan.steps)
        const result = await fetch(`/api/projects/${uid}/run_chain`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // 按已展示的计划执行；参数变化导致依赖失效时必须重新查看计划。
          body: JSON.stringify({ steps: plan.steps }),
        })
        const data = await result.json()
        if (!result.ok) { message.error(data.message || '启动失败'); return false }
        accepted = true
        message.success(data.started ? '已启动运行计划' : '所选步骤已是最新')
      } catch { message.error('运行请求失败'); return false }
      finally {
        sending = false
        refreshInBackground(afterRun, () => message.warning('操作结果已返回，但状态刷新失败，请手动刷新'))
      }
    }
    const requirements: Dependency[] = plan.requirements ?? []
    const labelsFor = (states: string[]) => [...new Set(requirements
      .filter(i => states.includes(i.state)).map(i => i.label))].join('、')
    const missing = labelsFor(['missing', 'deleted'])
    const stale = labelsFor(['stale'])
    dialog.info({ title: includeDependencies ? dependencyActionLabel(requirements) : `确认运行 ${plan.steps.length} 个步骤`,
      content: runPlanDescription(plan)
        + (missing ? `需生成产物：${missing}。` : '')
        + (stale ? `需更新产物：${stale}。` : ''),
      positiveText: '按此计划运行', negativeText: '取消', onPositiveClick: execute })
  } catch { message.error('读取运行计划失败') }
}
