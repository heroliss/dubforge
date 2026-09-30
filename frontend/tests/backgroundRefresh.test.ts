import assert from 'node:assert/strict'
import test from 'node:test'
import { refreshInBackground } from '../src/utils/backgroundRefresh.ts'

test('慢刷新不占用操作完成路径；同步和异步刷新错误由独立回调处理', async () => {
  let reject: (error: Error) => void = () => {}, failures = 0
  const pending = new Promise<void>((_, fail) => { reject = fail })
  assert.equal(refreshInBackground(() => pending, () => failures++), undefined)
  assert.equal(failures, 0)
  reject(new Error('刷新失败')); await Promise.resolve(); await Promise.resolve()
  assert.equal(failures, 1)
  refreshInBackground(() => { throw new Error('同步失败') }, () => failures++)
  assert.equal(failures, 2)
})
