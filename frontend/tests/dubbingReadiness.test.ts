import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null } })
  Panel = (await server.ssrLoadModule('/src/components/DubbingReadiness.vue')).default
})
after(async () => { await server.close() })
const render = (summary: any) => renderToString(createSSRApp({ render: () => h(Panel, { summary }) }))

test('未配齐明确显示待补句数，不暗示改用原声', async () => {
  const html = await render({ needed: true, known: true, ready: 110, required: 310, pending: 200 })
  assert.match(html, /已就绪 110 \/ 310 句/)
  assert.match(html, /待补 200 句/)
  assert.match(html, /仅补缺失或失效的句子/)
  assert.doesNotMatch(html, /跳过配音|无需补配/)
})
test('齐全、主动原声和静音说明跳过原因', async () => {
  const html = await render({ needed: true, known: true, ready: 8, required: 8, pending: 0, original: 2, silent: 1 })
  assert.match(html, /无需补配/); assert.match(html, /跳过配音步骤/)
  assert.match(html, /已选原声 2 句/); assert.match(html, /已设静音 1 句/)
})
test('未知文稿不显示零句已齐全，原声版本不要求配音', async () => {
  const unknown = await render({ needed: true, known: false, ready: 0, required: 0, pending: 0 })
  assert.match(unknown, /原文与译文就绪后/); assert.doesNotMatch(unknown, /无需补配|已就绪/)
  const original = await render({ needed: false })
  assert.match(original, /所选版本使用原声，不运行配音/); assert.doesNotMatch(original, /待补/)
})

test('大量缺失配音默认压缩明细，可以展开全部', async () => {
  const Notice = (await server.ssrLoadModule('/src/components/DependencyNotice.vue')).default
  const items = Array.from({ length: 200 }, (_, i) => ({ name: `${i + 111}.wav`, step: 'tts', label: '合成配音', state: 'missing', reason: '尚未生成', desc: `第 ${i + 111} 句` }))
  for (const expanded of [false, true]) {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
    await router.push('/')
    const app = createSSRApp({ render: () => h(Notice, { uid: 'fixture', items }) })
    app.use(router); setup(app)
    app.mixin({ created() { if (this.$options.__name === 'DependencyNotice') this.$.setupState.expanded = expanded } })
    const html = await renderToString(app)
    assert.equal((html.match(/class="dependency"/g) || []).length, expanded ? 200 : 6)
    assert.match(html, expanded ? /收起明细/ : /展开其余 194 项（共 200 项）/)
    assert.match(html, /111.wav/)
  }
})
