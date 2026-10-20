import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/PublishRemoteSyncPanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止远端请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })
function respond(data: any) { return Promise.resolve({ ok: true, json: async () => data }) }

async function render(props: any) {
  let page: any
  const app = createSSRApp({ render: () => h(Panel, props) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === Panel.__name) page = this.$.setupState } })
  return { html: await renderToString(app), page }
}

test('有BVID时在发布页显示同步入口，不自动读取或写入B站', async () => {
  const { html } = await render({ accountId: '404575122', bvid: 'BV1234567890', selectedFields: ['description'], localValues: {
    title: '本地标题', description: '本地简介', tags: ['DubForge'], dynamic: '', declaration_id: null,
  } })
  assert.match(html, /同步B站稿件资料/)
  assert.match(html, /读取B站资料/)
  assert.match(html, /不会创建新的投稿/)
})

test('没有有效BVID时不渲染远端同步面板', async () => {
  const { html } = await render({ accountId: '404575122', bvid: '', localValues: {} })
  assert.doesNotMatch(html, /同步B站稿件资料/)
})

test('远端同步具名区分读取、预检、提交和回读；忙态结束后不残留旧阶段',async t=>{
  const {page}=await render({accountId:'123',bvid:'BV1234567890',selectedFields:['description'],localValues:{description:'新简介'}})
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url:any)=>new Promise(resolve=>requests.push({url:String(url),resolve})))
  const remote={settings:{description:'旧简介'}}
  const read=page.readRemote();assert.match(page.operation,/正在读取B站稿件/)
  requests[0].resolve({ok:true,json:async()=>remote});await read;assert.equal(page.operation,'')
  const plan=page.prepare();assert.match(page.operation,/核对远端资料.*同步计划/)
  requests[1].resolve({ok:true,json:async()=>({id:'plan',preview:{before:remote,after:{settings:{description:'新简介'}}}})})
  await plan;assert.equal(page.operation,'')
  const commit=page.commit();assert.match(page.operation,/正在提交/)
  await page.commit();assert.equal(requests.length,3)
  requests[2].resolve({ok:true,json:async()=>({status:'done',accepted:true})})
  for(let i=0;i<12;i++)await Promise.resolve()
  assert.match(page.operation,/正在回读.*核对同步结果/)
  requests[3].resolve({ok:true,json:async()=>({settings:{description:'新简介'}})});await commit
  assert.equal(page.loading,false);assert.equal(page.operation,'');assert.match(page.result,/完成回读核验/)
})

test('只提交明确载入的字段，不带旧分P顺序；确认框使用服务端差异', async () => {
  const { page } = await render({ accountId: '404575122', bvid: 'BV1234567890', selectedFields: ['description'], localValues: {
    title: '旧标题', description: '新简介', tags: ['旧标签'], dynamic: '旧动态',
  } })
  const calls: any[] = []
  const remote = { bvid: 'BV1234567890', settings: { title: '远端标题', description: '旧简介', tags: ['远端标签'], dynamic: '远端动态' }, videos: [{ cid: 2, title: 'P2' }, { cid: 1, title: 'P1' }] }
  mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const body = options?.body ? JSON.parse(options.body) : undefined
    calls.push({ url: String(url), body })
    if (String(url).endsWith('/read/detail')) return respond(remote)
    if (String(url).endsWith('/changes')) return respond({ id: 'plan', bvid: remote.bvid, kind: 'edit', preview: { before: remote, after: { settings: { ...remote.settings, description: '新简介' }, videos: remote.videos } } })
    throw new Error(`非预期请求：${url}`)
  })
  await page.readRemote()
  await page.prepare()
  assert.deepEqual(calls[1].body.values, { description: '新简介' })
  assert.equal(Object.hasOwn(calls[1].body, 'order'), false)
  assert.equal(page.previewDifferences[0].before, '旧简介')
  assert.equal(page.previewDifferences[0].after, '新简介')
})

test('提交结果不明时关闭旧预览且双击不重复写入', async () => {
  const { page } = await render({ accountId: '404575122', bvid: 'BV1234567890', selectedFields: ['description'], localValues: { description: '新简介' } })
  page.preview = { id: 'plan', bvid: 'BV1234567890', preview: { before: { settings: { description: '旧简介' } }, after: { settings: { description: '新简介' } } } }
  page.showPreview = true
  let writes = 0
  mock.method(globalThis, 'fetch', async (url: any) => {
    if (String(url).endsWith('/commit')) { writes++; return { ok: false, json: async () => ({ code: 'PUBLISH_WRITE_UNKNOWN', message: '请核对B站' }) } }
    throw new Error(`非预期请求：${url}`)
  })
  const first = page.commit()
  const second = page.commit()
  await Promise.all([first, second])
  assert.equal(writes, 1)
  assert.equal(page.showPreview, false)
  assert.equal(page.preview, null)
})
