import assert from 'node:assert/strict'
import test from 'node:test'
import { publicationRecordDate, videoFileInfo, videoFileLabel, videoFileTime } from '../src/utils/publishVideo.ts'

test('成片标题采用成功时设置名，后续重命名单独提示，不由抽象文件名决定', () => {
  const info = videoFileInfo({name:'final_version_abcdef.mp4',version_name:'新名称',version_generated_name:'配音 · 局部修订',version_summary:'中文配音 · 双语字幕 · 无评论'})
  assert.equal(info.title,'配音 · 局部修订');assert.equal(info.currentName,'新名称')
  assert.equal(info.summary,'中文配音 · 双语字幕 · 无评论');assert.equal(info.filename,'final_version_abcdef.mp4')
})

test('成功时间优先于登记时间，同名文件可按完整日期时间搜索区分', () => {
  const file={name:'final_version_dub.mp4',version_name:'中文配音',generated_at:'2026-10-17T12:34:56+00:00',created_at:'2026-10-01T00:00:00+00:00'}
  const info=videoFileInfo(file)
  assert.equal(info.timeLabel,'生成时间');assert.equal(info.timeIso,'2026-10-17T12:34:56.000Z')
  assert.match(info.timeValue,/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  assert.match(videoFileLabel(file),/中文配音.*生成时间.*final_version_dub\.mp4/)
  assert.notEqual(videoFileLabel(file),videoFileLabel({...file,generated_at:'2026-10-17T13:34:56+00:00'}))
})

test('旧UTC时间正确转换，未知或非法时间不显示Invalid Date、不冒充成功生成', () => {
  assert.deepEqual(videoFileTime('2026-10-17T12:34:56'),videoFileTime('2026-10-17T12:34:56Z'))
  assert.deepEqual(videoFileTime('2026-10-17T20:34:56+08:00'),videoFileTime('2026-10-17T12:34:56Z'))
  assert.equal(videoFileInfo({created_at:'2026-10-17T12:34:56Z'}).timeLabel,'登记时间')
  assert.equal(videoFileInfo({generated_at:'broken',created_at:'2026-10-17T12:34:56Z'}).timeLabel,'登记时间')
  for(const generated_at of [null,undefined,'broken','2026-99-99T99:99:99Z'])assert.equal(videoFileInfo({generated_at}).timeText,'')
  assert.equal(videoFileTime(''),null);assert.equal(videoFileInfo(undefined).filename,'')
})

test('原片显示获取时间；缺失与待审状态清楚保留，未知时长不拼虚假占位', () => {
  assert.equal(videoFileInfo({is_source:true,name:'video.mp4',created_at:'2026-10-17T12:34:56Z'}).timeLabel,'获取时间')
  assert.equal(videoFileInfo({is_source:true}).title,'原声原片')
  assert.match(videoFileLabel({version_name:'原声修订',exists:false}),/原声修订.*文件缺失/)
  assert.match(videoFileLabel({review_ready:false}),/需重新审查或合成/)
  assert.doesNotMatch(videoFileLabel({version_name:'原声修订'}),/0:00|未知时长|Invalid Date/)
})

test('发布记录日期将旧UTC与带时区时间统一转换到本机日期，未知不冒充当天',()=>{
  const expected=new Date('2026-10-19T21:18:57Z').toLocaleDateString('zh-CN')
  for(const stamp of ['2026-10-19T21:18:57','2026-10-19 21:18:57','2026-10-19T21:18:57Z','2026-10-20T05:18:57+08:00'])
    assert.equal(publicationRecordDate(stamp),expected)
  for(const stamp of [null,undefined,'','broken','2026-99-99T99:99:99Z']) assert.equal(publicationRecordDate(stamp),'时间未知')
})
