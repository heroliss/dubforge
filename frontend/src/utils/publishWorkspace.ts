import { PUBLISH_ACTIVE, publicationStage } from './publishing.ts'
import { updateIsActive, updateNeedsReconcile } from './publicationUpdate.ts'

/** 页面任务导航只解释当前记录，不赋予上传、提交或远端修改权限。 */
export type PublishTask = 'video' | 'metadata' | 'confirm' | 'status' | 'update' | 'sync' | 'snapshot' | 'more' | 'prepare'
export type PublishWorkspaceMode = 'empty' | 'draft' | 'resume' | 'remote' | 'verify' | 'readonly' | 'running'
export interface PublishTaskItem {
  key: PublishTask
  label: string
  description: string
  badge?: string
}
export interface PublishWorkspaceOptions {
  recordOnly?: boolean
  originalUpdateActive?: boolean
  originalUpdateDirty?: boolean
  remoteCopyDirty?: boolean
  preparationStep?: number
}
export interface PublishWorkspace {
  mode: PublishWorkspaceMode
  title: string
  description: string
  tone: 'success' | 'error' | 'warning' | 'info' | 'default'
  defaultTask: PublishTask
  tasks: PublishTaskItem[]
  hasRemote: boolean
  readOnly: boolean
}

const snapshot = (): PublishTaskItem => ({ key: 'snapshot', label: '稿件快照', description: '查看本次确认的视频与投稿资料' })
const status = (): PublishTaskItem => ({ key: 'status', label: '进度与回执', description: '查看上传、审核与章节交付状态' })
const preparation = (uploaded = false): PublishTaskItem[] => [
  { key: 'video', label: uploaded ? '已上传视频' : '选择视频',
    description: uploaded ? '查看已冻结的视频、分P与顺序' : '选择本次投稿的视频，核对分P顺序' },
  { key: 'metadata', label: '投稿资料', description: '核对账号、封面、文案与章节选项' },
  { key: 'confirm', label: '确认投稿', description: uploaded ? '保存资料后确认提交已上传视频' : '统一预检并确认上传与投稿' },
  status(),
]
const initialPreparationTask = (step: unknown): PublishTask => step === 2 ? 'metadata' : step === 3 ? 'confirm' : 'video'

export function publishWorkspace(publication: any, options: PublishWorkspaceOptions = {}): PublishWorkspace {
  const state = String(publication?.status || '')
  const hasRemote = !!String(publication?.receipt?.bvid || '').trim()
  const inactive = !!publication && (publication.lifecycle ? publication.lifecycle !== 'active'
    : !!publication.archived_at || !!publication.deleted_at)
  const readOnly = !!options.recordOnly || inactive
  const tone = publicationStage(state).tone
  const result = (mode: PublishWorkspaceMode, title: string, description: string, tasks: PublishTaskItem[], defaultTask: PublishTask,
    customTone = tone): PublishWorkspace => ({ mode, title, description, tasks, defaultTask, tone: customTone, hasRemote, readOnly })

  if (!publication) return result('empty', '选择发布记录', '打开已有草稿或稿件记录，也可新建本地草稿。', [], 'status', 'default')
  if (readOnly) return result('readonly', inactive ? '本地记录已归档或回收' : '查看保留记录',
    '资料与回执保留；恢复可编辑记录后再继续制作或发布。', [status(), snapshot()], 'status', 'default')
  if (state === 'unknown') return result('verify', '先核对投稿结果',
    '本次投稿可能已成功；请核对B站并关联回执，再继续其他操作。', [status(), snapshot()], 'status', 'warning')
  if (state === 'draft') return result('draft', '准备新稿', '选择视频与投稿资料；保存草稿不会开始上传。',
    preparation(), initialPreparationTask(options.preparationStep), 'info')
  if (PUBLISH_ACTIVE.includes(state)) return result('running', '后台正在处理投稿',
    '进度与回执会自动更新，关闭页面后后台任务仍会继续。', [status(), snapshot()], 'status', 'info')
  if (['uploaded', 'paused', 'failed'].includes(state)) {
    const uploaded = state === 'uploaded'
    return result('resume', uploaded ? '文件已上传，等待确认投稿' : state === 'paused' ? '上传已暂停' : '继续未完成的上传',
      uploaded ? '核对并保存资料后确认提交；已上传的视频、封面和顺序保持不变。'
        : '继续时复用已完成上传；更换视频或账号需要准备新的草稿。',
      uploaded ? preparation(true) : [{ key: 'confirm', label: '继续发布', description: '沿用冻结清单继续上传或提交' }, status(), snapshot()],
      uploaded ? options.preparationStep === 3 ? 'confirm' : 'metadata' : 'status')
  }
  const remoteEditable = hasRemote && ['submitted', 'published', 'private', 'reviewed', 'rejected', 'collection_pending'].includes(state)
  if (!remoteEditable) return result('readonly', '查看发布记录', '查看已确认资料与本次发布回执。', [status(), snapshot()], 'status')

  const task = publication.latest_update
  const updating = !!options.originalUpdateActive || updateIsActive(task)
  const updateNeedsAttention = updateNeedsReconcile(task) || ['prepared', 'failed', 'paused'].includes(task?.status)
  const chapterState = String(publication.chapter_sync?.status || '')
  const videoComplete = ['published', 'private', 'reviewed'].includes(state)
  const chapterNeedsAttention = videoComplete && ['failed', 'unknown'].includes(chapterState)
  const chapterWorking = videoComplete && ['pending', 'running'].includes(chapterState)
  const completedLabel = state === 'published' ? '视频已发布' : '视频已通过'
  const defaultTask: PublishTask = options.originalUpdateDirty || updating || updateNeedsAttention ? 'update'
    : options.remoteCopyDirty ? 'sync' : state === 'rejected' ? 'update' : 'status'
  const tasks: PublishTaskItem[] = [{ ...status(), ...(chapterNeedsAttention ? { badge: '需处理' }
    : chapterWorking ? { badge: '进行中' } : {}) }, {
    key: 'update', label: '更新原稿件', description: '保留原BV，按选择替换视频并同步资料与章节',
    ...(updating ? { badge: '进行中' } : updateNeedsAttention ? { badge: '需处理' }
      : options.originalUpdateDirty ? { badge: '未保存' } : {}),
  }, { key: 'sync', label: '文案与章节', description: '只处理资料或章节，不替换视频',
    ...(options.remoteCopyDirty ? { badge: '待同步' } : {}) }, snapshot()]
  return result('remote', updating ? '原稿更新进行中' : updateNeedsAttention ? '原稿更新还需处理'
    : chapterNeedsAttention ? chapterState === 'unknown' ? `${completedLabel}，章节结果待核对` : `${completedLabel}，章节还需处理`
      : chapterWorking ? `${completedLabel}，章节处理中`
    : state === 'rejected' ? '稿件未通过审核' : '管理已有稿件',
    updating ? '本次更新的进度独立于原稿审核状态，后续章节与合集继续由同一任务完成。'
      : updateNeedsAttention ? '打开原稿更新核对方案或继续未完成步骤；无需重新创建投稿。'
        : chapterNeedsAttention ? chapterState === 'unknown' ? '视频审核已通过；请先核对已有章节，不重新上传或重复投稿。'
          : '视频审核已通过，可在进度与回执中单独重试章节，不重新上传或重复投稿。'
          : chapterWorking ? '视频审核已通过，章节由后台继续处理；可在进度与回执查看。'
        : state === 'rejected' ? '查看驳回原因后，可用新成片更新原稿件并保留原BV。'
          : '在当前记录中查看状态、更新原稿，或单独同步文案与章节。', tasks, defaultTask,
    updateNeedsAttention ? 'warning' : updating ? 'info' : chapterNeedsAttention ? 'warning' : chapterWorking ? 'info' : tone)
}

/** 保留有效的主动选择；任务失效时回到当前状态对应的安全入口。 */
export function resolvePublishTask(requested: unknown, workspace: PublishWorkspace): PublishTask {
  return workspace.tasks.some(task => task.key === requested) ? requested as PublishTask : workspace.defaultTask
}
