import assert from 'node:assert/strict'
import test from 'node:test'
import { executionEtaSeconds } from '../src/utils/executionEta.ts'
import { createExecutionProjection } from '../src/utils/executionSnapshot.ts'

const step = (patch: Record<string, any> = {}) => ({
  key: 'assemble', status: 'running', job_id: 19, progress: 49.5, eta_seconds: .1, eta_scope: 'phase',
  execution: { schema: 1, owner: 'step', job_id: 19, state: 'running', phase: 'correcting_output_audio',
    label: '复核并纠正 AAC 编码真峰值', completed: 0, total: 3, unit: '轮' }, ...patch,
})

test('合成从编码切入AAC复核时撤下旧阶段ETA，保留整体49.5和真实0/3轮', () => {
  const source = step(), before = structuredClone(source)
  assert.equal(executionEtaSeconds(source), 0)
  assert.equal(source.progress, 49.5); assert.equal(source.execution.completed, 0)
  assert.equal(source.execution.total, 3); assert.equal(source.execution.unit, '轮')
  assert.deepEqual(source, before)
})

test('同Job实际编码与剪辑编码继续保留当前阶段ETA，不推算新ETA', () => {
  for (const phase of ['encoding', 'encoding_timeline']) {
    const source = step({ eta_seconds: 120 })
    Object.assign(source.execution, { phase, completed: 1444.01, total: 1726.92, unit: '成片秒' })
    assert.equal(executionEtaSeconds(source), 120)
    assert.equal(source.progress, 49.5)
    source.execution.job_id = '19' as any
    assert.equal(executionEtaSeconds(source), 120)
    source.job_id = null as any
    assert.equal(executionEtaSeconds(source), 120, '完整响应可用其最新execution中的Job身份')
  }
})

test('测量、纠偏、保存、下一版准备及未知阶段都不能借旧编码ETA', () => {
  for (const phase of ['measuring_output_audio', 'correcting_output_audio', 'checking_version',
    'checking_render_resources', 'saving_output', 'future_phase', undefined]) {
    const source = step(); Object.assign(source.execution, { phase, unit: '成片秒' })
    assert.equal(executionEtaSeconds(source), 0, String(phase))
  }
})

test('旧Job、缺身份、错误owner、暂停/停止/排队及未知单位均不展示阶段ETA', () => {
  for (const patch of [{ job_id: 18 }, { job_id: undefined }, { owner: 'workflow' }, { state: 'queued' },
    { state: 'paused' }, { state: 'stopping' }, { state: 'completed' }, { finished_at: '2026-10-20T00:00:01Z' },
    { unit: '轮' }, { unit: undefined }, { schema: 2 }]) {
    const source = step()
    Object.assign(source.execution, { phase: 'encoding', unit: '成片秒' }, patch)
    assert.equal(executionEtaSeconds(source), 0, JSON.stringify(patch))
  }
  for (const execution of [null, undefined, {}]) assert.equal(executionEtaSeconds(step({ execution })), 0)
  const paused = step({ pause_state: 'paused' })
  Object.assign(paused.execution, { phase: 'encoding', unit: '成片秒' })
  assert.equal(executionEtaSeconds(paused), 0)
})

test('其他步骤的真实阶段ETA及合成整步ETA保持原值，未知仍未知', () => {
  for (const key of ['tts', 'download', 'transcribe', 'split', 'translate', 'subtitles']) {
    assert.equal(executionEtaSeconds(step({ key, eta_seconds: 120 })), 120)
  }
  for (const eta_scope of ['step', undefined]) {
    assert.equal(executionEtaSeconds(step({ eta_scope, eta_seconds: 120 })), 120)
    assert.equal(executionEtaSeconds(step({ eta_scope, eta_seconds: undefined })), undefined)
  }
  assert.equal(executionEtaSeconds(null), undefined)
})

test('实际合成步骤排队或缺少运行态时，旧running编码快照不能证明本次正在编码', () => {
  for (const status of ['queued', 'pending', 'done', 'failed', 'paused', undefined, null]) {
    for (const eta_scope of ['phase', 'step', undefined]) {
      const source = step({ status, eta_scope, eta_seconds: 120 })
      Object.assign(source.execution, { phase: 'encoding', unit: '成片秒' })
      assert.equal(executionEtaSeconds(source), 0, `${status}/${eta_scope}`)
    }
  }
  assert.equal(executionEtaSeconds(step({ eta_scope: undefined, execution: null, eta_seconds: 120 })), 120,
    '运行中的旧接口整步ETA不要求新execution协议')
})

test('真实轻量投影的同Job阶段切换不会把旧编码ETA解释为新复核预计', () => {
  const previous = step({ eta_seconds: 5 })
  Object.assign(previous.execution, { phase: 'encoding', unit: '成片秒', completed: 3266.5, total: 3266.76 })
  const snapshot = step()
  const current = createExecutionProjection().apply([previous], [snapshot]).steps[0]
  assert.equal(current.execution.phase, 'correcting_output_audio')
  assert.equal(current.progress, 49.5); assert.equal(current.eta_seconds, .1)
  assert.equal(executionEtaSeconds(current), 0)
})
