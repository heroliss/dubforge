<script setup lang="ts">
// 项目列表: 真实 CRUD + 新建 (YouTube 链接 / 本地导入)
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const projects = ref<any[]>([])
const loading = ref(true)
const showNew = ref(false)
const creating = ref(false)
const form = ref({ source_kind: 'youtube', source_url: '', title: '' })

async function load() {
  loading.value = true
  try {
    const r = await fetch('/api/projects')
    projects.value = await r.json()
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function create() {
  if (!form.value.source_url.trim()) { message.warning('请填写视频链接或本地路径'); return }
  creating.value = true
  try {
    const r = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form.value),
    })
    const j = await r.json()
    if (j.uid) {
      showNew.value = false
      message.success('项目已创建')
      router.push(`/projects/${j.uid}`)
    } else {
      message.error(j.message || '创建失败')
    }
  } finally {
    creating.value = false
  }
}

function remove(p: any) {
  dialog.warning({
    title: '删除项目',
    content: `确定删除「${p.title}」？\n项目目录与全部产物将一并删除，不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      await fetch(`/api/projects/${p.uid}`, { method: 'DELETE' })
      message.success('已删除')
      await load()
    },
  })
}

const STATUS_TEXT: Record<string, string> = {
  created: '待处理', processing: '进行中', done: '已完成', failed: '有失败步骤',
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-space justify="space-between" align="center" style="margin-bottom: 16px">
      <n-h1 style="margin: 0">项目</n-h1>
      <n-button type="primary" @click="showNew = true">＋ 新建项目</n-button>
    </n-space>

    <n-spin :show="loading">
      <n-empty v-if="!loading && !projects.length" description="还没有项目 —— 点击右上角新建，粘贴 YouTube 链接或导入本地视频" />
      <n-space vertical size="large" v-else>
        <n-card v-for="p in projects" :key="p.uid" size="small"
                style="cursor: pointer" @click="router.push(`/projects/${p.uid}`)">
          <n-space justify="space-between" align="center">
            <div>
              <n-space align="center">
                <n-text strong style="font-size: 15px">{{ p.title }}</n-text>
                <n-tag size="small" :type="p.status === 'done' ? 'success' : p.status === 'failed' ? 'error' : p.status === 'processing' ? 'info' : 'default'">
                  {{ STATUS_TEXT[p.status] || p.status }}
                </n-tag>
              </n-space>
              <n-text depth="3" style="font-size: 12px; display: block; margin-top: 4px">
                {{ p.source_kind === 'youtube' ? p.source_url : '本地导入' }} · {{ p.uid }}
              </n-text>
            </div>
            <n-space>
              <n-button size="small" @click.stop="router.push(`/projects/${p.uid}`)">打开管线</n-button>
              <n-button size="small" quaternary type="error" @click.stop="remove(p)">删除</n-button>
            </n-space>
          </n-space>
        </n-card>
      </n-space>
    </n-spin>

    <n-modal v-model:show="showNew" preset="card" title="新建项目" style="width: 560px">
      <n-form label-placement="left" label-width="90">
        <n-form-item label="来源类型">
          <n-radio-group v-model:value="form.source_kind">
            <n-radio value="youtube">YouTube / 链接下载</n-radio>
            <n-radio value="local">本地视频导入</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="form.source_kind === 'youtube' ? '视频链接' : '本地路径'">
          <n-input v-model:value="form.source_url"
                   :placeholder="form.source_kind === 'youtube'
                     ? 'https://www.youtube.com/watch?v=...'
                     : 'D:\\videos\\my.mp4'" />
        </n-form-item>
        <n-form-item label="标题 (可选)">
          <n-input v-model:value="form.title" placeholder="留空自动使用视频标题/文件名" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showNew = false">取消</n-button>
          <n-button type="primary" :loading="creating" @click="create">创建</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-layout-content>
</template>
