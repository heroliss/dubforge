import assert from 'node:assert/strict'
import test from 'node:test'
import { publicationChapterScope, publicationChapterState, preferredPublicationChapterParts } from '../src/utils/publicationChapters.ts'

test('已整理章节按CID替换本地候选且保留摘要，不根据P顺序或名称猜测',()=>{
  const prepared=[{cid:101,items:[{title:'整理完成',start_time:0,end_time:100,source_indices:[0,1]}],
    adaptation:{source_count:21,output_count:1,merged_count:20}}]
  const row={chapter_parts:[{chapter_cid:102,title:'另一P',chapter_items:[{title:'原始P2'}]},
    {chapter_cid:101,title:'正文',chapter_items:[{title:'旧21章候选'}]}],chapter_sync:{parts:prepared}}
  const before=JSON.stringify(row),parts=preferredPublicationChapterParts(row)
  assert.equal(parts[0].chapter_items[0].title,'原始P2')
  assert.deepEqual(parts[1].chapter_items,prepared[0].items)
  assert.deepEqual(parts[1].chapter_adaptation,prepared[0].adaptation)
  assert.equal(parts[1].chapter_timeline.kind,'adapted');assert.equal(JSON.stringify(row),before)
  assert.equal(preferredPublicationChapterParts({snapshot:{parts:[{title:'无CID',chapter_items:[]}]},chapter_sync:{parts:prepared}})[0].chapter_items.length,0)
})

test('运行说明按冻结配方显示章节意图，旧配方缺键不声称自动开启',()=>{
  assert.match(publicationChapterScope({recipe:{mode:'publish'},settings:{}}),/不自动设置/)
  assert.match(publicationChapterScope({recipe:{mode:'publish'},settings:{chapters_enabled:true}}),/不自动设置/)
  assert.match(publicationChapterScope({recipe:{chapters_enabled:false},settings:{chapters_enabled:true}}),/不自动设置/)
  assert.match(publicationChapterScope({recipe:{chapters_enabled:true,chapters_translate:false}}),/保留源章节标题/)
  assert.match(publicationChapterScope({chapters_enabled:true,chapters_translate:true}),/标题翻译为中文.*无章节时跳过/)
})

test('章节独立状态保留历史关闭语义，未知结果不提供写重试',()=>{
  const disabled=publicationChapterState({snapshot:{chapter_options:{enabled:false}}})
  assert.equal(disabled.supplement,true);assert.equal(disabled.retry,false)
  const unknown=publicationChapterState({chapter_sync:{status:'unknown',enabled:true}})
  assert.equal(unknown.reconcile,true);assert.equal(unknown.retry,false);assert.equal(unknown.supplement,false)
  const waiting=publicationChapterState({chapter_sync:{status:'pending',phase:'waiting_review',enabled:true}})
  assert.match(waiting.message,/等待视频审核通过/);assert.equal(waiting.active,true)
})
