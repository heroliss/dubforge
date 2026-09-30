"""可测任务使用实际工作量；LLM 用历史耗时和实际阶段事件显示预计进度。"""
import hashlib
import json
import math
import threading
import time
from statistics import mean

from dubforge import config
from dubforge.utils.fs import atomic_replace

LLM_DEFAULT_SECONDS = 600.0
HISTORY_LIMIT = 20
_history_lock = threading.Lock()


def middle_third_mean(values):
    """两端各去掉 floor(n/3) 条后取平均；一两条样本直接平均。"""
    values = sorted(v for v in values if math.isfinite(v) and v > 0)
    trim = len(values) // 3
    return mean(values[trim:len(values) - trim]) if values else None


def _clock(ctx):
    return getattr(ctx, "active_seconds", time.monotonic)


def _bind(ctx, provider, scope="step"):
    setter = getattr(ctx, "set_eta_provider", None)
    if setter:
        setter(provider, scope=scope)


class RateProgress:
    """剩余工作量 / 本次累计平均速度；缓存不算测速量，等待也计入时间。"""

    def __init__(self, ctx, total, *, start=0.0, end=99.0,
                 scope="step", eta_on_update=False):
        self.ctx, self.clock = ctx, _clock(ctx)
        self.total = max(0.0, float(total))
        self.start, self.end = start, end
        self.done = self.worked = 0.0
        self.started = self.clock()
        self.eta_on_update = eta_on_update
        self.eta_deadline = None
        self.lock = threading.RLock()
        _bind(ctx, self.remaining, scope)

    def update(self, completed, *, cached=False):
        with self.lock:
            completed = min(self.total, max(self.done, float(completed)))
            advanced = completed > self.done
            if not cached:
                self.worked += completed - self.done
            self.done = completed
            if self.eta_on_update and advanced:
                now = self.clock()
                elapsed = now - self.started
                self.eta_deadline = (now + (self.total - self.done) * elapsed / self.worked
                                     if self.worked > 0 and elapsed >= 2 else None)
            pct = self.start + (self.end - self.start) * self.done / max(self.total, 1e-9)
        self.ctx.report_progress(pct)

    def remaining(self):
        with self.lock:
            if self.done >= self.total or self.worked <= 0:
                return None
            if self.eta_on_update:
                # 分批返回的计算任务只在有新结果时测速；两批之间倒计时，
                # 不因完成量暂时不变而每次轮询都把 ETA 拉长。
                if self.eta_deadline is None:
                    return None
                return max(0.0, self.eta_deadline - self.clock()) or None
            elapsed = self.clock() - self.started
            if elapsed < 2.0:
                return None
            return (self.total - self.done) * elapsed / self.worked


def _history_path():
    return config.data_root() / "progress_timing.json"


def timing_history():
    """只保存数值、步骤和模型标签；不保存请求正文或连接密钥。"""
    try:
        data = json.loads(_history_path().read_text(encoding="utf-8"))
        result = {}
        for key, group in data.items() if isinstance(data, dict) else []:
            if not isinstance(group, dict) or not isinstance(group.get("samples"), list):
                continue
            rows = []
            for row in group["samples"][-HISTORY_LIMIT:]:
                try:
                    values = {k: float(row[k]) for k in ("work", "seconds", "ts")}
                    if all(math.isfinite(v) and v > 0 for v in values.values()):
                        rows.append(values)
                except (KeyError, TypeError, ValueError):
                    continue
            result[str(key)] = {"label": str(group.get("label", key)), "samples": rows}
        return result
    except (OSError, ValueError):
        return {}


def _save_sample(key, label, work, seconds):
    if not all(math.isfinite(v) and v > 0 for v in (work, seconds)):
        return
    with _history_lock:
        path = _history_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            data = timing_history()
            group = data.setdefault(key, {"label": label, "samples": []})
            group["samples"] = (group["samples"] + [
                {"ts": time.time(), "work": work, "seconds": seconds}])[-HISTORY_LIMIT:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            atomic_replace(tmp, path)
        except (OSError, ValueError, TypeError, KeyError):
            # 统计失败不影响正式产物。
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class LlmProgress:
    """预计进度：准备 1%、思考 60%、输出 38%、实际收尾 1%。"""

    def __init__(self, ctx, weights, *, step, profile, mode="", start=1.0, end=99.0):
        self.ctx, self.clock = ctx, _clock(ctx)
        self.weights = [max(1.0, float(w)) for w in weights]
        self.start, self.end = start, end
        profiles = profile if isinstance(profile, list) else [profile]
        self.expected_profile = profiles[0].get("name") if profiles and isinstance(profiles[0], dict) else None
        identity = [{k: p.get(k) for k in ("base_url", "model", "temperature", "params")}
                    if isinstance(p, dict) else p for p in profiles]
        spec = json.dumps(["v1", step, identity, mode], sort_keys=True, ensure_ascii=False)
        self.key = hashlib.sha256(spec.encode()).hexdigest()[:24]
        models = " → ".join(str(p.get("model", "")) if isinstance(p, dict) else str(p or "默认")
                            for p in profiles)
        self.label = f"{'语义整理' if step == 'split' else '翻译'} / {models} / {mode}"
        group = timing_history().get(self.key, {})
        self.samples = group.get("samples", []) if isinstance(group, dict) else []
        self.index = 0
        self.started = self.clock()
        self.run_started = self.started
        self.phase_started = self.started
        self.phase = "thinking"
        self.active = False
        self.last_progress = start
        self.predictions = []
        self.estimate_source = "默认10分钟"
        self.requests = set()
        self.retried = False
        self.lock = threading.RLock()
        _bind(ctx, self.remaining)
        setter = getattr(ctx, "set_progress_provider", None)
        if setter:
            setter(self.snapshot)

    def begin(self, index):
        with self.lock:
            self.index, self.started = index, self.clock()
            self.phase_started, self.phase, self.active = self.started, "thinking", True
            self.requests, self.retried = set(), False
            estimates = [self._estimate(w) for w in self.weights[index:]]
            self.predictions = [value if value is not None else
                                LLM_DEFAULT_SECONDS * w / sum(self.weights)
                                for value, w in zip(estimates, self.weights[index:])]
            self.estimate_source = ("历史截尾平均" if all(v is not None for v in estimates)
                                    else "默认10分钟" if all(v is None for v in estimates)
                                    else "历史与默认估算")
        self.ctx.report_progress(self.snapshot()["progress"])

    def _estimate(self, work):
        rates = []
        for sample in self.samples[-HISTORY_LIMIT:]:
            try:
                w, seconds = float(sample["work"]), float(sample["seconds"])
                if work / 2 <= w <= work * 2 and math.isfinite(seconds) and seconds > 0:
                    rates.append(seconds / w)
            except (TypeError, KeyError, ValueError):
                continue
        rate = middle_third_mean(rates)
        return rate * work if rate is not None else None

    def remaining(self):
        return self.snapshot()["eta_seconds"] or None

    @staticmethod
    def _phase_fraction(elapsed, budget):
        # 前 80% 按时间线性推进；超时后缓慢靠近阶段上限，不伪造下一阶段。
        ratio = max(0.0, elapsed) / max(budget, 0.001)
        return ratio if ratio <= 0.8 else 1 - 0.2 / (1 + (ratio - 0.8) / 0.2)

    def snapshot(self):
        """API 轮询实时计算；即使服务没有思考流回调，进度也会推进。"""
        with self.lock:
            now = self.clock()
            remaining, overdue = 0.0, False
            if self.active and self.predictions:
                estimate = self.predictions[0]
                # 准备和收尾由实际事件推进；可预测的请求时间按 60:38 分配。
                thinking, output = estimate * 60 / 98, estimate * 38 / 98
                elapsed = max(0.0, now - self.phase_started)
                budget = thinking if self.phase == "thinking" else output
                fraction = self._phase_fraction(elapsed, budget)
                phase_work = (60 * fraction if self.phase == "thinking"
                              else 60 + 38 * fraction) / 98
                done = sum(self.weights[:self.index]) + self.weights[self.index] * phase_work
                pct = self.start + (self.end - self.start) * done / sum(self.weights)
                self.last_progress = max(self.last_progress, min(self.end - 0.1, pct))
                overdue = elapsed >= budget
                if not overdue and not self.retried:
                    remaining = (budget - elapsed + sum(self.predictions[1:])
                                 + (output if self.phase == "thinking" else 0))
            return {
                "progress": round(self.last_progress, 1),
                "progress_estimated": True,
                "progress_phase": ("等待响应 / 深度思考" if self.phase == "thinking"
                                   else "模型输出" if self.phase == "output" else "校验与收尾"),
                "estimate_source": self.estimate_source,
                "estimate_overdue": overdue,
                "estimate_retrying": self.retried,
                "elapsed_seconds": round(max(0.0, now - self.run_started), 1),
                "eta_seconds": round(remaining, 1),
                "estimated_total_seconds": round(now - self.run_started + remaining, 1)
                if remaining > 0 else None,
            }

    def trace(self, event, content="", **meta):
        with self.lock:
            if event == "request":
                request_id = meta.get("request_id")
                if self.requests and request_id not in self.requests:
                    self.phase, self.phase_started = "thinking", self.clock()
                self.requests.add(request_id)
                if self.expected_profile and meta.get("profile") != self.expected_profile:
                    self.retried = True
            if event in {"validation_error", "retry"} or len(self.requests) > 1 or meta.get("retry_attempt", 1) > 1:
                self.retried = True
            if event in {"assistant_delta", "assistant_response"} and content and self.phase == "thinking":
                self.phase, self.phase_started = "output", self.clock()
        self.ctx.llm_trace(event, content, **meta)

    def finish(self, *, success=True):
        # 响应完成到采样之间也可能收到取消/超时，不能把已终止窗口记为正常完成。
        cancel_check = getattr(self.ctx, "check_cancel", None)
        if success and callable(cancel_check):
            cancel_check()
        with self.lock:
            seconds = self.clock() - self.started
            work = self.weights[self.index]
            record = success and len(self.requests) == 1 and not self.retried
            self.active, self.phase = False, "finalizing"
            self.predictions = []
            completed = sum(self.weights[:self.index + 1]) / max(sum(self.weights), 1.0)
            self.last_progress = max(self.last_progress, self.start + (self.end - self.start) * completed)
        # 缓存命中没有 request 事件；重试/降级/失败不污染正常 LLM 样本。
        if record:
            _save_sample(self.key, self.label, work, seconds)
            self.samples = (self.samples + [{"work": work, "seconds": seconds}])[-HISTORY_LIMIT:]
        self.ctx.report_progress(self.last_progress)
