import assert from 'node:assert/strict'
import test from 'node:test'
import { chainKind, chainSequence, holdDescription, parseChain } from '../src/utils/chainStatus.ts'

const labels = { translate: '翻译', tts: '字幕与配音' }

test('解析新版 chain 载荷: 计划与活性分离', () => {
  const chain = parseChain({ remaining: ['translate', 'tts'], state: 'held', hold: { step: 'translate', code: 'STEP_FAILED', message: '模拟失败' } })
  assert.equal(chain!.state, 'held')
  assert.deepEqual(chain!.remaining, ['translate', 'tts'])
  assert.equal(chain!.hold!.step, 'translate')
})

test('兼容旧版 params._chain 数组 (视为 active), 空值返回 null', () => {
  assert.equal(parseChain(['translate'])!.state, 'active')
  assert.equal(parseChain({ remaining: [], state: 'active' }), null)
  assert.equal(parseChain(null), null)
  assert.equal(parseChain(undefined), null)
})

test('挂起原因分类: 失败/停止/中断', () => {
  const held = (code: string) => ({ remaining: ['tts'], state: 'held' as const, hold: { step: 'tts', code, message: 'x' } })
  assert.equal(chainKind(held('LLM_REQUEST_FAILED')), 'failed')
  assert.equal(chainKind(held('STEP_CANCELLED')), 'stopped')
  assert.equal(chainKind(held('STEP_FORCE_STOPPED')), 'stopped')
  assert.equal(chainKind(held('STEP_INTERRUPTED')), 'interrupted')
  assert.equal(chainKind({ remaining: ['tts'], state: 'active' }), null)
  assert.equal(chainKind(null), null)
})

test('挂起与剩余步骤的中文文案', () => {
  const chain = parseChain({ remaining: ['translate', 'tts'], state: 'held', hold: { step: 'translate', code: 'STEP_TIMEOUT', message: '超时' } })!
  assert.equal(holdDescription(chain, labels), '「翻译」失败 [STEP_TIMEOUT]：超时')
  assert.equal(chainSequence(chain, labels), '翻译 → 字幕与配音')
  const stopped = parseChain({ remaining: ['tts'], state: 'held', hold: { step: 'tts', code: 'STEP_CANCELLED', message: '用户取消' } })!
  assert.equal(holdDescription(stopped, labels), '「字幕与配音」已停止 [STEP_CANCELLED]：用户取消')
  const interrupted = parseChain({ remaining: ['tts'], state: 'held', hold: { step: 'tts', code: 'STEP_INTERRUPTED', message: '上次执行被中断' } })!
  assert.equal(holdDescription(interrupted, labels), '「字幕与配音」被中断 [STEP_INTERRUPTED]：上次执行被中断')
})
