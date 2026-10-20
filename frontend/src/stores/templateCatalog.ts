import { defineStore } from 'pinia'

type Pending = { context: string; promise: Promise<void> }
const pending = new WeakMap<object, Pending>()

// 卡片共享目录，只在展开选择器时读取；不会为每个项目启动配置轮询。
export const useTemplateCatalogStore = defineStore('template-catalog', {
  state: () => ({ context: '', rows: [] as any[], loading: false, error: '' }),
  actions: {
    async refresh(context: string) {
      const current = pending.get(this)
      if (current?.context === context) return current.promise
      if (this.context !== context) { this.context = context; this.rows = [] }
      this.loading = true; this.error = ''
      const request = { context, promise: Promise.resolve() }
      pending.set(this, request)
      request.promise = (async () => {
        try {
          const response = await fetch('/api/configuration/templates', { cache: 'no-store' })
          const data = await response.json()
          if (!response.ok) throw new Error(data.message || '读取模板失败')
          if (pending.get(this) === request) this.rows = Array.isArray(data) ? data : []
        } catch (error: any) {
          if (pending.get(this) === request) this.error = error.message || '读取模板失败，请重试'
        } finally {
          if (pending.get(this) === request) { this.loading = false; pending.delete(this) }
        }
      })()
      return request.promise
    },
  },
})
