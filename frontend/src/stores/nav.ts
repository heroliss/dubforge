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

let pollSeq = 0                    // 轮询序号 (丢弃过期响应, 防状态闪回)

export const useNavStore = defineStore('nav', {
  state: () => ({
    uid: '',
    title: '',
    status: '',
    steps: [] as any[],
    autoSteps: null as string[] | null,   // 勾选集 (null = 全部, 未自定义过)
    chain: [] as string[],                // 待自动续跑队列
    timer: 0 as unknown as ReturnType<typeof setInterval> | 0,
  }),  getters: {
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
      // 序号守卫: 手动 poll 与 2.5s 定时器并发时, 慢的旧响应不得覆盖新状态
      const seq = ++pollSeq
      try {
        const [ps, ss] = await Promise.all([
          fetch(`/api/projects/${this.uid}`),
          fetch(`/api/projects/${this.uid}/steps`),
        ])
        if (seq !== pollSeq) return
        if (ps.ok && ss.ok) {
          const p = await ps.json()
          const steps = await ss.json()
          if (seq !== pollSeq) return
          this.title = p.title
          this.status = p.status
          this.autoSteps = Array.isArray(p.params?.auto_steps)
            ? p.params.auto_steps : null
          this.chain = Array.isArray(p.params?._chain) ? p.params._chain : []
          this.steps = steps
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
