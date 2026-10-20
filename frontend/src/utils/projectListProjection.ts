const ACTIVE = new Set(['running', 'queued', 'stopping', 'pausing'])
const job = (step: any) => Number(step?.job_id || step?.execution?.job_id || 0)

function settingsSummary(summary: any, previous: any, retain: boolean) {
  if (!retain || !previous || !summary) return summary
  const next = { ...previous, ...summary }
  const sameTarget = previous.known === true && summary.known === true
    && ['account_id', 'season_id', 'section_id'].every(key => String(previous[key] ?? '') === String(summary[key] ?? ''))
  if (sameTarget) for (const key of ['season_title', 'section_title']) {
    if (!summary[key] && previous[key]) next[key] = previous[key]
  }
  return next
}

export function projectFreshnessKnown(project: any): boolean {
  return project?.freshness_known !== false
}

export function currentGenerationSteps(previous: any[], next: any[]): { steps: any[]; changed: boolean } {
  const before = new Map(previous.map(step => [step.key, step]))
  let changed = false
  const steps = next.map(step => {
    const old = before.get(step.key)
    if (!old || !job(step) || job(old) === job(step)) return step
    changed = true
    return { ...step, fresh: null, freshness_known: false, freshness_label: '', freshness_reason: '',
      settings_changed: false, upstream_changed: false, artifact_missing: false }
  })
  return { steps, changed }
}

/** 摘要只证明登记与活动事实；同修订保留上次完整核验，换代不借用旧产物结论。 */
export function mergeProjectSummary(summary: any, previous: any): any {
  const same = previous && summary.summary_revision && previous.summary_revision === summary.summary_revision
  const oldSteps = new Map<string, any>((previous?.steps || []).map((step: any) => [step.key, step]))
  const stableSteps = (summary.steps || []).every((step: any) => {
    const old = oldSteps.get(step.key)
    return old && job(old) === job(step) && (!ACTIVE.has(old.status) && !ACTIVE.has(step.status) || old.status === step.status)
  })
  const retain = !!(same && stableSteps && projectFreshnessKnown(previous))
  const steps = (summary.steps || []).map((step: any) => {
    const old = oldSteps.get(step.key)
    if (old && (ACTIVE.has(old.status) || old.status === 'checking') && job(old) > 0 && job(step) > 0 && job(step) < job(old)) {
      return { ...old, fresh: null, freshness_known: false }
    }
    // 已知任务结束后，摘要尚未核验产物，不能解除同 Job 的核验门禁。
    if (old?.status === 'checking' && job(old) > 0 && !(job(step) > job(old) && ACTIVE.has(step.status))) {
      const current = job(step) > job(old) ? step : job(step) === job(old) ? { ...old, ...step, execution: old.execution } : old
      return { ...current, status: 'checking', fresh: null, freshness_known: false }
    }
    // 即使摘要修订不变，实时活动的 Job 身份也必须先于旧完成结论。
    return retain && old ? { ...old, ...step, status: ACTIVE.has(step.status) ? step.status : old.status,
      fresh: old.fresh, freshness_known: true, freshness_label: old.freshness_label,
      freshness_reason: old.freshness_reason, settings_changed: old.settings_changed,
      upstream_changed: old.upstream_changed, artifact_missing: old.artifact_missing }
      : step
  })
  const verifying = steps.some((step: any) => step.status === 'checking')
  return { ...(retain ? previous : {}), ...summary, steps, freshness_known: retain && !verifying,
    status: summary.status === 'processing' ? 'processing' : verifying ? 'checking' : retain ? previous.status : 'checking',
    dir_exists: retain ? previous.dir_exists : null,
    params: { ...(retain ? previous.params : {}), ...summary.params },
    project_settings: settingsSummary(summary.project_settings, previous?.project_settings, retain),
    comments_workspace: summary.comments_workspace || (retain ? previous.comments_workspace : null),
    last_verified_at: retain ? previous.last_verified_at : null }
}

/** 完整读取开始后修订变化时，只接受仍属同一修订的核验，不复活新增/删除前的成员。 */
export function mergeVerifiedProjects(rows: any[], current: any[], revisions: Map<string, string>, membershipChanged: boolean, checkedAt: number): any[] {
  const old = new Map(current.map(project => [project.uid, project]))
  const checked = new Map(rows.map(project => [project.uid, project]))
  const members = membershipChanged ? current : rows
  return members.map(member => {
    const latest = old.get(member.uid), full = checked.get(member.uid)
    if (!full || latest?.summary_revision && revisions.get(member.uid) !== latest.summary_revision) return latest || member
    // 已知核验中的代次不能被更旧的完整 Job 或同 Job 旧活动响应解除。
    if ((latest?.steps || []).some((step: any) => {
      if (step.status !== 'checking' || job(step) <= 0) return false
      const incoming = (full.steps || []).find((row: any) => row.key === step.key)
      return incoming && (job(incoming) > 0 && job(incoming) < job(step)
        || ACTIVE.has(incoming.status) && job(incoming) <= job(step))
    })) return latest
    return { ...full, projection: 'full', freshness_known: true, last_verified_at: checkedAt,
      ...(latest?.summary_revision ? { summary_revision: latest.summary_revision } : {}) }
  })
}

/** 完整核验单飞；显式补读合并为下一轮，其调用者等待自己的补读而非写前读取。 */
export function createProjectVerificationReader<T>(read: (signal: AbortSignal) => Promise<T>, apply: (value: T) => void, onError: (error: unknown) => void) {
  type Waiter = { resolve: () => void }
  let active: { controller: AbortController; promise: Promise<void> } | null = null
  let pending: Waiter[] = []
  function cancel() {
    const old = active; active = null; old?.controller.abort()
    const waiting = pending; pending = []; waiting.forEach(waiter => waiter.resolve())
  }
  function refresh(): Promise<void> {
    if (active) return new Promise(resolve => pending.push({ resolve }))
    const request = { controller: new AbortController(), promise: Promise.resolve() }
    active = request
    request.promise = (async () => {
      try { const result = await read(request.controller.signal); if (active === request) apply(result) }
      catch (error) { if (active === request && !request.controller.signal.aborted) onError(error) }
      finally {
        if (active === request) {
          active = null
          const waiting = pending; pending = []
          if (waiting.length) void refresh().finally(() => waiting.forEach(waiter => waiter.resolve()))
        }
      }
    })()
    return request.promise
  }
  return { refresh, cancel }
}
