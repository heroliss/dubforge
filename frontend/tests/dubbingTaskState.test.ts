import assert from 'node:assert/strict'
import test from 'node:test'
import { currentDubbingTask, dubbingTaskState, normalizeDubbingWorkbench } from '../src/utils/dubbingTaskState.ts'
import { createExecutionProjection } from '../src/utils/executionSnapshot.ts'

test('排队策略任务尚未固定逐句范围，缺 lines 是待准备而非零句或接口读取失败', () => {
  const raw = { lines: [{ id: 1 }, { id: 2 }], synthesis_task: { job_id: 518, scope: 'policy', queued: true, active: true, total: 0 } }
  const normalized = normalizeDubbingWorkbench(raw)
  assert.deepEqual(normalized.synthesis_task.lines, [])
  assert.equal(normalized.synthesis_task.range_prepared, false)
  assert.equal('lines' in raw.synthesis_task, false, '兼容处理不能修改原始响应')
  assert.deepEqual(dubbingTaskState(normalized.synthesis_task, 646), {
    total: null, rangePending: true, documentLines: 646, completed: null, failed: null, queued: true, selected: false,
  })
  assert.equal(dubbingTaskState(raw.synthesis_task)?.total, null)
})

test('未知文稿不编造零句，逐句范围明确后保留真实数量与已完成数量', () => {
  for (const count of [undefined, null, 0, -1, Number.NaN, Infinity]) {
    assert.equal(dubbingTaskState({ active: true, total: 0 }, count)?.documentLines, null)
  }
  assert.equal(dubbingTaskState(null), null)
  const prepared = normalizeDubbingWorkbench({ lines: [], synthesis_task: { scope: 'selected', lines: [{ id: 1 }, { id: 2 }], total: 2, completed: 1, failed: 1 } })
  assert.equal(prepared.synthesis_task.range_prepared, true)
  assert.deepEqual(dubbingTaskState(prepared.synthesis_task), {
    total: 2, rangePending: false, documentLines: null, completed: 1, failed: 1, queued: false, selected: true,
  })
  assert.equal(dubbingTaskState({ lines: [], total: 0 })?.total, 0, '明确核定的空范围允许真实零值')
  assert.equal(dubbingTaskState({ total: 8 })?.total, 8, '旧服务已知正数范围仍可使用')
})

test('真实不完整响应和损坏范围保持错误，不能一律伪装成等待', () => {
  for (const response of [null, {}, { lines: {} }, { lines: [], synthesis_task: { lines: 'bad' } }, { lines: [], synthesis_task: [] }]) {
    assert.throws(() => normalizeDubbingWorkbench(response), /返回不完整|返回异常/)
  }
})

test('真实实时投影保留旧任务历史，但新Job的配音说明不能借用旧范围和失败句数', () => {
  const oldTask = { job_id: 17, scope: 'selected', lines: [{ id: 1 }], total: 1, completed: 0, failed: 1 }
  const previous = { key: 'tts', status: 'failed', job_id: 17, synthesis_task: oldTask }
  const { steps } = createExecutionProjection().apply([previous], [{ key: 'tts', status: 'queued', job_id: 18 }])
  assert.equal(steps[0].synthesis_task, oldTask, '投影和展示校验均保留原始历史记录')
  const summary = dubbingTaskState(steps[0].synthesis_task, 646, steps[0].job_id)!
  assert.equal(summary.rangePending, true); assert.equal(summary.total, null)
  assert.equal(summary.selected, false); assert.equal(summary.failed, null); assert.equal(summary.completed, null)
  assert.equal(summary.documentLines, 646)
  assert.equal(dubbingTaskState(null, 646, 18)?.rangePending, true, '新Job尚未返回范围也显示待准备')
})

test('配音任务身份匹配支持数字字符串，旧接口缺一侧身份仍兼容，同代次真实失败保留', () => {
  const task = { job_id: 17, scope: 'selected', total: 1, failed: 1, lines: [{ id: 1 }] }
  assert.equal(currentDubbingTask(task, '17'), task)
  assert.equal(currentDubbingTask({ ...task, job_id: '17' }, 17)?.failed, 1)
  assert.equal(currentDubbingTask(task, 18), null)
  assert.equal(currentDubbingTask(task), task)
  const { job_id: _job, ...legacy } = task
  assert.equal(currentDubbingTask(legacy, 18), legacy)
  assert.equal(dubbingTaskState(task, 646, 17)?.failed, 1)
  assert.equal(dubbingTaskState(legacy, 646, 18)?.total, 1)
})
