# -*- coding: utf-8 -*-
"""
步骤执行器
==========
核心语义 (对应 AGENTS.md 铁律 3/4 与 ADR-0001 原则 1/2):

- 单步启动: start_step(uid, key, force)
    1) 若 done 且输入指纹未变 且未 force → skip (不重算)
    2) 产出 Job 记录, 提交线程池 (全局串行, 保护 8G 显存)
    3) 执行中: 心跳定时器 + 协作式取消 (ctx.check_cancel())
    4) 结束: done (登记产物) / failed (错误三件套入库) / cancelled
- 重跑语义: force 重跑 = 删本步产物 → 下游全部置 stale
- 指纹: sha256( cfg_keys 配置快照 + 上游产物[name,sha256] 列表 )
- 崩溃恢复: recover() —— 启动时 running→interrupted / running Job→dead
- 日志: 项目 logs/<step>.log 追加; 步骤内通过 ctx.log() 打点
"""
import datetime
import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from dubforge import config
from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEPS, STEP_MAP, STEP_KEYS, downstream_keys

HEARTBEAT_SEC = 5.0


class StepCancelled(DubForgeError):
    code = "STEP_CANCELLED"
    suggestion = "步骤已取消, 可随时重新运行; 产物不会留下半成品。"


class StepContext:
    """步骤运行上下文: 步骤实现只依赖它, 不接触 DB/线程细节"""

    def __init__(self, project: Project, step_key: str, pdir: Path,
                 log_file: Path, cancel_event: threading.Event):
        self.project = project
        self.step_key = step_key
        self.pdir = pdir
        self._log_file = log_file
        self._cancel = cancel_event
        self.project_params: dict = json.loads(project.params or "{}")

    # ---- 日志 (带时间戳追加; 立即落盘, 崩溃后可读到最后状态) ----
    def log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")

    # ---- 取消 (协作式: 步骤在循环/分块间主动调用) ----
    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise StepCancelled("步骤被用户取消")

    # ---- 产物写入 (原子: tmp → os.replace → 登记 sha256) ----
    def artifact_dir(self) -> Path:
        d = self.pdir / "artifacts" / self.step_key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_bytes(self, name: str, data: bytes, kind: str = "bin") -> Path:
        final = self.artifact_dir() / name
        tmp = final.with_suffix(final.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, final)
        crud.add_artifact(self.project.id, self.step_key, kind, name,
                          final, str(final.relative_to(self.pdir)))
        return final

    def write_json(self, name: str, data, kind: str = "json") -> Path:
        return self.write_bytes(name, json.dumps(data, ensure_ascii=False,
                                                 indent=1).encode("utf-8"), kind)

    def register_file(self, name: str, path: Path, kind: str) -> Path:
        """登记一个已存在的文件为产物 (大文件不复制)。
        项目目录内存相对路径; 外部引用文件 (本地导入零拷贝) 存绝对路径。"""
        path = Path(path)
        try:
            rel = str(path.relative_to(self.pdir))
        except ValueError:
            rel = str(path)
        crud.add_artifact(self.project.id, self.step_key, kind, name,
                          path, rel)
        return path

    # ---- 配置读取 (每步实时读, 改配置立即生效) ----
    def cfg(self, key: str, default=None):
        return config.get(key, default)

    # ---- 进度上报 (0-99; 整数变化才落盘, 完成时执行器置 100) ----
    def report_progress(self, pct: float) -> None:
        pct = max(0.0, min(99.0, float(pct)))
        if int(pct) <= getattr(self, "_last_pct", -1):
            return
        self._last_pct = int(pct)
        step = crud.get_step(self.project.id, self.step_key)
        if step:
            step.progress = pct
            crud.save_step(step)

    # ---- 上游产物访问 (执行器保证上游已完成; 拿不到即管线违约) ----
    def upstream(self, step_key: str, name: str) -> Path:
        arts = crud.latest_artifacts(self.project.id, [step_key])
        for a in arts.values():
            if a.step_key == step_key and a.name == name:
                p = Path(a.rel_path)
                if not p.is_absolute():     # 外部引用文件 (本地导入) 存的是绝对路径
                    p = self.pdir / p
                if p.exists():
                    return p
        from dubforge.errors import ArtifactMissingError
        raise ArtifactMissingError(
            f"缺少上游产物 {step_key}/{name} (项目 {self.project.uid})")


def _cfg_snapshot(cfg_keys: tuple[str, ...]) -> dict:
    """按注册表的 cfg_keys 提取配置子集 (前缀语义: 'llm' 匹配整段)"""
    cfg = config.load_config()
    snap: dict = {}
    for key in cfg_keys:
        parts = key.split(".")
        node = cfg
        ok = True
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                ok = False
                break
            node = node[p]
        if ok:
            snap[key] = node
    return snap


def compute_fingerprint(project: Project, step_key: str) -> str:
    """输入指纹 = sha256( 配置子集 + 上游全部产物[name,sha256] )"""
    idx = STEP_KEYS.index(step_key)
    upstream = STEP_KEYS[:idx]
    arts = crud.latest_artifacts(project.id, upstream)
    payload = {
        "step": step_key,
        "cfg": _cfg_snapshot(STEP_MAP[step_key].cfg_keys),
        "upstream": sorted([f"{a.step_key}/{a.name}:{a.sha256}"
                            for a in arts.values()]),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


class Runner:
    """全局单例: 串行线程池 + 取消标志 + 心跳"""

    def __init__(self, max_workers: int = 1):
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="dubforge-step")
        self._cancels: dict[tuple[int, str], threading.Event] = {}
        self._lock = threading.Lock()
        self._active_jobs: dict[int, Job] = {}          # job.id -> Job (心跳用)
        self._heartbeat_stop = threading.Event()
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="dubforge-heartbeat").start()

    # ============ 启动/恢复 ============

    def recover(self) -> dict:
        """进程启动时调用: 上次运行残留的 running 状态一律判中断 (原子写保证产物完整)"""
        import sqlmodel
        now = datetime.datetime.now(datetime.timezone.utc)
        steps_fixed, jobs_fixed = 0, 0
        with crud.session() as s:
            for job in s.exec(sqlmodel.select(Job).where(Job.status == "running")).all():
                job.status = "dead"
                job.finished_at = now
                job.error_detail = "进程重启, 任务中断"
                s.add(job)
                jobs_fixed += 1
            for step in s.exec(sqlmodel.select(StepRun).where(
                    StepRun.status == "running")).all():
                step.status = "failed"
                step.error_code = "STEP_INTERRUPTED"
                step.error_message = "上次执行被中断 (进程退出)"
                step.error_suggestion = "产物无半成品残留, 直接重跑即可; 已完成的下游会自动标记过期。"
                step.finished_at = now
                s.add(step)
                steps_fixed += 1
            # queued = 还在线程池队列里没开跑 → 直接回待运行 (无半成品)
            for step in s.exec(sqlmodel.select(StepRun).where(
                    StepRun.status == "queued")).all():
                step.status = "pending"
                s.add(step)
                steps_fixed += 1
            s.commit()
        if steps_fixed or jobs_fixed:
            logging.warning("崩溃恢复: %d 个步骤判中断, %d 个 Job 判 dead",
                            steps_fixed, jobs_fixed)
        return {"steps_interrupted": steps_fixed, "jobs_dead": jobs_fixed}

    # ============ 启动步骤 ============

    def start_step(self, uid: str, step_key: str, force: bool = False) -> dict:
        if step_key not in STEP_MAP:
            raise DubForgeError(f"步骤不存在: {step_key}", code="STEP_NOT_FOUND",
                                suggestion=f"合法步骤: {', '.join(STEP_KEYS)}")
        step_def = STEP_MAP[step_key]
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        step = crud.get_step(project.id, step_key)
        if not step:
            raise DubForgeError(f"步骤不存在: {step_key}", code="STEP_NOT_FOUND")
        if step.status in ("running", "queued"):
            raise DubForgeError("该步骤正在运行或排队中", code="STEP_RUNNING",
                                suggestion="请等待完成, 或先取消再重跑。")

        # 依赖门禁: 上游步骤必须至少产出过 (done/stale), 否则入口处明确拒绝 ——
        # 跑到一半才报"缺少上游产物"是陷阱; stale 上游不算阻塞 (产物在盘上, 重跑下游正是其语义)
        idx = STEP_KEYS.index(step_key)
        if idx:
            steps_by_key = {s.step_key: s for s in crud.get_steps(project.id)}
            blockers = [f"「{STEP_MAP[k].label}」({steps_by_key[k].status})"
                        for k in STEP_KEYS[:idx]
                        if steps_by_key[k].status not in ("done", "stale")]
            if blockers:
                raise DubForgeError(
                    f"无法运行 [{step_def.label}] —— 上游步骤未就绪: {', '.join(blockers)}",
                    code="DEPS_NOT_MET",
                    suggestion="请先运行上游步骤; 依赖就绪后本步骤才能执行。")

        # 非 force 且已完成且指纹未变 → 跳过 (配置/上游都没变, 无需重算)
        fp = compute_fingerprint(project, step_key)
        if not force and step.status == "done" and step.input_fingerprint == fp:
            return {"status": "skipped", "message": "输入未变化 (配置与上游产物一致), 无需重跑"}

        if force:
            # 重跑语义: 删本步产物, 下游置 stale
            crud.delete_step_artifacts(project.id, step_key)
            self._mark_downstream_stale(project.id, step_key)

        cancel = threading.Event()
        with self._lock:
            self._cancels[(project.id, step_key)] = cancel

        # 线程池全局串行 (max_workers=1, 保护显存): 提交后先显示排队,
        # 轮到它执行时 _execute 再置 running
        step.status = "queued"
        crud.save_step(step)

        log_file = crud.project_dir(uid) / "logs" / f"{step_key}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n===== 运行开始 {datetime.datetime.now().isoformat(timespec='seconds')} "
                    f"(force={force}) =====\n")

        job = crud.add_job(project.id, step_key, str(log_file))
        fut = self._pool.submit(self._execute, project, step_key, step_def, job,
                                log_file, cancel, fp)
        # future 异常必须被观测, 否则线程池静默吞错 (排查问题时是盲区)
        fut.add_done_callback(
            lambda f: f.exception() and logging.error(
                "步骤任务异常退出: %s", f.exception(), exc_info=f.exception()))
        return {"status": "started", "job_id": job.id, "log": str(log_file)}

    # ============ 执行体 (线程池内) ============

    def _execute(self, project: Project, step_key: str, step_def, job: Job,
                 log_file: Path, cancel: threading.Event, fingerprint: str) -> None:
        from dubforge.pipeline.steps import get_runner
        pdir = crud.project_dir(project.uid)
        ctx = StepContext(project, step_key, pdir, log_file, cancel)

        # 取 DB 里的最新 step 行 (start_step 读到的可能是旧快照)
        step = crud.get_step(project.id, step_key)
        step.status = "running"
        step.started_at = datetime.datetime.now(datetime.timezone.utc)
        step.finished_at = None
        step.error_code = step.error_message = step.error_suggestion = ""
        step.input_fingerprint = fingerprint
        step.progress = 0.0
        crud.save_step(step)
        with self._lock:
            self._active_jobs[job.id] = job

        try:
            # 队列中已被取消 → 直接退出
            if cancel.is_set():
                raise StepCancelled("步骤在排队时被取消")
            ctx.log(f"▶ 开始步骤 [{step_def.label}] ({step_key})")
            run_fn = get_runner(step_key)
            run_fn(ctx)
            step = crud.get_step(project.id, step_key)
            step.status = "done"
            step.progress = 100.0
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
            job.status = "done"
            ctx.log("✅ 步骤完成")
        except StepCancelled as e:
            step = crud.get_step(project.id, step_key)
            step.status = "failed"
            step.error_code, step.error_message = e.code, e.message
            step.error_suggestion = e.suggestion
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
            job.status, job.error_detail = "cancelled", "用户取消"
            ctx.log(f"⛔ 已取消: {e.message}")
        except DubForgeError as e:
            step = crud.get_step(project.id, step_key)
            step.status = "failed"
            step.error_code, step.error_message = e.code, e.message
            step.error_suggestion = e.suggestion
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
            job.status, job.error_detail = "failed", str(e.detail or e.message)
            ctx.log(f"❌ 失败 [{e.code}]: {e.message}")
        except Exception as e:  # 未知异常兜底: 不让线程静默死亡
            import traceback
            tb = traceback.format_exc()
            step = crud.get_step(project.id, step_key)
            step.status = "failed"
            step.error_code = "UNEXPECTED_ERROR"
            step.error_message = f"未预期的错误: {type(e).__name__}: {e}"
            step.error_suggestion = "请查看日志详情; 可直接重跑该步骤。"
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
            job.status, job.error_detail = "failed", tb
            ctx.log(f"❌ 未预期异常:\n{tb}")
        finally:
            job.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.update_job(job)
            with self._lock:
                self._active_jobs.pop(job.id, None)
                self._cancels.pop((project.id, step_key), None)
            crud.write_manifest(project.uid)
            self._refresh_project_status(project.uid)

    @staticmethod
    def _refresh_project_status(uid: str) -> None:
        """项目状态 = 步骤聚合: 有 running→processing / 有 failed→failed / 全 done→done"""
        project = crud.get_project(uid)
        if not project:
            return
        steps = crud.get_steps(project.id)
        statuses = {s.status for s in steps}
        if "running" in statuses or "queued" in statuses:
            new = "processing"
        elif "failed" in statuses:
            new = "failed"
        elif statuses == {"done"}:
            new = "done"
        elif "pending" in statuses or "stale" in statuses:
            new = "created" if "done" not in statuses else "processing"
        else:
            new = project.status
        if new != project.status:
            project.status = new
            crud.save_project(project)

    # ============ 取消 ============

    def cancel_step(self, uid: str, step_key: str) -> dict:
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        with self._lock:
            ev = self._cancels.get((project.id, step_key))
        if ev is None:
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        ev.set()
        return {"status": "cancelling", "message": "取消信号已发送, 等待步骤在下一个检查点退出"}

    def is_cancelling(self, project_id: int, step_key: str) -> bool:
        """该步骤是否已发出取消信号且尚未退出 (前端显示「取消中…」)"""
        with self._lock:
            return (project_id, step_key) in self._cancels

    # ============ 心跳 ============

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_SEC):
            with self._lock:
                jobs = list(self._active_jobs.values())
            for job in jobs:
                job.heartbeat_at = datetime.datetime.now(datetime.timezone.utc)
                try:
                    crud.update_job(job)
                except Exception:
                    logging.exception("心跳更新失败 job=%s", job.id)

    # ============ 内部 ============

    def _mark_downstream_stale(self, project_id: int, step_key: str) -> None:
        for key in downstream_keys(step_key):
            s = crud.get_step(project_id, key)
            if s.status in ("done", "stale"):
                s.status = "stale"
                s.error_message = f"上游步骤 [{step_key}] 已重跑, 本步结果过期"
                crud.save_step(s)


runner = Runner()
