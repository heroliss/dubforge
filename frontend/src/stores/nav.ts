import { defineStore } from 'pinia'

// 流程导航共享状态: MainLayout (左侧步骤导航) 与 StepView/PipelineView 共用一份轮询,
// 避免多处各自 fetch 造成状态不同步
export const STEP_ORDER = ['download', 'preprocess', 'transcribe', 'split',
  'translate', 'subtitles', 'dubtask', 'tts', 'assemble'] as const

export const STATUS_EMOJI: Record<string, string> = {
  pending: '⚪', queued: '⏳', running: '🔄', done: '✅',
  failed: '❌', stale: '🟠', interrupted: '❌',
}

export const STATUS_TEXT: Record<string, string> = {
  pending: '待运行', queued: '⏳ 排队中', running: '运行中', done: '✓ 完成',
  failed: '✗ 失败', stale: '⟳ 已过期', interrupted: '已中断',
}

export const STATUS_TAG: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  pending: 'default', queued: 'warning', running: 'info', done: 'success',
  failed: 'error', stale: 'warning', interrupted: 'error',
}

export const useNavStore = defineStore('nav', {
  state: () => ({
    uid: '',
    title: '',
    status: '',
    steps: [] as any[],
    timer: 0 as unknown as ReturnType<typeof setInterval> | 0,
  }),
  getters: {
    stepMap(): Record<string, any> {
      return Object.fromEntries(this.steps.map((x: any) => [x.key, x]))
    },
    busy(): boolean {
      return this.steps.some((x: any) => x.status === 'running' || x.status === 'queued')
    },
    doneCount(): number {
      return this.steps.filter((x: any) => x.status === 'done').length
    },
  },
  actions: {
    async poll() {
      if (!this.uid) return
      try {
        const [ps, ss] = await Promise.all([
          fetch(`/api/projects/${this.uid}`),
          fetch(`/api/projects/${this.uid}/steps`),
        ])
        if (ps.ok && ss.ok) {
          const p = await ps.json()
          this.title = p.title
          this.status = p.status
          this.steps = await ss.json()
        }
      } catch { /* 后端短暂不可达: 保留旧状态, 下一轮再试 */ }
    },
    enter(uid: string) {
      if (this.uid !== uid) {
        this.uid = uid
        this.title = ''
        this.steps = []
      }
      this.poll()
      if (!this.timer) this.timer = setInterval(() => this.poll(), 2500)
    },
    reset() {
      this.uid = ''
      this.title = ''
      this.steps = []
      if (this.timer) {
        clearInterval(this.timer)
        this.timer = 0
      }
    },
  },
})
