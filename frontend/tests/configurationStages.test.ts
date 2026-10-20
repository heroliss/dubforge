import assert from 'node:assert/strict'
import test from 'node:test'
import { WORKFLOW_STAGES } from '../src/utils/workflowStages.ts'
import { configurationSettingsKey, configurationStageKey, configurationStageSections } from '../src/utils/configurationStages.ts'

const field = (path: string) => ({ path, label: path, type: 'str' })
function schema() {
  const sections: Record<string, any> = { content_review: { label:'内容审查', fields:[field('content_review.enabled')] },
    subtitles: { label:'字幕导出', fields:[field('subtitles.mode'), field('subtitles.source_first')] },
    dubtask: {label:'配音计划', fields:[]} }
  for (const stage of [...WORKFLOW_STAGES].reverse()) {
    sections[stage.settingsKeys[0]] = { label:'旧名称', fields:[field(`${stage.settingsKeys[0]}.example`)] }
  }
  return sections
}

test('设置始终沿工作阶段清单排序与命名，独立字幕归入配音，审查列为通用规则', () => {
  const sections=configurationStageSections(schema())
  assert.deepEqual(sections.slice(0,9).map(([,section])=>section.stageKey),WORKFLOW_STAGES.map(stage=>stage.key))
  assert.deepEqual(sections.slice(0,9).map(([,section])=>section.label),WORKFLOW_STAGES.map(stage=>stage.label))
  assert.deepEqual(sections.slice(0,9).map(([,section])=>section.stageNumber),[1,2,3,4,5,6,7,8,9])
  assert.equal(sections.some(([key])=>['subtitles','dubtask'].includes(key)),false)
  const dubbing=sections.find(([key])=>key==='tts')![1]
  assert.deepEqual(dubbing.fields.map(field=>field.path),['tts.example','subtitles.mode','subtitles.source_first'])
  assert.equal(dubbing.fields[1].group,'字幕导出与时间')
  assert.equal(sections.at(-1)![0],'content_review');assert.equal(sections.at(-1)![1].category,'shared')
})

test('旧字幕局部范围只显示字幕参数，tts范围显示整个阶段，path过滤与新合并schema均兼容', () => {
  const old=schema()
  const subtitles=configurationStageSections(old,['subtitles'])
  assert.equal(subtitles.length,1);assert.equal(subtitles[0][0],'tts')
  assert.deepEqual(subtitles[0][1].fields.map(field=>field.path),['subtitles.mode','subtitles.source_first'])
  const merged={tts:{label:'配音',fields:[...old.tts.fields,...old.subtitles.fields]}}
  assert.deepEqual(configurationStageSections(merged,['subtitles'])[0][1].fields.map(field=>field.path),['subtitles.mode','subtitles.source_first'])
  assert.equal(configurationStageSections(old,['tts'])[0][1].fields.length,3)
  assert.deepEqual(configurationStageSections(old,['subtitles'],['subtitles.source_first'])[0][1].fields.map(field=>field.path),['subtitles.source_first'])
  assert.deepEqual(configurationStageSections(old,['tts'],[]),[])
})

test('同一参数只显示一次，不改变原schema；发布和字幕深链复用稳定设置key', () => {
  const original=schema(),before=JSON.stringify(original)
  original.tts.fields.push(field('subtitles.mode'))
  original.assemble.fields.push(field('assemble.burn_subtitles'))
  const modified=JSON.stringify(original),sections=configurationStageSections(original)
  const paths=sections.flatMap(([,section])=>section.fields.map(field=>field.path))
  assert.equal(new Set(paths).size,paths.length)
  assert.equal(paths.includes('assemble.burn_subtitles'),false)
  assert.equal(JSON.stringify(original),modified);assert.notEqual(before,modified)
  assert.equal(configurationStageKey('publication'),'publish');assert.equal(configurationSettingsKey('publish'),'publication')
  assert.equal(configurationStageKey('subtitles'),'tts');assert.equal(configurationSettingsKey('subtitles'),'tts')
  assert.equal(configurationStageKey('content_review'),'content_review')
})
