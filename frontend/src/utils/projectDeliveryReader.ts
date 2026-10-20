import { supersedingPublicationUpdate } from './workflow.ts'

type ReadSummary = (uid: string, signal: AbortSignal) => Promise<any>
type Entry = { uid: string; key: string; generation: number; state: 'queued' | 'reading' | 'ready' | 'failed'; value?: any; checkedAt?: number; rechecking: boolean; controller: AbortController }

function builtIn(project: any) {
  const value = project?.workflow_summary || project?.workflow
  return value && typeof value === 'object' && !Array.isArray(value) && 'run' in value ? value : null
}

export function projectDeliveryReadKey(project: any): string | null {
  const update = project?.publication?.latest_update
  if (!project?.uid || project.archived || builtIn(project)
    || !['attention', 'paused', 'failed'].includes(project.workflow_status)
    || !project.publication?.id || !update?.id || typeof update.updated_at !== 'string'
    || !['queued', 'uploading', 'submitting', 'waiting', 'delivering', 'uploaded', 'failed', 'unknown', 'paused', 'completed'].includes(update.status)) return null
  const job = Math.max(0, ...(project.steps || []).map((step: any) => Number(step.job_id || step.execution?.job_id) || 0))
  return JSON.stringify([project.uid, project.workflow_status, project.publication.id, project.publication.status,
    project.publication.revision, project.publication.updated_at, update.id, update.status, update.updated_at, job])
}

/** 只补读无法由列表判定的历史交付；队列独立于首屏，最多两个轻量请求。 */
export function createProjectDeliveryReader(read: ReadSummary, changed: () => void = () => {}, options: { now?: () => number; ttlMs?: number } = {}) {
  const entries = new Map<string, Entry>(), queue: Entry[] = []
  const now = options.now || Date.now, ttlMs = options.ttlMs ?? 30000
  let context: string | null = null, generation = 0, active = 0
  const current = (entry: Entry) => entry.generation === generation && entries.get(entry.uid) === entry && !entry.controller.signal.aborted
  const expired = (entry: Entry) => entry.state === 'ready' && now() - (entry.checkedAt ?? 0) >= ttlMs
  const wasResolved = (entry: Entry, project: any) => {
    const update = supersedingPublicationUpdate(entry.value, project?.publication)
    return !!update && !['unknown', 'failed', 'paused'].includes(update.status)
  }
  function drain() {
    while (active < 2 && queue.length) {
      const entry = queue.shift()!
      if (!current(entry)) continue
      active++; entry.state = 'reading'
      void Promise.resolve().then(() => read(entry.uid, entry.controller.signal)).then(value => {
        if (!current(entry)) return
        if (!value || typeof value !== 'object' || Array.isArray(value) || !('run' in value)
          || value.run !== null && (typeof value.run !== 'object' || !value.run.id || typeof value.run.status !== 'string')) {
          throw new Error('自动流程状态返回不完整')
        }
        entry.value = value; entry.checkedAt = now(); entry.rechecking = false; entry.state = 'ready'; changed()
      }).catch(() => {
        if (!current(entry)) return
        entry.state = 'failed'; entry.value = undefined; changed()
      }).finally(() => { active--; drain() })
    }
  }
  function clear() {
    generation++
    for (const entry of entries.values()) entry.controller.abort()
    entries.clear(); queue.length = 0
    changed()
  }
  return {
    sync(projects: any[], nextContext: string, fresh = false) {
      if (context !== nextContext) { clear(); context = nextContext }
      const byUid = new Map(projects.map(project => [project.uid, project]))
      const wanted = new Map(projects.map(project => [project.uid, projectDeliveryReadKey(project)]))
      const rechecking = new Set<string>()
      for (const [uid, entry] of entries) {
        const same = wanted.get(uid) === entry.key
        if (!same || expired(entry) || fresh && ['failed', 'ready'].includes(entry.state)) {
          if (same && entry.state === 'ready' && wasResolved(entry, byUid.get(uid))) rechecking.add(uid)
          entry.controller.abort(); entries.delete(uid)
        }
      }
      for (const [uid, key] of wanted) {
        if (!key || entries.has(uid)) continue
        const entry: Entry = { uid, key, generation, state: 'queued', rechecking: rechecking.has(uid), controller: new AbortController() }
        entries.set(uid, entry); queue.push(entry)
      }
      drain()
    },
    get(project: any) {
      const direct = builtIn(project)
      if (direct) return direct
      const entry = entries.get(project?.uid)
      return entry?.key === projectDeliveryReadKey(project) && entry?.state === 'ready' && !expired(entry) ? entry.value : null
    },
    rechecking(project: any) {
      const entry = entries.get(project?.uid)
      return !!entry && entry.key === projectDeliveryReadKey(project)
        && (expired(entry) && wasResolved(entry, project) || entry.rechecking && ['queued', 'reading'].includes(entry.state))
    },
    clear,
  }
}
