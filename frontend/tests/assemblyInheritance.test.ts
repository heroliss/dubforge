import assert from 'node:assert/strict'
import test from 'node:test'
import { assemblySettingValue, assemblyVersionFieldSource } from '../src/utils/assemblyInheritance.ts'

test('版本专用参数沿真实基础清单来源显示，人工更改明确为版本设置', () => {
  const path = 'assemble.subtitle_font_size'
  const base = { id: 'v1', name: '双语原声', overrides: { [path]: 40 }, sources: { [path]: { kind: 'series' as const, name: '旅行', frozen: true, version_name: '双语原声' } } }
  assert.deepEqual(assemblyVersionFieldSource(base, path, {}, base), base.sources[path])
  const changed = { ...base, overrides: { [path]: 42 } }
  assert.equal(assemblyVersionFieldSource(changed, path, {}, base)?.kind, 'version')
  const restored = { ...base, overrides: {} }
  assert.deepEqual(assemblyVersionFieldSource(restored, path, { [path]: { kind: 'project' } }, base), { kind: 'project' })
  const inherited = { ...base, overrides: {} }
  assert.deepEqual(assemblyVersionFieldSource(inherited, path, { [path]: { kind: 'project' } }, inherited), { kind: 'project' })
})

test('明确固定同值、零和false仍是版本值，不按等值推断删除覆盖', () => {
  const base = { id: 'v1', name: '配音', overrides: {} }
  for (const value of [0, false, 40]) {
    const changed = { ...base, overrides: { setting: value } }
    assert.equal(assemblyVersionFieldSource(changed, 'setting', { setting: { kind: 'global', frozen: true } }, base)?.kind, 'version')
    assert.ok(Object.hasOwn(changed.overrides, 'setting'))
  }
})

test('配置表单缺少版本来源元数据时沿清单身份回退，不把基础专用值误标为人工精调', () => {
  const base = { id: 'v1', name: '原声', overrides: { setting: 40 } }
  assert.deepEqual(assemblyVersionFieldSource(base, 'setting', {}, base, { kind: 'global', frozen: true }), { kind: 'global', frozen: true, version_name: '原声' })
  assert.equal(assemblyVersionFieldSource(base, 'setting', {}, base, { kind: 'project' })?.kind, 'version')
  assert.equal(assemblySettingValue([{ id: 'hidden-id', name: '原声版' }]), '原声版')
  assert.equal(assemblySettingValue(false), '关闭')
})
