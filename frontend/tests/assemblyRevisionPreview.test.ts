import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const source = await readFile(new URL('../src/components/AssemblyVersionWorkspace.vue', import.meta.url), 'utf8')

test('局部修订与所选更新保留当前正式成片预览入口', () => {
  assert.match(source, /active\.update \|\| active\.partial/)
  assert.match(source, /当前修订版成片预览/)
  assert.match(source, /activeFormalPreviewUrl/)
  assert.match(source, /<VideoPreview v-if="activeFormalPreviewUrl"/)
  assert.match(source, /还没有可播放的修订成片/)
})
