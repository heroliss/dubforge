import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import { formatTimecode, parseTimecode, timecodeError } from '../src/utils/timecode.ts'

test('时分秒在分钟、小时边界正确进位，长视频保留小时且未知时间不伪装为零', () => {
  assert.equal(formatTimecode(0, 3), '00:00:00.000')
  assert.equal(formatTimecode(1898.52, 3), '00:31:38.520')
  assert.equal(formatTimecode(59.9996, 3), '00:01:00.000')
  assert.equal(formatTimecode(3599.96, 1), '01:00:00.0')
  assert.equal(formatTimecode(59.6), '00:01:00')
  assert.equal(formatTimecode(360000), '100:00:00')
  for (const value of [null, undefined, NaN, Infinity, -1, Number.MAX_VALUE]) assert.equal(formatTimecode(value), '—')
})

test('粘贴秒、分秒、中文冒号和时分秒均保留精确秒值，非法字段不会被静默转换', () => {
  for (const text of ['3931.125', '65:31.125', '01:05:31.125', ' 01：05：31.125 '])
    assert.equal(parseTimecode(text), 3931.125)
  for (const text of ['', '-1', '1:60', '1:60:00', '1.5:02', '1e3', 'Infinity', '1:2:3:4', '1.'])
    assert.equal(parseTimecode(text), null)
  assert.match(timecodeError('99', 100), /00:01:40.000/)
  assert.match(timecodeError('101', 0, 100), /00:01:40.000/)
  assert.equal(timecodeError('01:05:31.125', 0, 4000), '')
})

const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/TimecodeInput.vue', import.meta.url), 'utf8'))
const result = await build({ stdin: { contents: `import {reactive} from 'vue';
  const fixtureProps=reactive({value:12,min:0,max:7200,precision:3,size:'small'});
  const defineProps=()=>fixtureProps,withDefaults=(value)=>value;
  const emitted=[],defineEmits=()=>((name,...args)=>{emitted.push({name,args});if(name==='update:value')fixtureProps.value=args[0]});
  ${descriptor.scriptSetup!.content}
  export {fixtureProps,emitted,text,validation,error,inputAttributes,editing,update,commit,blur,revert,onKeydown};
`, resolveDir: fileURLToPath(new URL('../src/components', import.meta.url)), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'component-boundaries', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$/ }, a => ({ path: a.path, namespace: 'fixture' }))
    b.onLoad({ filter: /.*/, namespace: 'fixture' }, a => ({ contents: a.path === 'vue'
      ? `export {ref,computed,watch,reactive} from '@vue/reactivity';export const useId=()=> 'timecode-fixture';`
      : 'export const NInput={};', resolveDir: root }))
  } }] })
let serial = 0
async function fixture() {
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text + `\n//${serial++}`).toString('base64')}`)
}

test('无效时间保留原文并指出字段错误，不能发送旧值或NaN', async () => {
  const p = await fixture()
  p.update('01:99'); p.blur()
  assert.equal(p.text.value, '01:99')
  assert.ok(p.error.value)
  assert.equal(p.inputAttributes.value['aria-invalid'], true)
  assert.equal(p.fixtureProps.value, 12)
  assert.equal(p.emitted.filter((event:any) => event.name === 'update:value').length, 0)
  assert.ok(p.emitted.some((event:any) => event.name === 'invalid-change' && event.args[0]))
  p.update('01:02:03.125'); p.commit()
  assert.equal(p.fixtureProps.value, 3723.125)
  assert.equal(p.text.value, '01:02:03.125')
  assert.equal(p.error.value, '')
})

test('输入未完成时外部轮询不覆盖草稿，Esc还原最新数值并解除错误', async () => {
  const p = await fixture()
  p.editing.value = true; p.update('01:')
  p.fixtureProps.value = 20
  assert.equal(p.text.value, '01:')
  let stopped = 0
  p.onKeydown({key:'Escape',preventDefault(){stopped++},stopPropagation(){stopped++}})
  assert.equal(stopped, 2)
  assert.equal(p.text.value, '00:00:20.000')
  assert.equal(p.validation.value, '')
})

test('亚毫秒媒体终点不会因显示舍入被误判越界，无改动失焦不改变底层精度', async () => {
  const p = await fixture()
  p.fixtureProps.max = 3601.4567; p.fixtureProps.value = 3601.4567
  assert.equal(p.text.value, '01:00:01.457')
  assert.equal(p.validation.value, '')
  p.blur()
  assert.equal(p.fixtureProps.value, 3601.4567)
  assert.equal(p.emitted.filter((event:any) => event.name === 'update:value').length, 0)
})

test('Enter确认秒数后规范显示，越界输入保持错误直到修正', async () => {
  const p = await fixture()
  p.update('7201'); p.commit()
  assert.match(p.error.value, /02:00:00.000/)
  assert.equal(p.fixtureProps.value, 12)
  p.update('65.125')
  p.onKeydown({key:'Enter',preventDefault(){},stopPropagation(){}})
  assert.equal(p.fixtureProps.value, 65.125)
  assert.equal(p.text.value, '00:01:05.125')
})

test('原始秒值轻微越界也被拦截，不会被毫秒显示的舍入掩盖', async () => {
  const p = await fixture()
  p.fixtureProps.max = 60; p.fixtureProps.value = 60.0001
  assert.ok(p.validation.value)
  p.fixtureProps.min = .2; p.fixtureProps.value = .19999
  assert.ok(p.validation.value)
})

test('输入法确认按键不提交时间，也不干扰组合输入', async () => {
  const p = await fixture()
  p.update('100')
  p.onKeydown({key:'Enter',isComposing:true,preventDefault(){assert.fail('不应拦截输入法')},stopPropagation(){assert.fail('不应拦截输入法')}})
  assert.equal(p.fixtureProps.value, 12)
  assert.equal(p.text.value, '100')
})
