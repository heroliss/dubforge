import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'

const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/OutputCutsEditor.vue', import.meta.url), 'utf8'))
const outputCutsTemplate = descriptor.template?.content || ''
const outputCutsScript = descriptor.scriptSetup?.content || ''
const result = await build({ stdin: { contents: `import { reactive, unmountHooks } from 'vue';
  import { dialogs } from 'naive-ui';
  const fixtureProps=reactive({uid:'fixture',running:true,disabled:false,targetArtifactName:undefined,targetArtifactHash:undefined}),defineProps=()=>fixtureProps;
  const emitted=[],defineEmits=()=>((name,...args)=>emitted.push({name,args}));
  ${descriptor.scriptSetup!.content}
  export {fixtureProps,saved,artifactContext,artifactMappingReady,hasCutChanges,createTimelineVersion,timelineEncodingText,currentTimelinePlan,loadArtifactContext,enabled,ranges,dirty,loading,saving,error,video,current,referenceVideo,markStart,markEnd,markStartText,markEndText,markerTextError,markerValidation,markReason,skipCuts,effective,previewModeLabel,previewModeNote,playbackLinkState,baselineId,selectedBaselineName,baselinePreviewUrl,targetPreviewUrl,referenceCuts,previewTargetCuts,referenceSync,referenceCurrent,audioSource,audioSourceOptions,applyLinkedAudioPolicy,onSourceVolumeChange,onReferenceVolumeChange,onSourceSeeking,onReferenceSeeking,onSourceSeekStart,onSourceSeek,onSourceSeekEnd,onReferenceSeekStart,onReferenceSeek,onReferenceSeekEnd,seekSyncOrigin,activeSeekOrigin,playbackOrigin,pendingPlaybackOrigin,syncPlaybackClock,startPlaybackSync,clearPlaybackSync,reconcilePlaybackLink,onPlaybackIntent,mirrorReferencePlayback,retryPendingPlayback,onSourcePlay,onReferencePlay,onSourcePause,onReferencePause,load,save,persist,adopt,reload,addRange,commitMarker,setMarkerValue,onTimeUpdate,onSourceTimeUpdate,onReferenceTimeUpdate,skipSourceCutIfNeeded,skipReferenceCutIfNeeded,changeBaseline,mappedOutputTime,mappedSourceTime,unmountHooks,dialogs,emitted};
  export {playbackTimeline,previewFrameRate,referenceDuration,previewPosition,previewScope,previewSourceWindow,startVirtualPreview,virtualPreviewStatus,virtualPreviewDuration,virtualPreviewFrameRate,virtualPreviewSegments,virtualDonorKind,virtualDonorVersionId,onReferenceMetadata,resetVirtualPreview,seek,setRangeInputInvalid,removeRange,hasInvalidRangeInput};
  export {listDirty,hasPendingMarker,pendingMarkerReady,markerActionError,markerSignature,clearMarker,includePendingMarker,previewDraft,setPreviewCuts};`, loader: 'ts', resolveDir: `${root}/src/components` },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'boundaries', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
    b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue'
      ? `export {ref,computed,watch,reactive} from '@vue/reactivity';export const nextTick=()=>Promise.resolve();export const unmountHooks=[],onMounted=()=>{},onBeforeUnmount=f=>unmountHooks.push(f);`
      : a.path === 'naive-ui' ? `export const dialogs=[];export const useDialog=()=>({warning(opts){dialogs.push(opts);return{destroy(){}}}});export const useMessage=()=>({success(){},warning(){},info(){}});` : 'export default {};', resolveDir: root }))
  } }] })
let serial = 0
async function fixture(t: any) {
  const page = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text + `\n//${serial++}`).toString('base64')}`)
  t.after(() => page.unmountHooks.forEach((hook: () => void) => hook()))
  return page
}
function state(extra: any = {}) { const cuts = {source_duration: extra.duration ?? 60, ranges: extra.enabled ? (extra.ranges || []).filter((r: any)=>r.enabled!==false).map((r: any)=>({start:r.start,end:r.end})) : [], output_duration: extra.output_duration ?? 60}; return { artifact_cuts: cuts, artifact_validation: {valid:true}, revision: 'r1', source_fingerprint: 'source-1', duration: 60, enabled: false, ranges: [], effective_ranges: [], removed_duration: 0, output_duration: 60, warnings: [], source_url: '/video', source_changed: false, ...extra } }
function response(data: any, status = 200) { return new Response(JSON.stringify(data), { status }) }
function row() { return { id: 'cut-1', start: 5, end: 10, reason: '广告', enabled: true } }

test('仅标记起终点也属于未保存草稿，清除标记后解除离页保护', async t => {
  const p = await fixture(t)
  p.adopt(state())
  assert.equal(p.listDirty.value, false); assert.equal(p.hasPendingMarker.value, false)
  p.markStartText.value = '00:00:05.000'
  assert.equal(p.listDirty.value, false); assert.equal(p.hasPendingMarker.value, true)
  assert.equal(p.pendingMarkerReady.value, false); assert.equal(p.dirty.value, true)
  assert.ok(p.emitted.some((event: any) => event.name === 'dirty-change' && event.args[0] === true))
  p.markEndText.value = '00:00:10.000'
  assert.equal(p.pendingMarkerReady.value, true)
  const signature = p.markerSignature.value
  p.markReason.value = '广告'
  assert.notEqual(p.markerSignature.value, signature)
  p.clearMarker()
  assert.equal(p.markStartText.value, '00:00:00.000'); assert.equal(p.markEndText.value, '')
  assert.equal(p.markReason.value, ''); assert.equal(p.hasPendingMarker.value, false)
  assert.equal(p.dirty.value, false)
})

test('未加入清单的标记也阻止直接重新读取，必须先确认放弃', async t => {
  const p = await fixture(t); let requests = 0
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  p.reload()
  assert.equal(requests, 0); assert.equal(p.dialogs.length, 1)
  assert.match(p.dialogs[0].title, /放弃/)
  assert.equal(p.markStart.value, 5); assert.equal(p.markEnd.value, 10)
})

test('忘记加入清单直接保存会收录标记，并保留接续起点且重复保存不写空请求', async t => {
  const p = await fixture(t), calls: any[] = []
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10); p.markReason.value = ' 广告 '
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    const body = JSON.parse(init.body); calls.push({ url: String(url), method: init.method, body })
    return response(state({ revision: 'r2', enabled: body.enabled, ranges: body.ranges }))
  })
  await p.save()
  assert.equal(calls.length, 1); assert.equal(calls[0].method, 'PUT')
  assert.equal(calls[0].body.ranges.length, 1)
  assert.deepEqual({ ...calls[0].body.ranges[0], id: 'cut' }, { id: 'cut', start: 5, end: 10, reason: '广告', enabled: true })
  assert.equal(p.markStart.value, 10); assert.equal(p.markStartText.value, '00:00:10.000')
  assert.equal(p.markEndText.value, ''); assert.equal(p.markReason.value, '')
  assert.equal(p.hasPendingMarker.value, false); assert.equal(p.dirty.value, false)
  await p.save(); await p.persist()
  assert.equal(calls.length, 1)
  assert.equal(p.markStart.value, 10); assert.equal(p.markStartText.value, '00:00:10.000')
})

for (const [label, start, end, reason] of [
  ['缺少终点', '00:00:05.000', '', '广告'],
  ['非法终点', '00:00:05.000', 'xx:yy', '广告'],
  ['超出时长', '00:00:05.000', '00:01:01.000', '广告'],
  ['反向范围', '00:00:10.000', '00:00:05.000', '广告'],
  ['只有原因', '00:00:00.000', '', '还在定位'],
]) {
  test(`${label}的待加入标记阻止保存并完整保留文本及原清单`, async t => {
    const p = await fixture(t); let requests = 0
    p.adopt(state({ enabled: true, ranges: [row()] }))
    p.markStartText.value = start; p.markEndText.value = end; p.markReason.value = reason
    t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
    await p.save(); await p.persist()
    assert.equal(requests, 0); assert.equal(p.dirty.value, true)
    assert.equal(p.markStartText.value, start); assert.equal(p.markEndText.value, end); assert.equal(p.markReason.value, reason)
    assert.deepEqual(JSON.parse(JSON.stringify(p.ranges.value)), [row()])
    assert.ok(p.markerActionError.value, '应说明标记未完成，不能只禁用按钮或静默跳过')
  })
}

test('清单已满时不静默丢弃待加入标记或保存已有清单', async t => {
  const p = await fixture(t); let requests = 0
  const rows = Array.from({ length: 200 }, (_, i) => ({ ...row(), id: `cut-${i}` }))
  p.adopt(state({ enabled: true, ranges: rows }))
  p.setMarkerValue('start', 20); p.setMarkerValue('end', 25)
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  assert.equal(p.pendingMarkerReady.value, false)
  await p.save()
  assert.equal(requests, 0); assert.equal(p.ranges.value.length, 200)
  assert.equal(p.markStart.value, 20); assert.equal(p.markEnd.value, 25)
  assert.ok(p.markerActionError.value)
})

test('自动收录标记后的保存失败仍保留该片段，不重复收录且可重新保存', async t => {
  const p = await fixture(t), calls: any[] = []
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async (_url: any, init: any) => {
    const body = JSON.parse(init.body); calls.push(body)
    if (calls.length === 1) return response({ code: 'OUTPUT_CUTS_CONFLICT', message: '方案已更新' }, 409)
    return response(state({ revision: 'r2', enabled: body.enabled, ranges: body.ranges }))
  })
  await p.save()
  assert.equal(p.saved.value.revision, 'r1'); assert.equal(p.ranges.value.length, 1)
  assert.equal(p.ranges.value[0].start, 5); assert.equal(p.ranges.value[0].end, 10)
  assert.equal(p.hasPendingMarker.value, false); assert.equal(p.dirty.value, true)
  assert.match(p.error.value, /草稿已保留/)
  await p.save()
  assert.equal(calls.length, 2); assert.equal(calls[1].ranges.length, 1)
  assert.equal(p.dirty.value, false)
})

test('没有修改时保存不发请求，也不清除已有接续起点', async t => {
  const p = await fixture(t); let requests = 0
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  assert.equal(p.addRange(), true)
  p.adopt(state({ enabled: true, ranges: JSON.parse(JSON.stringify(p.ranges.value)) }), { preserveMarker: true })
  assert.equal(p.dirty.value, false)
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  await p.save(); await p.persist()
  assert.equal(requests, 0); assert.equal(p.markStartText.value, '00:00:10.000')
})

test('未保存标记可直接预览，加入草稿后自动跳过且不写配置、不自动播放', async t => {
  const p = await fixture(t); let requests = 0, plays = 0
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  p.referenceVideo.value = { currentTime: 6, paused: true, play() { plays++ } }
  p.referenceDuration.value = 60
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  assert.equal(p.previewDraft(), true)
  assert.equal(p.ranges.value.length, 1); assert.equal(p.skipCuts.value, true)
  assert.equal(p.hasPendingMarker.value, false); assert.equal(p.dirty.value, true)
  assert.equal(p.referenceVideo.value.currentTime, 3, '准备预览时定位到新片段接缝前')
  assert.equal(p.referenceVideo.value.paused, true, '准备预览不能自动开始播放')
  p.referenceVideo.value.currentTime = 6; p.referenceVideo.value.paused = false; p.onReferenceTimeUpdate()
  assert.equal(p.referenceVideo.value.currentTime, 10)
  assert.equal(requests, 0); assert.equal(plays, 0)
  assert.equal(p.previewDraft(), true); assert.equal(p.previewDraft(), true)
  assert.equal(p.ranges.value.length, 1, '重复预览不能重复加入刚才的标记')
  p.setMarkerValue('start', 20); p.setMarkerValue('end', 25)
  assert.equal(p.previewDraft(), true)
  assert.equal(p.ranges.value.length, 2, '已开启预览后，仍能收录新的待加入片段')
  assert.equal(requests, 0)
})

test('预览开关开启时收录标记，半截标记阻止开启但不阻止关闭', async t => {
  const p = await fixture(t); let requests = 0
  p.adopt(state()); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  p.setPreviewCuts(true)
  assert.equal(p.skipCuts.value, true); assert.equal(p.ranges.value.length, 1)
  p.setMarkerValue('start', 20)
  p.setPreviewCuts(false)
  assert.equal(p.skipCuts.value, false); assert.equal(p.markStart.value, 20)
  p.setPreviewCuts(true)
  assert.equal(p.skipCuts.value, false); assert.equal(p.ranges.value.length, 1)
  assert.equal(p.markStartText.value, '00:00:20.000'); assert.ok(p.markerActionError.value)
  assert.equal(requests, 0)
})

test('已经开启剪后预览时直接播放也会收录新标记，无需再保存或切换开关', async t => {
  const p = await fixture(t); let requests = 0, plays = 0
  p.adopt(state()); p.skipCuts.value = true
  p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  p.referenceVideo.value = { currentTime: 3, paused: false, playbackRate: 1, readyState: 4, play() { plays++ }, pause() { this.paused = true } }
  p.referenceDuration.value = 60
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  p.onReferencePlay()
  assert.equal(p.ranges.value.length, 1); assert.equal(p.ranges.value[0].start, 5)
  assert.equal(p.hasPendingMarker.value, false); assert.equal(p.dirty.value, true)
  assert.equal(p.skipCuts.value, true); assert.equal(requests, 0); assert.equal(plays, 0)
  p.onReferencePlay()
  assert.equal(p.ranges.value.length, 1)
})

test('已经开启剪后预览时直接播放遇到半截标记会暂停并保留待完成内容', async t => {
  const p = await fixture(t); let requests = 0, pauses = 0
  p.adopt(state()); p.skipCuts.value = true; p.setMarkerValue('start', 5); p.markReason.value = '待核对'
  p.referenceVideo.value = { currentTime: 3, paused: false, pause() { pauses++; this.paused = true } }
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  p.onReferencePlay()
  assert.equal(pauses, 1); assert.equal(p.referenceVideo.value.paused, true)
  assert.equal(p.ranges.value.length, 0); assert.equal(p.hasPendingMarker.value, true)
  assert.equal(p.markStartText.value, '00:00:05.000'); assert.equal(p.markEndText.value, '')
  assert.equal(p.markReason.value, '待核对'); assert.ok(p.markerActionError.value)
  assert.equal(requests, 0)
})

test('源已更换时直接保存待加入标记仍需先确认再重绑写入', async t => {
  const p = await fixture(t), calls: any[] = []
  p.adopt(state({ source_changed: true })); p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async (_url: any, init: any) => {
    const body = JSON.parse(init.body); calls.push(body)
    return response(state({ revision: 'r2', enabled: body.enabled, ranges: body.ranges }))
  })
  await p.save()
  assert.equal(calls.length, 0); assert.equal(p.dialogs.length, 1)
  assert.match(p.dialogs[0].content, /原视频已更换/)
  await p.dialogs[0].onPositiveClick()
  assert.equal(calls.length, 1); assert.equal(calls[0].rebind_source, true)
  assert.equal(calls[0].ranges.length, 1); assert.equal(calls[0].ranges[0].start, 5)
  assert.equal(p.dirty.value, false)
})

test('运行期间可编辑并保存剪辑草稿，唯一写请求不启动制作且携带乐观锁', async t => {
  const p = await fixture(t), calls: any[] = []
  p.adopt(state()); p.ranges.value = [row()]; p.enabled.value = true
  assert.equal(p.dirty.value, true)
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    calls.push({ url: String(url), ...init, body: JSON.parse(init.body) })
    return response(state({ revision: 'r2', enabled: true, ranges: [row()] }))
  })
  await p.persist()
  assert.equal(calls.length, 1); assert.equal(calls[0].method, 'PUT'); assert.match(calls[0].url, /\/output-cuts$/)
  assert.deepEqual(calls[0].body, { revision: 'r1', source_fingerprint: 'source-1', enabled: true, ranges: [row()] })
  assert.equal(p.dirty.value, false)
  assert.ok(p.emitted.some((event: any) => event.name === 'saved'))
  assert.ok(p.emitted.some((event: any) => event.name === 'busy-change' && event.args[0] === true))
})

test('未完成的清单时间保留为草稿，不能静默使用旧数值保存', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  assert.equal(p.dirty.value, false)
  p.setRangeInputInvalid('cut-1', 'start', true)
  assert.equal(p.dirty.value, true)
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  await p.persist()
  assert.equal(requests, 0)
  assert.equal(p.ranges.value[0].start, 5)
  p.setRangeInputInvalid('cut-1', 'start', false)
  assert.equal(p.dirty.value, false)
  p.setRangeInputInvalid('cut-1', 'end', true)
  p.removeRange('cut-1')
  assert.equal(p.hasInvalidRangeInput.value, false)
  p.setRangeInputInvalid('old-cut', 'start', true)
  p.adopt(state())
  assert.equal(p.hasInvalidRangeInput.value, false)
})

test('源变更必须明确核对后才重绑，确认前不写入', async t => {
  const p = await fixture(t), calls: any[] = []
  p.adopt(state({ source_changed: true, ranges: [row()], enabled: true }))
  t.mock.method(globalThis, 'fetch', async (_url: any, init: any) => { calls.push(JSON.parse(init.body)); return response(state()) })
  p.save(); assert.equal(calls.length, 0); assert.equal(p.dialogs.length, 1)
  assert.match(p.dialogs[0].content, /原视频已更换/)
  await p.dialogs[0].onPositiveClick()
  assert.equal(calls[0].rebind_source, true)
})

test('并发保存冲突保留草稿和原revision，读取已保存也需显式放弃', async t => {
  const p = await fixture(t)
  p.adopt(state()); p.ranges.value = [row()]; p.enabled.value = true
  t.mock.method(globalThis, 'fetch', async () => response({ code: 'OUTPUT_CUTS_CONFLICT', message: '方案已更新' }, 400))
  await p.persist()
  assert.equal(p.saved.value.revision, 'r1'); assert.deepEqual(JSON.parse(JSON.stringify(p.ranges.value)), [row()]); assert.equal(p.dirty.value, true)
  assert.match(p.error.value, /草稿已保留/)
  p.reload(); assert.equal(p.dialogs.length, 1); assert.match(p.dialogs[0].title, /放弃/)
})

test('uid切换中迟到旧响应和离开后响应均不覆盖当前项目', async t => {
  const p = await fixture(t), calls: any[] = [], replies: any[] = []
  t.mock.method(globalThis, 'fetch', (url: any, init: any) => { calls.push({ url: String(url), init }); return new Promise(resolve => replies.push(resolve)) })
  const old = p.load(); p.fixtureProps.uid = 'next'
  assert.equal(calls.length, 2); assert.equal(calls[0].init.signal.aborted, true)
  replies[0](response(state({ revision: 'old' }))); await old
  assert.equal(p.saved.value, null)
  p.unmountHooks.forEach((hook: () => void) => hook())
  assert.equal(calls[1].init.signal.aborted, true)
  replies[1](response(state({ revision: 'next' }))); await Promise.resolve(); await Promise.resolve()
  assert.equal(p.saved.value, null)
})

test('标记仅创建草稿；跳过试听只在播放中生效且不会自动播放', async t => {
  const p = await fixture(t)
  p.adopt(state()); p.markStartText.value = '5'; p.markEndText.value = '10'; p.commitMarker('start'); p.commitMarker('end'); p.markReason.value = '广告'; p.addRange()
  assert.equal(p.ranges.value.length, 1); assert.equal(p.enabled.value, true); assert.equal(p.dirty.value, true)
  let plays = 0
  p.video.value = { currentTime: 6, paused: true, play() { plays++ } }; p.skipCuts.value = true
  p.onTimeUpdate(); assert.equal(p.video.value.currentTime, 6)
  p.video.value.paused = false; p.onTimeUpdate(); assert.equal(p.video.value.currentTime, 10); assert.equal(plays, 0)
})

test('打开跳过试听后会立即跨过当前播放头所在的草稿删除段', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 6, paused: false }
  p.video.value = source
  p.skipCuts.value = true
  assert.equal(source.currentTime, 10)
  // 已经跳到接缝后，再次收到 timeupdate 不应反复 seek 或改变位置。
  p.skipSourceCutIfNeeded()
  assert.equal(source.currentTime, 10)
})

test('未保存删除草稿与当前成片联动时明确提示新增删除的试听范围', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.fixtureProps.targetArtifactName = 'current.mp4'
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.ranges.value = [row(), { id: 'cut-2', start: 20, end: 30, reason: '', enabled: true }]
  p.skipCuts.value = true
  assert.match(p.previewModeLabel.value, /当前草稿.*跳过 1 段/)
  assert.match(p.previewModeNote.value, /新增删除/)
})

test('当前成片试听只跳过本次新增删除，关闭跳过时不会改动源视频', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.fixtureProps.targetArtifactName = 'current.mp4'
  p.adopt(state({ enabled: true, ranges: [row()] }))
  // 当前成片已包含 5–10 秒删除；草稿新增 15–20 秒只应映射这一段。
  p.ranges.value = [row(), { id: 'cut-2', start: 15, end: 20, reason: '', enabled: true }]
  assert.deepEqual(p.previewTargetCuts.value, [{ start: 10, end: 15 }])
  p.skipCuts.value = true
  assert.match(p.previewModeNote.value, /新增删除/)
  const source: any = { currentTime: 6, paused: false }
  const target: any = { currentTime: 11, paused: false }
  p.video.value = source; p.referenceVideo.value = target
  // 有成片参考时跳过发生在成片预览，源时间轴继续作为完整标记轴。
  p.skipSourceCutIfNeeded(); assert.equal(source.currentTime, 6)
  p.onReferenceTimeUpdate(); assert.equal(target.currentTime, 15)
  p.skipCuts.value = false; target.currentTime = 11; p.onReferenceTimeUpdate()
  assert.equal(target.currentTime, 11)
})

test('工作稿待更新不会阻止同源旧成片按真实快照试听', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.fixtureProps.targetArtifactName = 'current.mp4'
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.fixtureProps.targetArtifactFresh = false
  p.adopt(state({ enabled: true, ranges: [row()] }))
  p.ranges.value = [row(), {...row(),id:'cut-2',start:15,end:20}]
  p.skipCuts.value = true
  assert.equal(p.artifactMappingReady.value, true)
  assert.match(p.previewModeLabel.value, /跳过 1 段/)
  assert.match(p.previewModeNote.value, /新增删除/)
  const target: any = { currentTime: 6, paused: false }
  p.referenceVideo.value = target
  p.onReferenceTimeUpdate()
  assert.equal(target.currentTime, 6)
})

test('单播放器在接缝定位时冻结允许画面并明确显示等待状态', () => {
  assert.match(outputCutsTemplate, /previewTransitioning/)
  assert.match(outputCutsTemplate, /正在定位保留画面/)
  assert.match(outputCutsScript, /createCutPreviewPlayback/)
})

test('手动输入时分秒不会被当前播放时间覆盖，可准确标记 31:40–31:44', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 2400 }))
  p.current.value = 1878.256
  p.markStartText.value = '31:40'
  p.markEndText.value = '31:44'
  p.commitMarker('start'); p.commitMarker('end')
  p.addRange()
  assert.deepEqual(p.ranges.value.map((row: any) => ({ start: row.start, end: row.end })), [{ start: 1900, end: 1904 }])
})

test('输入草稿无效时保留原文并就地提示，不回写旧终点', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 2400 })); p.markStartText.value = '31:40'; p.commitMarker('start')
  p.markEndText.value = '31:99'; p.commitMarker('end')
  assert.equal(p.markEnd.value, null); assert.equal(p.markEndText.value, '31:99')
  assert.match(p.markerTextError.value, /有效的终点时间/)
})

test('无原片、全片删除或禁用时不会绕过按钮检查写入', async t => {
  const p = await fixture(t); let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return response(state()) })
  p.adopt(state({ duration: null, source_url: null })); await p.persist()
  p.adopt(state({ enabled: true, ranges: [{ ...row(), start: 0, end: 60 }] })); await p.persist()
  p.adopt(state()); p.fixtureProps.disabled = true; await p.persist()
  assert.equal(requests, 0)
})

test('版本剪辑使用 version_id 查询和保存，并携带旧成片基准', async t => {
  const p = await fixture(t), calls: any[] = []
  p.fixtureProps.versionId = 'v2'; p.loading.value = false; p.adopt(state()); p.baselineId.value = 'v1'; p.ranges.value = [row()]; p.enabled.value = true
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null })
    return response(state({ revision: 'r2', enabled: true, ranges: [row()], cut_baseline: { version_id: 'v1' } }))
  })
  await p.persist()
  assert.match(calls[0].url, /output-cuts\?version_id=v2$/)
  assert.equal(calls[0].body.version_id, 'v2')
  assert.equal(calls[0].body.base_version_id, 'v1')
})

test('选择旧成片时仅提供只读参考地址，编辑仍使用原视频时间轴', async t => {
  const p = await fixture(t)
  p.fixtureProps.baselineOptions = [{ id: 'v1', name: '旧成片', artifact_id: 9, artifact_name: 'v1.mp4', artifact_hash: 'abc' }]
  p.baselineId.value = 'v1'
  assert.equal(p.selectedBaselineName.value, '旧成片')
  assert.equal(p.baselinePreviewUrl.value, '/api/projects/fixture/steps/assemble/artifacts/v1.mp4/raw?v=abc')
  p.baselineId.value = null
  assert.equal(p.baselinePreviewUrl.value, '')
})

test('当前版本有正式产物时提供含字幕和配音的只读参考地址', async t => {
  const p = await fixture(t)
  p.fixtureProps.targetArtifactName = 'version-v2.mp4'
  p.fixtureProps.targetArtifactHash = 'sha-1'
  assert.equal(p.targetPreviewUrl.value, '/api/projects/fixture/steps/assemble/artifacts/version-v2.mp4/raw?v=sha-1')
  p.fixtureProps.targetArtifactName = undefined
  assert.equal(p.targetPreviewUrl.value, '')
})

test('当前成片参考使用已保存剪辑方案映射，未保存草稿不会改变旧文件定位', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  p.ranges.value = [{ ...row(), start: 20, end: 30 }]
  assert.equal(p.referenceCuts.value?.ranges[0]?.start, 5)
  assert.equal(p.mappedSourceTime(5), 10)
})

test('联动试听默认只播放当前成片，切换声音来源不会叠加两条音轨', async t => {
  const p = await fixture(t)
  assert.ok(p.audioSource, `audioSource export missing: ${Object.keys(p).join(',')}`)
  assert.ok(p.video, `video export missing: ${Object.keys(p).join(',')}`)
  const source: any = { muted: false }, target: any = { muted: false }
  p.video.value = source; p.referenceVideo.value = target
  p.applyLinkedAudioPolicy()
  assert.equal(source.muted, true)
  assert.equal(target.muted, false)
  p.audioSource.value = 'source'; p.applyLinkedAudioPolicy()
  assert.equal(source.muted, false)
  assert.equal(target.muted, true)
  p.audioSource.value = 'none'; p.applyLinkedAudioPolicy()
  assert.equal(source.muted, true)
  assert.equal(target.muted, true)
})

test('预览仅有一个媒体播放器，恢复来源只在需要时显示，不暴露旧双播放器联动', () => {
  assert.equal((outputCutsTemplate.match(/<video\s/g) || []).length, 1)
  assert.match(outputCutsTemplate, /skipCuts && hasRestoredIntervals/)
  assert.match(outputCutsTemplate, /恢复片段取自/)
  assert.doesNotMatch(outputCutsTemplate, /联动定位与播放/)
  assert.match(outputCutsTemplate, /:timeline="playbackTimeline"/)
})

test('声音来源切换只改变 muted 语义，不改动两个播放器的音量', async t => {
  const p = await fixture(t)
  const source = { muted: false, volume: .4 }, target = { muted: false, volume: .8 }
  p.video.value = source; p.referenceVideo.value = target
  assert.equal(p.audioSource.value, 'target')
  p.applyLinkedAudioPolicy()
  assert.equal(source.muted, true); assert.equal(target.muted, false)
  assert.equal(source.volume, .4); assert.equal(target.volume, .8)

  p.audioSource.value = 'source'; p.applyLinkedAudioPolicy()
  assert.equal(source.muted, false); assert.equal(target.muted, true)
  p.audioSource.value = 'none'; p.applyLinkedAudioPolicy()
  assert.equal(source.muted, true); assert.equal(target.muted, true)

  // 浏览器自身的 volumechange 不能绕过唯一声音来源策略。
  p.audioSource.value = 'target'; source.muted = false; p.onSourceVolumeChange()
  assert.equal(source.muted, true)
  p.audioSource.value = 'source'; target.muted = false; p.onReferenceVolumeChange()
  assert.equal(target.muted, true)
})

test('拖动一侧时间轴时由发起方保持主导，另一侧不会反向抢回位置', async t => {
  const p = await fixture(t)
  p.adopt(state())
  p.video.value = { currentTime: 20, paused: true } as any
  p.referenceVideo.value = { currentTime: 0, paused: true } as any
  p.onSourceSeeking()
  assert.equal(p.seekSyncOrigin, 'source')
  assert.equal(p.referenceVideo.value.currentTime, 20)
  p.onReferenceSeeking()
  assert.equal(p.video.value.currentTime, 20)
  p.referenceVideo.value.currentTime = 40
  p.onReferenceSeeking()
  assert.equal(p.video.value.currentTime, 40)
})

test('预览说明区分剪后播放时间与完整原片编辑时间，并可生成连续预览', () => {
  const text = outputCutsTemplate.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')
  assert.match(text, /剪后时间/)
  assert.match(text, /原片完整时间轴/)
  assert.match(text, /生成连贯预览/)
  assert.match(text, /即时跳转可能短暂缓冲/)
})

test('输出时间轴映射会将删除段收敛到单一定位点，恢复后回到原始时间', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  assert.equal(p.mappedOutputTime(5), 5)
  assert.equal(p.mappedOutputTime(10), 5)
  assert.equal(p.mappedOutputTime(20), 15)
  p.ranges.value[0].enabled = false
  assert.equal(p.mappedOutputTime(20), 20)
})

test('源视频拖动联动时保持拖动来源，不能被成片接缝的反向 timeupdate 拉回', async t => {
  const p = await fixture(t)
  p.adopt(state({ enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 6, paused: true }
  const target: any = { currentTime: 0, paused: true }
  p.video.value = source; p.referenceVideo.value = target

  // 源时间 6 秒位于删除段 5–10 秒内，成片定位点应收敛到 5 秒。
  // 但这是“源播放器拖动”发起的联动，目标播放器随后发出的 timeupdate
  // 不能再把源播放器反向跳到删除段末尾 10 秒，否则拖动会抖动/回弹。
  p.onSourceTimeUpdate()
  assert.equal(target.currentTime, 5)
  p.onReferenceTimeUpdate()
  assert.equal(source.currentTime, 6)

  // 用户随后真正拖动成片到 20 秒，才允许反向映射到源视频 25 秒。
  target.currentTime = 20
  p.onReferenceSeeking()
  p.onReferenceTimeUpdate()
  assert.equal(source.currentTime, 25)
})

test('播放中拖动也以显式 seek 生命周期为主导，忽略对侧播放时钟', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 20, paused: false }
  const target: any = { currentTime: 0, paused: false }
  p.video.value = source; p.referenceVideo.value = target

  // VideoControls 的 seek-start/seek/seek-end 是用户事件；拖动期间即使
  // 另一播放器仍在播放，也不能让它的 timeupdate 夺回主导权。
  p.onSourceSeekStart()
  source.currentTime = 21
  p.onSourceSeek()
  assert.equal(target.currentTime, 16)
  target.currentTime = 16 // 联动写入产生的对侧 timeupdate
  p.onReferenceTimeUpdate()
  assert.equal(source.currentTime, 21)

  // 结束拖动后释放锁；下一次明确从成片侧拖动可立即反向定位。
  p.onSourceSeekEnd()
  target.currentTime = 20
  p.onReferenceSeekStart()
  p.onReferenceSeek()
  assert.equal(source.currentTime, 25)
  p.onReferenceSeekEnd()
})

test('拖动过程中切换到另一侧会立即交接主导权，不丢失第一定位', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 20, paused: false }
  const target: any = { currentTime: 0, paused: false }
  p.video.value = source; p.referenceVideo.value = target

  p.onSourceSeekStart()
  source.currentTime = 21
  p.onSourceSeek()
  assert.equal(target.currentTime, 16)

  // 用户尚未松开源时间轴就改拖成片时间轴；第一下成片定位必须立即
  // 覆盖上一轮来源，不能因清理旧锁时误清 activeSeekOrigin 而被丢弃。
  p.onReferenceSeekStart()
  target.currentTime = 30
  p.onReferenceSeek()
  assert.equal(source.currentTime, 35)
  assert.equal(p.activeSeekOrigin, 'reference')
  p.onReferenceSeekEnd()
})

test('播放同步使用单一主时钟，小漂移只变速不会每个 timeupdate 反复定位', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 20, paused: false, ended: false, playbackRate: 1 }
  const target: any = { currentTime: 0, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  p.video.value = source; p.referenceVideo.value = target
  p.startPlaybackSync('source')
  assert.equal(p.playbackOrigin, 'source')
  assert.equal(target.currentTime, 15) // 5–10 秒删除后，源 20 秒对应成片 15 秒
  target.currentTime = 14.8
  source.currentTime = 20.2
  p.syncPlaybackClock()
  assert.equal(target.currentTime, 14.8, '小漂移不应反复写 currentTime 造成卡顿')
  assert.ok(target.playbackRate > 1, '小漂移应通过轻微变速收敛')
})

test('跟随播放器尚未进入可定位状态时不循环 seek，恢复后只补做一次映射', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 20, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  let writes = 0
  let targetTime = 0
  const target: any = {
    get currentTime() { return targetTime },
    set currentTime(value) { writes++; targetTime = value },
    paused: false, ended: false, playbackRate: 1, readyState: 0,
  }
  p.video.value = source; p.referenceVideo.value = target
  p.startPlaybackSync('source')
  assert.equal(writes, 0, '媒体未就绪时不能反复设置 currentTime')
  target.readyState = 4
  p.syncPlaybackClock()
  assert.equal(writes, 1)
  assert.equal(targetTime, 15)
  p.clearPlaybackSync()
})

test('跟随媒体未就绪时暂停主播放器，媒体可播放后自动恢复联动', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = {
    currentTime: 20, paused: false, ended: false, playbackRate: 1, readyState: 4,
    pause() { this.paused = true },
    play() { this.paused = false; return Promise.resolve() },
  }
  const target: any = {
    currentTime: 0, paused: true, ended: false, playbackRate: 1, readyState: 0,
    play() { this.paused = false; return Promise.resolve() },
    pause() { this.paused = true },
  }
  p.video.value = source; p.referenceVideo.value = target
  p.onSourcePlay()
  assert.equal(p.pendingPlaybackOrigin, 'source')
  assert.equal(source.paused, true, '跟随端未就绪时主端不应独自播放')
  target.readyState = 4
  p.retryPendingPlayback()
  for (let i = 0; i < 6; i++) await Promise.resolve()
  assert.equal(p.pendingPlaybackOrigin, null)
  assert.equal(source.paused, false)
  assert.equal(target.paused, false)
  assert.equal(p.playbackOrigin, 'source')
})

test('关闭再打开联动时重新选择正在播放的一侧并立即收敛时间', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 22, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  const target: any = { currentTime: 1, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  p.video.value = source; p.referenceVideo.value = target
  p.startPlaybackSync('source')
  p.referenceSync.value = false
  source.currentTime = 22
  target.currentTime = 1
  p.referenceSync.value = true
  assert.equal(p.playbackOrigin, 'source')
  assert.equal(target.currentTime, 17, '重新打开时应按源时间轴映射当前成片，而不是等待下一次 play 事件')
})

test('迟到的跟随播放事件不会把主时钟反向夺走，快速暂停后再播放仍保持单一来源', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = {
    currentTime: 20, paused: false, ended: false, playbackRate: 1, readyState: 4,
    play() { this.paused = false; return Promise.resolve() },
    pause() { this.paused = true },
  }
  const target: any = {
    currentTime: 0, paused: true, ended: false, playbackRate: 1, readyState: 4,
    play() { this.paused = false; return Promise.resolve() },
    pause() { this.paused = true },
  }
  p.video.value = source; p.referenceVideo.value = target
  p.onSourcePlay()
  assert.equal(p.playbackOrigin, 'source')
  // 这是 mirrorReferencePlayback 发出的 target.play() 的迟到原生事件，
  // 不应触发 target -> source 的再次镜像。
  p.onReferencePlay()
  assert.equal(p.playbackOrigin, 'source')
  source.paused = true
  p.onSourcePause()
  p.onReferencePause() // mirror pause 的迟到事件，只消费命令，不反向暂停/清理
  source.paused = false
  p.onPlaybackIntent('source')
  p.onSourcePlay()
  p.onReferencePlay()
  assert.equal(p.playbackOrigin, 'source')
  assert.equal(p.pendingPlaybackOrigin, null)
})

test('成片作为主时钟时，源播放器在删除接缝跳到下一段', async t => {
  const p = await fixture(t)
  p.adopt(state({ duration: 60, enabled: true, ranges: [row()] }))
  const source: any = { currentTime: 5, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  const target: any = { currentTime: 5, paused: false, ended: false, playbackRate: 1, readyState: 4 }
  p.video.value = source; p.referenceVideo.value = target
  p.startPlaybackSync('reference')
  // 成片 5 秒正好是删除接缝，next 偏置应回到源 10 秒之后。
  assert.equal(source.currentTime, 10)
})


test('已保存新清单不能冒充旧成片真实剪辑记录，旧版无删除仍正确标记待应用', async t => {
  const p = await fixture(t)
  p.fixtureProps.targetArtifactName = 'current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  assert.deepEqual(p.referenceCuts.value.ranges, [])
  assert.equal(p.hasCutChanges.value, true)
  assert.deepEqual(p.previewTargetCuts.value,[{start:5,end:10}])
  assert.equal(p.mappedSourceTime(5),5)
})

test('未知成片映射保留原样预览，但不猜测删除位置或允许正式剪辑', async t => {
  const p = await fixture(t); let requests=0
  p.fixtureProps.targetArtifactName = 'current.mp4'; p.fixtureProps.versionId='v1'; p.fixtureProps.running=false
  p.adopt(state({enabled:true,ranges:[row()],artifact_cuts:null,artifact_validation:{valid:false,message:'时长不匹配'}}))
  t.mock.method(globalThis,'fetch',async()=>{requests++;throw new Error('不应发起制作')})
  assert.equal(p.artifactMappingReady.value,false); assert.deepEqual(p.previewTargetCuts.value,[])
  assert.equal(p.mappedSourceTime(5),null)
  await p.createTimelineVersion(); assert.equal(requests,0)
})

test('剪辑版确认固定当前草稿和恢复来源，不先覆盖旧版删除清单', async t => {
  const p = await fixture(t),calls:any[]=[]
  p.fixtureProps.targetArtifactName='current.mp4';p.fixtureProps.versionId='v1';p.fixtureProps.running=false
  p.adopt(state({enabled:false,artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.loading.value=false;p.enabled.value=true;p.ranges.value=[row()]
  t.mock.method(globalThis,'fetch',async(url:any,init:any)=>{
    calls.push({url:String(url),method:init?.method,body:JSON.parse(init.body)})
    return response(String(url).endsWith('/plan')?{fingerprint:'timeline-fp',base_name:'原声修订3',output_duration:55,encoding:{method:'smart_cut',encoded_seconds:1.25,copied_seconds:53.75,audio_mode:'encode_once'},warnings:['接缝会在生成时再次校验']}:{version:{id:'timeline-1',timeline:{plan_id:'p1'}}})
  })
  await p.createTimelineVersion()
  assert.equal(calls.length,1); assert.match(calls[0].url,/assembly-versions\/timeline\/plan$/)
  assert.equal(calls[0].body.base_version_id,'v1');assert.deepEqual(calls[0].body.draft.ranges,[row()])
  assert.match(p.dialogs[0].content,/旧成片保留/)
  assert.match(p.dialogs[0].content,/接缝编码，复用主体/)
  assert.match(p.dialogs[0].content,/预计视频重编码 00:00:01.250/)
  assert.match(p.dialogs[0].content,/音频精确裁剪后编码一次/)
  assert.match(p.dialogs[0].content,/接缝会在生成时再次校验/)
  await p.dialogs[0].onPositiveClick()
  assert.equal(calls.length,2);assert.match(calls[1].url,/assembly-versions\/timeline$/)
  assert.equal(calls[1].body.fingerprint,'timeline-fp')
  assert.ok(p.emitted.some((e:any)=>e.name==='timeline-created'&&e.args[0].id==='timeline-1'))
  assert.equal(p.saved.value.ranges.length,0)
})

test('生成剪辑版会包含待加入标记，确认期间再次修改标记则拒绝旧计划', async t => {
  const p = await fixture(t), calls: any[] = []
  p.fixtureProps.targetArtifactName = 'current.mp4'; p.fixtureProps.versionId = 'v1'; p.fixtureProps.running = false
  p.adopt(state({ artifact_cuts: { source_duration: 60, ranges: [], output_duration: 60 }, artifact_validation: { valid: true } }))
  p.loading.value = false; p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    calls.push({ url: String(url), method: init?.method, body: JSON.parse(init.body) })
    return response({ fingerprint: 'timeline-fp', base_name: '原声版', output_duration: 55 })
  })
  await p.createTimelineVersion()
  assert.equal(calls.length, 1); assert.match(calls[0].url, /\/timeline\/plan$/)
  assert.equal(calls[0].body.draft.ranges.length, 1)
  assert.equal(calls[0].body.draft.ranges[0].start, 5); assert.equal(calls[0].body.draft.ranges[0].end, 10)
  assert.equal(p.saved.value.ranges.length, 0, '独立剪辑版不能先覆盖旧版清单')
  assert.equal(p.dialogs.length, 1)
  p.setMarkerValue('start', 20); p.setMarkerValue('end', 25)
  assert.equal(await p.dialogs[0].onPositiveClick(), false)
  assert.equal(calls.length, 1); assert.match(p.error.value, /变化|重新/)
  assert.equal(p.markStart.value, 20); assert.equal(p.markEnd.value, 25)
})

test('连贯预览直接收录待加入标记，提交草稿不先持久化清单', async t => {
  const p = await fixture(t), calls: any[] = []
  p.fixtureProps.targetArtifactName = 'current.mp4'
  p.adopt(state({ artifact_cuts: { source_duration: 60, ranges: [], output_duration: 60 }, artifact_validation: { valid: true } }))
  p.loading.value = false; p.skipCuts.value = true
  p.setMarkerValue('start', 5); p.setMarkerValue('end', 10)
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    calls.push({ url: String(url), method: init?.method, body: JSON.parse(init.body) })
    return response({ task_id: 'cached', status: 'done', output_url: '/preview.mp4', duration: 10, frame_rate: 25,
      segments: [{ source_start: 0, source_end: 5, virtual_start: 0, virtual_end: 5 }, { source_start: 10, source_end: 15, virtual_start: 5, virtual_end: 10 }] })
  })
  await p.startVirtualPreview()
  assert.equal(calls.length, 1); assert.equal(calls[0].method, 'POST')
  assert.match(calls[0].url, /\/output-cuts\/virtual-preview$/)
  assert.equal(calls[0].body.draft.ranges.length, 1)
  assert.equal(calls[0].body.draft.ranges[0].start, 5); assert.equal(calls[0].body.draft.ranges[0].end, 10)
  assert.equal(p.virtualPreviewStatus.value, 'ready'); assert.equal(p.hasPendingMarker.value, false)
  assert.equal(p.saved.value.ranges.length, 0); assert.equal(p.dirty.value, true)
})


test('剪辑上下文必须匹配当前媒体哈希，文件切换后的迟到响应不采用', async t => {
  const p = await fixture(t)
  p.fixtureProps.versionId='v1';p.fixtureProps.targetArtifactName='current.mp4';p.fixtureProps.targetArtifactHash='current-hash'
  p.adopt(state());p.loading.value=false
  t.mock.method(globalThis,'fetch',async()=>response({artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true,artifact_hash:'old-hash'}}))
  await p.loadArtifactContext()
  assert.equal(p.artifactMappingReady.value,false)
  assert.match(p.artifactContext.value.artifact_validation.message,/文件已变化/)
  let reply: any
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{reply=resolve}))
  const pending=p.loadArtifactContext()
  const oldReply=reply
  p.fixtureProps.targetArtifactName='another.mp4'
  oldReply(response({artifact_cuts:{source_duration:60,ranges:[{start:5,end:10}]},artifact_validation:{valid:true,artifact_hash:'current-hash'}}))
  await pending
  assert.equal(p.referenceCuts.value,null)
})


test('仅新增删除时立即跳过，不在后台自动编码整部虚拟试听', async t => {
  const p=await fixture(t);let requests=0
  p.fixtureProps.targetArtifactName='current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.referenceVideo.value={currentTime:6,paused:false}
  t.mock.method(globalThis,'fetch',async()=>{requests++;throw new Error('纯删除不需要编码')})
  p.skipCuts.value=true
  assert.equal(p.referenceVideo.value.currentTime,10)
  await new Promise(resolve=>setTimeout(resolve,450))
  assert.equal(requests,0)
})


test('完整编码的预检明确回退原因与编码量，不宣称主体直接复用', async t => {
  const p=await fixture(t)
  const text=p.timelineEncodingText({method:'full_encode',encoded_seconds:120,copied_seconds:0,audio_mode:'none',fallback_reason:'恢复片段使用了另一来源'})
  assert.match(text,/完整重新编码/);assert.match(text,/00:02:00.000/);assert.match(text,/直接复用 00:00:00.000/)
  assert.match(text,/需整片编码：恢复片段使用了另一来源/);assert.match(text,/此版本无音轨/)
})

test('单视频计划预览使用实际成片帧率和剪后总长，源时间定位不会停在删除段', async t => {
  const p=await fixture(t)
  p.fixtureProps.targetArtifactName='current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],frame_rate:30,artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true,actual_duration:60,frame_rate:25}}))
  p.referenceVideo.value={currentTime:0,paused:true,duration:60,playbackRate:1,readyState:4,textTracks:[]}
  p.referenceDuration.value=60;p.skipCuts.value=true
  assert.equal(p.playbackTimeline.value.duration,55)
  assert.equal(p.playbackTimeline.value.fps,25)
  p.seek(7)
  assert.equal(p.referenceVideo.value.currentTime,10)
  p.onReferenceSeek(12)
  assert.equal(p.current.value,12)
  p.skipCuts.value=false
  assert.equal(p.playbackTimeline.value,null)
})

test('连贯预览默认只请求当前接缝附近，完成后按量化segments重定位，返回时保留源位置', async t => {
  const p=await fixture(t), requests:any[]=[]
  p.fixtureProps.targetArtifactName='current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],frame_rate:30,artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.referenceVideo.value={currentTime:6,paused:true,duration:60,playbackRate:1,readyState:4,textTracks:[]}
  p.referenceDuration.value=60;p.current.value=6;p.skipCuts.value=true
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({url:String(url),body:JSON.parse(String(options?.body))})
    return response({task_id:'cached',status:'done',cache_hit:true,output_url:'/preview.mp4',duration:10,frame_rate:25,
      segments:[{source_start:0,source_end:5,virtual_start:0,virtual_end:5},{source_start:10,source_end:15,virtual_start:5,virtual_end:10}]})
  })
  await p.startVirtualPreview()
  assert.deepEqual(requests[0].body.preview_range,{start:0,end:15})
  assert.equal(p.virtualPreviewStatus.value,'ready')
  assert.equal(p.previewFrameRate.value,25)
  assert.equal(p.playbackTimeline.value,null,'已生成的预览不能再次应用删除映射')
  p.referenceVideo.value.duration=10;p.onReferenceMetadata()
  assert.equal(p.referenceVideo.value.currentTime,5,'原片6秒在删除区，应映到下一保留帧')
  p.referenceVideo.value.currentTime=5;p.onReferenceTimeUpdate()
  assert.equal(p.current.value,10,'剪后接缝属于后一保留片段，不能反映到被删起点')
  p.referenceVideo.value.currentTime=9;p.onReferenceTimeUpdate()
  assert.equal(p.current.value,14)
  p.resetVirtualPreview();p.referenceVideo.value.duration=60;p.onReferenceMetadata()
  assert.equal(p.referenceVideo.value.currentTime,14)
})

test('完整清单预览必须显式选择，不受当前播放位置缩小范围', async t => {
  const p=await fixture(t)
  p.adopt(state({enabled:true,ranges:[row()]}));p.previewScope.value='full'
  p.current.value=40
  assert.equal(p.previewSourceWindow.value,null)
})

test('短预览外的原片定位回到完整媒体，不出现游标移动但画面未动', async t => {
  const p=await fixture(t)
  p.fixtureProps.targetArtifactName='current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],frame_rate:30,artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.skipCuts.value=true;p.virtualPreviewStatus.value='ready'
  p.virtualPreviewSegments.value=[{source_start:0,source_end:5,virtual_start:0,virtual_end:5},{source_start:10,source_end:15,virtual_start:5,virtual_end:10}]
  let paused=false
  p.referenceVideo.value={currentTime:3,paused:true,duration:10,playbackRate:1,readyState:4,textTracks:[],pause(){paused=true}}
  p.referenceDuration.value=10
  p.seek(40)
  assert.equal(paused,true);assert.equal(p.virtualPreviewStatus.value,'idle');assert.equal(p.current.value,40)
  p.referenceVideo.value.duration=60;p.onReferenceMetadata()
  assert.equal(p.referenceVideo.value.currentTime,40)
  assert.equal(p.current.value,40)
})

test('取消后迟到的同源恢复来源响应不能覆盖新的短预览映射或启动旧任务', async t => {
  const p=await fixture(t)
  p.fixtureProps.targetArtifactName='current.mp4'
  p.fixtureProps.baselineOptions=[{id:'donor-v',name:'旧恢复来源'}]
  p.adopt(state({enabled:true,ranges:[row()],artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.referenceVideo.value={currentTime:0,paused:true,duration:60,playbackRate:1,readyState:4,textTracks:[]}
  p.referenceDuration.value=60;p.skipCuts.value=true
  p.virtualDonorKind.value='version';p.virtualDonorVersionId.value='donor-v'
  let reply:any,posts=0
  t.mock.method(globalThis,'fetch',(url:any,options:any)=>{
    if (options?.method==='POST') { posts++;return Promise.resolve(response({status:'done',output_url:'/unexpected.mp4'})) }
    assert.match(String(url),/virtual-preview\/context\?version_id=donor-v/)
    return new Promise(resolve=>{reply=resolve})
  })
  const pending=p.startVirtualPreview()
  assert.equal(typeof reply,'function')
  p.resetVirtualPreview()
  const latest=[{source_start:40,source_end:45,virtual_start:0,virtual_end:5,available:true}]
  p.virtualPreviewStatus.value='ready';p.virtualPreviewSegments.value=latest;p.virtualPreviewDuration.value=5
  reply(response({source_fingerprint:'source-1',artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  await pending
  assert.deepEqual(JSON.parse(JSON.stringify(p.virtualPreviewSegments.value)),latest)
  assert.equal(p.virtualPreviewDuration.value,5)
  assert.equal(p.virtualPreviewStatus.value,'ready')
  assert.equal(posts,0)
})

test('切换项目丢弃旧虚拟试听恢复点，新媒体不从旧项目位置开始', async t => {
  const p=await fixture(t)
  p.fixtureProps.targetArtifactName='current.mp4'
  p.adopt(state({enabled:true,ranges:[row()],artifact_cuts:{source_duration:60,ranges:[],output_duration:60},artifact_validation:{valid:true}}))
  p.referenceVideo.value={currentTime:9,paused:true,duration:10,playbackRate:1,readyState:4,textTracks:[]}
  p.skipCuts.value=true;p.virtualPreviewStatus.value='ready'
  p.virtualPreviewSegments.value=[{source_start:0,source_end:5,virtual_start:0,virtual_end:5},{source_start:10,source_end:15,virtual_start:5,virtual_end:10}]
  p.current.value=14
  const next=state({duration:20,output_duration:20,source_fingerprint:'new-source',artifact_cuts:{source_duration:20,ranges:[],output_duration:20}})
  t.mock.method(globalThis,'fetch',async()=>response(next))
  p.fixtureProps.uid='next-project'
  await p.load()
  p.referenceVideo.value={currentTime:0,paused:true,duration:20,playbackRate:1,readyState:4,textTracks:[]}
  p.onReferenceMetadata()
  assert.equal(p.referenceVideo.value.currentTime,0)
  assert.equal(p.current.value,0)
  assert.equal(p.virtualPreviewStatus.value,'idle')
})
