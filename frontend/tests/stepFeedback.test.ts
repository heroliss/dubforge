import assert from 'node:assert/strict'
import test from 'node:test'
import { showStepError, stepDisplayStatus } from '../src/utils/stepFeedback.ts'

test('暂停请求与实际暂停分开显示，旧后端不误报已暂停', () => {
  assert.equal(stepDisplayStatus({ status: 'running', pausing: true }), 'pausing')
  assert.equal(stepDisplayStatus({ status: 'running', pause_state: 'requested' }), 'pausing')
  assert.equal(stepDisplayStatus({ status: 'running', pause_state: 'paused' }), 'paused')
  assert.equal(stepDisplayStatus({ status: 'running', pause_state: 'none' }), 'running')
})

test('取消优先于暂停，已结束的任务忽略残留暂停标志', () => {
  assert.equal(stepDisplayStatus({ status: 'running', pause_state: 'paused', cancelling: true }), 'running')
  for (const status of ['done', 'failed', 'interrupted', 'pending']) {
    assert.equal(stepDisplayStatus({ status, pause_state: 'paused' }), status)
  }
})

test('主动取消和强制停止显示中性状态，不常驻错误提示', () => {
  for (const code of ['STEP_CANCELLED', 'STEP_FORCE_STOPPED']) {
    const step = {status: 'failed', error: {code}}
    assert.equal(stepDisplayStatus(step), 'stopped')
    assert.equal(showStepError(step), false)
  }
})

test('超时、服务失败与进程中断仍显示错误，重跑时旧错误不再出现', () => {
  for (const code of ['STEP_TIMEOUT', 'TTS_FAILED', 'STEP_INTERRUPTED']) {
    assert.equal(showStepError({status: 'failed', error: {code}}), true)
    assert.equal(showStepError({status: 'interrupted', error: {code}}), true)
    for (const status of ['queued', 'running', 'done', 'pending']) {
      const step = {status, error: {code}}
      assert.equal(stepDisplayStatus(step), status)
      assert.equal(showStepError(step), false)
    }
  }
  assert.equal(showStepError({status: 'failed'}), false)
  assert.equal(stepDisplayStatus({status: 'running', error: {code: 'STEP_CANCELLED'}}), 'running')
})
