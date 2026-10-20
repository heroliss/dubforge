import { CollectionFailure, CollectionJoinFailure } from './collection-error.mjs'

// 只映射已明确支持的设置，不把页面对象直接传给第三方接口。
export function submissionOptions(settings, cover) {
  const options = {
    title: settings.title, desc: settings.description, tag: settings.tags.join(','),
    tid: settings.tid || 21, copyright: settings.copyright, source: settings.source,
    cover, dynamic: settings.dynamic, is_only_self: settings.visibility === 'self' ? 1 : 0,
    up_close_reply: settings.close_comments, up_close_danmu: settings.close_danmaku,
    up_selection_reply: settings.selected_comments,
    no_reprint: 0, recreate: -1,
  }
  // 与上游投稿表单一致：转载不提交水印参数。
  if (settings.copyright !== 2) options.watermark = { state: settings.watermark ? 1 : 0 }
  if (settings.human_type2) options.human_type2 = settings.human_type2
  if (settings.copyright !== 2 && settings.declaration_id !== null) options.creation_statement = { id: settings.declaration_id }
  if (settings.publish_at) options.dtime = settings.publish_at
  if (settings.topic_id && settings.topic_name) {
    options.topic_id = settings.topic_id
    options.topic_name = settings.topic_name
    if (settings.mission_id) options.mission_id = settings.mission_id
  }
  return options
}

// 上游作者的实际集成映射（2026-05-24，PR #454）；不是猜测的编号。
// https://github.com/renmu123/biliLive-tools/pull/454/files
// 如平台返回声明列表，以平台列表为准；升级时必须复核此映射。
export const KNOWN_DECLARATIONS = [
  { value: -1, label: '内容无需标注' }, { value: 1, label: '含AI生成内容' },
  { value: 2, label: '含虚构演绎内容' }, { value: 3, label: '内容含营销信息' },
  { value: 4, label: '个人观点，仅供参考' },
]

export function declarationsFrom(pre) {
  // 只接受服务器实际给出的声明枚举，不猜测 AI 声明编号。
  const found = new Map()
  function walk(value, declarationContext = false) {
    if (!value || typeof value !== 'object') return
    if (declarationContext && Number.isInteger(value.id)) {
      const label = value.name || value.title || value.text || value.desc
      if (typeof label === 'string' && label.length < 180) found.set(value.id, { value: value.id, label })
    }
    for (const [key, child] of Object.entries(value)) {
      walk(child, declarationContext || /creation_statement/.test(key))
    }
  }
  walk(pre)
  return [...found.values()]
}

const LOGIN_ERRORS = {
  PUBLISH_COLLECTION_ORDER_INVALID: '合集排序资料无效，尚未发送修改请求，请重新检查发布批次',
  PUBLISH_COLLECTION_ORDER_RESPONSE: '无法完整读取合集成员与顺序，尚未发送修改请求，请刷新后重试',
  PUBLISH_COLLECTION_ORDER_CHANGED: '合集成员、资料或顺序已变化，尚未修改，请重新预览并确认',
  PUBLISH_COLLECTION_ORDER_REJECTED: 'B站拒绝了合集排序，请在官网检查合集状态和账号权限后重新预览',
  PUBLISH_COLLECTION_ORDER_UNKNOWN: '合集排序结果尚未确认，请先核对平台顺序，不会自动重复排序',
  PUBLISH_REMOTE_OWNERSHIP: '无法确认该稿件属于当前账号，未执行修改',
  PUBLISH_EDIT_UNSUPPORTED: '私密、联合投稿、付费等特殊稿件请在B站官网修改，未发送修改请求',
  PUBLISH_RESPONSE_UNSUPPORTED: '平台响应结构不完整或暂不兼容，旧资料已保留',
  PUBLISH_SUBTITLE_CHANGED: '所选字幕已变化，请重新读取语言列表',
  PUBLISH_REMOTE_CHANGED: 'B站资料已变化，请重新预览变更后确认',
  PUBLISH_WRITE_NOT_SENT: '尚未发送修改请求，请核对账号、资料和网络后重新预览',
  PUBLISH_WRITE_REJECTED: 'B站拒绝修改，请核对稿件状态、资料和账号权限',
  PUBLISH_WRITE_UNKNOWN: '修改结果尚未确认，请先核对平台状态，不要重复提交',
  PUBLISH_CHAPTER_INVALID: '章节资料无效，尚未发送修改请求，请检查时间和标题',
  PUBLISH_CHAPTER_RULE_UNAVAILABLE: '无法读取B站章节规则，尚未发送修改请求，请稍后重试',
  PUBLISH_CHAPTER_RULE_REJECTED: 'B站章节规则禁止当前章节设置，尚未发送修改请求',
  PUBLISH_CHAPTER_TITLE_TOO_LONG: '章节标题超过B站当前16字符限制，尚未发送修改请求',
  PUBLISH_CHAPTER_LIMIT_EXCEEDED: '章节数量超过B站当前限制，尚未发送修改请求',
  PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED: 'B站章节响应结构不完整，尚未发送修改请求',
  PUBLISH_CHAPTER_REMOTE_CHANGED: 'B站章节已变化，请重新预览后确认',
  PUBLISH_CHAPTER_NOT_SENT: '尚未发送章节修改请求，请先预览并确认',
  PUBLISH_CHAPTER_REJECTED: 'B站拒绝修改章节，请核对稿件状态和账号权限',
  PUBLISH_CHAPTER_UNKNOWN: '章节修改结果尚未确认，请先回到B站核对，不要重复提交',
  PUBLISH_CHAPTER_TIME: '章节时间超出当前分P或取整后无有效时长，尚未发送修改请求',
  PUBLISH_CHAPTER_OVERLAP: '章节时间存在重叠，尚未发送修改请求',
  PUBLISH_CHAPTER_INTERVAL: '章节间隔未达到B站当前规则，尚未发送修改请求',
  PUBLISH_CHAPTER_NOOP: '章节内容没有变化，未发送修改请求',
  PUBLISH_COLLECTION_NOT_SENT: '尚未发送创建合集请求，请检查登录、封面及网络后重试',
  PUBLISH_COLLECTION_REJECTED: 'B站拒绝创建合集，请核对名称、简介、封面及账号权限',
  PUBLISH_COLLECTION_UNKNOWN: '合集创建结果尚未确认，请刷新列表或到B站核对，不要重复创建',
  PUBLISH_QR_EXPIRED: '二维码已过期，请重新获取二维码',
  PUBLISH_QR_RESPONSE: 'B站登录响应不完整，请重新扫码；若持续出现，请更新发布组件适配',
  PUBLISH_QR_DESTINATION: 'B站返回了未支持的登录地址，已停止连接以保护账号；请反馈此错误码',
  PUBLISH_ACCOUNT_MISMATCH: '登录账号与所选账号不一致，请重新扫码连接所选账号',
  PUBLISH_ACCOUNT_RESPONSE: 'B站账号校验响应不完整，暂时无法确认登录状态，请稍后重试',
}

// 写操作只回传脱敏后的阶段与平台状态，禁止把第三方异常对象、URL 或凭据带出工作进程。
const DIAGNOSTIC_STAGES = new Set(['preflight', 'ownership', 'version', 'validate', 'request', 'rules', 'read', 'verify',
  'auth', 'input', 'cover', 'create', 'receipt', 'join_auth', 'join_season', 'join_section',
  'join_input', 'join_add', 'join_verify'])
const safeDetail = value => {
  if (!value || typeof value !== 'object' || !DIAGNOSTIC_STAGES.has(value.stage)) return undefined
  const result = {stage:value.stage}
  for (const key of ['remote_code', 'http_status'])
    if (Number.isInteger(value[key]) && Math.abs(value[key]) < 2 ** 31) result[key] = value[key]
  // 章节边界错误只携带有界数字，不回传用户标题、来源文本或上游异常。
  for (const key of ['count', 'limit', 'index', 'title_length', 'max_title_length'])
    if (Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 86400) result[key] = value[key]
  for (const key of ['minimum_seconds', 'actual_seconds'])
    if (Number.isFinite(value[key]) && value[key] >= 0 && value[key] <= 86400) result[key] = value[key]
  if (['ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED', 'ENOTFOUND', 'EAI_AGAIN',
    'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'DEPTH_ZERO_SELF_SIGNED_CERT',
    'ERR_TLS_CERT_ALTNAME_INVALID', 'SELF_SIGNED_CERT_IN_CHAIN'].includes(value.network_code))
    result.network_code = value.network_code
  return result
}

export function safeError(error) {
  if (error instanceof CollectionFailure || error instanceof CollectionJoinFailure) return {code:error.code, message:error.message, detail:error.detail}
  if (Object.hasOwn(LOGIN_ERRORS, error?.code || '')) {
    const detail = safeDetail(error.detail)
    let message = LOGIN_ERRORS[error.code]
    if (error.code === 'PUBLISH_CHAPTER_LIMIT_EXCEEDED' && detail?.limit > 0 && Number.isInteger(detail.count))
      message = `当前有${detail.count}个章节，B站当前最多允许${detail.limit}个章节（${Math.max(0, detail.limit - 1)}个内部分界点），请先整理合并章节后重新预览；尚未发送修改请求`
    if (error.code === 'PUBLISH_CHAPTER_TITLE_TOO_LONG' && detail?.index > 0 && detail.max_title_length > 0)
      message = `第${detail.index}个章节标题超过${detail.max_title_length}字符限制，请缩写或重新整理；尚未发送修改请求`
    if (error.code === 'PUBLISH_CHAPTER_TIME' && detail?.index > 0 && detail.minimum_seconds > 0)
      message = `第${detail.index}个章节时长为${detail.actual_seconds}秒，至少需要${detail.minimum_seconds}秒，请合并过短章节或调整分界点；尚未发送修改请求`
    return { code: error.code, message, ...(detail ? {detail} : {}) }
  }
  if (['CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'DEPTH_ZERO_SELF_SIGNED_CERT',
    'ERR_TLS_CERT_ALTNAME_INVALID', 'SELF_SIGNED_CERT_IN_CHAIN'].includes(error?.code)) {
    return { code:'PUBLISH_TLS', message:'HTTPS证书校验失败，请检查系统时间、网络与受信任证书；不会关闭证书校验后继续上传' }
  }
  const code = Number(error?.code ?? error?.response?.data?.code)
  if ([-101, -111].includes(code)) return { code: 'PUBLISH_LOGIN_EXPIRED', message: '登录已失效，请重新扫码连接账号' }
  if ([-412, -352, -351].includes(code) || [403, 406, 412, 429].includes(error?.response?.status ?? error?.statusCode)) {
    return { code: 'PUBLISH_VERIFICATION', message: 'B站要求验证或限制了请求，请前往官方网页处理后重试' }
  }
  return { code: 'PUBLISH_REMOTE_ERROR', message: Number.isFinite(code)
    ? `B站拒绝了请求（错误码 ${code}），请在官方创作中心检查设置或验证要求`
    : 'B站请求失败，请检查网络；若正在投稿，请先核对是否已成功，勿直接重投' }
}
