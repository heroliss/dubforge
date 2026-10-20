// 各配置范围共用同一种分组；原文样式关闭时只隐藏其独立参数，不清除保存值。
export function sourceStyleFieldVisible(field: any, values: Record<string, any>) {
  return !(field.depends_on || []).some(([key, value]: [string, any]) => key.endsWith('source_style_enabled') && values[key] !== value)
}
export function textStyleFieldGroups(fields: any[], prefix: string, values: Record<string, any>, commonName = '') {
  const visible = fields.filter(field => sourceStyleFieldVisible(field, values))
  if (!prefix) return [{ name: commonName, fields: visible }]
  const common: any[] = [], translated: any[] = [], original: any[] = []
  for (const field of visible) {
    const name = field.path.startsWith(prefix) ? field.path.slice(prefix.length) : ''
    if (['font','size','min_size','font_size','min_font_size','color','outline','outline_color'].includes(name)) translated.push(field)
    else if (name.startsWith('source_') && name !== 'source_first') original.push(field)
    else common.push(field)
  }
  return [{name: commonName, fields:common}, {name:'译文样式', fields:translated},
    {name:'原文样式', fields:original}].filter(group => group.fields.length)
}
