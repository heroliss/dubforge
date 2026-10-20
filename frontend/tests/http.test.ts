import assert from 'node:assert/strict'
import test from 'node:test'
import { BACKEND_CONNECTION_NOTICE, friendlyError, isNetworkError } from '../src/utils/http.ts'

test('网络提示保留后台状态的不确定性，不宣称任务已经中断', () => {
  assert.equal(friendlyError(new TypeError('Failed to fetch')), BACKEND_CONNECTION_NOTICE)
  assert.match(BACKEND_CONNECTION_NOTICE, /后台任务可能仍在继续/)
  assert.doesNotMatch(BACKEND_CONNECTION_NOTICE, /已被安全中断|可「重试并继续」/)
  assert.equal(isNetworkError(Object.assign(new Error('Failed to fetch upstream'), {status: 503})), false)
  assert.equal(friendlyError(Object.assign(new Error('服务状态响应无效'), {readFailure: true})), '服务状态响应无效')
  assert.equal(friendlyError(null, '读取失败'), '读取失败')
})
