import type { App } from 'vue'
import {
  NConfigProvider,
  NDialogProvider,
  NLayout,
  NLayoutSider,
  NMenu,
  NMessageProvider,
} from 'naive-ui'

// 只把应用壳需要的组件放入全局插件；页面组件在各自懒加载的路由块中按需引入。
const components = {
  'n-config-provider': NConfigProvider,
  'n-dialog-provider': NDialogProvider,
  'n-layout': NLayout,
  'n-layout-sider': NLayoutSider,
  'n-menu': NMenu,
  'n-message-provider': NMessageProvider,
}

export default {
  install(app: App) {
    for (const [name, component] of Object.entries(components)) {
      app.component(name, component)
    }
  },
}
