import assert from 'node:assert/strict'
import test from 'node:test'
import { createProjectRunSync, PROJECT_RUN_SYNC_DELAYS, projectRequestSteps, projectRequestStage } from '../src/utils/projectRunFeedback.ts'

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }

test('本轮核对仅中性展示旧失败，保存事实、有效结果和真实运行都保持不变', () => {
  const stages = [{ key: 'download', status: 'done', complete: true }, { key: 'tts', status: 'failed', detail: '旧错误', progress: 66 }, { key: 'comments', status: 'running', progress: 20 }]
  const pending = { stage: 'checking' as const, message: '正在核对已有结果…' }
  const display = projectRequestSteps(stages, pending)
  assert.equal(display[1].status, 'pending'); assert.equal(display[1].statusText, '核对中')
  assert.equal(display[1].progress, undefined); assert.equal(display[1].progress_indeterminate, true)
  assert.equal(display[0], stages[0]); assert.equal(display[2], stages[2])
  assert.equal(stages[1].status, 'failed'); assert.equal(stages[1].detail, '旧错误')
  assert.equal(projectRequestSteps(stages, { stage: 'blocked', message: '本次失败' }), stages)
  assert.equal(projectRequestStage({ stage: 'unknown', message: '回执未知' }), null)
})

test('受理后的轻量同步只有六次，结束后不会继续轮询', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0
  const sync = createProjectRunSync(async () => { calls++; return false })
  sync.start()
  for (const delay of PROJECT_RUN_SYNC_DELAYS) { t.mock.timers.tick(delay); await flush() }
  assert.equal(calls, 6)
  t.mock.timers.tick(60000); await flush(); assert.equal(calls, 6); sync.stop()
})

test('轻量同步不并发，真实进度出现后停止', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0, release: (value: boolean) => void = () => {}
  const gate = new Promise<boolean>(resolve => { release = resolve })
  const sync = createProjectRunSync(async () => { calls++; return gate })
  sync.start(); t.mock.timers.tick(500); await flush()
  t.mock.timers.tick(10000); await flush(); assert.equal(calls, 1)
  release(true); await flush(); t.mock.timers.tick(60000); await flush(); assert.equal(calls, 1); sync.stop()
})

test('卸载停止与新请求替换都取消旧定时器，隐藏页面不加速读取', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0, visible = true
  const sync = createProjectRunSync(async () => { calls++; return false }, () => visible)
  sync.start(); sync.stop(); t.mock.timers.tick(10000); await flush(); assert.equal(calls, 0)
  sync.start(); sync.start(); t.mock.timers.tick(500); await flush(); assert.equal(calls, 1)
  visible = false; t.mock.timers.tick(750); await flush(); t.mock.timers.tick(10000); await flush(); assert.equal(calls, 1)
  sync.stop()
})

test('在途读取结束于取消之后不会再安排同步', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0, release: (value: boolean) => void = () => {}
  const gate = new Promise<boolean>(resolve => { release = resolve })
  const sync = createProjectRunSync(async () => { calls++; return gate })
  sync.start(); t.mock.timers.tick(500); await flush(); sync.stop()
  release(false); await flush(); t.mock.timers.tick(60000); await flush(); assert.equal(calls, 1)
})

test('状态读取过慢超过二十秒窗口后不再加速重读', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 })
  let calls = 0, release: (value: boolean) => void = () => {}
  const gate = new Promise<boolean>(resolve => { release = resolve })
  const sync = createProjectRunSync(async () => { calls++; return gate })
  sync.start(); t.mock.timers.tick(500); await flush()
  t.mock.timers.tick(21000); release(false); await flush()
  t.mock.timers.tick(60000); await flush(); assert.equal(calls, 1); sync.stop()
})
