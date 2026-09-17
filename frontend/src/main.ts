import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'

// M0 全量引入 naive-ui (构建体积优化放 M4 用按需引入)
import naive from 'naive-ui'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(naive)
app.mount('#app')
