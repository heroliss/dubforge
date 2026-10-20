import assert from 'node:assert/strict'
import test from 'node:test'
import { commentCandidateReadiness, commentTextBlockingIssues, commentTextIssues, commentTextLength, defaultCommentTextLimits as limits } from '../src/utils/commentOverlay.ts'

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

test('候选处理阶段与最终文字校验分开，未评分或未翻译不当成上屏失败',()=>{
  const pending={source_text:'x'.repeat(1011),display_text:'  '}
  assert.deepEqual(commentCandidateReadiness(pending,'bilingual',limits,null),{status:'pending-score',issues:[]})
  assert.deepEqual(commentCandidateReadiness(pending,'bilingual',limits,false),{status:'not-suggested',issues:[]})
  for(const mode of ['translated','bilingual']){
    assert.deepEqual(commentCandidateReadiness(pending,mode,limits,true),{status:'pending-translation',issues:[]})
  }
  // 全部问题仍保留给提示展示；是否阻止人工选择由专用门禁区分。
  assert.equal(commentTextIssues(pending,'bilingual',limits).length,2)
  assert.equal(pending.source_text.length,1011)
  assert.equal(pending.display_text,'  ')
})

test('只有明确手动保留的超长文字可提示后继续，自动与旧来源仍遵守候选限制',()=>{
  const long={source_text:'x'.repeat(601),display_text:'中'.repeat(241)}
  const manual={...long,selection_origin:'manual'}
  assert.equal(commentTextIssues(manual,'bilingual',limits).length,2,'警告仍包含原文和中文，不丢失信息')
  assert.deepEqual(commentTextBlockingIssues(manual,'bilingual',limits),[])
  for(const origin of [undefined,'automatic','legacy','unexpected']){
    assert.deepEqual(commentTextBlockingIssues({...long,selection_origin:origin},'bilingual',limits),commentTextIssues(long,'bilingual',limits))
  }
  assert.equal(manual.source_text.length,601)
  assert.equal(manual.display_text.length,241)
})

test('人工保留不豁免空文字，所选语言需要的每种文字都必须存在',()=>{
  const item={source_text:'x'.repeat(601),display_text:' \n ',selection_origin:'manual'}
  assert.deepEqual(commentTextBlockingIssues(item,'bilingual',limits),[{field:'display_text',message:'中文上屏文字为空'}])
  assert.deepEqual(commentTextBlockingIssues(item,'original',limits),[])
  assert.deepEqual(commentTextBlockingIssues({...item,source_text:'\t'},'bilingual',limits),[
    {field:'display_text',message:'中文上屏文字为空'},
    {field:'source_text',message:'原文上屏文字为空'},
  ])
})

test('原文模式无需翻译，完成文字后的真实超长按所选语言检查',()=>{
  const original={source_text:'无需中文也可上屏',display_text:''}
  assert.deepEqual(commentCandidateReadiness(original,'original',limits,true),{status:'ready',issues:[]})
  const long={source_text:'x'.repeat(601),display_text:'有效中文'}
  assert.deepEqual(commentCandidateReadiness(long,'translated',limits,true),{status:'ready',issues:[]})
  for(const mode of ['original','bilingual']){
    const result=commentCandidateReadiness(long,mode,limits,true)
    assert.equal(result.status,'needs-text')
    assert.deepEqual(result.issues,[{field:'source_text',message:'原文 601/600 字符，超出 1 字符'}])
  }
  const translated={source_text:'valid source',display_text:'中'.repeat(241)}
  assert.equal(commentCandidateReadiness(translated,'translated',limits,true).status,'needs-text')
  assert.equal(commentCandidateReadiness(translated,'translated',{translated:300,original:600},true).status,'ready')
})
