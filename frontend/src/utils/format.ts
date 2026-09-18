// 共享格式化工具 (此前 fmtSize/fmtDur 在 4 个文件里各有一份, 格式还不一致)
export function fmtSize(n?: number): string {
  if (!n && n !== 0) return '—'
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB'
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

/** 时长 → m:ss (波形/列表用) */
export function fmtDur(sec?: number | null): string {
  if (!sec && sec !== 0) return ''
  const m = Math.floor(sec / 60)
  return `${m}:${Math.round(sec % 60).toString().padStart(2, '0')}`
}

/** 时长 → 中文长格式 X分YY秒 (媒体信息栏用) */
export function fmtDurCN(sec?: number | null): string {
  if (!sec) return '—'
  const m = Math.floor(sec / 60)
  return `${m}分${Math.round(sec % 60).toString().padStart(2, '0')}秒`
}
