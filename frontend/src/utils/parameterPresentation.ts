type Field = { path?: string; label: string; help?: string }

/** 一个依赖可接受多个枚举值；其他依赖条件之间仍是同时满足。 */
export function parameterDependencyMatches(actual: unknown, expected: unknown) {
  return Array.isArray(expected) ? expected.some(value => Object.is(actual, value)) : Object.is(actual, expected)
}

/** 后端保留完整参数名；标题只收起与字段键一致的技术后缀，单位仍显示。 */
export function parameterPresentation(field: Field) {
  const suffix = field.label.match(/[（(]([^（）()]+)[）)]\s*$/)
  const key = field.path?.split('.').at(-1)
  if (suffix && suffix[1] === key && /^[a-z][a-z0-9_]*$/.test(key ?? '')) {
    return { title: field.label.slice(0, suffix.index).trim(), key }
  }
  return { title: field.label, key: undefined }
}

export function parameterHelp(field: Field, help = field.help ?? '') {
  const { key } = parameterPresentation(field)
  return key ? [help, `参数名：${key}`].filter(Boolean).join('\n\n') : help
}
