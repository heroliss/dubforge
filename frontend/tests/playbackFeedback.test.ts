import assert from 'node:assert/strict'
import test from 'node:test'
import { playbackErrorMessage } from '../src/utils/playbackFeedback.ts'

test('播放失败提示区分权限、格式与加载，不误导用户重新合成', () => {
  assert.match(playbackErrorMessage({ name: 'NotAllowedError' }, 'audio'), /浏览器.*声音权限/)
  assert.match(playbackErrorMessage({ name: 'NotSupportedError' }, 'audio'), /音频无法解码/)
  assert.match(playbackErrorMessage({ name: 'NotSupportedError' }, 'video'), /视频无法解码/)
  assert.match(playbackErrorMessage(new Error('network'), 'audio'), /音频加载.*重新加载失败的媒体/)
  assert.match(playbackErrorMessage(null, 'video'), /视频加载.*后台连接/)
})
