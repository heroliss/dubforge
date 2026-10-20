import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { NMessageProvider } from 'naive-ui'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Chapters: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Chapters = (await server.ssrLoadModule('/src/components/BiliChaptersPanel.vue')).default
})
after(async () => { mock.restoreAll(); await server?.close() })

async function render(props: any, remote: any[], control: {
  request?: (url: string, options: any) => any
  setup?: (page: any, instance: any) => void
  instance?: (instance: any) => void
} = {}) {
  mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const body = options?.body ? JSON.parse(options.body) : {}
    const custom = await control.request?.(String(url), options)
    const data = custom !== undefined ? custom : String(url).endsWith('/read/chapters') ? { parts: remote }
      : { items: remote.find(row => Number(row.cid) === body.cid)?.items || [] }
    return { ok: true, json: async () => data } as Response
  })
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Chapters,
    { accountId: '123', bvid: 'BV1234567890', aid: 1, ...props }) }) })
  app.mixin({ created() {
    control.instance?.(this.$)
    if (this.$options.__name === 'BiliChaptersPanel') {
      page = this.$.setupState
      control.setup?.(page, this.$)
    }
  } })
  setup(app)
  await renderToString(app)
  await page.read()
  return page
}

const preferredParts = [
  { chapter_cid: '101', title: '剪辑版', chapter_items: [{ title: '正文', start_time: 0, end_time: 20 }],
    chapter_timeline: { kind: 'cut', label: '按所选成片剪辑时间轴调整（删去 10 秒）' } },
  { chapter_cid: '102', title: '原片', chapter_items: [{ title: '正文', start_time: 10, end_time: 30 }],
    chapter_timeline: { kind: 'source', label: '源视频时间轴' } },
]

test('多P按上传cid各自载入剪辑候选，远端调整分P顺序也不串时间轴', async () => {
  const page = await render({ preferredParts }, [
    { cid: 102, title: '原片', duration: 30, items: [] },
    { cid: 101, title: '剪辑版', duration: 20, items: [] },
  ])
  assert.equal(page.parts[0].items[0].start_time, 10)
  assert.equal(page.parts[1].items[0].start_time, 0)
  assert.match(page.parts[1].timelineNote, /剪辑时间轴/)
})

test('已有手工B站章节保留原时间，不被源章或剪辑候选覆盖', async () => {
  const existing = [{ title: '人工微调', start_time: 4, end_time: 12 }]
  const page = await render({ preferredParts }, [{ cid: 101, title: '剪辑版', duration: 20, items: existing }])
  assert.deepEqual(page.parts[0].items.map((row: any) => ({ ...row })), existing)
  assert.match(page.parts[0].timelineNote, /保留远端时间轴/)
})

test('自动任务已整理候选保留来源和摘要，远端现有章节仍优先',async()=>{
  const items=[{title:'已整理',start_time:0,end_time:20,source_indices:[0,1,2]}]
  const adaptation={source_count:21,output_count:1,merged_count:20}
  const preferredParts=[{chapter_cid:101,title:'正文',chapter_items:items,chapter_adaptation:adaptation,
    chapter_timeline:{kind:'adapted',label:'本次自动任务已整理的成片章节'}}]
  const page=await render({preferredParts},[{cid:101,title:'正文',duration:20,items:[]}])
  assert.deepEqual(page.chapters,items);assert.deepEqual(page.activePart.adaptation,adaptation)
  assert.match(page.activePart.timelineNote,/本次自动任务已整理/)
  const remote=await render({preferredParts},remotePart)
  assert.deepEqual(remote.chapters,remotePart[0].items);assert.equal(remote.activePart.adaptation,undefined)
})

test('陌生分P不盲用全局源章，并显示需要人工核对', async () => {
  const page = await render({ preferredParts, preferredChapters: [{ title: '错误候选', start_time: 10 }] },
    [{ cid: 999, title: '后来新增的P', duration: 20, items: [] }])
  assert.deepEqual(page.parts[0].items, [])
  assert.match(page.parts[0].timelineNote, /未能核对/)
})

const remotePart = [{ cid: 101, title: '剪辑版', duration: 20,
  items: [{ title: '正文', start_time: 0, end_time: 20 }] }]
const chapterPlan = { id: 'chapter-plan', after: { items: remotePart[0].items, permanent: true } }

// 状态竞态使用真实客户端 watch；空宿主省去浏览器布局，按钮仍在 SSR 用真实组件核对。
const host = createRenderer<any, any>({
  createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, patchProp() {}, parentNode: node => node.parent,
  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
})
async function client(t: any, request: (url: string) => any) {
  mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const custom = await request(String(url))
    const body = JSON.parse(options.body)
    const data = custom !== undefined ? custom : String(url).endsWith('/read/chapters') ? { parts: remotePart }
      : { items: remotePart.find(row => row.cid === body.cid)?.items || [] }
    return { ok: true, json: async () => data } as Response
  })
  const props = reactive({ accountId: '123', bvid: 'BV1234567890', aid: 1, disabled: false })
  const Logic = { ...Chapters, render: () => null, ssrRender: undefined }
  let page: any
  const app = host.createApp({ render: () => h(NMessageProvider, null, { default: () => h(Logic, { ...props }) }) })
  app.provide(ssrContextKey, {})
  app.mixin({ created() { if (this.$options.__name === 'BiliChaptersPanel') page = this.$.setupState } })
  app.mount({ children: [] }); t.after(() => app.unmount())
  await page.read()
  return { page, props }
}

test('禁用时计划与确认处理均不发写请求，保留只读章节核对和禁用确认按钮', async () => {
  const calls: string[] = [], instances: any[] = []
  const page = await render({ disabled: true }, remotePart, {
    request: url => { calls.push(url) },
    setup: page => { page.syncPreview = chapterPlan },
    instance: instance => { instances.push(instance) },
  })
  const confirm = instances.find(instance => instance.type.name === 'Button'
    && instance.vnode.props?.onClick === page.commitSync)
  assert.ok(confirm, '未找到真实章节确认按钮')
  assert.equal(confirm.vnode.props.disabled, true)
  calls.length = 0
  await page.prepareSync(); await page.commitSync(); await page.read()
  assert.ok(calls.length > 0, '禁用写入后仍应允许读取章节')
  assert.ok(calls.every(url => url.includes('/studio/read/')), '禁用时不能创建计划或提交章节')
})

test('已打开计划在禁用时失效，恢复可写后迟到确认也不提交', async t => {
  const calls: string[] = []
  const { page, props } = await client(t, url => { calls.push(url); if (url.endsWith('/studio/changes')) return chapterPlan })
  await page.prepareSync()
  assert.equal(page.syncPreview.id, chapterPlan.id)
  const lateCommit = page.commitSync
  props.disabled = true; await nextTick()
  assert.equal(page.syncPreview, null)
  await lateCommit()
  props.disabled = false; await nextTick()
  await lateCommit()
  assert.equal(calls.filter(url => url.endsWith('/commit')).length, 0)
})

test('禁用后迟到返回的预览不能恢复确认计划，重新启用也必须重建', async t => {
  const calls: string[] = []
  let resolvePlan: (value: any) => void = () => {}
  const delayed = new Promise(resolve => { resolvePlan = resolve })
  const { page, props } = await client(t, url => { calls.push(url); if (url.endsWith('/studio/changes')) return delayed })
  const preparing = page.prepareSync()
  props.disabled = true; await nextTick()
  props.disabled = false; await nextTick()
  resolvePlan(chapterPlan)
  await preparing
  assert.equal(page.syncPreview, null)
  assert.equal(page.syncLoading, false)
  await page.commitSync()
  assert.equal(calls.filter(url => url.endsWith('/commit')).length, 0)
})

test('允许写入时仍按预览确认提交并回读核验', async () => {
  const calls: { url: string; body: any }[] = []
  const page = await render({}, remotePart, {
    request: (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) })
      if (url.endsWith('/studio/changes')) return chapterPlan
      if (url.endsWith('/commit')) return { status: 'done', result: { verified: true } }
    },
  })
  await page.prepareSync(); await page.commitSync()
  const writes = calls.filter(call => call.url.includes('/studio/changes'))
  assert.equal(writes.length, 2)
  assert.equal(writes[0].body.kind, 'chapters')
  assert.deepEqual(writes[0].body.values.items, remotePart[0].items)
  assert.equal(writes[1].url, '/api/publishing/accounts/123/studio/changes/chapter-plan/commit')
  assert.deepEqual(writes[1].body, { confirmed: true })
  assert.equal(page.syncPreview, null)
  assert.match(page.syncResult, /已写入并完成回读核验/)
  assert.ok(calls.slice(calls.indexOf(writes[1]) + 1).some(call => call.url.includes('/studio/read/')))
})

test('手动章节提交未核验时退出旧确认框，错误可见且旧计划不能重复提交',async()=>{
  const calls:string[]=[]
  const page=await render({},remotePart,{request:url=>{
    calls.push(url)
    if(url.endsWith('/studio/changes'))return chapterPlan
    if(url.endsWith('/commit'))return {status:'unknown',result:{verified:false}}
  }})
  await page.prepareSync();await page.commitSync()
  assert.equal(page.syncPreview,null);assert.equal(page.syncLoading,false)
  assert.match(page.syncError,/尚未完成核验.*到B站核对/)
  await page.commitSync()
  assert.equal(calls.filter(url=>url.endsWith('/commit')).length,1)
})

test('切换稿件后迟到的章节提交回执不污染新稿件进度或发起新稿件读取',async t=>{
  const calls:string[]=[]
  let release:(value:any)=>void=()=>{}
  const {page,props}=await client(t,url=>{
    calls.push(url)
    if(url.endsWith('/studio/changes'))return chapterPlan
    if(url.endsWith('/commit'))return new Promise(resolve=>{release=resolve})
  })
  await page.prepareSync();const pending=page.commitSync()
  props.bvid='BV0000000000';await nextTick()
  for(let i=0;i<10;i++)await Promise.resolve()
  const reads=calls.filter(url=>url.includes('/studio/read/')).length
  release({status:'done',result:{verified:true}});await pending
  assert.equal(page.syncPreview,null);assert.equal(page.syncResult,'');assert.equal(page.syncError,'')
  assert.equal(page.syncLoading,false)
  assert.equal(calls.filter(url=>url.includes('/studio/read/')).length,reads)
})

test('21条源章先在本地AI合并再生成计划，保留来源且不直接提交B站',async()=>{
  const calls:{url:string;body:any}[]=[]
  const source=Array.from({length:21},(_,index)=>({title:`Topic ${index+1}`,start_time:index*10,end_time:(index+1)*10}))
  const items=[{title:'主题总览',start_time:0,end_time:210,source_indices:Array.from({length:21},(_,index)=>index)}]
  const adaptation={source_count:21,output_count:1,merged_count:20,rules:{max_chapters:10},notes:['合并相邻主题']}
  const page=await render({},[{cid:101,title:'正文',duration:210,items:source}],{request:(url,options)=>{
    calls.push({url,body:JSON.parse(options.body)})
    if(url.endsWith('/chapters/adapt'))return {items,adaptation}
  }})
  assert.match(page.issues[0],/超过 10 个上限/)
  calls.length=0;await page.prepareSync();assert.equal(calls.length,0)
  await page.adapt()
  assert.deepEqual(calls,[{url:'/api/publishing/chapters/adapt',body:{items:source,duration:210,translate:true,instruction:''}}])
  assert.deepEqual(page.chapters,items);assert.deepEqual(page.activePart.adaptation,adaptation)
  assert.equal(page.invalidCount,0);assert.equal(source.length,21);assert.equal(page.syncPreview,null)
})

test('原语言整理可用，失败时保留全部源章并可再次操作',async()=>{
  const calls:any[]=[]
  const page=await render({},remotePart,{request:(url,options)=>{
    if(url.endsWith('/chapters/adapt')){calls.push(JSON.parse(options.body));throw new Error('模型超时')}
  }})
  page.translateTitles=false
  await page.adapt()
  assert.equal(calls[0].translate,false);assert.deepEqual(page.chapters,remotePart[0].items)
  assert.equal(page.adapting,false);assert.match(page.syncError,/模型超时/)
})

test('禁用及整理忙态不会发起额外整理或写入计划',async()=>{
  const calls:string[]=[]
  const disabled=await render({disabled:true},remotePart,{request:url=>{calls.push(url)}})
  calls.length=0;await disabled.adapt();assert.equal(calls.length,0)
  let release:(value:any)=>void=()=>{}
  const page=await render({},remotePart,{request:url=>{
    calls.push(url)
    if(url.endsWith('/chapters/adapt'))return new Promise(resolve=>{release=resolve})
  }})
  calls.length=0;const pending=page.adapt();assert.equal(page.adapting,true)
  const before=JSON.stringify(page.chapters)
  await page.adapt();await page.prepareSync();page.add();page.remove(0)
  assert.equal(JSON.stringify(page.chapters),before);assert.equal(calls.length,1)
  release({items:remotePart[0].items});await pending
  assert.equal(page.adapting,false)
})

test('切换稿件后迟到的整理不会污染新稿件',async t=>{
  let release:(value:any)=>void=()=>{}
  const {page,props}=await client(t,url=>{
    if(url.endsWith('/chapters/adapt'))return new Promise(resolve=>{release=resolve})
  })
  const pending=page.adapt();props.bvid='BV0000000000';await nextTick()
  for(let i=0;i<10;i++)await Promise.resolve()
  release({items:[{title:'迟到结果',start_time:0,end_time:20}]});await pending
  assert.equal(page.chapters[0].title,'正文');assert.equal(page.adapting,false);assert.equal(page.syncPreview,null)
})
