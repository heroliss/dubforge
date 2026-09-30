// 媒体失败不应只表现为静音；提示重试播放，而不是让用户重新生成产物。
export function playbackErrorMessage(error: unknown, source: 'audio' | 'video') {
  const name = error && typeof error === 'object' && 'name' in error ? error.name : ''
  const media = source === 'video' ? '视频' : '音频'
  if (name === 'NotAllowedError') return '浏览器暂未允许有声播放，请再次点击「连续播放」或本句试听；若仍被阻止，请检查网站声音权限。'
  if (name === 'NotSupportedError') return `${media}无法解码或格式不受浏览器支持。可先重试播放；若反复失败，请检查对应媒体文件。`
  return `${media}加载或播放失败。请再次点击「连续播放」或本句试听，重新加载失败的媒体；若仍失败，请检查后台连接。`
}
