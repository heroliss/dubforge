import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({ entryPoints: [fileURLToPath(new URL('../src/composables/useDubbingSelection.ts', import.meta.url))],
  bundle: true, write: false, format: 'esm', platform: 'browser', define: {'process.env.NODE_ENV': '"production"'} })
const { useDubbingSelection } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

function fixture() {
  const requests: any[] = [], errors: unknown[] = []
  let saved = 0
  const edits = useDubbingSelection({
    save: (id: number, selection: any) => new Promise((resolve, reject) => requests.push({id, selection, resolve, reject})),
    saved: () => { saved++ }, error: (error: unknown) => errors.push(error),
  })
  const state = { counts: { total: 2, synthesized: 2, confirmed: 2 }, lines: [1, 2].map(id => {
    const versions = [1, 2].map(take => ({ id: take, exists: true, url: `${id}-v${take}`, seconds: 2, confirmed: take === 1 }))
    return { id, selection: { source: 'dub', take: 1 }, versions, wav: versions[0], confirmed: true }
  }) }
  return { edits, requests, errors, state, saved: () => saved }
}

test('声源立即切换，仅本句保存中，其他句子可同时操作', async () => {
  const f = fixture()
  const first = f.edits.choose(1, {source: 'original', take: 1})
  assert.equal(f.edits.apply(f.state).lines[0].selection.source, 'original')
  assert.equal(f.edits.isSaving(1), true)
  assert.equal(f.edits.isSaving(2), false)
  const second = f.edits.choose(2, {source: 'original', take: 1})
  await f.edits.choose(1, {source: 'dub', take: 2})
  assert.equal(f.requests.length, 2, '同一句保存中不重复发送；不同句子互不阻塞')
  f.requests[0].resolve({ok: true}); await first
  assert.equal(f.edits.isSaving(1), false, '不等待列表或导航刷新就解除本句保存状态')
  assert.equal(f.edits.saving.value, true, '仍有保存时禁止开始合成，避免使用旧选择')
  f.requests[1].resolve({ok: true}); await second
  assert.equal(f.edits.saving.value, false)
  assert.equal(f.saved(), 2)
})

test('保存前或保存中的旧轮询不能覆盖选择，后续新读取恢复服务端权威状态', async () => {
  const f = fixture()
  const beforeSave = f.edits.revision()
  const saving = f.edits.choose(1, {source: 'original', take: 1})
  const duringSave = f.edits.revision()
  f.edits.reconcile(beforeSave)
  assert.equal(f.edits.apply(f.state).lines[0].selection.source, 'original')
  f.requests[0].resolve({ok: true}); await saving
  f.edits.reconcile(duringSave)
  assert.equal(f.edits.apply(f.state).lines[0].selection.source, 'original')
  const current = structuredClone(f.state)
  current.lines[0].selection.source = 'original'
  f.edits.reconcile(f.edits.revision())
  assert.equal(f.edits.apply(current).lines[0].selection.source, 'original')
  assert.equal(f.edits.apply(f.state).lines[0].selection.source, 'dub', '确认刷新后不再用本地覆盖屏蔽其他页面的修改')
})

test('切换版本立即更新试听文件和满意状态；空位静音；失败恢复此前已保存的选择', async () => {
  const f = fixture()
  const secondTake = f.edits.choose(1, {source: 'dub', take: 2})
  let state = f.edits.apply(f.state)
  assert.equal(state.lines[0].wav.url, '1-v2')
  assert.equal(state.lines[0].confirmed, false)
  assert.equal(state.counts.confirmed, 1)
  f.requests[0].resolve({ok: true}); await secondTake
  const empty = f.edits.choose(1, {source: 'dub', take: null})
  state = f.edits.apply(f.state)
  assert.equal(state.lines[0].wav.exists, false)
  assert.equal(state.counts.synthesized, 1)
  f.requests[1].reject(new Error('保存失败')); await empty
  assert.equal(f.edits.apply(f.state).lines[0].wav.url, '1-v2')
  assert.equal(f.edits.isSaving(1), false)
  assert.equal(f.errors.length, 1)
})

test('首次保存失败恢复原声源，离开页面后的响应不刷新或提示新页面', async () => {
  const f = fixture()
  const fail = f.edits.choose(1, {source: 'original', take: 1})
  f.requests[0].reject(new Error('保存失败')); await fail
  assert.equal(f.edits.apply(f.state).lines[0].selection.source, 'dub')
  assert.equal(f.edits.saving.value, false)
  const late = f.edits.choose(1, {source: 'original', take: 1})
  f.edits.dispose()
  f.requests[1].resolve({ok: true}); await late
  assert.equal(f.saved(), 0)
  assert.equal(f.errors.length, 1)
})
