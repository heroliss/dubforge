import assert from 'node:assert/strict'
import test from 'node:test'
import { chapterIssues, chapterRuleText, chapterSourceLabel, preserveChapterSources } from '../src/utils/chapterRules.ts'

test('平台硬规则区分首章0秒与首分界1秒，以及10章9分界',()=>{
  const text=chapterRuleText()
  assert.match(text,/10 个章节（9 个分界点）/);assert.match(text,/首个分界点至少 1 秒/)
  const items=[{title:'开场',start_time:0,end_time:1},{title:'正文',start_time:1,end_time:6}]
  assert.deepEqual(chapterIssues(items,6),[])
  assert.match(chapterIssues([{title:'开场',start_time:1,end_time:6}],6).join(''),/首章应从 0 秒开始/)
  assert.match(chapterIssues([{title:'开场',start_time:0,end_time:.4}],1).join(''),/至少保留 1 秒/)
})

test('超量标题超限和尾章过短分别显示，emoji按平台UTF16字符位算',()=>{
  const items=Array.from({length:11},(_,index)=>({title:'章',start_time:index*5,end_time:index*5+5}))
  assert.match(chapterIssues(items,55).join(''),/超过 10 个上限/)
  assert.deepEqual(chapterIssues([{title:'😀'.repeat(8),start_time:0,end_time:8}],8),[])
  assert.match(chapterIssues([{title:'😀'.repeat(9),start_time:0,end_time:8}],8).join(''),/超过 16 个字符/)
  assert.match(chapterIssues([{title:'开场',start_time:0,end_time:1},{title:'结尾',start_time:1,end_time:5}],5).join(''),/第 2 章需至少保留 5 秒/)
})

test('小数时间按网页取整后检查，原始小数区间与整数长度可能不同',()=>{
  assert.deepEqual(chapterIssues([{title:'首章',start_time:0,end_time:1},{title:'正文',start_time:1,end_time:5.6}],5.6),[])
  assert.deepEqual(chapterIssues([{title:'首章',start_time:0,end_time:1.6},{title:'正文',start_time:1.6,end_time:6.7}],6.7),[])
  assert.deepEqual(chapterIssues([{title:'首章',start_time:0,end_time:1.4},{title:'正文',start_time:1.4,end_time:5.5}],5.5),[])
  assert.match(chapterIssues([{title:'首章',start_time:0,end_time:1.6},{title:'正文',start_time:1.6,end_time:6.4}],6.4).join(''),/第 2 章需至少保留 5 秒/)
})

test('未知末尾时长不虚构通过，显式结束时间与重叠实际提交一致',()=>{
  assert.match(chapterIssues([{title:'开场',start_time:0}]).join(''),/至少保留 1 秒/)
  assert.match(chapterIssues([{title:'开场',start_time:0,end_time:7},{title:'正文',start_time:5,end_time:10}],10).join(''),/与下一章时间重叠/)
})

test('读取后端实时规则并将零起源章编号显示为用户序号范围',()=>{
  assert.match(chapterIssues([{title:'四个字符',start_time:0,end_time:8}],8,{title_max_length:3}).join(''),/超过 3 个字符/)
  assert.equal(chapterSourceLabel([0,1,2,4,6,7,7]),'合并自源章节 1–3、5、7–8')
  assert.equal(chapterSourceLabel([3]),'来自源章节 4');assert.equal(chapterSourceLabel(null),'')
})

test('再次整理展开已有来源，返回的本次索引不会冒充原视频索引',()=>{
  const original=[{title:'AB',source_indices:[0,1]},{title:'C',source_indices:[2]},{title:'D',source_indices:[3]}]
  const before=JSON.stringify(original)
  const items=preserveChapterSources([{title:'ABC',source_indices:[0,1]},{title:'D',source_indices:[2]}],original)
  assert.deepEqual(items.map(row=>row.source_indices),[[0,1,2],[3]])
  assert.equal(chapterSourceLabel(items[0].source_indices),'合并自源章节 1–3')
  assert.equal(JSON.stringify(original),before)
})
