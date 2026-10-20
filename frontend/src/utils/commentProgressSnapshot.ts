// 导航与评论工作区共享轻量读取，避免同时请求同一份进度；不缓存完整评论列表。
type Snapshot = { ok:boolean; status:number; value:any }
type Entry = { promise?:Promise<Snapshot>; snapshot?:Snapshot; checkedAt:number }
const entries = new Map<string,Entry>()
const MAX_ENTRIES=32, MAX_AGE=1000

export async function readCommentProgress(uid:string,signal?:AbortSignal,force=false):Promise<Snapshot> {
  if(signal?.aborted) throw new DOMException('操作已取消','AbortError')
  let entry=entries.get(uid)
  if(!entry) {
    entry={checkedAt:0};entries.set(uid,entry)
    if(entries.size>MAX_ENTRIES) entries.delete(entries.keys().next().value!)
  }
  if(!entry.promise && !force && entry.snapshot && Date.now()-entry.checkedAt<MAX_AGE) return entry.snapshot
  if(!entry.promise) {
    const current=entry
    current.promise=(async()=>{
      const response=await fetch(`/api/projects/${encodeURIComponent(uid)}/comments/progress`,{cache:'no-store',signal:AbortSignal.timeout(10000)})
      const value=[404,405].includes(response.status)?{}:await response.json()
      const snapshot={ok:response.ok,status:response.status,value}
      if(response.ok) {current.snapshot=snapshot;current.checkedAt=Date.now()}
      return snapshot
    })().finally(()=>{current.promise=undefined})
  }
  const pending=entry.promise!
  if(!signal) return pending
  let cancel:(()=>void)|undefined
  try {
    return await Promise.race([pending,new Promise<never>((_,reject)=>{
      cancel=()=>reject(new DOMException('操作已取消','AbortError'))
      signal.addEventListener('abort',cancel,{once:true})
      if(signal.aborted) cancel()
    })])
  } finally { if(cancel)signal.removeEventListener('abort',cancel) }
}
