// 写操作回执与刷新分开：刷新失败不能把已成功的写操作误报为失败，也不能拖住确认框。
export function refreshInBackground(refresh: () => Promise<unknown>, onError: () => void = () => {}) {
  try { void Promise.resolve(refresh()).catch(onError) }
  catch { onError() }
}
