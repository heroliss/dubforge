import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import { TEXT_COLOR_GROUPS, TEXT_COLOR_PRESETS, colorPairContrast, matchingTextColorPreset, normalizeTextColor, textColorPatch } from '../src/utils/textColorPresets.ts'

test('16 款预设名称与色对唯一、分类完整，保留暖象牙；亮度对比未经取整即超过 7:1', () => {
  assert.equal(TEXT_COLOR_PRESETS.length, 16)
  assert.equal(new Set(TEXT_COLOR_PRESETS.map(p => p.id)).size, 16)
  assert.equal(new Set(TEXT_COLOR_PRESETS.map(p => p.name)).size, 16)
  assert.equal(new Set(TEXT_COLOR_PRESETS.map(p => `${p.color}/${p.outline_color}`)).size, 16)
  for (const p of TEXT_COLOR_PRESETS) {
    assert.match(p.color, /^#[0-9A-F]{6}$/); assert.match(p.outline_color, /^#[0-9A-F]{6}$/)
    assert.ok(colorPairContrast(p.color, p.outline_color)! >= 7, p.name)
    assert.ok(p.scene && p.rationale)
    assert.ok(TEXT_COLOR_GROUPS.some(group => group.id === p.group))
  }
  for (const group of TEXT_COLOR_GROUPS) assert.ok(TEXT_COLOR_PRESETS.some(p => p.group === group.id))
  assert.equal(matchingTextColorPreset('#f7f3e8', '#202631')?.id, 'ivory')
  assert.deepEqual(textColorPatch('sunshine'), { color: '#FFD65A', outline_color: '#30213A' })
  assert.equal(colorPairContrast('#000000', '#FFFFFF'), 21)
  assert.equal(colorPairContrast('#202631', '#202631'), 1)
  assert.ok(Math.abs(colorPairContrast('#777777', '#FFFFFF')! - 4.478089) < .00001)
})

// OKLab 距离防止列表堆叠近似色；0.08 是本项目策划门槛，不是通用审美定律。
function lab(hex: string) {
  const [r, g, b] = [1, 3, 5].map(i => {
    const v = parseInt(hex.slice(i, i + 2), 16) / 255
    return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4
  })
  const l = Math.cbrt(.4122214708 * r + .5363325363 * g + .0514459929 * b)
  const m = Math.cbrt(.2119034982 * r + .6806995451 * g + .1073969566 * b)
  const s = Math.cbrt(.0883024619 * r + .2817188376 * g + .6299787005 * b)
  return [.2104542553 * l + .793617785 * m - .0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + .4505937099 * s, .0259040371 * l + .7827717662 * m - .808675766 * s]
}

test('感知色差去重：暖色、冷色、低彩度与深字反转不是微调色值凑数', () => {
  for (let i = 0; i < TEXT_COLOR_PRESETS.length; i++) {
    for (let j = 0; j < i; j++) {
      const a = lab(TEXT_COLOR_PRESETS[i].color), b = lab(TEXT_COLOR_PRESETS[j].color)
      assert.ok(Math.hypot(...a.map((v, k) => v - b[k])) >= .08, `${TEXT_COLOR_PRESETS[i].name}/${TEXT_COLOR_PRESETS[j].name}`)
    }
  }
  assert.ok(lab(TEXT_COLOR_PRESETS.find(p => p.id === 'ink')!.color)[0] < .4)
})

test('手动修改自动识别为自定义；补丁仅含两色，不修改默认集合或其他参数', () => {
  assert.equal(matchingTextColorPreset('#FFD65A', '#000000'), undefined)
  assert.equal(matchingTextColorPreset(undefined, '#30213A'), undefined)
  assert.equal(normalizeTextColor('red'), undefined)
  assert.equal(normalizeTextColor('#fff; color:red'), undefined)
  assert.equal(colorPairContrast(null, '#000000'), null)
  assert.equal(textColorPatch('unknown'), null)
  const patch = textColorPatch('sunshine', 'assemble.subtitle_')!
  assert.deepEqual(Object.keys(patch), ['assemble.subtitle_color', 'assemble.subtitle_outline_color'])
  patch['assemble.subtitle_color'] = '#000000'
  assert.equal(textColorPatch('sunshine')!.color, '#FFD65A')
})

const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/TextColorPresets.vue', import.meta.url), 'utf8'))
const compiled = await build({ stdin: { contents: `import { reactive } from 'vue';
  const fixtureProps = reactive({ color: '#F7F3E8', outlineColor: '#202631', outline: 2, kind: '评论', disabled: false });
  const events = [], defineProps = () => fixtureProps, defineEmits = () => (...event) => events.push(event);
  ${descriptor.scriptSetup!.content}
  export { fixtureProps, events, open, group, source, visiblePresets, selected, choose, sampleStyle, selectTarget };`, loader: 'ts', resolveDir: `${root}/src/components` },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'palette-test-boundary', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$/ }, a => ({ path: a.path, namespace: 'palette-test' }))
    b.onLoad({ filter: /.*/, namespace: 'palette-test' }, a => ({ contents: a.path === 'vue'
      ? `export { ref, computed, reactive, watch } from '@vue/reactivity';` : 'export {};', resolveDir: root }))
  } }] })

test('实际选择器仅发草稿事件并关闭弹层，分类不选中、不联网、不私自启用描边', async t => {
  t.mock.method(globalThis, 'fetch', () => { throw new Error('切换配色不应直接请求接口') })
  const p = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
  assert.equal(p.selected.value.id, 'ivory')
  assert.equal(p.visiblePresets.value.length, 16)
  p.group.value = 'ink'; assert.equal(p.visiblePresets.value.length, 5)
  assert.equal(p.events.length, 0); assert.equal(p.selected.value.id, 'ivory')
  p.open.value = true; p.choose('sunshine')
  assert.deepEqual(p.events, [['select', 'sunshine', false]]); assert.equal(p.open.value, false)
  assert.equal(p.fixtureProps.color, '#F7F3E8')
  p.fixtureProps.color = '#FFD65A'; p.fixtureProps.outlineColor = '#30213A'
  assert.equal(p.selected.value.id, 'sunshine')
  p.fixtureProps.outlineColor = '#000000'; assert.equal(p.selected.value, undefined)
  p.fixtureProps.disabled = true; p.open.value = true; p.choose('mint')
  assert.equal(p.events.length, 1); assert.equal(p.open.value, true)
  p.fixtureProps.disabled = false; p.fixtureProps.outline = 0; p.choose('ink')
  assert.equal(p.fixtureProps.outline, 0)
  p.choose('__proto__'); assert.equal(p.events.length, 2)
  assert.equal(p.sampleStyle('url(secret)', null)['--sample-color'], '#F7F3E8')
  p.fixtureProps.sourceEnabled = true; p.fixtureProps.sourceColor = '#FFD65A'; p.fixtureProps.sourceOutlineColor = '#30213A'
  p.source.value = true
  assert.equal(p.selected.value.id, 'sunshine')
  p.choose('mint')
  assert.deepEqual(p.events.at(-1), ['select', 'mint', true])
  p.fixtureProps.sourceEnabled = false
  assert.equal(p.source.value, false)
})

test('原文入口一键请求独立设置；只切配色对象不保存，禁用时无事件', async () => {
  const p = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text+'\n// source shortcut').toString('base64')}`)
  p.fixtureProps.sourceEnabled = false
  p.selectTarget(true)
  assert.deepEqual(p.events, [['enable-source']])
  assert.equal(p.source.value, true)
  p.choose('mint')
  assert.equal(p.events.length, 1) // 父级未接受时不能误将原文配色应用到通用样式。
  p.fixtureProps.sourceEnabled = true
  p.choose('mint')
  assert.deepEqual(p.events.at(-1), ['select','mint',true])
  p.selectTarget(false); p.selectTarget(true)
  assert.equal(p.events.length, 2)
  p.fixtureProps.sourceEnabled = false
  assert.equal(p.source.value, false)
  p.fixtureProps.disabled = true
  p.selectTarget(true)
  assert.equal(p.events.length, 2)
  assert.equal(p.source.value, false)
})
