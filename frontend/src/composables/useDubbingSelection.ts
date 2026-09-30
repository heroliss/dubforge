import { computed, shallowRef } from 'vue'

export type DubbingSelection = { source: 'original' | 'dub'; take: number | null }
type Edit = { selection: DubbingSelection; saving: boolean; revision: number }

// 声源/版本先在本句生效；保存与轮询独立，旧列表响应不能覆盖刚刚点选的结果。
export function useDubbingSelection(options: {
  save: (id: number, selection: DubbingSelection) => Promise<unknown>
  saved: () => void; error: (error: unknown) => void
}) {
  const edits = shallowRef<Record<number, Edit>>({})
  let revision = 0, disposed = false
  const saving = computed(() => Object.values(edits.value).some(edit => edit.saving))
  const isSaving = (id: number) => !!edits.value[id]?.saving

  async function choose(id: number, selection: DubbingSelection) {
    if (disposed || isSaving(id)) return
    const previous = edits.value[id]
    edits.value = { ...edits.value, [id]: { selection, saving: true, revision: ++revision } }
    try {
      await options.save(id, selection)
      if (disposed) return
      edits.value = { ...edits.value, [id]: { selection, saving: false, revision: ++revision } }
      options.saved()
    } catch (error) {
      if (disposed) return
      const next = { ...edits.value }
      if (previous) next[id] = previous
      else delete next[id]
      edits.value = next
      options.error(error)
    }
  }

  function reconcile(readRevision: number) {
    const next = { ...edits.value }
    for (const [id, edit] of Object.entries(next)) {
      // 只有保存成功后发起的读取才可信，不能让保存前的慢响应把界面切回去。
      if (!edit.saving && edit.revision <= readRevision) delete next[Number(id)]
    }
    edits.value = next
  }

  function apply(state: any) {
    if (!state) return state
    const lines = state.lines.map((line: any) => {
      const edit = edits.value[line.id]
      if (!edit) return line
      const wav = line.versions.find((take: any) => take.id === edit.selection.take)
        ?? { exists: false, stale: edit.selection.take !== null, url: null, seconds: null }
      return { ...line, selection: { ...edit.selection, explicit: true }, wav, confirmed: !!wav.confirmed }
    })
    return { ...state, lines, counts: { ...state.counts,
      synthesized: lines.filter((line: any) => line.wav.exists).length,
      confirmed: lines.filter((line: any) => line.confirmed).length } }
  }

  return { saving, isSaving, choose, apply, reconcile, revision: () => revision,
    dispose: () => { disposed = true } }
}
