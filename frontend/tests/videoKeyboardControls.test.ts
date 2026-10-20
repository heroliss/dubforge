import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { buildCutPreviewTimeline } from '../src/utils/cutPreviewTimeline.ts'

let server: ViteDevServer, Controls: any
before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false },
  })
  Controls = (await server.ssrLoadModule('/src/components/VideoControls.vue')).default
})
after(async () => { await server.close() })

class Media extends EventTarget {
  currentTime = 12; duration = 30; paused = true; ended = false
  muted = false; volume = .6; playbackRate = 1; plays = 0; pauses = 0
  textTracks: any[] = []; buffered = { length: 0 }
  async play() { this.plays++; this.paused = false; this.dispatchEvent(new Event('play')) }
  pause() { this.pauses++; this.paused = true; this.dispatchEvent(new Event('pause')) }
}

// 只提供事件目标查询能力；测试无需全局 DOM，也不会给浏览器原生控件伪造键盘行为。
class Target {
  readonly tagName: string
  readonly classList: { contains: (name: string) => boolean }
  readonly attributes: Record<string, string>
  readonly parentElement: Target | null
  constructor(tag = 'div', attributes: Record<string, string> = {}, parentElement: Target | null = null) {
    this.attributes = attributes
    this.parentElement = parentElement
    this.tagName = tag.toUpperCase()
    this.classList = { contains: name => (attributes.class || '').split(/\s+/).includes(name) }
  }
  get isContentEditable(): boolean {
    const value = this.attributes.contenteditable
    return value === '' || value === 'true' || (value !== 'false' && !!this.parentElement?.isContentEditable)
  }
  getAttribute(name: string) { return this.attributes[name] ?? null }
  matches(selectors: string): boolean {
    return selectors.split(',').some(raw => {
      let selector = raw.trim()
      const exclusions = [...selector.matchAll(/:not\(([^)]+)\)/g)].map(match => match[1])
      if (exclusions.some(exclusion => this.matches(exclusion))) return false
      selector = selector.replace(/:not\([^)]+\)/g, '')
      const tag = selector.match(/^[a-z][a-z0-9-]*/i)?.[0]
      if (tag && tag.toUpperCase() !== this.tagName) return false
      const classes = [...selector.matchAll(/\.([a-z0-9_-]+)/gi)].map(match => match[1])
      if (classes.some(name => !this.classList.contains(name))) return false
      return [...selector.matchAll(/\[([^\]=\s]+)(?:\s*=\s*["']?([^\]"']*)["']?)?\]/g)].every(match =>
        match[1] in this.attributes && (match[2] === undefined || this.attributes[match[1]] === match[2].trim()))
    })
  }
  closest(selector: string): Target | null {
    return this.matches(selector) ? this : this.parentElement?.closest(selector) || null
  }
}

function keyEvent(key: string, overrides: Record<string, any> = {}) {
  return {
    key, code: key === ' ' ? 'Space' : key, target: new Target('video'), defaultPrevented: false,
    ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, isComposing: false, repeat: false,
    prevented: 0, stopped: 0,
    preventDefault() { this.defaultPrevented = true; this.prevented++ },
    stopPropagation() { this.stopped++ },
    ...overrides,
  }
}
async function fixture(props: Record<string, any> = {}) {
  const media = props.media ?? new Media()
  let page: any
  const seeks: number[] = []
  const app = createSSRApp({ render: () => h(Controls, { media, ...props, onSeek: (value: number) => seeks.push(value) }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === Controls.__name) page = this.$.setupState } })
  const html = await renderToString(app)
  return {
    page, media, seeks, html,
    press(key: string, overrides: Record<string, any> = {}) {
      const event = keyEvent(key, overrides)
      page.onPlayerKeydown(event)
      return event
    },
    cleanup() {
      page.detach?.()
      page.clearHideControlsTimer?.()
      if (page.clearMessageTimer) clearTimeout(page.clearMessageTimer)
    },
  }
}

test('1500条字幕的不变播放时钟不重新读取文本，控制条位置变化仍更新全部字幕',async()=>{
  const media=new Media();let reads=0
  const cues=Array.from({length:1500},(_,index)=>({get text(){reads++;return index===0?'第一行\n第二行\n第三行':'普通字幕'}})) as any[]
  media.textTracks=[{cues,mode:'showing'}]
  const f=await fixture({media})
  try{
    assert.equal(cues[0].line,61);assert.equal(cues[1].line,68)
    reads=0
    for(let index=0;index<100;index++)media.dispatchEvent(new Event('timeupdate'))
    media.dispatchEvent(new Event('volumechange'));media.dispatchEvent(new Event('progress'))
    assert.equal(reads,0,'缓存命中必须发生在cue.text读取和换行计算之前')
    f.page.controlsHidden=true;f.page.updateCuePosition()
    assert.equal(reads,1500);assert.equal(cues[0].line,80);assert.equal(cues[1].line,87)
    reads=0;f.page.updateCuePosition();assert.equal(reads,0)
  }finally{f.cleanup()}
})

test('相同条数的新轨道或cue列表仍重排，同列表重新载入时显式刷新并解绑事件',async()=>{
  const media=new Media(),first={text:'原字幕'} as any
  media.textTracks=[{cues:[first],mode:'showing'}]
  const f=await fixture({media})
  try{
    const replacement={text:'新字幕\n第二行\n第三行'} as any
    media.textTracks[0].cues=[replacement]
    media.dispatchEvent(new Event('timeupdate'))
    assert.equal(replacement.line,61,'替换cue列表且条数相同也需要重排')
    const trackCue={text:'另一轨道'} as any
    media.textTracks=[{cues:[trackCue],mode:'showing'}]
    media.dispatchEvent(new Event('timeupdate'));assert.equal(trackCue.line,68)
    trackCue.text='同列表内容更新\n第二行\n第三行\n第四行'
    const load=new Event('load');Object.defineProperty(load,'target',{value:{tagName:'TRACK'}})
    media.dispatchEvent(load);assert.equal(trackCue.line,54,'track load显式刷新同身份和同数量内容')
    f.cleanup();trackCue.line=0
    const afterDetach=new Event('load');Object.defineProperty(afterDetach,'target',{value:{tagName:'TRACK'}})
    media.dispatchEvent(afterDetach);media.dispatchEvent(new Event('timeupdate'))
    assert.equal(trackCue.line,0,'卸载后不保留字幕事件订阅')
  }finally{f.cleanup()}
})
function handled(event: ReturnType<typeof keyEvent>) {
  assert.equal(event.defaultPrevented, true)
  assert.equal(event.prevented, 1)
  assert.equal(event.stopped, 1)
}
function untouched(event: ReturnType<typeof keyEvent>) {
  assert.equal(event.prevented, 0)
  assert.equal(event.stopped, 0)
}

test('方向键按实时视频位置单次跳转五秒，并限制在首尾', async () => {
  const f = await fixture()
  try {
    handled(f.press('ArrowRight')); assert.equal(f.media.currentTime, 17)
    handled(f.press('ArrowLeft')); assert.equal(f.media.currentTime, 12)
    f.media.currentTime = 26
    handled(f.press('ArrowRight')); assert.equal(f.media.currentTime, 30)
    f.media.currentTime = 2
    handled(f.press('ArrowLeft')); assert.equal(f.media.currentTime, 0)
    assert.equal(f.media.plays, 0); assert.equal(f.media.pauses, 0)
    assert.ok(f.page.controlMessage, '跳转后应有可见反馈')
  } finally { f.cleanup() }
})

test('逗号和句号逐帧定位并自动暂停，默认按30fps限制在首尾', async () => {
  const f = await fixture()
  try {
    handled(f.press('.'))
    assert.ok(Math.abs(f.media.currentTime - (12 + 1 / 30)) < 1e-9)
    assert.equal(f.media.pauses, 0, '已经暂停时不应重复调用 pause')
    f.media.paused = false
    handled(f.press(','))
    assert.ok(Math.abs(f.media.currentTime - 12) < 1e-9)
    assert.equal(f.media.paused, true)
    assert.equal(f.media.pauses, 1)
    f.media.currentTime = 0
    handled(f.press(',')); assert.equal(f.media.currentTime, 0)
    f.media.currentTime = 30
    handled(f.press('.')); assert.equal(f.media.currentTime, 30)
    assert.match(f.page.controlMessage, /下一帧|上一帧/)
  } finally { f.cleanup() }
})

test('可传入源视频帧率，逐帧步长随配置变化', async () => {
  const f = await fixture({ frameRate: 24 })
  try {
    handled(f.press('.'))
    assert.ok(Math.abs(f.media.currentTime - (12 + 1 / 24)) < 1e-9)
    assert.match(f.page.controlMessage, /1\/24 秒/)
  } finally { f.cleanup() }
})

test('接受 ffprobe 的分数帧率字符串，避免把29.97fps误当30fps', async () => {
  const f = await fixture({ frameRate: '30000/1001' })
  try {
    handled(f.press('.'))
    // 12 秒并非 29.97fps 的整帧边界；先取最近的帧，再前进一帧。
    assert.ok(Math.abs(f.media.currentTime - (361 * 1001 / 30000)) < 1e-9)
    assert.match(f.page.controlMessage, /1\/29\.97 秒/)
  } finally { f.cleanup() }
})

test('逐帧连续操作按帧序号计算，避免非整帧 currentTime 累积误差', async () => {
  const f = await fixture({ frameRate: 24 })
  try {
    f.media.currentTime = 10.001
    handled(f.press('.')); assert.ok(Math.abs(f.media.currentTime - (10 + 1 / 24)) < 1e-9)
    handled(f.press(',')); assert.ok(Math.abs(f.media.currentTime - 10) < 1e-9)
  } finally { f.cleanup() }
})

test('长按逗号或句号可以连续逐帧，其他切换键仍忽略连发', async () => {
  const f = await fixture()
  try {
    handled(f.press('.', { repeat: true })); assert.ok(Math.abs(f.media.currentTime - (12 + 1 / 30)) < 1e-9)
    handled(f.press('.', { repeat: true })); assert.ok(Math.abs(f.media.currentTime - (12 + 2 / 30)) < 1e-9)
    handled(f.press(' ', { repeat: true })); assert.equal(f.media.plays, 0)
  } finally { f.cleanup() }
})

test('Home 和 End 定位首尾；长按方向键继续移动', async () => {
  const f = await fixture()
  try {
    handled(f.press('Home')); assert.equal(f.media.currentTime, 0)
    handled(f.press('ArrowRight', { repeat: true })); assert.equal(f.media.currentTime, 5)
    handled(f.press('End')); assert.equal(f.media.currentTime, 30)
    untouched(f.press('q')); assert.equal(f.media.currentTime, 30)
  } finally { f.cleanup() }
})

test('上下键调整普通视频监听音量，遵守零至一边界，不修改播放位置', async () => {
  const f = await fixture()
  try {
    handled(f.press('ArrowUp')); assert.ok(Math.abs(f.media.volume - .65) < 1e-9)
    handled(f.press('ArrowDown')); assert.ok(Math.abs(f.media.volume - .6) < 1e-9)
    f.media.volume = .99; f.media.dispatchEvent(new Event('volumechange'))
    handled(f.press('ArrowUp')); assert.equal(f.media.volume, 1)
    f.media.volume = .01; f.media.dispatchEvent(new Event('volumechange'))
    handled(f.press('ArrowDown')); assert.equal(f.media.volume, 0); assert.equal(f.media.muted, true)
    assert.equal(f.media.currentTime, 12); assert.equal(f.media.plays, 0)
    assert.ok(f.page.controlMessage, '音量修改后应有可见反馈')
  } finally { f.cleanup() }
})

test('多音轨方向键音量与静音走监听合同，不直接修改强制静音的视频', async () => {
  const audio = reactive({ volume: .4, muted: false }), changes: any[] = []
  const f = await fixture({ audioControl: audio, onAudioChange: (value: any) => { changes.push(value); Object.assign(audio, value) } })
  f.media.muted = true
  try {
    handled(f.press('ArrowUp')); assert.ok(Math.abs(audio.volume - .45) < 1e-9)
    handled(f.press('ArrowDown')); assert.ok(Math.abs(audio.volume - .4) < 1e-9)
    handled(f.press('m')); assert.equal(audio.muted, true)
    handled(f.press('m')); assert.equal(audio.muted, false)
    assert.equal(changes.length, 4)
    assert.equal(f.media.volume, .6); assert.equal(f.media.muted, true)
    assert.equal(f.media.currentTime, 12); assert.equal(f.media.plays, 0)
  } finally { f.cleanup() }
})

test('空格和 K 切换播放，切换类按键连发只拦截不重复切换', async () => {
  const f = await fixture()
  try {
    handled(f.press(' ')); assert.equal(f.media.plays, 1); assert.equal(f.media.paused, false)
    handled(f.press(' ', { repeat: true })); assert.equal(f.media.pauses, 0)
    handled(f.press('k')); assert.equal(f.media.pauses, 1); assert.equal(f.media.paused, true)
    handled(f.press('k', { repeat: true })); assert.equal(f.media.plays, 1)
    const muted = f.media.muted
    handled(f.press('m', { repeat: true })); assert.equal(f.media.muted, muted)
    handled(f.press('m')); assert.equal(f.media.muted, !muted)
  } finally { f.cleanup() }
})

test('点击画面后立即按 K 不会被延迟的单击判定反向暂停', async () => {
  const f = await fixture()
  try {
    f.page.onMediaClick()
    handled(f.press('k'))
    assert.equal(f.media.plays, 1); assert.equal(f.media.pauses, 0)
    await new Promise(resolve => setTimeout(resolve, 250))
    assert.equal(f.media.plays, 1); assert.equal(f.media.pauses, 0)
    assert.equal(f.media.paused, false)
  } finally { f.cleanup() }
})

test('修饰键、输入法和已处理事件均不接管，避免进度条按键重复跳转', async () => {
  const f = await fixture()
  try {
    for (const modifier of ['ctrlKey', 'altKey', 'metaKey', 'shiftKey', 'isComposing']) {
      untouched(f.press('ArrowRight', { [modifier]: true }))
    }
    const event = keyEvent('ArrowRight', { target: new Target('div', { role: 'slider', class: 'timeline' }) })
    f.page.onProgressKey(event)
    assert.equal(f.media.currentTime, 17)
    f.page.onPlayerKeydown(event)
    assert.equal(f.media.currentTime, 17, '进度条已处理后，容器不能再前进五秒')
    assert.equal(event.prevented, 1)
  } finally { f.cleanup() }
})

test('编辑字段、下拉菜单和音量滑条保留原有按键语义，包括其内部子元素', async () => {
  const f = await fixture()
  try {
    const targets = [
      new Target('input'), new Target('textarea'), new Target('select'),
      new Target('div', { contenteditable: 'true' }),
      ...['textbox', 'combobox', 'listbox', 'menu', 'spinbutton', 'slider'].map(role => new Target('div', { role })),
    ]
    for (const parent of targets) {
      for (const target of [parent, new Target('span', {}, parent)]) {
        untouched(f.press('ArrowRight', { target }))
        untouched(f.press(' ', { target }))
      }
    }
    assert.equal(f.media.currentTime, 12); assert.equal(f.media.plays, 0)
    handled(f.press('ArrowRight', { target: new Target('div', { role: 'slider', class: 'timeline' }) }))
    assert.equal(f.media.currentTime, 17, '视频进度滑条仍可执行播放器跳转')
  } finally { f.cleanup() }
})

test('按钮和链接上的空格保留原生操作，不同时切换视频', async () => {
  const f = await fixture()
  try {
    for (const parent of [new Target('button'), new Target('a', { href: '#' }), new Target('div', { role: 'button' })]) {
      untouched(f.press(' ', { target: parent }))
      untouched(f.press(' ', { target: new Target('svg', {}, parent) }))
    }
    assert.equal(f.media.plays, 0); assert.equal(f.media.pauses, 0)
  } finally { f.cleanup() }
})

test('不同播放器实例的键盘操作互不影响', async () => {
  const first = await fixture(), second = await fixture()
  try {
    handled(first.press('ArrowRight'))
    assert.equal(first.media.currentTime, 17); assert.equal(second.media.currentTime, 12)
    handled(second.press('ArrowDown'))
    assert.equal(first.media.volume, .6); assert.ok(Math.abs(second.media.volume - .55) < 1e-9)
  } finally { first.cleanup(); second.cleanup() }
})


function mappedTimeline() {
  return buildCutPreviewTimeline({ mediaDuration: 30, fps: 10, deleteRanges: [{start: 5, end: 10}] })
}

test('剪后进度、时长与拖动都使用逻辑时间，seek只写入已映射真实媒体位置', async () => {
  const media=new Media(), writes:number[]=[];let position=12
  Object.defineProperty(media,'currentTime',{get:()=>position,set:(value:number)=>{writes.push(value);position=value}})
  const f=await fixture({media,timeline:mappedTimeline()})
  try {
    assert.equal(f.page.duration,25);assert.equal(f.page.current,7)
    assert.match(f.html,/剪后视频进度/)
    f.page.seek(6)
    assert.equal(media.currentTime,11);assert.deepEqual(writes,[11]);assert.deepEqual(f.seeks,[11])
    const target={getBoundingClientRect:()=>({left:0,width:100}),setPointerCapture(){},hasPointerCapture(){return false}}
    f.page.beginScrub({currentTarget:target,clientX:25,pointerId:1})
    assert.equal(media.currentTime,11.25)
    f.page.endScrub({currentTarget:target,pointerId:1})
    assert.ok(writes.every(value=>value<5 || value>=10),'不能先写入被删除区再要求父级纠正')
  } finally {f.cleanup()}
})

test('剪后左右5秒、按钮及HomeEnd跨过删除段，所有seek事件仍是媒体秒', async () => {
  const media=new Media();media.currentTime=4.8
  const f=await fixture({media,timeline:mappedTimeline()})
  try {
    handled(f.press('ArrowRight'));assert.ok(Math.abs(media.currentTime-14.8)<1e-9)
    handled(f.press('ArrowLeft'));assert.ok(Math.abs(media.currentTime-4.8)<1e-9)
    f.page.jumpBy(5);assert.ok(Math.abs(media.currentTime-14.8)<1e-9)
    handled(f.press('Home'));assert.equal(media.currentTime,0)
    handled(f.press('End'));assert.equal(media.currentTime,29.9)
    assert.equal(f.seeks.at(-1),29.9);assert.match(f.page.controlMessage,/剪后跳到结尾/)
  } finally {f.cleanup()}
})

test('剪后逐帧跨接缝前后均落在保留帧，支持媒体尚未追上时快速连续输入', async () => {
  const media=new Media(),writes:number[]=[];let position=4.9
  Object.defineProperty(media,'currentTime',{get:()=>position,set:(value:number)=>writes.push(value)})
  const f=await fixture({media,timeline:mappedTimeline()})
  try {
    handled(f.press('.'));assert.equal(writes.at(-1),10)
    handled(f.press('.'));assert.equal(writes.at(-1),10.1)
    position=10 // 第一条seek异步完成，不能覆盖用户已请求的第二条目标
    handled(f.press('.'));assert.equal(writes.at(-1),10.2)
    position=10.2;media.dispatchEvent(new Event('seeked'))
    handled(f.press(','));assert.equal(writes.at(-1),10.1)
    handled(f.press(','));assert.equal(writes.at(-1),10)
    handled(f.press(','));assert.equal(writes.at(-1),4.9)
    assert.ok(writes.every(value=>value<5 || value>=10))
    position=2;media.dispatchEvent(new Event('timeupdate'))
    handled(f.press('.'));assert.equal(writes.at(-1),2.1,'父级/原生外部定位后应以新位置为准')
  } finally {f.cleanup()}
})

test('连续左右键使用尚未完成的最新目标，缓冲与章节也映到剪后进度', async () => {
  const media=new Media(),writes:number[]=[];let position=2
  Object.defineProperty(media,'currentTime',{get:()=>position,set:(value:number)=>writes.push(value)})
  media.buffered={length:1,end:()=>20} as any
  const f=await fixture({media,timeline:mappedTimeline(),chapters:[{title:'删除后章节',start_time:10,end_time:20}]})
  try {
    handled(f.press('ArrowRight'));assert.equal(writes.at(-1),12)
    handled(f.press('ArrowRight'));assert.equal(writes.at(-1),17)
    assert.equal(f.page.current,12)
    assert.equal(f.page.buffered,15);assert.equal(f.page.normalizedChapters[0].start,5)
    assert.equal(f.page.normalizedChapters[0].end,15)
  } finally {f.cleanup()}
})

test('无可保留帧的剪后映射不会播放或定位原始删除区', async () => {
  const media=new Media(),timeline=buildCutPreviewTimeline({mediaDuration:30,fps:10,deleteRanges:[{start:0,end:30}]})
  const f=await fixture({media,timeline})
  try {
    f.page.seek(5);f.page.stepFrame(1);f.page.jumpBy(5);f.page.togglePlay()
    assert.equal(f.page.duration,0);assert.equal(media.currentTime,12);assert.equal(media.plays,0)
    assert.deepEqual(f.seeks,[])
  } finally {f.cleanup()}
})


test('微秒舍入的60fps媒体可连续跨过真实接缝，不能每次重复第一保留帧', async () => {
  const media=new Media(),writes:number[]=[];media.duration=1921.841
  let position=1898.533333
  Object.defineProperty(media,'currentTime',{get:()=>position,set:(value:number)=>{writes.push(value);position=Math.round(value*1e6)/1e6}})
  const timeline=buildCutPreviewTimeline({mediaDuration:media.duration,fps:60,deleteRanges:[{start:1898.55,end:1904.2333333333333}]})
  const f=await fixture({media,timeline})
  try {
    for(const expected of [1904.233333,1904.25,1904.266667]) {
      handled(f.press('.'))
      assert.ok(Math.abs(media.currentTime-expected)<1e-6,`应前进到${expected}，实际${media.currentTime}`)
      media.dispatchEvent(new Event('timeupdate'))
    }
    assert.equal(writes.length,3)
    assert.ok(writes[0]<writes[1]&&writes[1]<writes[2])
    // 真正外部移动一帧，远大于微秒容差，必须从新位置继续。
    position=1904.3;media.dispatchEvent(new Event('timeupdate'))
    handled(f.press('.'));assert.ok(Math.abs(media.currentTime-1904.316667)<1e-6)
  } finally {f.cleanup()}
})
