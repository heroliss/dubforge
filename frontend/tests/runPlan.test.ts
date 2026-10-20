import assert from 'node:assert/strict'
import test from 'node:test'
import { runPlannedSteps } from '../src/utils/runPlan.ts'

const stale = { step: 'translate', name: 'translate.txt', state: 'stale',
  label: '翻译', reason: '依赖已过期：输入或设置已变化，需更新' }

test('无依赖补齐也必须确认，取消不运行；确认仅提交已展示的步骤', async t => {
  const requests: any[] = []
  let shown: any
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    requests.push({ url, body: JSON.parse(options.body) })
    return { ok: true, json: async () => url.endsWith('run_plan') ? {
      steps: ['assemble'], labels: { assemble: '合成成片' }, blockers: [],
      effects: ['默认方案：配音 · 不烧录字幕（1 种）；不读取成品卡片勾选。'],
    } : { started: 'assemble' } }
  })
  await runPlannedSteps('p', ['assemble'], false,
    { warning: assert.fail, error: assert.fail, success: () => {} },
    { info: (options: any) => { shown = options } }, async () => {}, async () => {})
  assert.equal(requests.length, 1, '未确认时只有只读计划请求')
  assert.match(shown.content, /不读取成品卡片勾选/)
  assert.equal(shown.negativeText, '取消')
  await shown.onPositiveClick()
  assert.deepEqual(requests[1].body, { steps: ['assemble'] })
})

test('只有过期依赖且缺失列表为空时，仍阻止直接运行', async t => {
  const requests: string[] = [], warnings: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    requests.push(url)
    return { ok: true, json: async () => ({ missing: [], blockers: [stale] }) }
  })
  await runPlannedSteps('p', ['dubtask'], false,
    { warning: (s: string) => warnings.push(s), error: assert.fail },
    { info: assert.fail }, async () => assert.fail('不应开始执行'), async () => {})
  assert.deepEqual(requests, ['/api/projects/p/run_plan'])
  assert.equal(warnings.length, 1)
  assert.match(warnings[0], /translate.txt.*过期/)
})

test('准备依赖时明确提示更新，并按展示的计划执行', async t => {
  const requests: any[] = [], events: string[] = []
  let shown: any
  t.mock.method(globalThis, 'fetch', async (url: string, options: any) => {
    requests.push({ url, body: JSON.parse(options.body) })
    return { ok: true, json: async () => url.endsWith('run_plan') ? {
      steps: ['translate', 'dubtask'], added: ['translate'], missing: [], blockers: [],
      requirements: [stale], labels: { translate: '翻译', dubtask: '配音计划' },
    } : { started: 'translate' } }
  })
  await runPlannedSteps('p', ['dubtask'], true,
    { warning: assert.fail, error: assert.fail, success: () => events.push('started') },
    { info: (options: any) => { shown = options } },
    async steps => { assert.deepEqual(steps, ['translate', 'dubtask']); events.push('before') },
    async () => { events.push('after') })
  assert.equal(requests.length, 1)
  assert.equal(shown.title, '更新依赖并运行')
  assert.match(shown.content, /需更新产物：翻译/)
  assert.doesNotMatch(shown.content, /需生成产物|补齐/)
  await shown.onPositiveClick()
  assert.deepEqual(requests[1], {
    url: '/api/projects/p/run_chain', body: { steps: ['translate', 'dubtask'] },
  })
  assert.deepEqual(events, ['before', 'started', 'after'])
})

test('启动成功不等待慢刷新且不重复提交，刷新失败不误报启动失败', async t => {
  let shown:any, posts=0, failedRefresh:(reason:Error)=>void=()=>{}, settled=false
  const warnings:string[]=[], successes:string[]=[]
  const pending = new Promise<void>((_,reject)=>{failedRefresh=reject})
  t.mock.method(globalThis,'fetch',async (url:string)=>({ok:true,json:async()=>{
    if(url.endsWith('run_plan'))return {steps:['assemble'],labels:{assemble:'合成'},blockers:[]}
    posts++;return {started:'assemble'}
  }}))
  await runPlannedSteps('p',['assemble'],false,
    {warning:(x:string)=>warnings.push(x),error:assert.fail,success:(x:string)=>successes.push(x)},
    {info:(options:any)=>{shown=options}},async()=>{},()=>pending)
  const done=shown.onPositiveClick().then(()=>{settled=true})
  for(let i=0;i<16;i++)await Promise.resolve()
  try {
    assert.equal(settled,true,'弹窗回调应在后台刷新完成之前结束')
    assert.equal(posts,1);assert.equal(successes.length,1)
    await shown.onPositiveClick();assert.equal(posts,1)
  } finally {failedRefresh(new Error('慢刷新失败'));await done;await Promise.resolve()}
  assert.match(warnings[0],/状态刷新失败/)
})

test('制作计划确认窗口打开后项目归档，不再发送启动请求', async t => {
  let archived=false,shown:any
  const requests:string[]=[],warnings:string[]=[]
  t.mock.method(globalThis,'fetch',async(url:string)=>{
    requests.push(url);return {ok:true,json:async()=>({steps:['split'],labels:{split:'语义整理'},blockers:[]})}
  })
  await runPlannedSteps('p',['split'],false,{warning:(text:string)=>warnings.push(text),error:assert.fail},
    {info:(options:any)=>{shown=options}},async()=>assert.fail('归档后不暂停预览或启动'),async()=>{},()=>archived)
  archived=true
  assert.equal(await shown.onPositiveClick(),false)
  assert.deepEqual(requests,['/api/projects/p/run_plan']);assert.match(warnings[0],/已归档.*恢复/)
  await runPlannedSteps('p',['split'],false,{warning:(text:string)=>warnings.push(text),error:assert.fail},
    {info:assert.fail},async()=>{},async()=>{},()=>archived)
  assert.equal(requests.length,1,'已知归档的项目不预检')
})
