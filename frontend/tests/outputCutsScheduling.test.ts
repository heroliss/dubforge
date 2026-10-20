import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'

const root = fileURLToPath(new URL('..', import.meta.url))
const require = createRequire(import.meta.url)
const vueRuntime = require.resolve('vue/dist/vue.runtime.esm-bundler.js')
const { descriptor } = parse(await readFile(new URL('../src/components/OutputCutsEditor.vue', import.meta.url), 'utf8'))

// 执行完整组件脚本，保留真正 Vue watch/nextTick 的队列调度。
// 仅替换未挂载的生命周期和界面组件，避免同步 reactivity 替身掩盖竞态。
const result = await build({
  stdin: {
    contents: `import { reactive, nextTick, unmountHooks } from 'vue';
      const fixtureProps=reactive({uid:'fixture',versionId:'v1',running:false,disabled:false,targetArtifactName:'current.mp4',targetArtifactHash:'current-hash'});
      const defineProps=()=>fixtureProps, defineEmits=()=>()=>{};
      ${descriptor.scriptSetup!.content}
      export {nextTick,unmountHooks,adopt,ranges,skipCuts,setMarkerValue,startVirtualPreview,hasPendingMarker,virtualPreviewStatus,virtualPreviewTaskId,virtualPreviewUrl,virtualPreviewGeneration};`,
    loader: 'ts',
    resolveDir: `${root}/src/components`,
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node',
  plugins: [{ name: 'ui-boundaries', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
    b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({
      contents: a.path === 'vue'
        ? `export {ref,computed,watch,reactive,nextTick} from ${JSON.stringify(vueRuntime)};
           export const unmountHooks=[],onMounted=()=>{},onBeforeUnmount=f=>unmountHooks.push(f);`
        : a.path === 'naive-ui'
          ? 'export const useDialog=()=>({warning(){return{destroy(){}}}});export const useMessage=()=>({success(){},warning(){},info(){}});'
          : 'export default {};',
      resolveDir: root,
    }))
  } }],
})

let serial = 0
async function fixture(t: any) {
  const page = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text + `\n//${serial++}`).toString('base64')}`)
  t.after(async () => {
    page.unmountHooks.forEach((hook: () => void) => hook())
    await page.nextTick()
  })
  page.adopt({
    revision: 'r1', source_fingerprint: 'source-1', duration: 60, source_url: '/source',
    enabled: true, ranges: [{ id: 'first', start: 5, end: 10, reason: '', enabled: true }],
    artifact_cuts: { source_duration: 60, ranges: [], output_duration: 60 },
    artifact_validation: { valid: true },
  })
  page.skipCuts.value = true
  await page.nextTick()
  return page
}

function response(value: unknown) { return new Response(JSON.stringify(value)) }
function ready(taskId: string) {
  return response({ task_id: taskId, status: 'done', output_url: `/${taskId}.mp4`, duration: 50, frame_rate: 25,
    segments: [{ source_start: 0, source_end: 5, virtual_start: 0, virtual_end: 5 },
      { source_start: 10, source_end: 20, virtual_start: 5, virtual_end: 15 },
      { source_start: 25, source_end: 60, virtual_start: 15, virtual_end: 50 }] })
}
function pendingMarker(page: any) {
  page.setMarkerValue('start', 20)
  page.setMarkerValue('end', 25)
}

test('真实 Vue 队列不会将自动收录标记后刚启动的预览误取消', async t => {
  const page = await fixture(t), calls: any[] = []
  let reply!: (value: Response) => void
  t.mock.method(globalThis, 'fetch', (url: any, init: any) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : null })
    if (String(url).endsWith('/cancel')) return Promise.resolve(response({ status: 'cancelled' }))
    return new Promise<Response>(resolve => { reply = resolve })
  })
  pendingMarker(page)
  const start = page.startVirtualPreview()
  const generation = page.virtualPreviewGeneration
  assert.equal(page.ranges.value.length, 2)
  assert.equal(page.hasPendingMarker.value, false)
  assert.equal(page.virtualPreviewStatus.value, 'loading')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].body.draft.ranges.length, 2)

  await page.nextTick()
  assert.equal(page.virtualPreviewStatus.value, 'loading', '草稿的延后 watcher 不能取消本轮新任务')
  assert.equal(page.virtualPreviewGeneration, generation)
  reply(ready('current'))
  await start
  await page.nextTick()
  assert.equal(page.virtualPreviewStatus.value, 'ready')
  assert.equal(page.virtualPreviewTaskId.value, 'current')
  assert.equal(page.virtualPreviewUrl.value, '/current.mp4')
  assert.equal(calls.length, 1)
})

test('手工标记的新片段决定接缝预览窗口，不沿用播放头附近的旧片段', async t => {
  const page = await fixture(t), requests: any[] = []
  page.ranges.value[0].end = 8
  page.setMarkerValue('start', 12)
  page.setMarkerValue('end', 16)
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    if (String(url).endsWith('/cancel')) return response({ status: 'cancelled' })
    requests.push(JSON.parse(init.body))
    return response({ task_id: 'new-seam', status: 'done', output_url: '/new-seam.mp4', duration: 9,
      segments: [{ source_start: 8, source_end: 12, virtual_start: 0, virtual_end: 4 },
        { source_start: 16, source_end: 21, virtual_start: 4, virtual_end: 9 }] })
  })

  await page.startVirtualPreview()
  await page.nextTick()
  assert.equal(requests.length, 1)
  assert.deepEqual(requests[0].preview_range, { start: 7, end: 21 })
  assert.deepEqual(requests[0].draft.ranges.map((row: any) => ({ start: row.start, end: row.end })),
    [{ start: 5, end: 8 }, { start: 12, end: 16 }])
  assert.equal(page.hasPendingMarker.value, false)
  assert.equal(page.virtualPreviewStatus.value, 'ready')
  assert.equal(page.virtualPreviewTaskId.value, 'new-seam')
  assert.equal(page.virtualPreviewUrl.value, '/new-seam.mp4')
})

test('自动收录新标记仍会取消旧的运行任务，新任务跨队列后保持有效', async t => {
  const page = await fixture(t), calls: string[] = []
  let starts = 0, reply!: (value: Response) => void
  t.mock.method(globalThis, 'fetch', (url: any) => {
    const path = String(url)
    calls.push(path)
    if (path.endsWith('/cancel')) return Promise.resolve(response({ status: 'cancelled' }))
    if (path.endsWith('/old')) return Promise.resolve(response({ task_id: 'old', status: 'running', progress: 10 }))
    if (path.endsWith('/virtual-preview')) {
      starts++
      if (starts === 1) return Promise.resolve(response({ task_id: 'old', status: 'queued' }))
      return new Promise<Response>(resolve => { reply = resolve })
    }
    throw new Error(`未预期的请求：${path}`)
  })
  await page.startVirtualPreview()
  assert.equal(page.virtualPreviewStatus.value, 'loading')
  assert.equal(page.virtualPreviewTaskId.value, 'old')

  pendingMarker(page)
  const start = page.startVirtualPreview()
  await page.nextTick()
  assert.equal(calls.filter(path => path.endsWith('/old/cancel')).length, 1)
  assert.equal(page.virtualPreviewStatus.value, 'loading')
  assert.equal(starts, 2)
  reply(ready('new'))
  await start
  await page.nextTick()
  assert.equal(page.virtualPreviewStatus.value, 'ready')
  assert.equal(page.virtualPreviewTaskId.value, 'new')
  assert.equal(calls.filter(path => path.endsWith('/old')).length, 1, '旧轮询只读过一次，替换后不再继续轮询')
})

test('旧启动响应迟到时取消其后台任务，并保留较新的就绪预览', async t => {
  const page = await fixture(t), calls: string[] = []
  let starts = 0, replyOld!: (value: Response) => void
  t.mock.method(globalThis, 'fetch', (url: any) => {
    const path = String(url)
    calls.push(path)
    if (path.endsWith('/cancel')) return Promise.resolve(response({ status: 'cancelled' }))
    if (path.endsWith('/virtual-preview')) {
      starts++
      return starts === 1
        ? new Promise<Response>(resolve => { replyOld = resolve })
        : Promise.resolve(ready('newer'))
    }
    throw new Error(`未预期的请求：${path}`)
  })
  const oldStart = page.startVirtualPreview()
  pendingMarker(page)
  await page.startVirtualPreview()
  await page.nextTick()
  assert.equal(page.virtualPreviewTaskId.value, 'newer')

  replyOld(response({ task_id: 'stale', status: 'queued' }))
  await oldStart
  await page.nextTick()
  assert.equal(calls.filter(path => path.endsWith('/stale/cancel')).length, 1)
  assert.equal(page.virtualPreviewStatus.value, 'ready')
  assert.equal(page.virtualPreviewTaskId.value, 'newer')
  assert.equal(page.virtualPreviewUrl.value, '/newer.mp4')
})
