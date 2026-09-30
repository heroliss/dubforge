import assert from 'node:assert/strict'
import test from 'node:test'
import { commentTextIssues, commentTextLength, defaultCommentTextLimits as limits } from '../src/utils/commentOverlay.ts'

test('字符计数与后端一致，边界、空白、表情不误判',()=>{
  assert.equal(commentTextLength('  🎉中\n  '),2)
  assert.deepEqual(commentTextIssues({display_text:'🎉'.repeat(240),source_text:'x'.repeat(600)},'bilingual',limits),[])
  assert.equal(commentTextIssues({display_text:'🎉'.repeat(241)},'translated',limits)[0].message,'中文 241/240 字符，超出 1 字符')
  assert.equal(commentTextIssues({display_text:'\n '},'translated',limits)[0].message,'中文上屏文字为空')
})

test('只校验所选语言并使用服务端提供的限制，不改写原文',()=>{
  const item={source_text:'x'.repeat(601),display_text:'完整中文'}
  assert.deepEqual(commentTextIssues(item,'translated',limits),[])
  assert.equal(commentTextIssues(item,'bilingual',limits)[0].field,'source_text')
  assert.equal(commentTextIssues(item,'original',{translated:240,original:500})[0].message,'原文 601/500 字符，超出 101 字符')
  assert.equal(item.source_text.length,601)
})
