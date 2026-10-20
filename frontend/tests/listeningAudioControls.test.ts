import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { MediaListeningGain } from '../src/utils/mediaListeningGain.ts'

let server:ViteDevServer,Controls:any,Tracks:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Controls=(await server.ssrLoadModule('/src/components/VideoControls.vue')).default
  Tracks=(await server.ssrLoadModule('/src/components/AudioTracksPlayer.vue')).default
  mock.method(globalThis,'fetch',async()=>({ok:false}))
})
after(async()=>{mock.restoreAll();await server.close()})

class Media extends EventTarget {
  currentTime=3;paused=false;ended=false;muted=true;volume=1;duration=30;playbackRate=1
  textTracks:any[]=[];buffered={length:0};readyState=4;pauses=0;plays=0
  pause(){this.paused=true;this.pauses++}
  async play(){this.paused=false;this.plays++}
}
async function render(Component:any,props:any,seed:(page:any)=>void=()=>{}) {
  let page:any
  const app=createSSRApp({render:()=>h(Component,props)});setup(app)
  app.mixin({created(){if(this.$options.__name===Component.__name){page=this.$.setupState;seed(page)}}})
  const html=await renderToString(app)
  return {page,html,cleanup:()=>page.detach?.()}
}

test('外部监听合同控制总音量和静音，不触碰已强制静音的视频或时间轴',async()=>{
  const video=new Media(),audio=reactive({volume:1,muted:false}),events:any[]=[]
  const {page,html,cleanup}=await render(Controls,{media:video,audioControl:audio,
    onAudioChange:(value:any)=>{events.push(value);Object.assign(audio,value)}})
  try {
    assert.equal(page.listeningMuted,false,'配音路由视频静音不代表实际监听静音')
    assert.match(html,/aria-label="静音"/);assert.match(html,/监听总音量/)
    page.setVolume(.4);assert.deepEqual(events.at(-1),{volume:.4,muted:false})
    assert.equal(page.listeningVolume,.4);assert.equal(video.volume,1);assert.equal(video.muted,true)
    page.toggleMute();assert.equal(audio.muted,true);assert.equal(audio.volume,.4)
    page.toggleMute();assert.equal(audio.muted,false);assert.equal(audio.volume,.4)
    page.setVolume(0);page.toggleMute();assert.equal(audio.volume,.7);assert.equal(audio.muted,false)
    assert.equal(video.currentTime,3);assert.equal(video.pauses,0);assert.equal(video.plays,0)
  } finally {cleanup()}
})

test('未传外部监听合同的普通视频保持原生音量行为',async()=>{
  const video=new Media(),{page,cleanup}=await render(Controls,{media:video})
  try {
    page.setVolume(.35);assert.equal(video.volume,.35);assert.equal(video.muted,false)
    page.toggleMute();assert.equal(video.muted,true)
    page.toggleMute();assert.equal(video.muted,false);assert.equal(video.currentTime,3)
  } finally {cleanup()}
})

test('音轨页静音恢复保持选择，监听音量作用所有轨及新媒体节点',async()=>{
  const video=new Media(),vocal=new Media(),background=new Media()
  const {page,cleanup}=await render(Tracks,{videoUrl:'/video.mp4',tracks:[
    {name:'raw.wav',label:'原始',url:'/raw.wav',peaks:'/raw'},
    {name:'vocal.wav',label:'人声',url:'/vocal.wav',peaks:'/vocal'},
    {name:'background.wav',label:'背景',url:'/bg.wav',peaks:'/bg'},
  ]},page=>{page.videoEl=video;page.setAudio('vocal.wav',vocal);page.setAudio('background.wav',background);page.selected='__mixed__'})
  try {
    await nextTick()
    page.setMonitorAudio({volume:.25,muted:true})
    assert.equal(page.selected,'__mixed__');assert.equal(vocal.muted,true);assert.equal(background.muted,true)
    page.setMonitorAudio({volume:.25,muted:false})
    assert.equal(page.selected,'__mixed__');assert.equal(video.muted,true)
    assert.equal(vocal.muted,false);assert.equal(vocal.volume,.25);assert.equal(background.volume,.25)
    const replacement=new Media();page.setAudio('vocal.wav',replacement)
    assert.equal(replacement.volume,.25);assert.equal(replacement.muted,false)
    page.selected='raw.wav';page.applyMonitorAudio();await nextTick()
    assert.equal(video.muted,false);assert.equal(video.volume,.25)
    assert.equal(video.currentTime,3)
  } finally {cleanup()}
})

test('正增益使用懒建WebAudio节点，后续静音与恢复不创建重复节点，释放时断开',async t=>{
  const gains:any[]=[],sources:any[]=[],contexts:any[]=[],warnings:string[]=[]
  class Context {
    state='suspended';destination={};resumes=0;closes=0
    constructor(){contexts.push(this)}
    createMediaElementSource(media:any){const source={media,disconnects:0,connect(){},disconnect(){this.disconnects++}};sources.push(source);return source}
    createGain(){const gain={gain:{value:1},disconnects:0,connect(){},disconnect(){this.disconnects++}};gains.push(gain);return gain}
    async resume(){this.resumes++;this.state='running'}
    async close(){this.closes++;this.state='closed'}
  }
  const previous=Object.getOwnPropertyDescriptor(globalThis,'AudioContext')
  Object.defineProperty(globalThis,'AudioContext',{configurable:true,value:Context})
  t.after(()=>previous?Object.defineProperty(globalThis,'AudioContext',previous):Reflect.deleteProperty(globalThis,'AudioContext'))
  const manager=new MediaListeningGain(message=>warnings.push(message)),media=new Media()
  manager.set(media as any,.5);assert.equal(contexts.length,0);assert.equal(media.volume,.5)
  manager.set(media as any,10**(6/20));assert.equal(contexts.length,1);assert.equal(media.volume,1)
  assert.equal(gains[0].gain.value,10**(6/20));assert.equal(contexts[0].resumes,1)
  manager.set(media as any,0);manager.set(media as any,.3)
  assert.equal(sources.length,1);assert.equal(gains[0].gain.value,.3)
  manager.release(media as any);assert.equal(sources[0].disconnects,1);assert.equal(gains[0].disconnects,1)
  manager.dispose();assert.equal(contexts[0].closes,1);assert.deepEqual(warnings,[])
})

test('浏览器无法处理正增益时明确提示能力限制，不静默宣称完整应用',t=>{
  const previous=Object.getOwnPropertyDescriptor(globalThis,'AudioContext')
  Object.defineProperty(globalThis,'AudioContext',{configurable:true,value:class{constructor(){throw new Error('不可用')}}})
  t.after(()=>previous?Object.defineProperty(globalThis,'AudioContext',previous):Reflect.deleteProperty(globalThis,'AudioContext'))
  const warnings:string[]=[],manager=new MediaListeningGain(message=>warnings.push(message)),media=new Media()
  manager.set(media as any,2);manager.set(media as any,2)
  assert.equal(warnings.length,1);assert.match(warnings[0],/当前最多按100%试听/);assert.equal(media.volume,1)
  manager.dispose()
})
