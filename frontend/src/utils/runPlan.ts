import { runPlanDescription } from './actionScope.ts'
import { refreshInBackground } from './backgroundRefresh.ts'
import { h, ref } from 'vue'
import { defaultDubbingRunPolicy, dubbingRunPolicy, dubbingPolicyError, type DubbingRunPolicy } from './dubbingRunPolicy.ts'
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
  message: any, dialog: any, beforeRun: (steps: string[]) => Promise<void>, afterRun: () => Promise<void>,
  isArchived: () => boolean = () => false) {
  const archiveBlocked = () => {
    if (!isArchived()) return false
    message.warning('项目已归档，请先恢复项目后再运行。'); return true
  }
  if (archiveBlocked()) return
  try {
    const body = { steps, include_dependencies: includeDependencies,
      ...(steps.includes('tts') ? {dubbing:defaultDubbingRunPolicy()}: {}) }
    const response = await fetch(`/api/projects/${uid}/run_plan`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const initialPlan = await response.json()
    if (!response.ok) { message.error(initialPlan.message || '无法生成运行计划'); return }
    if (archiveBlocked()) return
    const blockers = initialPlan.blockers ?? initialPlan.missing ?? []
    if (blockers.length) {
      message.warning(blockers.map((i: any) => `${i.name}：${i.reason}（${i.label}）`).join('；'))
      return
    }
    const plan = ref<any>(initialPlan), policy = ref(dubbingRunPolicy(initialPlan.dubbing?.policy))
    const checking = ref(false), planError = ref('')
    const hasDubbing = initialPlan.dubbing || initialPlan.steps?.includes('tts')
    const DubbingRunOptions=hasDubbing ? (await import('../components/DubbingRunOptions.vue')).default : null
    const sending=ref(false)
    let accepted = false, closed = false, revision = 0, panel:any
    const content = (value:any) => {
      const requirements: Dependency[] = value?.requirements ?? []
      const labelsFor = (states: string[]) => [...new Set(requirements
        .filter(i => states.includes(i.state)).map(i => i.label))].join('、')
      const missing = labelsFor(['missing', 'deleted']), stale = labelsFor(['stale'])
      return value ? runPlanDescription(value) + (missing ? `需生成产物：${missing}。` : '')
        + (stale ? `需更新产物：${stale}。` : '') : checking.value ? '正在重新检查配音范围与依赖…' : '请重新检查有效的配音方案。'
    }
    const updatePolicy = async (value:DubbingRunPolicy) => {
      if(sending.value || accepted || closed)return
      policy.value=value;plan.value=null;planError.value=dubbingPolicyError(value)
      const ticket=++revision
      if(panel)panel.positiveButtonProps={disabled:true}
      if(planError.value){checking.value=false;return}
      checking.value=true
      try{
        const response=await fetch(`/api/projects/${uid}/run_plan`,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({...body,dubbing:value})})
        const next=await response.json()
        if(closed || ticket!==revision)return
        if(!response.ok)throw new Error(next.message || '无法重新检查配音方案')
        const blocked=next.blockers ?? next.missing ?? []
        if(blocked.length){planError.value=blocked.map((i:any)=>`${i.label || i.step || '依赖'}：${i.reason || i.message || i}`).join('；');return}
        plan.value=next;policy.value=dubbingRunPolicy(next.dubbing?.policy || value)
        if(panel)panel.positiveButtonProps={disabled:false}
      }catch(failure:any){if(!closed && ticket===revision)planError.value=failure.message || '配音方案检查失败，请重试'}
      finally{if(ticket===revision)checking.value=false}
    }
    const execute = async () => {
      if (sending.value || accepted) return false
      if(checking.value || !plan.value || planError.value)return false
      if (archiveBlocked()) return false
      const snapshot=JSON.parse(JSON.stringify(plan.value))
      const frozenPolicy=dubbingRunPolicy(snapshot.dubbing?.policy || policy.value)
      sending.value = true
      try {
        await beforeRun(snapshot.steps)
        if (archiveBlocked()) return false
        const result = await fetch(`/api/projects/${uid}/run_chain`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // 按已展示的计划执行；参数变化导致依赖失效时必须重新查看计划。
          body: JSON.stringify({ steps: snapshot.steps,
            ...(hasDubbing ? {dubbing:frozenPolicy}: {}),
            ...(snapshot.fingerprint ? {plan_fingerprint:snapshot.fingerprint}: {}) }),
        })
        const data = await result.json()
        if (!result.ok) { message.error(data.message || '启动失败'); return false }
        accepted = true
        message.success(data.started ? '已启动运行计划' : '所选步骤已是最新')
      } catch { message.error('运行请求失败'); return false }
      finally {
        sending.value = false
        refreshInBackground(afterRun, () => message.warning('操作结果已返回，但状态刷新失败，请手动刷新'))
      }
    }
    const close=()=>{closed=true;revision++}
    panel=dialog.info({ title: includeDependencies ? dependencyActionLabel(initialPlan.requirements ?? []) : `确认运行 ${initialPlan.steps.length} 个步骤`,
      content: DubbingRunOptions ? ()=>h('div',{style:'display:grid;gap:14px'},[
        h(DubbingRunOptions,{uid,modelValue:policy.value,summary:plan.value?.dubbing,disabled:sending.value,
          'onUpdate:modelValue':updatePolicy}),
        h('p',{style:'white-space:pre-line;font-size:12px;line-height:1.8;margin:0'},content(plan.value)),
        ...(planError.value ? [h('p',{role:'alert',style:'color:var(--df-warning)'},planError.value)]:[])]) : content(initialPlan),
      positiveText: '按此计划运行', negativeText: '取消', onPositiveClick: execute,
      onNegativeClick:close,onClose:close })
  } catch { message.error('读取运行计划失败') }
}
