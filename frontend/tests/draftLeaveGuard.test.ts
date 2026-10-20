import assert from 'node:assert/strict'
import test from 'node:test'
import { createDraftLeaveGuard } from '../src/utils/draftLeaveGuard.ts'

function fixture() {
  const state = { busyReason: '', dirty: true, title: '翻译背景尚未保存', content: '离开会放弃背景与术语修改。' }
  const warnings: string[] = [], dialogs: any[] = []
  const guard = createDraftLeaveGuard(() => state, value => warnings.push(value), value => { dialogs.push(value) })
  return { state, warnings, dialogs, guard }
}

test('切换步骤或项目与离开页面共用一个确认，快速多次导航不重复弹窗', async () => {
  const { guard, dialogs } = fixture()
  const pending = guard.update({ path: '/projects/a/steps/tts' }, { path: '/projects/a/steps/translate' })
  assert.equal(guard.leave(), pending)
  assert.equal(guard.update({ path: '/projects/b/steps/translate' }, { path: '/projects/a/steps/translate' }), pending)
  assert.equal(dialogs.length, 1)
  assert.equal(dialogs[0].title, '翻译背景尚未保存')
  dialogs[0].onPositiveClick()
  dialogs[0].onClose()
  assert.equal(await pending, true)
})

test('同一步骤页只改query或hash保留草稿，不弹窗也不受保存占用阻止', () => {
  const { guard, state, dialogs, warnings } = fixture()
  state.busyReason = '正在保存背景'
  const path = '/projects/a/steps/translate'
  assert.equal(guard.update({ path, query: { panel: 'parameters' }, hash: '#llm' } as any,
    { path, query: { panel: 'logs' }, hash: '' } as any), true)
  assert.equal(dialogs.length, 0); assert.equal(warnings.length, 0)
})

test('实际切换步骤时，正在保存的背景不能被销毁', () => {
  const { guard, state, dialogs, warnings } = fixture()
  state.busyReason = '正在读取或保存翻译背景，请稍候再离开'
  assert.equal(guard.update({ path: '/projects/a/steps/tts' }, { path: '/projects/a/steps/translate' }), false)
  assert.deepEqual(warnings, [state.busyReason]); assert.equal(dialogs.length, 0)
})

for (const action of ['onNegativeClick', 'onClose', 'onMaskClick']) {
  test(`取消确认（${action}）保留草稿，下一次切换重新确认`, async () => {
    const { guard, dialogs } = fixture()
    const pending = guard.leave()
    dialogs[0][action]()
    assert.equal(await pending, false)
    const next = guard.leave()
    assert.notEqual(next, pending); assert.equal(dialogs.length, 2)
    dialogs[1].onPositiveClick()
    assert.equal(await next, true)
  })
}

test('背景已经保存或放弃后可以直接导航', () => {
  const { guard, state, dialogs } = fixture()
  state.dirty = false
  assert.equal(guard.leave(), true)
  assert.equal(guard.update({ path: '/projects/a/steps/tts' }, { path: '/projects/a/steps/translate' }), true)
  assert.equal(dialogs.length, 0)
})
