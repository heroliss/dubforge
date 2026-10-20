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

test('评论最小最大字号同属译文样式，目标宽高属于整体布局，原文范围随独立样式隐藏',()=>{
  const fields=[{path:'comments.width_percent'},{path:'comments.height_percent'},
    {path:'comments.min_font_size'},{path:'comments.font_size'},
    {path:'comments.source_style_enabled'},
    {path:'comments.source_min_font_size',depends_on:[['comments.source_style_enabled',true]]},
    {path:'comments.source_font_size',depends_on:[['comments.source_style_enabled',true]]}]
  const values={'comments.source_style_enabled':false,'comments.source_min_font_size':18,'comments.source_font_size':30}
  const hidden=textStyleFieldGroups(fields,'comments.',values,'整体布局')
  assert.deepEqual(hidden.find(group=>group.name==='整体布局')?.fields.map(field=>field.path),['comments.width_percent','comments.height_percent'])
  assert.deepEqual(hidden.find(group=>group.name==='译文样式')?.fields.map(field=>field.path),['comments.min_font_size','comments.font_size'])
  assert.deepEqual(hidden.find(group=>group.name==='原文样式')?.fields.map(field=>field.path),['comments.source_style_enabled'])
  values['comments.source_style_enabled']=true
  const visible=textStyleFieldGroups(fields,'comments.',values,'整体布局')
  assert.deepEqual(visible.find(group=>group.name==='原文样式')?.fields.map(field=>field.path),['comments.source_style_enabled','comments.source_min_font_size','comments.source_font_size'])
  assert.equal(values['comments.source_min_font_size'],18);assert.equal(values['comments.source_font_size'],30)
})
