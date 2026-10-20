<script setup lang="ts">
import FilePathLink from './FilePathLink.vue'
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NCode, NTag, NText } from 'naive-ui'
const data = ref<any>(null), error = ref(''), busy = ref(false)
async function refresh() {
  busy.value = true
  try {
    const r = await fetch('/api/settings/environment', { cache: 'no-store' })
    const body = await r.json()
    if (!r.ok) throw new Error(body.message || '环境检测失败')
    data.value = body
    error.value = ''
  } catch (e: any) { error.value = e.message || '环境检测失败' }
  finally { busy.value = false }
}
onMounted(refresh)
</script>

<template>
  <n-card title="环境与服务" size="small">
    <template #header-extra><n-button size="small" :loading="busy" @click="refresh">检测环境</n-button></template>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <template v-if="data">
      <n-text depth="3">按需安装：只下载视频、提取音轨或处理字幕，无需安装本地识别、分离及配音模型。</n-text>
      <div class="tools"><n-tag v-for="tool in data.tools" :key="tool.name" :type="tool.path ? 'success' : 'warning'"
        :title="tool.path || '未在 PATH 中找到'" size="small" :bordered="false"><FilePathLink v-if="tool.path" :path="tool.path" :label="`${tool.name} · 已找到`" /><template v-else>{{ tool.name }} · 未找到</template></n-tag></div>
      <n-text depth="3" class="small">FFmpeg / ffprobe 用于媒体处理；uv 用于安装，Node.js / npm 用于前端构建。安装检测不代表 GPU 和模型已就绪。</n-text>
      <div class="components">
        <div v-for="item in data.components" :key="item.key" class="component">
          <div class="component-heading"><span>{{ item.label }}</span><n-tag size="small" :bordered="false"
            :type="item.installed ? 'success' : 'default'">{{ item.installed ? '依赖已安装' : '可选 · 未安装' }}</n-tag></div>
          <n-code :code="item.command" language="powershell" word-wrap />
        </div>
      </div>
      <n-text depth="3" class="small">先关闭 DubForge，再在下方程序目录的 PowerShell 中执行所需命令；可同时使用 -Asr -Separation。模型在首次运行对应步骤时下载。</n-text>
      <FilePathLink class="path" :path="data.program_directory" />
      <div class="services">
        <div v-for="item in data.services" :key="item.backend" class="component">
          <div class="component-heading"><span>{{ item.label }}</span><n-tag size="small" :bordered="false"
            :type="item.installed ? 'success' : 'default'">{{ item.installed ? '已找到本机环境' : '未找到本机环境' }}</n-tag>
            <a :href="item.docs" target="_blank" rel="noopener noreferrer">官方安装说明 ↗</a></div>
          <n-text depth="3" class="path">{{ item.url }} · <FilePathLink v-if="item.directory" :path="item.directory" /><template v-else>{{ item.message }}</template></n-text>
        </div>
      </div>
      <n-text depth="3" class="small">配音服务各用独立环境，可只安装其中一种，或使用已有远程服务。地址与安装目录在「制作与发布 → 全局默认 → 字幕与配音」填写，也可在项目配音页覆盖；本机服务可从配音页启动。</n-text>
    </template>
  </n-card>
</template>

<style scoped>
.tools { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 8px; }
.small { display: block; font-size: 12px; margin-top: 8px; line-height: 1.7; }
.components { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 16px; margin-top: 20px; }
.component { min-width: 0; }
.component-heading { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.component-heading a { color: #63b3ed; font-size: 12px; }
.path { display: block; font-size: 12px; overflow-wrap: anywhere; }
.services { display: grid; gap: 16px; border-top: 1px solid rgba(128,128,128,.16); padding-top: 16px; margin-top: 20px; }
</style>
