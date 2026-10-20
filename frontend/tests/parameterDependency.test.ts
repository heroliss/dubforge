import assert from 'node:assert/strict'
import test from 'node:test'
import { parameterDependencyMatches } from '../src/utils/parameterPresentation.ts'

test('参数依赖同时兼容单值与多个可用枚举，不强制转换布尔和数字', () => {
  for (const mode of ['normalize','uniform']) assert.equal(parameterDependencyMatches(mode,['normalize','uniform']),true)
  assert.equal(parameterDependencyMatches('off',['normalize','uniform']),false)
  assert.equal(parameterDependencyMatches(true,true),true)
  assert.equal(parameterDependencyMatches(1,true),false)
  assert.equal(parameterDependencyMatches(false,[true,'false']),false)
  assert.equal(parameterDependencyMatches(undefined,[]),false)
})
