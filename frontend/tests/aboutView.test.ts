import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

test('关于页显示真实包版本、稳定版能力和文档入口，不再把历史里程碑当作当前进度', async () => {
  const server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false },
  })
  try {
    const { default: AboutView } = await server.ssrLoadModule('/src/views/AboutView.vue')
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/about', component: AboutView },
      { path: '/help', component: { render: () => h('div') } },
      { path: '/settings', component: { render: () => h('div') } },
    ] })
    await router.push('/about')
    await router.isReady()
    const html = await renderToString(createSSRApp(AboutView).use(router))
    const packageInfo = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
    assert.ok(html.includes(`版本 ${packageInfo.version}`))
    assert.match(html, /v1 稳定版能力/)
    assert.match(html, /href="\/help"/)
    assert.match(html, /href="\/settings"/)
    assert.match(html, /docs\/README\.md/)
    assert.match(html, /审核结果以平台实际回执为准/)
    assert.match(html, /独立开发的本地视频译制工作台/)
    assert.doesNotMatch(html, /VideoLingo|旧系统工作区|开发进度|里程碑|M[0-6]|⏳ 计划中/)
  } finally {
    await server.close()
  }
})
