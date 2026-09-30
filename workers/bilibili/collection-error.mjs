// 只提取平台业务错误和有界诊断；不输出原始请求、Cookie、URL或堆栈。
const STAGES = {auth:'登录校验',input:'创建资料校验',cover:'合集封面上传',create:'创建合集',receipt:'创建回执校验'}
const NETWORK = new Set(['ECONNRESET','ETIMEDOUT','ECONNABORTED','ENOTFOUND','EAI_AGAIN',
  'CERT_HAS_EXPIRED','UNABLE_TO_VERIFY_LEAF_SIGNATURE','DEPTH_ZERO_SELF_SIGNED_CERT',
  'ERR_TLS_CERT_ALTNAME_INVALID','SELF_SIGNED_CERT_IN_CHAIN'])

function platformMessage(error, secrets) {
  // 只允许业务响应中的文案；通用 Error.message 可能含路径或请求头。
  const raw = error?.rawResponse?.data?.message ?? error?.response?.data?.message
    ?? (Number.isInteger(error?.code) ? error.message : '')
  if (typeof raw !== 'string') return ''
  let message = raw.slice(0, 4096)
  for (const secret of Object.values(secrets || {})) {
    if (typeof secret === 'string' && secret.length >= 4) {
      message = message.split(secret).join('[已隐藏]').split(encodeURIComponent(secret)).join('[已隐藏]')
    }
  }
  return message.replace(/(?:https?:\/\/|[A-Z]:\\)\S+/gi,'[地址已隐藏]')
    .replace(/(?:SESSDATA|bili_jct|cookie|authorization|access_token|refresh_token)\s*[:=]\s*[^\s;,]+/gi,'[凭据已隐藏]')
    .replace(/\b(?:[a-f\d]{32,}|[A-Za-z\d_%-]{48,})\b/gi,'[标识已隐藏]')
    .replace(/[\x00-\x1f\x7f<>]/g,' ').replace(/\s+/g,' ').trim().slice(0,240)
}

export class CollectionFailure extends Error {
  constructor(stage, error, secrets = {}) {
    const rawCode = error?.rawResponse?.data?.code ?? error?.response?.data?.code ?? error?.code
    const remoteCode = Number.isSafeInteger(rawCode) ? rawCode : null
    const status = error?.rawResponse?.status ?? error?.response?.status ?? error?.statusCode
    const httpStatus = Number.isInteger(status) && status >= 100 && status <= 599 ? status : null
    const networkCode = NETWORK.has(error?.code) ? error.code : null
    const sent = stage === 'create' || stage === 'receipt'
    // 未发送创建和明确业务拒绝才允许人工重试；HTTP错误不是业务拒绝的证明。
    const code = !sent ? 'PUBLISH_COLLECTION_NOT_SENT'
      : stage === 'create' && remoteCode !== null && remoteCode !== 0
        ? 'PUBLISH_COLLECTION_REJECTED' : 'PUBLISH_COLLECTION_UNKNOWN'
    const reason = platformMessage(error, secrets)
    const diagnostic = [remoteCode !== null ? `平台错误码 ${remoteCode}` : '', httpStatus ? `HTTP ${httpStatus}` : '', networkCode || ''].filter(Boolean).join(' · ')
    const recovery = code === 'PUBLISH_COLLECTION_UNKNOWN' ? '创建结果待核对，请勿重复创建。'
      : [-101,-111].includes(remoteCode) ? '请重新扫码登录。'
      : [-412,-352,-351].includes(remoteCode) || [403,406,412,429].includes(httpStatus) ? '请到B站官网完成验证后再试。'
      : stage === 'cover' ? '合集创建请求尚未发送，请检查封面格式或网络。'
      : stage === 'auth' ? '合集创建请求尚未发送，请检查登录或网络。' : '未创建成功，请按具体原因调整后重试。'
    super(`${STAGES[stage] || '合集操作'}失败${diagnostic ? `（${diagnostic}）` : ''}${reason ? `：${reason}` : '。'} ${recovery}`)
    this.code = code
    this.detail = {stage, remote_code:remoteCode, http_status:httpStatus, network_code:networkCode}
  }
}

const JOIN_STAGES = {join_auth:'登录校验',join_season:'读取目标合集',join_section:'读取合集成员',
  join_input:'核对合集资料',join_add:'加入合集',join_verify:'核对加入结果'}
const JOIN_REASONS = {
  target:'目标合集或小节已变化，请刷新账号选项后核对。',
  members:'合集成员数据不完整或不一致，尚未发送加入请求。',
  input:'合集关联资料无效，尚未发送加入请求。',
  unconfirmed:'B站已接收加入请求，但暂未在目标小节读到该视频。',
}

export class CollectionJoinFailure extends Error {
  constructor(stage, error, secrets = {}, reason = '') {
    const rawCode = error?.rawResponse?.data?.code ?? error?.response?.data?.code ?? error?.code
    const remoteCode = Number.isSafeInteger(rawCode) ? rawCode : null
    const status = error?.rawResponse?.status ?? error?.response?.status ?? error?.statusCode
    const httpStatus = Number.isInteger(status) && status >= 100 && status <= 599 ? status : null
    const networkCode = NETWORK.has(error?.code) ? error.code : null
    const code = stage === 'join_verify' ? 'PUBLISH_COLLECTION_JOIN_UNKNOWN'
      : stage === 'join_add' ? remoteCode !== null && remoteCode !== 0
        ? 'PUBLISH_COLLECTION_JOIN_REJECTED' : 'PUBLISH_COLLECTION_JOIN_UNKNOWN'
      : 'PUBLISH_COLLECTION_JOIN_NOT_SENT'
    const diagnostic = [remoteCode !== null ? `平台错误码 ${remoteCode}` : '', httpStatus ? `HTTP ${httpStatus}` : '', networkCode || ''].filter(Boolean).join(' · ')
    const explanation = JOIN_REASONS[reason] || platformMessage(error, secrets)
    const recovery = [-101,-111].includes(remoteCode) ? '请重新扫码后再重试加入合集。'
      : [-412,-352,-351].includes(remoteCode) || [403,406,412,429].includes(httpStatus) ? '请到B站官网完成验证后再重试加入合集。'
      : code === 'PUBLISH_COLLECTION_JOIN_UNKNOWN' ? '关联结果待核对，不会自动重发；重试时先读取合集成员。'
      : '可按原因调整后重试加入合集。'
    super(`视频已投稿，但${JOIN_STAGES[stage] || '加入合集'}未完成${diagnostic ? `（${diagnostic}）` : ''}${explanation ? `：${explanation}` : '。'} ${recovery} 无需重新上传或投稿。`)
    this.code = code
    this.detail = {stage, remote_code:remoteCode, http_status:httpStatus, network_code:networkCode}
  }
}
