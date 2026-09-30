"""
步骤执行器
==========
核心语义 (对应 AGENTS.md 铁律 3/4 与 ADR-0001 原则 1/2):

- 单步启动: start_step(uid, key, force)
    1) 若 done、输入指纹未变且登记产物完整、未 force → skip (不重算)
    2) 产出 Job 记录, 提交线程池 (每步骤并行闸门保护显存)
    3) 执行中: 心跳定时器 + 协作式取消 + 每步活动时长超时兜底
    4) 结束: done (登记产物) / failed (错误三件套入库) / cancelled
- 重跑语义: force 重跑 = 删本步产物 → 已有结果的下游置 stale，未运行的保持 pending
- 指纹: sha256( cfg_keys 配置快照 + 上游产物[name,sha256] 列表 + 首步源快照 )
- 崩溃恢复: recover() —— 启动时 running→interrupted / running Job→dead
- 日志: 项目 logs/<step>.log 追加; 步骤内通过 ctx.log() 打点
"""
import datetime
import copy
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from dubforge import config
from dubforge.domain import crud
from dubforge.domain.models import Job, Project, StepRun
from dubforge.errors import DubForgeError, StepFailedError
from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
from dubforge.utils.fs import atomic_replace

HEARTBEAT_SEC = 5.0
logger = logging.getLogger(__name__)


class StepCancelled(DubForgeError):
    code = "STEP_CANCELLED"
    suggestion = "步骤已取消, 可随时重新运行; 产物不会留下半成品。"


class StepTimeout(StepCancelled):
    """步骤超过运行兜底后产生的可恢复失败。"""

    code = "STEP_TIMEOUT"
    suggestion = "步骤超过设定的运行时间, 已清理临时文件; 请检查服务是否卡住或适当调大本步骤超时。"


# 临时/半成品文件的清扫模式: 取消、失败、强制结束后清一遍 (原子写保证正式产物
# 永远完整, 这里清的只是过程文件: ffmpeg/yt-dlp 片段、步骤内临时名、demucs 工作目录)
_TEMP_FILE_PATTERNS = ("*.tmp", "*.part", "*.ytdl", ".tmp_*")
_TEMP_DIR_NAMES = (".demucs_tmp",)


def _sweep_temp_files(pdir: Path, step_key: str, log=None) -> list[str]:
    """清扫本步骤目录下的过程临时文件, 返回清理清单 (不存在则静默通过)。

    只扫本步骤自己的地盘 (主产物目录 + artifacts/<key>) —— 项目级清扫会与
    自动续跑中的下游步骤赛跑: 对方刚写入的 .tmp 会被误删, os.replace 直接
    FileNotFoundError (真实事故)。其他步骤的残留由其自己下次运行的收尾清扫处理。"""
    removed: list[str] = []
    if not pdir.exists():
        return removed
    step_def = STEP_MAP.get(step_key)
    roots = []
    if step_def and step_def.dir:
        roots.append(pdir / step_def.dir)
    roots.append(pdir / "artifacts" / step_key)
    for root in roots:
        if not root.is_dir():
            continue
        for pat in _TEMP_FILE_PATTERNS:
            for f in root.rglob(pat):
                if f.is_file():
                    try:
                        f.unlink()
                        removed.append(str(f.relative_to(pdir)))
                    except OSError:
                        pass
        for name in _TEMP_DIR_NAMES:
            for d in root.rglob(name):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(str(d.relative_to(pdir)) + "/")
        if step_key == "download":
            # 下载转正前的独占目录；硬退出后也不得把其中单流文件当成正式源视频。
            for d in root.glob(".download-*.tmp"):
                if d.is_dir() and not d.is_symlink() and d.resolve().is_relative_to(root.resolve()):
                    shutil.rmtree(d, ignore_errors=True)
                    if not d.exists():
                        removed.append(str(d.relative_to(pdir)) + "/")
    if removed and log:
        head = ", ".join(removed[:8])
        log(f"🧹 已清理临时文件 {len(removed)} 个: {head}{'…' if len(removed) > 8 else ''}")
    return removed


class StepContext:
    """步骤运行上下文: 步骤实现只依赖它, 不接触 DB/线程细节"""

    def __init__(self, project: Project, step_key: str, pdir: Path,
                 log_file: Path, cancel_event: threading.Event,
                 pause_event: threading.Event | None = None,
                 force: bool = False, job_id: int | None = None):
        self.project = project
        self.step_key = step_key
        self.pdir = pdir
        self._log_file = log_file
        self._cancel = cancel_event
        self._pause = pause_event or threading.Event()
        self.force = bool(force)             # 本次是否强制重跑 (步骤实现可据此决定真重做)
        self.trial_only = False              # 试合成等临时任务不改变正式产物指纹
        self.job_id = job_id
        self.task_data = {}
        self._job = None
        self.rebuild = False                # 输入变化/失败恢复时替换旧产物
        self._log_lock = threading.Lock()   # 分块并行时多线程写同一日志
        self._llm_trace_file = log_file.with_name(f"{step_key}.llm.jsonl")
        self._subprocs: set = set()          # 本步骤启动的子进程 (取消/强制结束时终止)
        self._subproc_lock = threading.Lock()
        self._pause_notice = False
        self._pause_notice_lock = threading.Lock()
        self._timeout = threading.Event()
        # ETA 与百分比独立；活动时钟排除排队和实际暂停等待。
        self._run_started = time.monotonic()
        self._paused_seconds = 0.0
        self._pause_started = None
        self._eta_provider = None
        self._eta_scope = "step"
        self._progress_provider = None
        self._progress_phase = ("", False)
        self._progress_lock = threading.RLock()
        # 运行期间固定配置快照。设置页允许随时编辑，但不能让同一轮任务
        # 的后续窗口突然换模型/参数，否则会表现为“模型重新开始思考”或
        # 同一任务前后结果不可复现。下一次运行才读取新配置。
        self._config_snapshot = copy.deepcopy(config.load_config())
        self.project_params: dict = crud.project_params(project)
        from dubforge.configuration import step_values
        self._project_step_params_snapshot = copy.deepcopy(
            step_values(project, settings=self._config_snapshot))
        self.llm_trace("run_start", "", job_id=job_id)
        llm_cfg = self._config_snapshot.get("llm", {})
        profile_names = [str(p.get("name") or "(未命名)")
                         for p in (llm_cfg.get("profiles") or [])
                         if isinstance(p, dict) and p.get("enabled", True)]
        self.log("🔒 本次运行已固定配置快照；运行中修改设置只对下一次运行生效"
                 + (f"（启用 LLM 配置: {'、'.join(profile_names)}）"
                    if profile_names else ""))

    def save_task_data(self) -> None:
        """仅在句子切换/结束时保存任务范围和结果；细粒度进度沿用步骤上报。"""
        if self._job is not None:
            self._job.task_data = json.dumps(self.task_data, ensure_ascii=False)
            crud.update_job(self._job)

    # ---- 日志 (带时间戳追加; 立即落盘, 崩溃后可读到最后状态) ----
    def log(self, msg: str) -> None:
        ts = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
        run_tag = f" [任务#{self.job_id}]" if self.job_id is not None else ""
        with self._log_lock, open(self._log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}]{run_tag} {msg}\n")

    def llm_trace(self, event: str, content: str = "", **meta) -> None:
        """记录 LLM 对话，供步骤页增量读取展示。

        只记录提示词/模型输出，不记录 API 地址、密钥或完整请求体；内容不截断，
        保证长提示词和思考内容可完整回看。JSONL 追加写入，多个翻译
        块并行时沿用日志锁保证每行完整。
        """
        record = {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "content": str(content or ""),
            **{k: v for k, v in meta.items() if k not in {"api_key", "token", "secret"}},
        }
        try:
            # 实时面板只服务于当前运行；新任务从头覆盖，避免长期运行把
            # 多轮对话无限追加，也让前端轮询不必读取历史大文件。
            mode = "w" if event == "run_start" else "a"
            with self._log_lock, open(self._llm_trace_file, mode, encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # 实时预览是辅助能力，磁盘写失败不能影响正式步骤。
            pass

    # ---- 取消 (协作式: 步骤在循环/分块间主动调用) ----
    def check_cancel(self) -> None:
        if self._timeout.is_set():
            raise StepTimeout("步骤超过运行超时兜底, 已请求终止")
        if self._cancel.is_set():
            raise StepCancelled("步骤被用户取消")

    def request_timeout(self, seconds: float) -> None:
        """由执行器看门狗调用; 先终止可控子进程, 再让步骤在检查点退出。"""
        if self._timeout.is_set():
            return
        self._timeout.set()
        self._cancel.set()
        self.log(f"⏰ 步骤超时兜底触发: 已运行超过 {seconds:g}s，正在请求安全终止")
        self.kill_all_subprocesses()

    # ---- 暂停 (协作式: 循环间的暂停点; 暂停期间阻塞等待, 取消优先) ----
    def check_pause(self) -> None:
        t0 = time.monotonic()
        announced = False
        while self._pause.is_set() and not self._cancel.is_set():
            if self._pause_started is None:
                self._pause_started = t0
            with self._pause_notice_lock:
                first_paused = not self._pause_notice
                if first_paused:
                    self._pause_notice = True
            if first_paused and not announced:
                self.log("⏸ 已暂停，等待继续")
                announced = True
            time.sleep(0.2)
        if announced and not self._cancel.is_set():
            self.log("▶ 已收到继续指令，继续处理下一个原子步骤")
            with self._pause_notice_lock:
                self._pause_notice = False
        waited = time.monotonic() - t0
        if waited > 0.5:    # 暂停等待不计入 ETA 的“已耗时”
            self._paused_seconds = getattr(self, "_paused_seconds", 0.0) + waited
        self._pause_started = None
        self.check_cancel()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    # ---- 子进程: 取消信号一到立即终止 (ffmpeg / yt-dlp 可即时停下) ----
    def run_subprocess(self, cmd: list, *, cwd=None, env=None,
                       on_stdout=None, on_stderr=None) -> tuple[int, str, str]:
        """启动子进程并等待; 期间轮询取消信号, 取消即终止进程树并抛 StepCancelled。
        on_stdout/on_stderr: 按行回调 (实时进度); 返回 (returncode, stdout, stderr)。"""
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, text=True,
            encoding="utf-8", errors="replace",   # 子进程 (yt-dlp/ffmpeg) 输出为 UTF-8;
                                                  # 不指定时 Windows 按 gbk 解码会乱码
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._subproc_lock:
            self._subprocs.add(proc)
        out_lines: list[str] = []
        err_lines: list[str] = []

        def _pump(pipe, sink, cb):
            try:
                for line in iter(pipe.readline, ""):
                    sink.append(line)
                    if cb:
                        try:
                            cb(line.rstrip("\r\n"))
                        except Exception:
                            pass
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        t_out = threading.Thread(target=_pump, args=(proc.stdout, out_lines, on_stdout),
                                 daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err_lines, on_stderr),
                                 daemon=True)
        t_out.start()
        t_err.start()
        try:
            while True:
                try:
                    rc = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self._cancel.is_set():
                        self._kill_proc(proc)
                        self.check_cancel()   # 抛 StepCancelled
        finally:
            with self._subproc_lock:
                self._subprocs.discard(proc)
            t_out.join(timeout=5)
            t_err.join(timeout=5)
        return rc, "".join(out_lines), "".join(err_lines)

    @staticmethod
    def _kill_proc(proc) -> None:
        """终止进程及其子进程 (yt-dlp 会派生 ffmpeg 合并进程, 必须整树杀)"""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, check=False)
            else:
                proc.kill()
        except Exception:
            pass

    def kill_all_subprocesses(self) -> None:
        with self._subproc_lock:
            procs = list(self._subprocs)
        for p in procs:
            self._kill_proc(p)

    # ---- 产物写入 (原子: tmp → os.replace → 登记 sha256) ----
    def artifact_dir(self) -> Path:
        d = self.pdir / "artifacts" / self.step_key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_bytes(self, name: str, data: bytes, kind: str = "bin") -> Path:
        final = self.artifact_dir() / name
        tmp = final.with_suffix(final.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            atomic_replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        crud.add_artifact(self.project.id, self.step_key, kind, name,
                          final, str(final.relative_to(self.pdir)))
        return final

    def write_json(self, name: str, data, kind: str = "json") -> Path:
        return self.write_bytes(name, json.dumps(data, ensure_ascii=False,
                                                 indent=1).encode("utf-8"), kind)

    def register_file(self, name: str, path: Path, kind: str, *, replace_existing: bool = False) -> Path:
        """登记一个已存在的文件为产物 (大文件不复制)。
        项目目录内存相对路径; 外部引用文件 (本地导入零拷贝) 存绝对路径。"""
        path = Path(path)
        try:
            rel = str(path.relative_to(self.pdir))
        except ValueError:
            rel = str(path)
        crud.add_artifact(self.project.id, self.step_key, kind, name,
                          path, rel, replace_existing=replace_existing)
        return path

    # ---- 配置读取 (项目级覆盖优先, 全局 config 兜底; 本次运行固定快照) ----
    def cfg(self, key: str, default=None):
        if key in self._project_step_params_snapshot:
            return copy.deepcopy(self._project_step_params_snapshot[key])
        node = self._config_snapshot
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        node = copy.deepcopy(node)
        if isinstance(node, dict):
            for path, value in self._project_step_params_snapshot.items():
                if path.startswith(key + '.'):
                    target = node
                    parts = path[len(key) + 1:].split('.')
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = copy.deepcopy(value)
        return node

    def llm_profile_snapshot(self, selected: str | None = None) -> list[dict]:
        """返回本次运行固定的 LLM 配置组快照。

        返回副本，调用方即使修改请求体也不会污染其他并发请求或运行记录。
        ``selected`` 为空时保留所有已启用组作为降级链。
        """
        llm_cfg = self._config_snapshot.get("llm", {})
        profiles = [copy.deepcopy(p) for p in (llm_cfg.get("profiles") or [])
                    if isinstance(p, dict) and p.get("enabled", True)]
        if not profiles and llm_cfg.get("base_url") and llm_cfg.get("api_key"):
            profiles = [{"name": "default", "base_url": llm_cfg["base_url"],
                         "api_key": llm_cfg["api_key"],
                         "model": llm_cfg.get("model", "")}]
        wanted = str(selected or "").strip()
        if not wanted:
            return profiles
        chosen = [p for p in profiles if str(p.get("name", "")).strip() == wanted]
        if not chosen:
            raise StepFailedError(
                f"未找到指定的 LLM 配置组「{wanted}」",
                suggestion="请在设置页检查配置组名称，或把步骤里的模型配置改为“跟随全局降级链”。")
        return chosen

    # ---- 进度上报 (0-99; 0.1 精度落盘, 完成时执行器置 100) ----
    def active_seconds(self) -> float:
        now = time.monotonic()
        waiting = now - self._pause_started if self._pause_started is not None else 0.0
        return max(0.0, now - self._run_started - self._paused_seconds - waiting)

    def set_eta_provider(self, provider, *, scope="step") -> None:
        """由步骤提供剩余时间算法；轮询时直接计算，不依赖进度增长。"""
        self._eta_provider, self._eta_scope = provider, scope

    def set_progress_provider(self, provider) -> None:
        """仅 LLM 提供实时预计进度；其他步骤继续使用实际回调进度。"""
        self._progress_provider = provider

    def set_progress_phase(self, label: str, *, indeterminate=False) -> None:
        """没有真实工作量回调的准备阶段只显示状态，不编造百分比。"""
        self._progress_phase = (label, indeterminate)

    def report_eta(self, remaining_seconds: float) -> None:
        """登记从现在起的剩余时间；超过估计后撤下倒计时。"""
        deadline = self.active_seconds() + max(0.0, float(remaining_seconds))
        self.set_eta_provider(lambda: max(0.0, deadline - self.active_seconds()))

    def eta_seconds(self) -> float:
        provider = self._eta_provider
        value = provider() if provider else None
        return round(float(value), 1) if value is not None and math.isfinite(value) and value > 0 else 0.0

    def report_progress(self, pct: float) -> None:
        pct = round(max(0.0, min(99.0, float(pct))), 1)
        with self._progress_lock:
            pct = max(pct, getattr(self, "_last_pct", 0.0))
            eta = self.eta_seconds()
            if (pct, eta) == getattr(self, "_last_progress", None):
                return
            self._last_pct, self._last_progress = pct, (pct, eta)
            step = crud.get_step(self.project.id, self.step_key)
            if step:
                step.progress, step.eta_seconds = pct, eta
                crud.save_step(step)

    # ---- 上游产物访问 (执行器保证上游已完成; 拿不到即管线违约) ----
    def upstream(self, step_key: str, name: str) -> Path:
        arts = crud.latest_artifacts(self.project.id, [step_key])
        for a in arts.values():
            if a.step_key == step_key and a.name == name:
                p = Path(a.rel_path)
                if not p.is_absolute():     # 外部引用文件 (本地导入) 存的是绝对路径
                    root = self.pdir.resolve()
                    p = (self.pdir / p).resolve()
                    if not p.is_relative_to(root):
                        continue
                if p.is_file():
                    return p
        from dubforge.errors import ArtifactMissingError
        raise ArtifactMissingError(
            f"缺少上游产物 {step_key}/{name} (项目 {self.project.uid})")


def _cfg_snapshot(cfg_keys: tuple[str, ...], overrides: dict | None = None, *, settings=None) -> dict:
    """按注册表的 cfg_keys 提取配置子集 (前缀语义: 'llm' 匹配整段)。
    overrides = 项目级覆盖 {点分路径: 值}, 只注入命中本步骤 cfg_keys 的键
    (精确命中或前缀段内); 无关覆盖忽略 —— 无覆盖时快照与旧版完全一致。"""
    cfg = copy.deepcopy(settings) if settings is not None else config.load_config()
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
    if overrides:
        key_set = set(cfg_keys)
        for path, value in overrides.items():
            if path in key_set:
                snap[path] = value
                continue
            hit = next((k for k in key_set if path.startswith(k + ".")), None)
            if hit is None:
                continue
            node = snap.get(hit)
            if not isinstance(node, dict):
                snap[hit] = {path[len(hit) + 1:]: value}
                continue
            parts = path[len(hit) + 1:].split(".")
            cur = node
            for p in parts[:-1]:
                if not isinstance(cur.get(p), dict):
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value
    return snap


def compute_fingerprint(project: Project, step_key: str, *, dependencies=None, legacy=False) -> str:
    """输入指纹 = 配置、上游产物以及首步源输入的稳定快照。"""
    from dubforge.pipeline.dependencies import Dependencies
    deps = dependencies or Dependencies(project)
    inputs = deps.inputs(step_key)
    arts = {f"{up}/{name}": deps.arts.get(f"{up}/{name}") for up, name, _ in inputs}
    cfg_keys = STEP_MAP[step_key].cfg_keys
    if legacy:
        # 旧版全上游指纹早于独立平台资料；补采/刷新资料不能改变历史制作输入。
        # 只排除此非制作产物，保留视频、音轨等所有原有校验，不放宽未知指纹。
        arts = {name: a for name, a in deps.arts.items()
                if a.step_key in STEP_KEYS[:STEP_KEYS.index(step_key)]
                and name != "download/video_info.json"}
        if step_key == "translate":
            cfg_keys = ("translate", "llm")
    cfg_snapshot = {key: value for key in cfg_keys if (value := deps.cfg(key)) is not None}
    if step_key == 'assemble' and isinstance(cfg_snapshot.get('assemble'), dict):
        assembly = cfg_snapshot['assemble']
        if not assembly.get('subtitle_source_style_enabled') or assembly.get('subtitle_track', 'trans') == 'trans':
            cfg_snapshot['assemble'] = {k: v for k, v in assembly.items() if not k.startswith('subtitle_source_')}
    # 原文来源已固定；保留旧指纹中的固定标记，复用整理稿对应的有效产物。
    # 旧设置不再参与选择；基于识别原稿的旧产物仍会因标记/输入不同而过期。
    if not legacy and step_key in {"translate", "subtitles", "dubtask"}:
        cfg_snapshot["text.source"] = "split"
    # segmentation_mode 只决定 split 给 LLM 发送哪种时间轴，不改变 Whisper
    # 已生成的 words.json/src.srt；切换它不应触发昂贵的 transcribe 重跑。
    if step_key == "transcribe" and isinstance(cfg_snapshot.get("asr"), dict):
        cfg_snapshot["asr"].pop("segmentation_mode", None)
        if cfg_snapshot['asr'].get('input_mode', 'speech') == 'speech':
            cfg_snapshot['asr'].pop('input_mode', None)  # 默认模式不改变已有识别产物指纹。
        else:
            from dubforge.publishing.imports import selected
            try:
                cfg_snapshot['platform_subtitle'] = selected(deps.root)
            except DubForgeError:
                cfg_snapshot['platform_subtitle'] = 'missing'
    if step_key == "tts" and isinstance(cfg_snapshot.get("tts"), dict):
        cfg_snapshot["tts"].pop("subtitles", None)
        cfg_snapshot["tts"].pop("tolerance", None)
        for backend in ("indextts25", "gptsovits"):
            if isinstance(cfg_snapshot["tts"].get(backend), dict):
                cfg_snapshot["tts"][backend].pop("project_dir", None)
                cfg_snapshot["tts"][backend].pop("request_timeout_seconds", None)
        for key in ("emo_text", "emo_random"):
            if not cfg_snapshot["tts"].get("indextts25", {}).get(key):
                cfg_snapshot["tts"].get("indextts25", {}).pop(key, None)
        # 参考文件同路径替换也会改变声音；使用大小和修改时间识别外部更新。
        refs = {}
        if deps.cfg("tts.backend") == "indextts25":
            for part in ("timbre", "emo"):
                if deps.cfg(f"tts.indextts25.{part}_mode") == "preset":
                    path = Path(deps.cfg(f"tts.indextts25.{part}_audio") or "")
                    if path.is_file():
                        stat = path.stat()
                        refs[part] = [stat.st_size, stat.st_mtime_ns]
        cfg_snapshot["reference_files"] = refs
        from dubforge.pipeline.line_settings import overrides
        from dubforge.pipeline.step_tts import line_fingerprint
        # 仅有有效逐句覆盖时扩展指纹，保留升级前的普通句级缓存。
        customized = [line for line in deps.dubbing_lines() if overrides(project, line)]
        if customized:
            cfg_snapshot["line_settings"] = [line_fingerprint(project, line, dependencies=deps)
                                              for line in customized]
    payload = {
        "step": step_key,
        "cfg": cfg_snapshot,
        "upstream": sorted(f"{name}:{a.sha256 if a else 'missing'}" for name, a in arts.items()),
    }
    from dubforge.pipeline.subtitle_document import fingerprint as edit_fingerprint
    if edited := edit_fingerprint(project, step_key, deps.cfg):
        payload['subtitle_edits'] = edited
    if step_key == 'assemble':
        from dubforge.pipeline.dubbing_edit import production_lines
        payload['dubbing_edit'] = [{k: l[k] for k in ('id', 'source', 'take')}
                                   for l in production_lines(project, deps.dubbing_lines(), deps.root, dependencies=deps)]
    if step_key in {"split", "translate"}:
        from dubforge.pipeline.text_prompts import ASR_CORRECTION_VERSION
        payload["correction_prompt"] = ASR_CORRECTION_VERSION
    if step_key == "translate":
        from dubforge.pipeline.text_prompts import TRANSLATION_PROMPT_VERSION
        payload["translation_prompt"] = TRANSLATION_PROMPT_VERSION
    if step_key == "download":
        # 下载步骤没有上游产物，必须把链接/本地源纳入指纹；否则用户替换
        # source_url 或外部文件内容后，旧的 done 会永远被错误复用。
        source = {"kind": project.source_kind, "url": project.source_url}
        if project.source_kind == "local":
            if project.source_url:
                source_path = Path(project.source_url)
                if not source_path.is_absolute():
                    source_path = crud.project_dir(project.uid) / source_path
            else:
                # 早期项目只在 media/ 留了副本，没有 source_url；保留其
                # 可恢复性，同时仍用副本元数据参与指纹。
                from dubforge.pipeline.video_source import find_final_video
                source_path = find_final_video(crud.project_dir(project.uid) / "media")
            if source_path is None:
                source["missing"] = True
            else:
                try:
                    st = source_path.stat()
                    source.update({"size": st.st_size, "mtime_ns": st.st_mtime_ns})
                except OSError:
                    source["missing"] = True
        payload["source"] = source
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


class Runner:
    """全局单例: 受每步骤并行闸门约束的线程池 + 取消标志 + 心跳"""

    def __init__(self, max_workers: int = 8):
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="dubforge-step")
        self._cancels: dict[tuple[int, str], threading.Event] = {}
        self._pauses: dict[tuple[int, str], threading.Event] = {}
        self._ctxs: dict[tuple[int, str], StepContext] = {}
        self._current_jobs: dict[tuple[int, str], Job] = {}
        self._abandoned: set[tuple[int, str]] = set()   # 强制结束后等待后台收尾的步骤
        # 链内失败自动重试: (项目, 步骤) -> (取消事件, 已重试次数, 计数时间)
        self._retries: dict[tuple[int, str], tuple[threading.Event, int, float]] = {}
        self._held: dict[str, int] = {}          # 每槽位键 (步骤或资源组) 当前占用数
        self._slot_cond = threading.Condition()
        self._lock = threading.Lock()
        # 防止重复点击在“检查状态→置 queued→提交 Job”之间竞态。
        self._start_lock = threading.RLock()
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
        project_ids: set[int] = set()
        interrupted: dict[int, list[str]] = {}
        from dubforge.pipeline.registry import STEP_KEYS
        with crud.session() as s:
            for job in s.exec(sqlmodel.select(Job).where(Job.status == "running")).all():
                job.status = "dead"
                job.finished_at = now
                job.error_detail = "进程重启, 任务中断"
                s.add(job)
                jobs_fixed += 1
                project_ids.add(job.project_id)
            for step in s.exec(sqlmodel.select(StepRun).where(
                    StepRun.status == "running")).all():
                step.status = "failed"
                step.error_code = "STEP_INTERRUPTED"
                step.error_message = "上次执行被中断 (进程退出)"
                step.error_suggestion = "产物无半成品残留, 直接重跑即可; 已完成的下游会自动标记过期。"
                step.finished_at = now
                s.add(step)
                steps_fixed += 1
                project_ids.add(step.project_id)
                interrupted.setdefault(step.project_id, []).append(step.step_key)
            # queued = 还在线程池队列里没开跑 → 直接回待运行 (无半成品)
            for step in s.exec(sqlmodel.select(StepRun).where(
                    StepRun.status == "queued")).all():
                step.status = "pending"
                s.add(step)
                steps_fixed += 1
                project_ids.add(step.project_id)
                interrupted.setdefault(step.project_id, []).append(step.step_key)
            # 链不再随重启销毁: 保留计划并挂起 (运行中的链头本就在链内),
            # 用户可一键「继续剩余步骤」。无链项目 (单步运行) 不受影响。
            for project in s.exec(sqlmodel.select(Project)).all():
                params = crud.project_params(project)
                if '_chain' in params:
                    chain = [k for k in (params.get('_chain') or []) if k in STEP_KEYS]
                    if chain:
                        # 兼容旧数据 (链头启动前已出队): 中断且位于链头之前的步骤补回。
                        heads = sorted(
                            (k for k in interrupted.get(project.id, [])
                             if k in STEP_KEYS and k not in chain
                             and STEP_KEYS.index(k) < STEP_KEYS.index(chain[0])),
                            key=STEP_KEYS.index)
                        params['_chain'] = heads + chain
                        params['_chain_state'] = 'held'
                        params['_chain_hold'] = {
                            'step': (heads or chain)[0],
                            'code': 'STEP_INTERRUPTED',
                            'message': '上次执行被中断 (进程退出)',
                        }
                    else:
                        # 空链 = 计划已跑完, 清理登记即可。
                        params.pop('_chain', None)
                        params.pop('_chain_state', None)
                        params.pop('_chain_hold', None)
                    project.params = json.dumps(params, ensure_ascii=False)
                    s.add(project)
                    project_ids.add(project.id)
            s.commit()
        # 同步项目聚合状态与 manifest；否则重启后列表会继续显示处理中。
        for project_id in project_ids:
            project = crud.get_project_by_id(project_id)
            if project:
                if crud.project_dir(project.uid).is_dir():
                    crud.write_manifest(project.uid)
                self._refresh_project_status(project.uid)
        if steps_fixed or jobs_fixed:
                logger.warning("崩溃恢复: %d 个步骤判中断, %d 个 Job 判 dead",
                               steps_fixed, jobs_fixed)
        return {"steps_interrupted": steps_fixed, "jobs_dead": jobs_fixed}

    # ============ 启动步骤 ============

    @contextmanager
    def output_file_edit(self, uid):
        """成品回收与合成入队互斥；强制结束后的收尾同样需要保护。"""
        with self._start_lock:
            project = crud.get_project(uid)
            if not project:
                raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
            step = crud.get_step(project.id, 'assemble')
            if (self.current_job_id(project.id, 'assemble') is not None
                    or step and step.status in {'running', 'queued'}):
                raise DubForgeError('正在合成或等待合成收尾，暂不能回收成品', code='OUTPUT_FILE_BUSY')
            yield project

    def start_step(self, uid: str, step_key: str, force: bool = False, *,
                   resume_chain: bool = False) -> dict:
        from dubforge.pipeline import dubbing_session
        from dubforge import video_info
        with self._start_lock, dubbing_session.gate, video_info.gate:
            if step_key == 'download':
                video_info.check_idle(uid)
            dubbing_session.check_idle(uid)
            # 启动成功后才激活挂起链: 校验失败 (依赖未就绪/占用中) 时保留
            # 挂起标记与原因, chain/resume 入口不丢失。
            r = self._start_step(uid, step_key, force)
            if resume_chain:
                self._activate_chain(uid)
            return r

    def start_dubbing(self, uid: str, line_ids, *, takes=None, force=True) -> dict:
        from dubforge.pipeline import dubbing_session
        with self._start_lock, dubbing_session.gate:
            dubbing_session.check_idle(uid)
            return self._start_step(uid, 'tts', force, selection=(line_ids, takes))

    def start_assemble_preview(self, uid: str, start: float, end: float, *, include_comments=False, overlay_revision=None, output=None) -> dict:
        import math
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)):
            raise DubForgeError("试合成区域无效，请填写有限时间", code="ASSEMBLE_PREVIEW_RANGE")
        start, end = float(start), float(end)
        if start < 0 or end <= start or end - start < 0.2:
            raise DubForgeError("试合成区域需满足 0 ≤ 起点 < 终点，且至少 0.2 秒",
                                code="ASSEMBLE_PREVIEW_RANGE",
                                suggestion="请在视频预览中重新拖选一段区域。")
        task_data = {"scope": "preview", "start": round(start, 3), "end": round(end, 3)}
        if output is not None:
            from dubforge.pipeline.assembly_outputs import resolve_outputs, dimensions
            task_data['variants'] = resolve_outputs(outputs=[output], include_comments=include_comments)
            include_comments = dimensions(task_data['variants'][0])['include_comments']
        from dubforge.pipeline import dubbing_session
        from dubforge import comment_studio, video_info
        with self._start_lock, dubbing_session.gate, video_info.gate:
            dubbing_session.check_idle(uid)
            if plan := comment_studio.for_task(uid, include_comments, overlay_revision):
                task_data['comment_overlay'] = plan
            return self._start_step(uid, "assemble", False, task_data=task_data,
                                    clear_chain_reason="试合成区域")

    def start_assemble_outputs(self, uid: str, variants=None, *, include_comments=False, overlay_revision=None, outputs=None) -> dict:
        """按组合清单合成成片；同组合成功后替换，不续跑下游链。"""
        from dubforge.pipeline.assembly_outputs import resolve_outputs, dimensions
        ordered = resolve_outputs(variants, outputs=outputs, include_comments=include_comments)
        task_data = {"scope": "outputs", "variants": ordered, "output_schema": 2}
        from dubforge.pipeline import dubbing_session
        from dubforge import comment_studio, video_info
        with self._start_lock, dubbing_session.gate, video_info.gate:
            dubbing_session.check_idle(uid)
            if plan := comment_studio.for_task(uid, any(dimensions(v)['include_comments'] for v in ordered), overlay_revision, ordered):
                task_data['comment_overlay'] = plan
            return self._start_step(uid, "assemble", False, task_data=task_data,
                                    clear_chain_reason="手动合成勾选成品")

    def start_versions(self, uid, body, *, preview=False, frozen=None):
        """版本入口与旧三维入口隔离；预检、入队和渲染共用冻结版本。"""
        from dubforge.pipeline import assembly_versions as versions, dubbing_session
        from dubforge import video_info
        with self._start_lock, dubbing_session.gate, video_info.gate:
            dubbing_session.check_idle(uid)
            candidate = frozen or versions.plan(uid, body, allow_draft=preview)
            if candidate.get('blockers'):
                raise DubForgeError('版本依赖未就绪，请先准备所需步骤', code='DEPS_NOT_MET')
            if not preview and frozen is None and candidate['fingerprint'] != body.get('fingerprint'):
                raise versions.VersionError('版本效果或输入已变化，请重新预检', code='ASSEMBLY_VERSION_CONFLICT')
            task = dict(scope='preview' if preview else 'versions', version_specs=candidate['versions'])
            if preview:
                start, end = body.get('start'), body.get('end')
                if (type(start) not in (int, float) or type(end) not in (int, float)
                        or not all(math.isfinite(x) for x in (start, end)) or start < 0 or end - start < .2 or len(candidate['versions']) != 1):
                    raise versions.VersionError('请选择一个版本及有效的试片范围（至少0.2秒）')
                task.update(start=start, end=end)
            return self._start_step(uid, 'assemble', task_data=task, clear_chain_reason='运行明确选择的成片版本')

    def _start_step(self, uid: str, step_key: str, force: bool = False, *,
                    selection=None, task_data=None, clear_chain_reason=None) -> dict:
        from dubforge.workflows import check_idle
        check_idle(uid)
        if step_key not in STEP_MAP:
            raise DubForgeError(f"步骤不存在: {step_key}", code="STEP_NOT_FOUND",
                                suggestion=f"合法步骤: {', '.join(STEP_KEYS)}")
        step_def = STEP_MAP[step_key]
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        # 幽灵项目门禁: 目录不在磁盘上 (被移走/删除) 不允许启动 —— 启动只会
        # 失败并"补回"一个空目录骨架, 让项目列表失真。从回收站还原目录后即可恢复。
        pdir = crud.project_dir(uid)
        if not pdir.exists():
            raise DubForgeError(
                f"项目目录不在磁盘上: {pdir}", code="PROJECT_DIR_MISSING",
                suggestion="目录可能被移动或删除。从回收站还原后重试; 确认不要了可删除该项目清理登记。")
        step = crud.get_step(project.id, step_key)
        if not step:
            raise DubForgeError(f"步骤不存在: {step_key}", code="STEP_NOT_FOUND")
        k = (project.id, step_key)
        # 强制结束只会立即改变界面状态，线程中的同步计算/HTTP 请求仍需自然
        # 返回。旧任务未收尾前禁止同一 key 重跑，避免旧任务 finally 清理新任务
        # 的取消/暂停标志，或旧产物写回新一轮。
        with self._lock:
            finalizing = k in self._abandoned
        if finalizing:
            raise DubForgeError(
                "上一次强制结束仍在后台收尾，暂时不能重跑该步骤",
                code="STEP_FINALIZING",
                suggestion="请等待后台收尾完成后再运行；限流请求通常会很快结束。")
        if step.status in ("running", "queued"):
            raise DubForgeError("该步骤正在运行或排队中", code="STEP_RUNNING",
                                suggestion="请等待完成, 或先取消再重跑。")
        if any(s.status in {"running", "queued"} for s in crud.get_steps(project.id)):
            raise DubForgeError("本项目已有步骤正在运行，请完成后再运行另一步", code="PIPELINE_BUSY",
                                suggestion="同一项目依次执行；不同项目可并行处理。")

        # 按当前设置检查实际需要的文件，缺失或过期均在入队前说明。
        from dubforge.pipeline.dependencies import Dependencies
        task_data = task_data or {}
        if step_key == 'assemble' and not task_data:
            from dubforge.pipeline import assembly_versions
            from dubforge import comment_studio
            definitions = assembly_versions.catalog(uid)['versions']
            base = Dependencies(project)
            versions = [assembly_versions.resolve(base,v) for v in definitions]
            base.with_versions(versions).require(step_key)
            overlay, auto_comments = None, False
            if any(v['include_comments'] for v in definitions):
                try:
                    overlay = comment_studio.confirmed(uid)
                except DubForgeError as exc:
                    # 评论准备=auto 时允许合成步骤内自动采集 (comments_auto);
                    # 非自动模式或来源不支持采集时, 维持「先确认再合成」的拦截。
                    from dubforge import configuration, video_info as _vi
                    if (exc.code != 'COMMENTS_NOT_CONFIRMED'
                            or configuration.resolve(uid)['values'].get('comments.mode') != 'auto'
                            or not _vi._youtube(project.source_url)):
                        raise
                    auto_comments = True
            task_data = dict(scope='versions', version_specs=[assembly_versions.resolve(base,v,comment_plan=overlay) for v in definitions])
            if auto_comments:
                task_data['comments_auto'] = True
        trial_only = task_data.get("scope") == "preview"
        outputs_run = task_data.get("scope") == "outputs"
        versions_run = bool(task_data.get('version_specs'))
        deps = Dependencies(project, assemble_variants=task_data.get("variants") if outputs_run or trial_only else None)
        if versions_run:
            deps = deps.with_versions(task_data['version_specs'])
        if selection is not None:
            from dubforge.pipeline.dubbing_task import prepare
            task_data = prepare(project, deps, *selection)
            self._clear_chain(project, '手动合成所选句子')
        else:
            deps.require(step_key)
            if clear_chain_reason:
                self._clear_chain(project, clear_chain_reason)

        # 非 force 且已完成且指纹未变 → 跳过 (配置/上游都没变, 无需重算)
        fp = compute_fingerprint(project, step_key)
        if versions_run:
            from dubforge.pipeline import assembly_versions
            fp = 'versions:' + assembly_versions.digest([assembly_versions.fingerprint(deps, v) for v in task_data['version_specs']])
            if not force and not trial_only and step.status == 'done' and all(
                    assembly_versions.is_current(deps, v, verify_hash=True) for v in task_data['version_specs']):
                return {'status':'skipped','message':'所选版本的输入与成品均未变化，无需重跑'}
        if outputs_run:
            # 展示成品状态使用本次明确方案的输入；默认流程的跳过判断仍保持独立。
            task_data['production_fingerprint'] = compute_fingerprint(project, step_key, dependencies=deps)
            from dubforge.pipeline.assembly_outputs import default_variants
            if default_variants(deps.cfg)[0] not in task_data['variants']:
                # 仅生成其他方案不能让默认成片被误判为已完成、后续流程被跳过。
                fp = 'outputs:' + compute_fingerprint(project, step_key, dependencies=deps)
            if task_data.get('comment_overlay') and default_variants(deps.cfg)[0] not in task_data['variants']:
                # 评论版不能使不含评论的流程默认版被误判为最新。
                fp = 'comments:' + hashlib.sha256((fp + json.dumps(task_data['comment_overlay'], sort_keys=True)).encode()).hexdigest()
        if (selection is None and not trial_only and not outputs_run and not versions_run and not force
                and ((step_key != 'tts' and step.status == "done" and deps.matches_fingerprint(step_key))
                     or (step_key == "tts" and deps.fresh("tts")))
                and (step_key == 'tts' or crud.step_artifacts_exist(project.id, step_key, verify_hash=True))):
            return {"status": "skipped", "message": "输入未变化 (配置与上游产物一致), 无需重跑"}

        # 入队前保存重建语义。下面会先把行状态改成 queued；若只在
        # _execute() 里读取状态，failed/stale 会被 queued 覆盖，重试时可能
        # 错误复用旧产物。
        rebuild = bool(
            not trial_only
            and (force
                 or step.status in ("stale", "failed", "interrupted")
                 or (step.input_fingerprint and step.input_fingerprint != fp)))

        if force:
            # 配音保留已完成的句子；下载在校验新文件后替换，不能在入队前删旧登记。
            # 其他步骤沿用各自产物清理规则；下游已有结果置 stale。
            if step_key not in {'tts', 'download'}:
                crud.delete_step_artifacts(project.id, step_key)
            self._mark_downstream_stale(project.id, step_key)

        cancel = threading.Event()
        pause = threading.Event()
        # 上游依赖检查会复用局部变量 k，入队前恢复为当前步骤的索引键。
        k = (project.id, step_key)
        with self._lock:
            self._cancels[(project.id, step_key)] = cancel
            self._pauses[(project.id, step_key)] = pause

        # 提交后先显示排队，轮到它拿到对应步骤的并行额度时再置 running。
        step.status = "queued"
        step.progress = step.eta_seconds = 0.0
        step.error_code = step.error_message = step.error_suggestion = ""
        crud.save_step(step)

        log_file = crud.project_dir(uid) / "logs" / f"{step_key}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        job = crud.add_job(project.id, step_key, str(log_file), task_data=task_data)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n===== 运行开始 {datetime.datetime.now().astimezone().isoformat(timespec='seconds')} "
                    f"(任务#{job.id}, force={force}) =====\n")

        with self._lock:
            self._current_jobs[k] = job
        fut = self._pool.submit(self._execute, project, step_key, step_def, job,
                                log_file, cancel, pause, fp, force, rebuild)
        # future 异常必须被观测, 否则线程池静默吞错 (排查问题时是盲区)
        fut.add_done_callback(
                lambda f: f.exception() and logger.error(
                "步骤任务异常退出: %s", f.exception(), exc_info=f.exception()))
        result = {"status": "started", "job_id": job.id, "log": str(log_file)}
        if task_data.get("scope") == "preview":
            result.update({"start": task_data["start"], "end": task_data["end"]})
        elif task_data:
            result["lines"] = task_data.get("lines", [])
        return result

    # ============ 执行体 (线程池内) ============

    def _execute(self, project: Project, step_key: str, step_def, job: Job,
                 log_file: Path, cancel: threading.Event,
                 pause: threading.Event, fingerprint: str,
                 force: bool = False, rebuild: bool = False) -> None:
        from dubforge.pipeline.steps import get_runner
        pdir = crud.project_dir(project.uid)
        ctx = StepContext(project, step_key, pdir, log_file, cancel, pause,
                          force=force, job_id=job.id)
        ctx.task_data = json.loads(job.task_data)
        ctx.trial_only = ctx.task_data.get("scope") == "preview"
        ctx._job = job
        k = (project.id, step_key)

        # 取 DB 里的最新 step 行 (start_step 读到的可能是旧快照);
        # 拿到并发额度前状态保持 queued
        step = crud.get_step(project.id, step_key)
        with self._lock:
            self._active_jobs[job.id] = job
            self._ctxs[k] = ctx

        try:
            # 队列中已被取消 → 直接退出
            if cancel.is_set():
                raise StepCancelled("步骤在排队时被取消")
            # 并发闸门: 资源组/每步骤并行度 (GPU 组=1, 下载/LLM 类可多并行)
            slot_key = self.acquire_slot(step_key, cancel)
            try:
                if cancel.is_set():   # 等额度期间被取消 → 不执行
                    raise StepCancelled("步骤在排队时被取消")
                if step is None:
                    raise StepCancelled("项目已删除, 结果丢弃")
                ctx.rebuild = bool(rebuild or force
                                   or (step.input_fingerprint
                                       and step.input_fingerprint != fingerprint))
                self._begin_running(ctx, step, step_def, fingerprint)
                run_fn = get_runner(step_key)
                timeout_seconds = self.step_timeout_seconds(step_key, ctx)
                watchdog_stop = threading.Event()
                watchdog = None
                if timeout_seconds > 0:
                    ctx.log(f"⏱ 本步骤超时兜底: {timeout_seconds:g}s（不含排队等待和暂停时间）")
                    watchdog = threading.Thread(
                        target=self._watch_step_timeout,
                        args=(ctx, timeout_seconds, watchdog_stop),
                        daemon=True, name=f"dubforge-timeout-{step_key}")
                    watchdog.start()
                try:
                    run_fn(ctx)
                    # 看门狗可能恰好在步骤函数返回前触发; 再检查一次，避免
                    # 超时运行被误记成 done。
                    ctx.check_cancel()
                finally:
                    watchdog_stop.set()
                    if watchdog:
                        watchdog.join(timeout=1)
                    if step_key in {'preprocess', 'transcribe'}:
                        from dubforge.utils.gpu_memory import release_unused
                        release_unused(ctx.log)
            finally:
                self.release_slot(step_key, slot_key)
            # 强制结束过的运行: 结果一律丢弃, 保持 failed 呈现
            with self._lock:
                abandoned = k in self._abandoned
            if abandoned:
                raise StepCancelled("步骤已被强制结束, 本次运行结果按废弃处理")
            step = crud.get_step(project.id, step_key)
            if step is None:              # 项目/步骤行已被删除 (强制删除流程)
                raise StepCancelled("项目已删除, 结果丢弃")
            step.status = "done"
            step.progress = 100.0
            step.eta_seconds = 0.0
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
            job.status = "done"
            self._retries.pop(k, None)
            ctx.log("✅ 步骤完成")
            if ctx.task_data.get('scope') not in {'selected', 'preview', 'outputs', 'versions'}:
                self.continue_chain(project, ctx)
        except StepCancelled as e:
            with self._lock:
                abandoned = k in self._abandoned
            # 强制结束时 force_stop 已写好挂起信息; 普通取消/超时在此挂起链。
            if not abandoned:
                self._hold_chain(project, step_key, e.code,
                                 "步骤超时" if e.code == "STEP_TIMEOUT" else "用户取消")
            step = crud.get_step(project.id, step_key)
            # 项目可能已被删除 (强制删除流程): 只留日志, 不把行写回成幽灵
            if step is not None:
                # 强制结束时 DB 行已由 force_stop 写好 (含「已强制结束」文案), 不覆盖
                if not abandoned:
                    step.status = "failed"
                    step.error_code, step.error_message = e.code, e.message
                    step.error_suggestion = e.suggestion
                    step.finished_at = datetime.datetime.now(datetime.timezone.utc)
                    # 先清掉本步骤临时文件，再把失败状态对外可见；前端/测试
                    # 看到 failed 时，收尾清理已经完成，不会短暂暴露半成品。
                    _sweep_temp_files(pdir, step_key, log=ctx.log)
                    crud.save_step(step)
            job.status = "failed" if e.code == "STEP_TIMEOUT" else "cancelled"
            job.error_detail = e.message
            ctx.log(f"⏰ 已超时: {e.message}" if e.code == "STEP_TIMEOUT"
                    else f"⛔ 已取消: {e.message}")
        except DubForgeError as e:
            if self._schedule_chain_retry(project, step_key, e, ctx.log):
                # 已排定自动重试: Job 落终态, 步骤保持 failed 可见, 等待期不占槽位。
                job.status, job.error_detail = "failed", f"等待自动重试: {e.message}"
            else:
                self._retries.pop(k, None)
                self._hold_chain(project, step_key, e.code, e.message)
                step = crud.get_step(project.id, step_key)
                if step is not None:
                    step.status = "failed"
                    step.error_code, step.error_message = e.code, e.message
                    step.error_suggestion = e.suggestion
                    step.finished_at = datetime.datetime.now(datetime.timezone.utc)
                    _sweep_temp_files(pdir, step_key, log=ctx.log)
                    crud.save_step(step)
                job.status, job.error_detail = "failed", str(e.detail or e.message)
                ctx.log(f"❌ 失败 [{e.code}]: {e.message}")
        except Exception as e:  # 未知异常兜底: 不让线程静默死亡
            self._hold_chain(project, step_key, "UNEXPECTED_ERROR", f"未预期的错误: {type(e).__name__}")
            import traceback
            tb = traceback.format_exc()
            step = crud.get_step(project.id, step_key)
            if step is not None:
                step.status = "failed"
                step.error_code = "UNEXPECTED_ERROR"
                step.error_message = f"未预期的错误: {type(e).__name__}: {e}"
                step.error_suggestion = "请查看日志详情; 可直接重跑该步骤。"
                step.finished_at = datetime.datetime.now(datetime.timezone.utc)
                _sweep_temp_files(pdir, step_key, log=ctx.log)
                crud.save_step(step)
            job.status, job.error_detail = "failed", tb
            ctx.log(f"❌ 未预期异常:\n{tb}")
        finally:
            # 收尾兜底: 暂停标志必须清掉, 否则后续运行会在第一个暂停点卡死
            pause.clear()
            if step_key in {'preprocess', 'transcribe'} and job.status != 'done':
                # 异常捕获结束后，traceback 引用的模型/张量才可回收；再归还这部分缓存。
                from dubforge.utils.gpu_memory import release_unused
                release_unused(ctx.log)
            with self._lock:
                abandoned_at_cleanup = k in self._abandoned
            if abandoned_at_cleanup:
                # 强制结束后的 worker 可能已经越过了取消检查并写出正式
                # 产物；统一清掉本次步骤登记/文件，避免失败结果被下游复用。
                try:
                    if step_key == 'download':
                        ctx.log("保留已有完整源视频与资料登记；失败状态阻止下游误用，临时下载单独清理")
                    elif step_key != 'tts':
                        crud.delete_step_artifacts(project.id, step_key)
                        ctx.log("🗑 强制结束收尾：已移除本次步骤产物登记与可删除文件，避免失败结果被复用")
                    else:
                        ctx.log("保留逐句已完成的配音版本，未完整写入的版本由生成快照校验拦截")
                except Exception:
                    logger.exception("强制结束产物清理失败: %s", pdir)
            # 半成品清扫: 只扫本步骤目录 (项目级会误删续跑中下游的 .tmp)
            try:
                removed = _sweep_temp_files(pdir, step_key, log=ctx.log)
                kept = []
                for artifact in crud.latest_artifacts(project.id, [step_key]).values():
                    artifact_path = Path(artifact.rel_path)
                    if not artifact_path.is_absolute():
                        artifact_path = pdir / artifact_path
                    if artifact.step_key == step_key and artifact_path.exists():
                        kept.append(artifact.name)
                kept_desc = ", ".join(kept[:12]) if kept else "无已登记正式产物"
                removed_desc = ", ".join(Path(p).name for p in removed[:12]) if removed else "无"
                ctx.log(f"收尾清理: 保留正式产物 [{kept_desc}]；删除临时文件 [{removed_desc}]")
            except Exception:
                logger.exception("临时文件清扫失败: %s", pdir)
            job.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.update_job(job)
            with self._lock:
                self._active_jobs.pop(job.id, None)
                if self._current_jobs.get(k) is job:
                    self._current_jobs.pop(k, None)
                if self._cancels.get(k) is cancel:
                    self._cancels.pop(k, None)
                if self._pauses.get(k) is pause:
                    self._pauses.pop(k, None)
                if self._ctxs.get(k) is ctx:
                    self._ctxs.pop(k, None)
                # 只有这个任务仍是该步骤的当前任务时才清掉废弃标记。
                # 正常情况下强制结束期间不会启动新任务；身份判断让该
                # 约束即使未来放宽也不会产生“旧 finally 清新状态”的回归。
                if self._current_jobs.get(k) is None:
                    self._abandoned.discard(k)
            if (crud.get_project(project.uid) is not None
                    and crud.project_dir(project.uid).is_dir()):
                crud.write_manifest(project.uid)
                self._refresh_project_status(project.uid)

    @staticmethod
    def _refresh_project_status(uid: str) -> None:
        """项目状态 = 步骤聚合: 有 running→processing / 有 failed→failed / 全 done→done。
        只写状态列: 整行 merge 会与链计划写入并发, 用旧 params 覆盖挂起状态。"""
        project = crud.get_project(uid)
        if not project:
            return
        from dubforge.pipeline.dependencies import project_status
        new = project_status(project)
        if new != project.status:
            crud.update_project_status(project.id, new)

    # ============ 取消 ============

    def current_job_id(self, project_id: int, step_key: str) -> int | None:
        with self._lock:
            job = self._current_jobs.get((project_id, step_key))
        return job.id if job is not None else None

    def _check_expected_job(self, project_id: int, step_key: str,
                            expected_job_id: int | None) -> dict | None:
        if expected_job_id is None:
            return None
        current = self.current_job_id(project_id, step_key)
        step = crud.get_step(project_id, step_key)
        if (current != expected_job_id
                or step is None or step.status not in ("running", "queued")):
            return {"status": "stale_action",
                    "message": "该控制窗口对应的任务已结束或已换成新一轮运行，未执行操作",
                    "job_id": current}
        return None

    @staticmethod
    def _step_is_active(project_id: int, step_key: str) -> bool:
        step = crud.get_step(project_id, step_key)
        return bool(step and step.status in ("running", "queued"))

    def cancel_step(self, uid: str, step_key: str,
                    expected_job_id: int | None = None) -> dict:
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        stale = self._check_expected_job(project.id, step_key, expected_job_id)
        if stale:
            return stale
        with self._lock:
            pending_retry = self._retries.get((project.id, step_key))
        # 仅当步骤没有在跑的实际任务时才处理重试取消; 重试已启动的运行走正常取消。
        if pending_retry is not None and expected_job_id is None \
                and not self._step_is_active(project.id, step_key):
            # 失败后等待自动重试的步骤: 取消即撤掉重试计划, 保留失败状态。
            pending_retry[0].set()
            return {"status": "retry_cancelled",
                    "message": "已取消自动重试; 步骤保持失败状态, 可随时手动重跑"}
        if expected_job_id is None and not self._step_is_active(project.id, step_key):
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        with self._lock:
            ev = self._cancels.get((project.id, step_key))
        if ev is None:
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        if ev.is_set():
            # 停止是幂等控制：双击或网络重试不能产生第二个取消流程。
            return {"status": "already_cancelling", "message": "该步骤已经在取消中, 无需重复操作"}
        ev.set()
        with self._lock:
            ctx = self._ctxs.get((project.id, step_key))
        if ctx:
            ctx.log("⛔ 收到停止请求：取消优先于暂停，当前原子步骤结束后退出")
        return {"status": "cancelling", "message": "取消信号已发送, 等待步骤在下一个检查点退出"}

    def pause_step(self, uid: str, step_key: str, pause: bool = True,
                   expected_job_id: int | None = None) -> dict:
        """暂停/继续: 协作式 —— 步骤在循环间的暂停点阻塞 (GPU 计算/单次 LLM 调用
        进行中无法立即暂停, 会在当前单元结束后停下)"""
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        stale = self._check_expected_job(project.id, step_key, expected_job_id)
        if stale:
            return stale
        if expected_job_id is None and not self._step_is_active(project.id, step_key):
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        with self._lock:
            ev = self._pauses.get((project.id, step_key))
        if ev is None:
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        (ev.set if pause else ev.clear)()
        if pause:
            with self._lock:
                ctx = self._ctxs.get((project.id, step_key))
            if ctx:
                ctx.log("⏸ 已请求暂停：当前处理完成后暂停")
        paused = pause and self.pause_state(project.id, step_key) == "paused"
        return {"status": ("paused" if paused else "pausing") if pause else "resumed",
                "message": ("已暂停，等待继续" if paused else "已请求暂停，当前处理完成后暂停")
                if pause else "已继续执行"}

    def force_stop(self, uid: str, step_key: str,
                   expected_job_id: int | None = None) -> dict:
        """强制结束: 取消信号 + 立即终止本步骤的全部子进程 (ffmpeg/yt-dlp 即刻死),
        步骤行立刻按「已取消」呈现。进程内计算 (demucs/torch) 无法腰斩 —— 线程仍在
        后台跑完当前单元, 但结果会被丢弃 (abandoned), 不产生半成品。"""
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        k = (project.id, step_key)
        stale = self._check_expected_job(project.id, step_key, expected_job_id)
        if stale:
            return stale
        with self._lock:
            if k in self._abandoned:
                current = self._current_jobs.get(k)
                return {"status": "already_force_stopped",
                        "message": "该步骤已经强制结束，正在等待后台收尾",
                        "job_id": current.id if current is not None else None}
            ev = self._cancels.get(k)
            ctx = self._ctxs.get(k)
            job = self._current_jobs.get(k)
        if not self._step_is_active(project.id, step_key):
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        if ev is None and ctx is None and job is None:
            return {"status": "not_running", "message": "该步骤当前没有在运行的任务"}
        if ev:
            ev.set()
        with self._lock:
            pending_retry = self._retries.get(k)
        if pending_retry is not None:
            pending_retry[0].set()
        if ctx:
            ctx.kill_all_subprocesses()
            ctx.log("⛔ 收到强制结束: 已终止全部可终止子进程；正在等待的同步请求返回后将丢弃结果")
        with self._lock:
            self._abandoned.add(k)
        if job is not None:      # 就地改内存对象, 防心跳把状态写回 running
            job.status, job.error_detail = "cancelled", "强制结束"
        step = crud.get_step(project.id, step_key)
        if step and step.status in ("running", "queued"):
            step.status = "failed"
            step.error_code = "STEP_FORCE_STOPPED"
            step.error_message = "已强制结束"
            step.error_suggestion = ("子进程已即时终止; 进程内计算 (如音轨分离) 若仍在"
                                     "后台收尾, 结果会自动丢弃。可直接重跑。")
            step.finished_at = datetime.datetime.now(datetime.timezone.utc)
            crud.save_step(step)
        self._hold_chain(project, step_key, "STEP_FORCE_STOPPED", "强制结束")
        crud.write_manifest(uid)
        self._refresh_project_status(uid)
        return {"status": "force_stopped", "message": "已强制结束 (后台残余计算将自动丢弃结果)"}

    def is_cancelling(self, project_id: int, step_key: str) -> bool:
        """该步骤是否已发出停止信号且尚未退出 (前端显示「取消中…」)。
        注意判 is_set 而非"键存在": 取消事件在步骤启动时就注册进表了,
        按"存在"判断会把整个运行期都当成取消中。"""
        with self._lock:
            ev = self._cancels.get((project_id, step_key))
        return bool(ev and ev.is_set())

    def stop_reason(self, project_id: int, step_key: str) -> str | None:
        """停止原因与停止标志分别表达，不能把超时说成用户取消。"""
        with self._lock:
            key = (project_id, step_key)
            ctx, cancel = self._ctxs.get(key), self._cancels.get(key)
            if not cancel or not cancel.is_set():
                return None
            return 'timeout' if ctx and ctx._timeout.is_set() else 'user'

    def is_paused(self, project_id: int, step_key: str) -> bool:
        """兼容既有控制接口：是否发出了暂停请求，不代表已到暂停点。"""
        with self._lock:
            ev = self._pauses.get((project_id, step_key))
        return bool(ev and ev.is_set())

    def pause_state(self, project_id: int, step_key: str) -> str:
        """区分正在等待当前处理结束和已经在暂停点等待继续。"""
        with self._lock:
            key = (project_id, step_key)
            ev, ctx, cancel = self._pauses.get(key), self._ctxs.get(key), self._cancels.get(key)
        if not ev or not ev.is_set() or (cancel and cancel.is_set()):
            return "none"
        return "paused" if ctx and ctx._pause_started is not None else "requested"

    def progress_eta(self, project_id: int, step_key: str) -> dict:
        with self._lock:
            ctx = self._ctxs.get((project_id, step_key))
        if ctx and ctx._progress_provider:
            return {**ctx._progress_provider(), "eta_scope": ctx._eta_scope}
        phase, indeterminate = ctx._progress_phase if ctx else ("", False)
        return {"eta_seconds": ctx.eta_seconds() if ctx else 0.0,
                "eta_scope": ctx._eta_scope if ctx else "step",
                "progress_estimated": False,
                "progress_phase": phase, "progress_indeterminate": indeterminate,
                "elapsed_seconds": round(ctx.active_seconds(), 1) if ctx else 0.0}

    def _begin_running(self, ctx: StepContext, step: StepRun, step_def,
                       fingerprint: str) -> None:
        """拿到并发额度后: 置 running + 清错误 + 进度归零"""
        step.status = "running"
        step.started_at = datetime.datetime.now(datetime.timezone.utc)
        step.finished_at = None
        step.error_code = step.error_message = step.error_suggestion = ""
        if not ctx.trial_only:
            step.input_fingerprint = fingerprint
        step.progress = 0.0
        step.eta_seconds = 0.0
        ctx._run_started = time.monotonic()   # 排队等待不计入已耗时
        ctx._paused_seconds = 0.0
        crud.save_step(step)
        ctx.log(f"▶ 开始步骤 [{step_def.label}] ({step.step_key})")

    def _slot_key(self, step_key: str) -> str:
        """槽位键: 配置了资源组的步骤共享同组槽位 (如 preprocess/transcribe/tts
        同属 gpu, 同时只跑一个, 防止显存/算力争用); 未分组的步骤保持各自额度。"""
        groups = config.get("runner.step_resources", {}) or {}
        return str(groups.get(step_key, '') or step_key)

    def acquire_slot(self, step_key: str, cancel: threading.Event | None = None) -> str:
        """占用一个并行槽位 (每次获取都实时读配置 → 改并行数即时生效);
        额度满则阻塞等待, 期间可被取消 (排队等待不是黑洞 —— 取消信号一到就退出)。
        返回实际占用的槽位键 —— 释放时必须用它, 配置中途改动才不会泄漏槽位。"""
        key = self._slot_key(step_key)
        with self._slot_cond:
            while True:
                if cancel is not None and cancel.is_set():
                    raise StepCancelled("步骤在排队时被取消")
                held = self._held.get(key, 0)
                if held < self.concurrency_limit(step_key):
                    self._held[key] = held + 1
                    return key
                self._slot_cond.wait(0.5)

    def release_slot(self, step_key: str, key: str | None = None) -> None:
        # key = acquire 时返回的槽位键; 不传则按当前配置推导 (兼容旧调用)。
        key = key or self._slot_key(step_key)
        with self._slot_cond:
            self._held[key] = max(0, self._held.get(key, 0) - 1)
            self._slot_cond.notify_all()

    def concurrency_limit(self, step_key: str) -> int:
        groups = config.get("runner.step_resources", {}) or {}
        if groups.get(step_key):
            # 资源组步骤取组上限 (如 gpu:1), 组内天然互斥。
            rc = config.get("runner.resource_concurrency", {}) or {}
            return max(1, int(rc.get(groups[step_key], 1) or 1))
        conf = config.get("runner.step_concurrency", {}) or {}
        return max(1, int(conf.get(step_key, 1) or 1))

    def step_timeout_seconds(self, step_key: str, ctx: StepContext | None = None) -> float:
        """读取单步骤超时; 项目覆盖优先, 0 表示关闭。"""
        defaults = config.get("runner.step_timeout_seconds", {}) or {}
        default = defaults.get(step_key, 0)
        raw = (ctx.cfg(f"runner.step_timeout_seconds.{step_key}", default)
               if ctx is not None else default)
        try:
            return max(0.0, float(raw or 0))
        except (TypeError, ValueError):
            return max(0.0, float(default or 0))

    @staticmethod
    def _watch_step_timeout(ctx: StepContext, timeout_seconds: float,
                            stop: threading.Event) -> None:
        """按活动运行时间计时, 用户暂停时冻结计时。"""
        active = 0.0
        previous = time.monotonic()
        while not stop.wait(0.2):
            # 用户已经主动停止时，不要把后续的迟迟未返回误报成超时。
            if ctx._cancel.is_set() and not ctx._timeout.is_set():
                return
            now = time.monotonic()
            if not ctx.is_paused():
                active += now - previous
            previous = now
            if active >= timeout_seconds:
                ctx.request_timeout(timeout_seconds)
                return

    # ---- 自动续跑链 (params._chain: 剩余待自动执行的步骤) ----
    # 计划与活性分离: _chain 列表是「剩余计划」; _chain_state (active/held, 缺省
    # active) 是「是否自动续跑」。失败/取消/中断只挂起 (held) 不销毁计划, 由用户
    # 显式恢复 (chain/resume 或带 resume_chain 的重试); 只有明确放弃才清空。

    def continue_chain(self, project: Project, ctx: StepContext | None = None) -> dict:
        """步骤成功完成后调用: 链队列非空则启动下一个 (已最新则跳过继续)。
        链空了/启动失败(依赖未就绪等) → 清链停止。"""
        from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
        # 与参数保存共用编辑锁，并遵循启动锁在前的顺序。只回写最新项目，
        # 不能用步骤开始时的旧对象覆盖用户在长任务期间保存的资料。
        from dubforge.pipeline import dubbing_session
        with self._start_lock, dubbing_session.gate:
            return self._continue_chain(project.uid, ctx)

    def _continue_chain(self, uid: str, ctx: StepContext | None = None) -> dict:
        """链内步骤成功后才出队 (失败/挂起时它仍在链头, 计划始终完整)。"""
        from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
        done_key = ctx.step_key if ctx is not None else None
        log = ctx.log if ctx is not None else (lambda msg: None)
        while True:
            project = crud.get_project(uid)
            if project is None:
                return {"started": None, "stopped": None}
            params = crud.project_params(project)
            if params.get("_chain_state") == "held":
                # 挂起的链不自动续跑: 等用户显式恢复 (重试并继续 / 继续剩余步骤)。
                return {"started": None, "stopped": None}
            chain = params.get("_chain") or []
            if not chain:
                if "_chain" in params:
                    # 跑完的空链: 清理计划三键, 项目回到无链状态。
                    for key in ("_chain", "_chain_state", "_chain_hold"):
                        params.pop(key, None)
                    project.params = json.dumps(params, ensure_ascii=False)
                    crud.save_project(project)
                return {"started": None, "stopped": None}
            # 刚完成的链头步骤出队; 后续循环中链头是待启动步骤, 不再出队。
            if done_key is not None and chain[0] == done_key:
                params["_chain"] = chain[1:]
                project.params = json.dumps(params, ensure_ascii=False)
                crud.save_project(project)
                done_key = None
                continue
            head = chain[0]
            if head not in STEP_KEYS:
                params["_chain"] = chain[1:]
                project.params = json.dumps(params, ensure_ascii=False)
                crud.save_project(project)
                continue
            try:
                r = self.start_step(project.uid, head)
            except DubForgeError as e:
                self._hold_chain(project, head, e.code,
                                 f"「{STEP_MAP[head].label}」无法启动: {e.message}")
                log(f"⛓ 自动续跑挂起: 「{STEP_MAP[head].label}」{e.message}")
                return {"started": None, "stopped": STEP_MAP[head].label}
            if r.get("status") == "started":
                log(f"⛓ 自动续跑: 已启动「{STEP_MAP[head].label}」")
                return {"started": head, "stopped": None}
            # skipped (已是最新) → 出队继续下一个
            params["_chain"] = chain[1:]
            project.params = json.dumps(params, ensure_ascii=False)
            crud.save_project(project)

    def _schedule_chain_retry(self, project: Project, step_key: str,
                              exc: DubForgeError, log) -> bool:
        """链内步骤失败且还有重试额度: 排定延迟自动重试 (ADR-0076)。
        只对 active 链的链头生效; 用户取消/超时/未预期异常不重试。
        返回是否已排定。"""
        from dubforge.pipeline import dubbing_session
        with dubbing_session.gate:
            latest = crud.get_project(project.uid)
            if latest is None:
                return False
            params = crud.project_params(latest)
            chain = params.get("_chain") or []
            if params.get("_chain_state") == "held" or not chain or chain[0] != step_key:
                return False
        k = (project.id, step_key)
        conf = config.get("runner.step_retries", {}) or {}
        try:
            limit = max(0, int(conf.get(step_key, 0) or 0))
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        with self._lock:
            if k in self._abandoned:
                return False
            entry = self._retries.get(k)
            attempts = entry[1] if entry and time.monotonic() - entry[2] <= 600 else 0
            if attempts >= limit:
                self._retries.pop(k, None)
                return False
            cancel = threading.Event()
            self._retries[k] = (cancel, attempts + 1, time.monotonic())
        try:
            delay = max(1, int(config.get("runner.retry_delay", 15) or 15))
        except (TypeError, ValueError):
            delay = 15
        log(f"⏳ 将在 {delay} 秒后自动重试 (第 {attempts + 1}/{limit} 次): {exc.message}")
        step = crud.get_step(project.id, step_key)
        if step is not None:
            step.status = "failed"
            step.error_code, step.error_message = exc.code, exc.message
            step.error_suggestion = f"将自动重试 (第 {attempts + 1}/{limit} 次); 也可立即手动重跑, 或停止以保留失败状态。"
            crud.save_step(step)

        def fire():
            with self._lock:
                entry = self._retries.get(k)
                if entry is None or entry[0] is not cancel:
                    return
                # 计数条目保留: 下次失败据此累加, 直到额度用尽; 成功/挂起/取消时清理。
            if cancel.is_set():
                with self._lock:
                    self._retries.pop(k, None)
                log("⏹ 自动重试已取消")
                return
            # 链可能在等待期被放弃/替换/再次挂起: 重启前重验, 避免废弃计划复活。
            from dubforge.pipeline import dubbing_session
            with dubbing_session.gate:
                latest = crud.get_project(project.uid)
                params = crud.project_params(latest) if latest else {}
                chain = params.get("_chain") or []
                if params.get("_chain_state") == "held" or not chain or chain[0] != step_key:
                    with self._lock:
                        self._retries.pop(k, None)
                    return
            try:
                r = self.start_step(project.uid, step_key)
            except DubForgeError as start_err:
                with self._lock:
                    self._retries.pop(k, None)
                current = crud.get_step(project.id, step_key)
                if current is not None and current.status in ("running", "queued"):
                    return          # 用户已手动重跑, 不与手动操作竞争
                self._hold_chain(project, step_key, getattr(start_err, "code", "RETRY_START_FAIL"),
                                 f"自动重试未能启动: {start_err.message}")
                log(f"⛓ 自动续跑挂起: {start_err.message}")
                return
            if r.get("status") == "skipped":
                # 等待期被手动跑好: 出队并继续链, 不然链会静默卡住。
                self.continue_chain(project)
        timer = threading.Timer(delay, fire)
        timer.daemon = True
        timer.start()
        return True

    def _hold_chain(self, project: Project, step_key: str, code: str, message: str) -> None:
        """失败/取消/中断只挂起自动续跑计划, 不销毁: 失败步骤保留在链头,
        记录挂起原因, 供「重试并继续」/「继续剩余步骤」恢复。无链 (普通单步
        运行) 或失败的不是链头 (挂起期间的手动运行) 则不动计划。"""
        from dubforge.pipeline import dubbing_session
        with dubbing_session.gate:
            latest = crud.get_project(project.uid)
            if latest is None:
                return
            params = crud.project_params(latest)
            chain = params.get("_chain")
            if not isinstance(chain, list) or not chain or chain[0] != step_key:
                return
            params["_chain_state"] = "held"
            params["_chain_hold"] = {"step": step_key, "code": code, "message": message}
            latest.params = json.dumps(params, ensure_ascii=False)
            crud.save_project(latest)
            logger.info("自动续跑已挂起 (%s): 项目 %s", message, project.uid)

    def _activate_chain(self, uid: str) -> None:
        """挂起的链重新置为 active (调用方需已持有 _start_lock 与 gate)。"""
        project = crud.get_project(uid)
        if project is None:
            return
        params = crud.project_params(project)
        if (isinstance(params.get("_chain"), list) and params.get("_chain")
                and params.get("_chain_state") == "held"):
            params["_chain_state"] = "active"
            params.pop("_chain_hold", None)
            project.params = json.dumps(params, ensure_ascii=False)
            crud.save_project(project)

    def _clear_chain(self, project: Project, reason: str) -> None:
        from dubforge.pipeline import dubbing_session
        with dubbing_session.gate:
            latest = crud.get_project(project.uid)
            if latest is None:
                return
            params = crud.project_params(latest)
            if params.pop("_chain", None) is not None:
                params.pop("_chain_state", None)
                params.pop("_chain_hold", None)
                latest.params = json.dumps(params, ensure_ascii=False)
                crud.save_project(latest)
                logger.info("自动续跑已停止 (%s): 项目 %s", reason, project.uid)

    # ============ 心跳 ============

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_SEC):
            with self._lock:
                jobs = list(self._active_jobs.values())
            for job in jobs:
                job.heartbeat_at = datetime.datetime.now(datetime.timezone.utc)
                try:
                    crud.update_job(job, heartbeat_only=True)
                except Exception:
                    logger.exception("心跳更新失败 job=%s", job.id)

    # ============ 内部 ============

    def _mark_downstream_stale(self, project_id: int, step_key: str) -> None:
        produced = {a.step_key for a in crud.latest_artifacts(project_id, None).values()}
        from dubforge.pipeline.dependencies import Dependencies
        project = crud.get_project_by_id(project_id)
        for key in Dependencies(project).downstream(step_key):
            s = crud.get_step(project_id, key)
            if s is None:
                continue
            if (s.status not in ("running", "queued")
                    and (s.status in ("done", "stale") or key in produced)):
                # 没运行、没产物的步骤没有旧结果可失效，保留待运行/失败状态。
                s.status = "stale"
                s.error_code = s.error_suggestion = ""
                s.error_message = f"上游步骤 [{step_key}] 已重跑, 本步结果过期"
                s.progress = 0.0
                s.eta_seconds = 0.0
                crud.save_step(s)


runner = Runner(max_workers=max(2, int(config.get("runner.pool_size", 8) or 8)))
