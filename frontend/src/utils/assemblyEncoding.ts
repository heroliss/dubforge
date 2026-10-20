export const VIDEO_ENCODING_PATHS = new Set(['assemble.video_encoder', 'assemble.crf', 'assemble.nvenc_cq'])
export const isVideoEncodingSetting = (path: string) => VIDEO_ENCODING_PATHS.has(path)

export function videoEncoderLabel(value: unknown): string {
  return value === 'software' ? '软件编码（H.264 / CPU）'
    : value === 'nvenc' ? 'NVIDIA 硬件编码（H.264 / NVENC）' : '编码方式待核对'
}

/** 历史版本的编码值只取已保存快照；缺编码方式的旧快照明确使用软件，不借用新全局值。 */
export function assemblyEncodingValues(values: Record<string, any>, frozen?: Record<string, any>) {
  const result = { ...values }
  if (frozen) for (const key of VIDEO_ENCODING_PATHS) {
    delete result[key]
    if (Object.hasOwn(frozen, key)) result[key] = frozen[key]
  }
  if (!Object.hasOwn(result, 'assemble.video_encoder')) result['assemble.video_encoder'] = 'software'
  return result
}

export function encodingFieldHelp(field: any, values: Record<string, any>, scope: 'version' | 'configuration' = 'version') {
  let help = String(field.help ?? '').trim()
  if (!isVideoEncodingSetting(field.path)) return help
  const mode = values['assemble.video_encoder'] ?? 'software'
  const comparison = 'CQ 与软件 CRF 不是同一质量刻度，数值相同不代表画质相同；建议先生成试片确认。'
  if (!help) help = field.path === 'assemble.video_encoder'
    ? '软件编码使用 CPU；NVIDIA 硬件编码使用 NVENC，需要运行前检查设备支持与可用资源。'
    : '数值越小通常画质更高、文件更大。'
  if (!/不是同一质量刻度|不代表|独立.*刻度|不等同/.test(help)) help += '\n' + comparison
  if (field.path === 'assemble.nvenc_cq' && mode !== 'nvenc') help += '\n当前未选择 NVIDIA 硬件编码，此 CQ 参数暂不生效，已设值保留。'
  if (field.path === 'assemble.crf' && mode !== 'software') help += '\n当前未选择软件编码，此 CRF 参数暂不生效，已设值保留。'
  help += '\n作用范围：' + (scope === 'version' ? '此版本后续生成的试片和成片' : '保存后采用这些设置的试片和成片；已有项目与版本专用值仍优先')
    + '；已有成片保留，切换方式不会复制或清空另一种质量参数。'
  return help
}
