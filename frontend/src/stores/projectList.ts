import { defineStore } from 'pinia'

export type ProjectListScope = 'active' | 'archived'
type Snapshot = { rows: any[]; checkedAt: number }

// 只保存列表的展示快照，不保持组件、定时器、选择或写操作授权。
// 实际服务身份绑定运行中的工作目录；同源后端切换目录后不能沿用旧卡片。
export const useProjectListStore = defineStore('project-list', {
  state: () => ({
    context: '',
    snapshots: { active: null, archived: null } as Record<ProjectListScope, Snapshot | null>,
    revision: 0,
    freshNeeded: { active: false, archived: false },
  }),
  actions: {
    activate(context: string) {
      if (!context || context !== this.context) {
        this.snapshots = { active: null, archived: null }
        this.context = context
        this.revision = 0
        this.freshNeeded = { active: false, archived: false }
      }
    },
    read(scope: ProjectListScope) {
      const snapshot = this.snapshots[scope]
      return this.context && snapshot ? { rows: [...snapshot.rows], checkedAt: snapshot.checkedAt } : null
    },
    save(scope: ProjectListScope, rows: any[]) {
      if (this.context) this.snapshots[scope] = { rows: [...rows], checkedAt: Date.now() }
    },
    invalidate(scope?: ProjectListScope) {
      if (scope) this.snapshots[scope] = null
      else this.snapshots = { active: null, archived: null }
    },
    changed() {
      this.invalidate()
      this.revision++
      // 后端仍可能在完成写入前开始的合并读取。两个范围分别补一次新读取，
      // 不能因当前范围已更新，就让稍后打开的另一个范围加入旧读取。
      this.freshNeeded = { active: true, archived: true }
    },
    acceptFresh(scope: ProjectListScope, revision: number) {
      if (revision === this.revision) this.freshNeeded[scope] = false
    },
  },
})
