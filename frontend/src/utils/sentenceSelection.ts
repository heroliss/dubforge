// 范围以当前筛选后的显示顺序为准，隐藏的选择保持不变。
export function selectRange(selected: number[], visible: number[], id: number, checked: boolean, anchor: number | null, shift: boolean) {
  const a = anchor === null ? -1 : visible.indexOf(anchor), b = visible.indexOf(id)
  const range = shift && a >= 0 && b >= 0 ? visible.slice(Math.min(a, b), Math.max(a, b) + 1) : [id]
  const result = new Set(selected)
  range.forEach(value => checked ? result.add(value) : result.delete(value))
  return [...result]
}
