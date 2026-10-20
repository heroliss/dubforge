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

test('最新任务的取消与活动事实优先于旧步骤失败，错代次和无身份不得借用', () => {
  const old = { status: 'failed', error: '步骤已停止，已完成产物保留' }
  assert.equal(stepDisplayStatus({...old,job_id:18,job_status:'cancelled'}),'stopped')
  assert.equal(stepDisplayStatus({...old,execution:{schema:1,owner:'step',job_id:18,state:'cancelled'}}),'stopped')
  assert.equal(stepDisplayStatus({...old,job_id:'18',execution:{schema:1,owner:'step',job_id:18,state:'cancelled'}}),'stopped')
  for (const execution of [
    {schema:1,owner:'step',job_id:17,state:'cancelled'},
    {schema:1,owner:'workflow',job_id:18,state:'cancelled'},
    {schema:1,owner:'step',state:'cancelled'},
  ]) assert.equal(stepDisplayStatus({...old,job_id:18,execution}),'failed')
  for (const status of ['queued','running']) {
    assert.equal(stepDisplayStatus({...old,job_id:19,job_status:status}),status)
    assert.equal(stepDisplayStatus({...old,job_id:19,execution:{schema:1,owner:'step',job_id:19,state:status}}),status)
    assert.equal(stepDisplayStatus({...old,job_id:19,execution:{schema:1,owner:'step',job_id:18,state:'cancelled'},job_status:status}),status)
  }
  assert.equal(stepDisplayStatus({...old,job_id:19,job_status:'failed',error:{code:'STEP_CANCELLED'}}),'failed','真实新任务失败不借旧取消原因')
  assert.equal(stepDisplayStatus(old),'failed','旧服务没有身份依据时保持原错误，不猜主动停止')
  assert.equal(stepDisplayStatus({...old,job_status:'cancelled'}),'failed')
})
