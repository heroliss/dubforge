<script setup lang="ts">
import FilePathLink from './FilePathLink.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NTag, NText } from 'naive-ui'

const props = defineProps<{ uid: string; disabled: boolean; revision: number }>()
const emit = defineEmits<{ (event: 'status', value: Service): void }>()
type Service = {
  backend: string; online: boolean; status: string; can_start: boolean; can_stop: boolean; managed: boolean; pid: number | null
  message: string; log_path: string | null
  progress_detail?: boolean | null
}
const service = ref<Service | null>(null)
const pending = ref(false)
const stopping = ref(false)
const error = ref('')
const showLog = ref(false), logBusy = ref(false), logText = ref(''), logError = ref(''), logTruncated = ref(false)
const starting = computed(() => service.value?.status === 'starting')
const label = computed(() => ({ online: '已连接', offline: '未连接', starting: '启动中', failed: '已退出', draining: '等待收尾' }[service.value?.status ?? ''] ?? '检测中'))
let generation = 0
let timer: number | undefined
let controller: AbortController | undefined
let logController: AbortController | undefined
let logTimer: number | undefined

function cancel() {
  generation++
  globalThis.clearTimeout(timer)
  controller?.abort()
  logController?.abort()
  globalThis.clearTimeout(logTimer)
  pending.value = false
  stopping.value = false
  logBusy.value = false
  showLog.value = false
}

async function request(action: '' | 'start' | 'stop' = '') {
  if (pending.value || !props.uid || (action && props.disabled)) return
  globalThis.clearTimeout(timer)
  const current = generation
  controller = new AbortController()
  pending.value = true
  stopping.value = action === 'stop'
  try {
    const r = await fetch(`/api/projects/${props.uid}/workbench/service${action ? '/' + action : ''}`, {
      method: action ? 'POST' : 'GET', cache: 'no-store', signal: controller.signal,
    })
    const body = await r.json()
    if (current !== generation) return
    if (!r.ok) throw new Error(body.message || '服务请求失败，请重试')
    service.value = body
    emit('status', body)
    error.value = ''
  } catch (e) {
    if (current !== generation) return
    error.value = e instanceof Error && e.name !== 'TypeError' ? e.message : '无法连接 DubForge，请稍后重试'
  } finally {
    if (current === generation) {
      pending.value = false
      stopping.value = false
      // 自己启动的服务继续探测退出状态；请求结束再计时，避免慢连接堆积。
      if (starting.value || service.value?.managed) timer = globalThis.setTimeout(() => void request(), 3000)
    }
  }
}

async function loadLog() {
  if (logBusy.value || !showLog.value) return
  globalThis.clearTimeout(logTimer)
  const current = generation
  const active = new AbortController()
  logController = active
  logBusy.value = true
  try {
    const r = await fetch(`/api/projects/${props.uid}/workbench/service/log`, { cache: 'no-store', signal: active.signal })
    const body = await r.json()
    if (current !== generation || active.signal.aborted) return
    if (!r.ok) throw new Error(body.message || '日志读取失败')
    logText.value = body.text || '暂无日志输出'
    logTruncated.value = body.truncated
    logError.value = ''
  } catch (e: any) {
    if (current === generation && !active.signal.aborted) logError.value = e.message || '日志读取失败'
  } finally {
    if (current === generation && logController === active) {
      logBusy.value = false
      if (showLog.value) logTimer = globalThis.setTimeout(() => void loadLog(), 3000)
    }
  }
}
watch(showLog, visible => {
  if (visible) { logText.value = ''; logError.value = ''; void loadLog() }
  else { globalThis.clearTimeout(logTimer); logController?.abort(); logBusy.value = false }
})

watch(() => [props.uid, props.revision], () => {
  cancel()
  service.value = null
  error.value = ''
  void request()
}, { immediate: true })
onUnmounted(cancel)
</script>

<template>
  <div class="service-control">
    <div class="service-row">
      <div class="service-name">
        <span>{{ service ? (service.backend === 'indextts25' ? 'IndexTTS 2.5' : 'GPT-SoVITS') : '配音服务' }}</span>
        <n-tag size="small" :bordered="false" :type="service?.status === 'draining' ? 'warning' : service?.online ? 'success' : starting ? 'info' : 'default'">{{ label }}</n-tag>
      </div>
      <div class="service-actions">
        <n-button v-if="!service?.online" size="small" secondary type="primary"
                  :loading="starting || (pending && !stopping)" :disabled="disabled || pending || starting || !service?.can_start"
                  @click="request('start')">{{ starting ? '正在启动' : service?.status === 'failed' ? '重新启动' : '启动服务' }}</n-button>
        <n-button v-if="service?.managed" size="small" secondary type="warning" :loading="stopping"
                  :disabled="disabled || pending || !service.can_stop" title="停止本机后台服务并释放模型资源；其他项目也会断开连接"
                  @click="request('stop')">停止服务</n-button>
        <n-button v-if="service?.log_path" size="small" quaternary @click="showLog = true">查看日志</n-button>
        <n-button size="small" quaternary :disabled="pending" @click="request()">检测连接</n-button>
      </div>
    </div>
    <n-text v-if="disabled" depth="3" class="service-hint">请先保存配音参数，再管理服务。</n-text>
    <n-text v-else-if="service?.message && service.status !== 'failed'" depth="3" class="service-hint">{{ service.message }}</n-text>
    <n-alert v-if="error || service?.status === 'failed'" type="error" :show-icon="false" class="service-error">
      {{ error || service?.message }}
    </n-alert>
    <n-text v-if="service?.managed" depth="3" class="service-hint">后台进程 PID {{ service.pid }} · 多个项目共用，停止后释放模型资源。</n-text>
    <n-text v-if="service?.progress_detail === false" type="warning" class="service-hint">当前服务使用旧启动方式，只能返回阶段进度。空闲后点击「停止服务」，再「启动服务」启用迭代进度。</n-text>
    <n-modal v-model:show="showLog" preset="card" title="配音服务日志" :style="{ width: '900px', maxWidth: 'calc(100vw - 32px)' }">
      <FilePathLink class="log-path" :path="service?.log_path" />
      <n-text depth="3" class="service-hint">每 3 秒刷新{{ logTruncated ? ' · 仅显示末尾 64 KB' : '' }}</n-text>
      <n-alert v-if="logError" type="error" :show-icon="false">{{ logError }}</n-alert>
      <pre class="service-log">{{ logText || '正在读取日志…' }}</pre>
      <div class="log-actions"><n-button size="small" :loading="logBusy" @click="loadLog">刷新日志</n-button>
        <n-button size="small" @click="showLog = false">关闭</n-button></div>
    </n-modal>
  </div>
</template>

<style scoped>
.service-control { margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid rgba(128,128,128,.16); }
.service-row, .service-name, .service-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.service-row { justify-content: space-between; gap: 12px; }
.service-name { font-size: 14px; }
.service-hint { display: block; font-size: 12px; margin-top: 8px; }
.log-path { overflow-wrap: anywhere; user-select: text; }
.service-error { margin-top: 12px; }
.service-log { white-space: pre-wrap; overflow-wrap: anywhere; overflow: auto; max-height: 60vh; padding: 12px; background: rgba(128,128,128,.08); font-size: 12px; }
.log-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
