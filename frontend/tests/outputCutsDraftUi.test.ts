import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NDialogProvider, NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Editor: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Editor = (await server.ssrLoadModule('/src/components/OutputCutsEditor.vue')).default
})
after(async () => { await server.close() })

function savedState() {
  return { revision: 'r1', source_fingerprint: 'source-1', duration: 60, frame_rate: 30,
    enabled: false, ranges: [], effective_ranges: [], removed_duration: 0, output_duration: 60,
    warnings: [], source_url: '/source.mp4', source_changed: false }
}

async function render(seed: (page: any) => void) {
  let page: any
  const controls: Record<string, any> = {}
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(Editor, { uid: 'fixture' }) }),
  }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === Editor.__name) {
      page = this.$.setupState; page.adopt(savedState()); seed(page)
      return
    }
    // 读取真实 Naive UI 控件收到的属性和事件，避免只测试组件内部计算值。
    if (!page) return
    const vnodeProps = this.$.vnode.props || {}
    if (this.$options.name === 'Button') {
      if (vnodeProps.onClick === page.save) controls.save = this
      if (vnodeProps.onClick === page.previewDraft) controls.preview = this
    }
    if (this.$options.name === 'Checkbox' && String(vnodeProps.title || '').includes('无需先保存')) controls.checkbox = this
  } })
  const html = await renderToString(app)
  assert.ok(controls.save, '应渲染实际保存按钮')
  assert.ok(controls.preview, '应渲染实际预览按钮')
  assert.ok(controls.checkbox, '应渲染实际剪后预览开关')
  return { page, html, controls }
}

test('完整待加入标记显示状态和直接保存、预览入口，无需先手动加入清单', async () => {
  const { page, html, controls } = await render(page => {
    page.setMarkerValue('start', 5); page.setMarkerValue('end', 10)
  })
  assert.match(html, /片段待加入/); assert.match(html, /待加入清单/)
  assert.match(html, /加入片段并保存清单/); assert.match(html, /加入并预览/)
  assert.match(html, /00:00:05\.000/); assert.match(html, /00:00:10\.000/)
  assert.equal(controls.save.$props.disabled, false); assert.equal(controls.preview.$props.disabled, false)
  assert.equal(page.ranges.length, 0); assert.equal(page.hasPendingMarker, true)
})

test('非法待加入标记在字段附近提示，并禁止继续保存或开启剪后预览', async () => {
  const { page, html, controls } = await render(page => {
    page.setMarkerValue('start', 5); page.markEndText = 'xx:yy'; page.commitMarker('end')
  })
  assert.match(html, /请填写有效的终点时间/)
  assert.match(html, /标记尚未完成/)
  assert.equal(controls.save.$props.disabled, true); assert.equal(controls.preview.$props.disabled, true)
  assert.equal(controls.checkbox.$props.disabled, true)
  assert.equal(page.markEndText, 'xx:yy'); assert.equal(page.ranges.length, 0)
})

test('剪后预览已开启时即使新标记未完成，真实开关仍可取消预览', async () => {
  const { page, html, controls } = await render(page => {
    page.skipCuts = true; page.setMarkerValue('start', 5)
  })
  assert.match(html, /下方标记尚未完成/)
  assert.equal(controls.checkbox.$props.checked, true)
  assert.equal(controls.checkbox.$props.disabled, false, '关闭预览不能被未完成标记锁住')
  const update = controls.checkbox.$.vnode.props['onUpdate:checked']
  assert.equal(typeof update, 'function')
  update(false)
  assert.equal(page.skipCuts, false); assert.equal(page.markStartText, '00:00:05.000')
  assert.equal(page.hasPendingMarker, true)
})

test('直接预览收录标记后显示清单未保存及无需先保存，已有保存状态保持原样', async () => {
  const { page, html, controls } = await render(page => {
    page.setMarkerValue('start', 5); page.setMarkerValue('end', 10)
    assert.equal(page.previewDraft(), true)
  })
  assert.match(html, /清单未保存/); assert.match(html, /当前草稿 · 尚未保存/)
  assert.match(html, /预览使用当前清单，无需先保存/)
  assert.match(html, /预览当前清单/); assert.match(html, /仅保存清单，稍后生成/)
  assert.doesNotMatch(html, /片段待加入|待加入清单/)
  assert.equal(controls.checkbox.$props.checked, true); assert.equal(controls.save.$props.disabled, false)
  assert.equal(page.ranges.length, 1); assert.equal(page.saved.ranges.length, 0)
  assert.equal(page.hasPendingMarker, false); assert.equal(page.listDirty, true)
})
