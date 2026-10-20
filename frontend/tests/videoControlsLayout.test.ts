import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

// VideoControls 目前通过原生 VTTCue 调整字幕位置，位置变化发生在浏览器媒体实例上，
// 因而不能只靠 SSR 的 HTML 快照覆盖。这里锁定用户可见的布局契约，避免以后只保留
// 控制条自动隐藏而忘记同步字幕安全区。
const source = await readFile(new URL('../src/components/VideoControls.vue', import.meta.url), 'utf8')
const outputCutsSource = await readFile(new URL('../src/components/OutputCutsEditor.vue', import.meta.url), 'utf8')

test('控制条隐藏时字幕下移，显示时字幕上移避开控制条', () => {
  assert.match(source, /controlsHidden = ref\(false\)/)
  assert.match(source, /:class="\{ dragging, hidden: controlsHidden \}"/)
  assert.match(source, /const line = controlsHidden\.value \? 87 : 68/)
  assert.match(source, /cue\.snapToLines = false/)
  assert.match(source, /const cueLine = Math\.max\(46, line - Math\.max\(0, cueLines - 2\) \* 7\)/)
  assert.match(source, /cue\.line = cueLine/)
  assert.match(source, /cue\.position = 50/)
})

test('播放期间自动隐藏且交互、暂停和结束都会恢复控制条与字幕安全区', () => {
  assert.match(source, /\}, 2400\)/, '控制条应有延迟隐藏，而不是立即消失')
  assert.match(source, /media\.addEventListener\('pointermove', onMediaPointerActivity\)/)
  assert.match(source, /const onContainerPointerActivity = \(event: PointerEvent\) => \{/)
  assert.match(source, /target\.addEventListener\('pointermove', onContainerPointerActivity\)/)
  assert.match(source, /target\.addEventListener\('pointerenter', onContainerPointerActivity\)/)
  assert.match(source, /pointerNearControls = ref\(false\)/)
  assert.match(source, /event\.clientY >= rect\.bottom - zone/)
  assert.match(source, /target\.addEventListener\('pointerleave', onContainerPointerLeave\)/)
  assert.match(source, /document\.addEventListener\('pointermove', onFullscreenPointerActivity\)/)
  assert.match(source, /document\.addEventListener\('mousemove', onFullscreenPointerActivity\)/)
  assert.match(source, /controls-reveal-zone/)
  assert.match(source, /onRevealPointerActivity/)
  assert.match(source, /controls-hidden.*controls-reveal-zone/s)
  assert.match(source, /if \(!controlsHidden\.value \|\| pointerNearControls\.value\) showControls\(\)/)
  assert.match(source, /height:64px/)
  assert.match(source, /target\.removeEventListener\('pointermove', onContainerPointerActivity\)/)
  assert.match(source, /@pointermove="showControls"/)
  assert.match(source, /@focusin="showControls"/)
  assert.match(source, /onPause = \(\) => \{[^}]*controlsHidden\.value = false/s)
  assert.match(source, /onEnded = \(\) => \{[^}]*controlsHidden\.value = false/s)
  assert.match(source, /\.video-controls\.hidden\{opacity:0;pointer-events:none\}/)
})

test('控制条提供逐帧按钮和快捷键提示，并默认按30fps估算步长', () => {
  assert.match(source, /frameRate\?: number/)
  assert.match(source, /frameStep = computed\(\(\) => 1 \/ effectiveFrameRate\.value\)/)
  assert.match(source, /function stepFrame\(direction: -1 \| 1\)/)
  assert.match(source, /上一帧（逗号）/)
  assert.match(source, /下一帧（句号）/)
  assert.match(source, /const frameKey = key === ',' \|\| key === '\.'/)
})

test('剪辑播放器的原生和降级全屏都覆盖整个视口', () => {
  // 成片剪辑使用 .cuts-player 作为 Fullscreen API 的目标；只覆盖
  // .video-shell/.video-preview 会让浏览器保留原来的上半屏尺寸。
  assert.match(source, /\.cuts-player:fullscreen/)
  assert.match(source, /\.cuts-player:-webkit-full-screen/)
  assert.match(source, /\.cuts-player:fullscreen > video/)
  assert.match(source, /\.cuts-player:-webkit-full-screen > video/)
  assert.match(source, /video-controls-fallback-fullscreen\)\{position:fixed!important/)
})

test('当前成片参考播放器的原生和降级全屏也必须覆盖整个视口', () => {
  // 当前成片参考播放器采用 .target-reference-player 作为 Fullscreen API 目标。
  // 如果这里只覆盖 .cuts-player，参考成片仍会受 max-height:420px 限制而显示在上半部分。
  assert.match(outputCutsSource, /<div class="target-reference-player">[\s\S]*?<VideoControls :media="referenceVideo"/, '参考播放器必须由独立的定位容器包裹')
  assert.match(source, /\.target-reference-player:fullscreen/)
  assert.match(source, /\.target-reference-player:-webkit-full-screen/)
  assert.match(source, /\.target-reference-player:fullscreen > video/)
  assert.match(source, /\.target-reference-player:-webkit-full-screen > video/)
  assert.match(source, /\.target-reference-player:fullscreen > \.video-controls-layer/)
  assert.match(source, /\.target-reference-player:-webkit-full-screen > \.video-controls-layer/)
})

test('定位时发出开始、位置和结束事件，让单一虚拟试听播放器保持时间轴主导', () => {
  assert.match(source, /\bseek: \[value: number\]/)
  assert.match(source, /'seek-start': \[\]/)
  assert.match(source, /'seek-end': \[\]/)
  assert.match(source, /emit\('seek', target\)/)
  assert.match(source, /emit\('seek-start'\)/)
  assert.match(source, /emit\('seek-end'\)/)
  assert.match(outputCutsSource, /@seek-start="onReferenceSeekStart" @seek="onReferenceSeek" @seek-end="onReferenceSeekEnd"/)
  assert.doesNotMatch(outputCutsSource, /@seek-start="onSourceSeekStart" @seek="onSourceSeek" @seek-end="onSourceSeekEnd"/)
})
