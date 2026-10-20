import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, ResultPanel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  ResultPanel = (await server.ssrLoadModule('/src/components/PublishResultPanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止结果组件发起网络请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })

function publication(status = 'draft', receipt: any = {}) {
  return { id: 'publication-test', status, receipt, progress: 0, error: '',
    snapshot: { account_name: '发布账号', parts: [{ artifact_id: 12, title: '原声版' }] } }
}
async function render(options: any = {}) {
  let page: any
  const events: string[] = [], row = reactive(options.publication || publication())
  const app = createSSRApp({ render: () => h(ResultPanel, { ...options, publication: row,
    onPause: () => events.push('pause'), onCheck: () => events.push('check'),
    onCollection: () => events.push('collection'), onCopy: () => events.push('copy'),
    onPrepareReview: () => events.push('prepareReview'),
    onChaptersRetry: (enable:boolean) => events.push(enable?'chaptersEnable':'chaptersRetry'),
    onChaptersReconcile: () => events.push('chaptersReconcile'),onChaptersRefresh: () => events.push('chaptersRefresh') }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'PublishResultPanel') page = this.$.setupState } })
  return { html: await renderToString(app), page, events, row }
}
function buttons(html: string) {
  return [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => ({ html: match[0],
    text: match[0].replace(/<[^>]*>/g, '').trim() }))
}
function button(html: string, text: string) { return buttons(html).find(item => item.text === text)?.html || '' }
function disabled(html: string) { return /\sdisabled(?:=|\s|>)/.test(html) }
const bv = { bvid: 'BV1234567890', state_desc: '审核完成' }

test('自动章节交付按各P折叠展示实际整理清单与合并来源，成功不误称尚未写入',async()=>{
  for(const status of ['completed','failed']){
    const row:any=publication('published',bv)
    row.chapter_sync={status,enabled:true,parts:[{cid:11,title:'原声版',status,
      items:[{title:'主题概览',start_time:0,end_time:120,source_indices:[0,1,2]}],
      adaptation:{source_count:21,output_count:1,merged_count:20,notes:['相邻主题合并']}}]}
    const result=await render({publication:row})
    assert.match(result.html,/查看本次整理清单 · 21 条源章节 → 1 章/)
    assert.match(result.html,/主题概览/);assert.match(result.html,/0:00–2:00/)
    assert.match(result.html,/合并自源章节 1–3/);assert.match(result.html,/相邻主题合并/)
    assert.doesNotMatch(result.html,/尚未写入B站/)
    assert.equal((result.html.match(/查看本次整理清单/g)||[]).length,1)
  }
})

test('草稿只有第4步占位，尚未上传且不出现重新发布或平台动作', async () => {
  const result = await render()
  assert.match(result.html, /4 · 进度与回执/)
  assert.match(result.html, /尚未启动上传/)
  assert.match(result.html, /本地草稿/)
  assert.doesNotMatch(result.html, /class="result-runtime"|data-publication-progress|正在处理当前任务|进行中/)
  assert.equal(buttons(result.html).length, 0)
  for (const action of ['pauseUpload', 'checkState', 'retryCollection', 'copyDraft', 'prepareReview']) result.page[action]()
  assert.deepEqual(result.events, [])
})

test('上传及封面阶段展示真实进度，100%不表示已通过审核；暂停仍可在项目锁定时执行', async () => {
  for (const phase of ['uploading', 'merging', 'cover', 'submitting']) {
    const row: any = publication(phase === 'submitting' ? 'submitting' : 'uploading')
    row.progress_detail = { phase, loaded: phase === 'uploading' ? 300 : 1000,
      total: 1000, current_part: 1, current_title: '原声版', completed_parts: 0, total_parts: 1 }
    const result = await render({ publication: row, projectLocked: true })
    assert.match(result.html, /data-publication-progress/)
    assert.match(result.html, phase === 'uploading' ? /视频文件 30%/ : /视频文件 100%/)
    assert.doesNotMatch(result.html, /已公开发布|已通过审核/)
    assert.match(result.html, /P1 \/ 1 · 原声版/)
    result.page.pauseUpload()
    assert.deepEqual(result.events, phase === 'submitting' ? [] : ['pause'])
  }
})

test('排队中可暂停；有旧进度但无详细字节时保留任务阶段提示', async () => {
  const result = await render({ publication: publication('queued') })
  assert.match(result.html, /等待发布任务/)
  assert.match(result.html, /正在准备本次稿件的上传任务/)
  assert.match(result.html, /class="result-runtime"|data-publication-progress/)
  assert.ok(button(result.html, '暂停上传'))
  result.page.pauseUpload(); assert.deepEqual(result.events, ['pause'])
  const busy = await render({ publication: publication('queued'), busy: true })
  assert.ok(disabled(button(busy.html, '暂停上传')))
  busy.page.pauseUpload(); assert.deepEqual(busy.events, [])
})

test('退回稿件将另发新BV收在次级操作内，保留与原稿更新的区别', async () => {
  const row = publication('rejected', { ...bv, reject_reason: '请修订不当字幕' })
  row.error = '查询平台状态时网络失败'
  const result = await render({ publication: row, canCreateReviewDraft: true })
  assert.match(result.html, /发布状态与回执/)
  assert.match(result.html, /请修订不当字幕/)
  assert.match(result.html, /查询平台状态时网络失败/)
  assert.match(result.html, /class="revision-workflow"/)
  assert.equal(buttons(result.html).filter(item => item.text === '使用审查修订版新建草稿').length, 1)
  assert.ok(result.html.indexOf('另发新稿与官网操作') < result.html.indexOf('使用审查修订版新建草稿'))
  assert.match(result.html, /发布后得到新BV；原BV及其视频保持原状/)
  assert.match(result.html, /不会自动换成审查修订版/)
  assert.match(result.html, /到官网替换原BV视频/)
  result.page.prepareReview(); result.page.copyDraft(); result.page.checkState()
  assert.deepEqual(result.events, ['prepareReview', 'copy', 'check'])
})

test('已发布稿件将重新发布与复制放入默认折叠区域，保留平台查询和合集结果', async () => {
  const result = await render({ publication: publication('published', { ...bv, collection_joined: true }), canCreateReviewDraft: true })
  assert.match(result.html, /已公开发布/)
  assert.match(result.html, /已加入合集/)
  assert.match(result.html, /BV1234567890/)
  assert.match(result.html, /审核完成/)
  assert.match(result.html, /<details[^>]*class="republication-options"/)
  assert.doesNotMatch(result.html, /<details[^>]*\sopen(?:=|\s|>)/)
  assert.ok(result.html.indexOf('另发新稿与官网操作') < result.html.indexOf('使用审查修订版新建草稿'))
  assert.equal(buttons(result.html).filter(item => item.text === '使用审查修订版新建草稿').length, 1)
  assert.ok(button(result.html, '查询平台状态'))
})

test('已上传只是待投稿，结果区不复制父级上传或投稿按钮', async () => {
  const result = await render({ publication: publication('uploaded') })
  assert.match(result.html, /视频与封面已上传，尚未正式投稿/)
  assert.ok(button(result.html, '复制为新草稿'))
  assert.doesNotMatch(buttons(result.html).map(item => item.text).join('\n'), /确认投稿|上传并投稿|仅上传文件|暂停上传/)
})

test('投稿结果未知时即使误传修订能力，也不提供复制或修订，仍可查询已知BV', async () => {
  const result = await render({ publication: publication('unknown', bv), canCreateReviewDraft: true })
  assert.match(result.html, /投稿结果尚未确认/)
  assert.doesNotMatch(buttons(result.html).map(item => item.text).join('\n'), /复制|修订|暂停|上传|投稿/)
  result.page.copyDraft(); result.page.prepareReview(); result.page.checkState()
  assert.deepEqual(result.events, ['check'])
})

test('只读记录省略制作编号及所有写操作，只保留状态查询且遵守忙态', async () => {
  for (const status of ['draft', 'queued', 'uploading', 'rejected', 'published', 'collection_pending']) {
    const result = await render({ publication: publication(status, bv), recordOnly: true, canCreateReviewDraft: true })
    assert.match(result.html, /发布状态与回执/)
    assert.doesNotMatch(result.html, /4 · 进度与回执/)
    assert.deepEqual(buttons(result.html).map(item => item.text), ['查询平台状态'])
    result.page.pauseUpload(); result.page.retryCollection(); result.page.copyDraft(); result.page.prepareReview(); result.page.checkState()
    assert.deepEqual(result.events, ['queued', 'uploading'].includes(status) ? [] : ['check'])
  }
  const busy = await render({ publication: publication('published', bv), recordOnly: true, busy: true })
  assert.ok(disabled(button(busy.html, '查询平台状态')))
  busy.page.checkState(); assert.deepEqual(busy.events, [])
})

test('复制与审查修订按钮和处理函数都阻止未保存与并发动作', async () => {
  for (const flag of ['projectLocked', 'busy', 'confirming', 'dirty', 'reviewDirty', 'reviewBusy']) {
    const result = await render({ publication: publication('rejected', bv), canCreateReviewDraft: true, [flag]: true })
    assert.ok(disabled(button(result.html, '复制为新草稿')), flag)
    assert.ok(disabled(button(result.html, '使用审查修订版新建草稿')), flag)
    result.page.copyDraft(); result.page.prepareReview(); assert.deepEqual(result.events, [], flag)
  }
  const copying = await render({ publication: publication('paused'), copying: true })
  assert.ok(disabled(button(copying.html, '正在复制草稿')))
  copying.page.copyDraft(); assert.deepEqual(copying.events, [])
  const checking = await render({ publication: publication('rejected', bv), canCreateReviewDraft: true, reviewDraftChecking: true })
  assert.match(checking.html, /正在核对修订文件/)
  assert.ok(disabled(button(checking.html, '使用审查修订版新建草稿')))
  checking.page.prepareReview(); assert.deepEqual(checking.events, [])
})

test('合集失败只显示合集重试并保留BV，锁定或忙态禁止重试但不禁止只读查询', async () => {
  const result = await render({ publication: publication('collection_pending', bv) })
  assert.ok(button(result.html, '重试加入合集'))
  result.page.retryCollection(); assert.deepEqual(result.events, ['collection'])
  for (const flag of ['projectLocked', 'busy']) {
    const locked = await render({ publication: publication('collection_pending', bv), [flag]: true })
    assert.ok(disabled(button(locked.html, '重试加入合集')))
    locked.page.retryCollection(); locked.page.checkState()
    assert.deepEqual(locked.events, flag === 'projectLocked' ? ['check'] : [])
  }
})

test('各分P保留平台标题和退回原因，显示转码码0且转义文本，不修改回执', async () => {
  const receipt = { ...bv, videos: [
    { cid: 11, title: '原声<script>版</script>', state_desc: '审核退回', xcode_state: 0, reject_reason: '字幕需修订' },
    { cid: 12, title: '配音版', xcode_state: null }
  ] }
  const before = JSON.stringify(receipt)
  const result = await render({ publication: publication('rejected', receipt) })
  assert.match(result.html, /各分P审核 \/ 转码信息/)
  assert.match(result.html, /P1 · 原声&lt;script&gt;版&lt;\/script&gt;/)
  assert.match(result.html, /P2 · 配音版/)
  assert.match(result.html, /转码状态码 0/)
  assert.match(result.html, /字幕需修订/)
  assert.match(result.html, /平台未提供状态说明/)
  assert.doesNotMatch(result.html, /转码状态码 null|<script>/)
  assert.equal(JSON.stringify(result.row.receipt), before)
})

test('动作处理按最新状态再次核对，已变为上传状态的旧按钮不能查询或复制', async () => {
  const result = await render({ publication: publication('published', bv), canCreateReviewDraft: true })
  result.row.status = 'uploading'
  result.page.checkState(); result.page.copyDraft(); result.page.prepareReview(); result.page.retryCollection()
  assert.deepEqual(result.events, [])
  result.page.pauseUpload(); assert.deepEqual(result.events, ['pause'])
})

test('章节失败独立显示，重试仅产生章节事件，主投稿成功状态保持不变', async () => {
  const row:any=publication('published',bv)
  row.chapter_sync={status:'failed',enabled:true,error:'章节标题未通过校验',parts:[{cid:11,title:'原声版',status:'completed'},{cid:12,title:'配音版',status:'failed',error:'稍后重试'}]}
  const result=await render({publication:row})
  assert.match(result.html,/已公开发布/);assert.match(result.html,/视频章节设置失败/)
  assert.match(result.html,/P1 · 原声版：视频章节已设置/);assert.match(result.html,/章节标题未通过校验/)
  assert.ok(button(result.html,'重试设置章节'))
  result.page.retryChapters();result.page.refreshChapters()
  assert.deepEqual(result.events,['chaptersRetry','chaptersRefresh']);assert.equal(result.row.status,'published')
})

test('章节结果未知只提供核对，迟到的重试动作和手工补充都不发写事件', async () => {
  const row:any=publication('published',bv);row.chapter_sync={status:'unknown',enabled:true}
  const result=await render({publication:row})
  assert.match(result.html,/章节结果待核对/);assert.ok(button(result.html,'核对章节'))
  assert.equal(button(result.html,'重试设置章节'),'');assert.equal(button(result.html,'补充视频章节'),'')
  result.page.retryChapters();result.page.reconcileChapters()
  assert.deepEqual(result.events,['chaptersReconcile'])
})
test('原稿更新接管章节后优先展示接管阶段，不显示历史成功、旧重试或旧执行进度', async () => {
  const row:any=publication('published',bv)
  row.execution={schema:1,state:'attention',detail:'原投稿的旧审核退回诊断'}
  row.chapter_sync={status:'superseded',enabled:true,phase:'章节已由原稿更新任务接管',superseded_by_update:'update-new',
    parts:[{cid:11,title:'旧原声版',status:'completed',items:[{title:'旧章节',start_time:0}]}]}
  const result=await render({publication:row})
  assert.match(result.html,/章节已由原稿更新任务接管/);assert.match(result.html,/“更新原稿件”/)
  assert.doesNotMatch(result.html,/视频章节已设置|旧章节|旧原声版|重试设置章节|补充视频章节|当前任务需要处理|原投稿的旧审核退回诊断/)
  result.page.retryChapters();result.page.reconcileChapters();assert.deepEqual(result.events,[])
  assert.equal(result.page.chapterExecution,null);assert.deepEqual(result.page.chapters.parts,[])
})
test('换片提交核验前保留原投稿诊断，核验后隐藏旧执行进度但不改冻结数据或新更新任务', async () => {
  for (const state of ['none', 'prepared', 'failed', 'verified', 'metadata-only']) {
    const row:any=publication('published',bv)
    row.execution={schema:1,state:'attention',detail:'原投稿审核退回时的诊断'}
    if(state!=='none') row.latest_update={id:'replacement-task',status:state==='verified'?'delivering':state,
      preview:{parts:[{old_cid:11,replaced:state!=='metadata-only'}]},
      execution:{stages:{edit:{status:['verified','metadata-only'].includes(state)?'completed':'pending'},chapters:{status:'running'}},
        phase:'新任务正在设置章节'}}
    const before=JSON.stringify(row)
    const result=await render({publication:row})
    if(state==='verified') {
      assert.doesNotMatch(result.html,/当前任务需要处理|原投稿审核退回时的诊断|class="result-runtime"/)
      assert.equal(result.page.originalRuntimeSuperseded,true)
    } else {
      assert.match(result.html,/当前任务需要处理/);assert.match(result.html,/原投稿审核退回时的诊断/)
      assert.equal(result.page.originalRuntimeSuperseded,false)
    }
    assert.equal(JSON.stringify(result.row),before)
    if(row.latest_update) assert.equal(result.row.latest_update.execution.phase,'新任务正在设置章节')
  }
})

test('历史关闭记录需显式补充，开启但尚无回执显示等待审核，源章为空显示跳过', async () => {
  const row:any=publication('published',bv);row.snapshot.chapter_options={enabled:false}
  const old=await render({publication:row})
  assert.match(old.html,/本次投稿未启用视频章节/);assert.ok(button(old.html,'补充视频章节'))
  old.page.retryChapters();assert.deepEqual(old.events,['chaptersEnable'])
  row.snapshot.chapter_options.enabled=true
  const waiting=await render({publication:row});assert.match(waiting.html,/等待审核后设置章节/)
  assert.equal(button(waiting.html,'补充视频章节'),'')
  row.chapter_sync={status:'skipped',enabled:true,reason:'源视频没有章节'}
  const skipped=await render({publication:row});assert.match(skipped.html,/本次跳过视频章节/)
  assert.match(skipped.html,/源视频没有章节/);assert.equal(button(skipped.html,'重试设置章节'),'')
})

test('章节写操作遵守只读、忙态、锁定与未保存保护，仍可单独刷新记录', async () => {
  const row:any=publication('published',bv);row.chapter_sync={status:'failed',enabled:true}
  for(const flag of ['recordOnly','projectLocked','busy','dirty','confirming']){
    const result=await render({publication:row,[flag]:true})
    result.page.retryChapters();assert.deepEqual(result.events,[],flag)
    if(flag==='recordOnly')assert.equal(button(result.html,'重试设置章节'),'')
    else assert.ok(disabled(button(result.html,'重试设置章节')),flag)
  }
})

test('状态与更多操作可分别展示，只有状态区拥有回执锚点，写操作仍遵守锁定', async () => {
  const row = publication('rejected', { ...bv, reject_reason: '请修订字幕' })
  const state = await render({ publication: row, surface: 'status', canCreateReviewDraft: true })
  assert.match(state.html, /id="publish-receipt"/)
  assert.match(state.html, /审核退回原因/)
  assert.match(state.html, /查询平台状态/)
  assert.doesNotMatch(state.html, /另发新稿与官网操作|使用审查修订版新建草稿|复制为新草稿/)
  const more = await render({ publication: row, surface: 'more', canCreateReviewDraft: true, dirty: true })
  assert.match(more.html, /更多操作/)
  assert.doesNotMatch(more.html, /id="publish-receipt"|审核退回原因|查询平台状态/)
  assert.match(more.html, /另发新稿与官网操作/)
  assert.ok(disabled(button(more.html, '使用审查修订版新建草稿')))
  more.page.prepareReview(); more.page.copyDraft()
  assert.deepEqual(more.events, [])
})
