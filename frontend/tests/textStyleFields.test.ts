import assert from 'node:assert/strict'
import test from 'node:test'
import { textStyleFieldGroups, sourceStyleFieldVisible } from '../src/utils/textStyleFields.ts'
import { SUBTITLE_PREVIEW_PATHS } from '../src/utils/subtitleWorkspace.ts'

test('双语样式按语言分组；关闭独立样式只隐藏参数，不改变已有值和继承关系',()=>{
  const fields=[{path:'comments.position'},{path:'comments.font_size'},{path:'comments.color'},
    {path:'comments.source_first'},{path:'comments.source_style_enabled'},
    {path:'comments.source_color',depends_on:[['comments.source_style_enabled',true]]}]
  const values={'comments.source_style_enabled':false,'comments.source_color':'#123456'}
  const groups=textStyleFieldGroups(fields,'comments.',values,'整体布局')
  assert.deepEqual(groups.map(g=>g.name),['整体布局','译文样式','原文样式'])
  assert.deepEqual(groups[0].fields.map(f=>f.path),['comments.position','comments.source_first'])
  assert.deepEqual(groups[2].fields.map(f=>f.path),['comments.source_style_enabled'])
  values['comments.source_style_enabled']=true
  assert.equal(textStyleFieldGroups(fields,'comments.',values)[2].fields.length,2)
  assert.equal(values['comments.source_color'],'#123456')
  assert.equal(sourceStyleFieldVisible(fields.at(-1),values),true)
})

test('字幕原文样式所有字段均进入实时预览请求',()=>{
  for(const key of ['style_enabled','font','size','color','outline_color','outline'])
    assert.ok(SUBTITLE_PREVIEW_PATHS.includes('assemble.subtitle_source_'+key))
})
