"""执行过程的有界诊断投影；不负责调度、结果判定或外部操作授权。"""
import copy
import datetime as dt
import math
import re
import threading
import time

HISTORY_LIMIT = 24
STATES = {'queued', 'running', 'waiting', 'retrying', 'pausing', 'paused', 'stopping',
          'completed', 'failed', 'interrupted', 'cancelled', 'skipped', 'attention', 'unknown'}
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled', 'skipped', 'attention', 'unknown', 'paused'}
METRICS = {'model', 'profile', 'batch_index', 'batch_total', 'current_id', 'cached', 'reused',
           'processed', 'failed', 'skipped', 'generated', 'repaired', 'output_chars', 'reasoning_chars',
           'request', 'input_chars', 'loaded_bytes', 'total_bytes', 'speed_bytes_per_second',
           'downloaded_bytes', 'elapsed_seconds', 'audio_seconds', 'duration', 'versions',
           'current_part', 'total_parts', 'resource', 'waiting_for', 'validation', 'attempt',
           'completed', 'total', 'unit', 'cached_lines', 'generated_lines', 'repair_lines'}
METRICS |= {'batch', 'batches', 'batch_size', 'chunk', 'chunks', 'current_chunk', 'total_chunks',
            'request_count', 'request_elapsed_seconds', 'current_title', 'completed_parts', 'line', 'active_line'}
METRICS |= {'duration_factor', 'device', 'source_lines', 'file', 'current_format', 'current_stream'}
METRICS |= {'sentence_progress', 'sentence_started_at', 'sentence_elapsed_seconds',
            'sentence_attempt', 'sentence_attempts'}


def _text(value, limit=400):
    value = str(value or '')
    value = re.sub(r'(?i)\b(?:sk-[\w-]+|ghp_[\w]+|[0-9a-f]{32}\.[A-Za-z0-9]{8})\b', '[已隐藏]', value)
    value = re.sub(r'(?i)(Bearer\s+)[^\s"\',;]+', r'\1[已隐藏]', value)
    value = re.sub(r'(https?://)[^/\s@]+@', r'\1[已隐藏]@', value)
    value = re.sub(r'(?i)([?&](?:api_key|token|key|secret)=)[^&\s]+', r'\1[已隐藏]', value)
    return value[:limit]


def _number(value):
    try:
        return float(value) if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None
    except (ValueError, OverflowError):
        return None


def _timestamp(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).timestamp()
    if type(value) in {int, float}:
        return _number(value)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(value):
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec='milliseconds')
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _metrics(value):
    if not isinstance(value, dict):
        return {}
    return {key: _text(item, 160) if isinstance(item, str) else item
            for key, item in value.items() if key in METRICS
            and (item is None or type(item) is bool or isinstance(item, str) or _number(item) is not None)}


def normalize(value=None, *, state=None, started_at=None, finished_at=None, now=None, **identity):
    """读取只计算耗时，不写库；父任务终态优先，历史阶段不冒充仍在执行。"""
    source = value if isinstance(value, dict) else {}
    aliases = {'done': 'completed', 'dead': 'interrupted', 'cancelled': 'cancelled'}
    parent = aliases.get(state, state) if isinstance(state, str) else None
    current = source.get('state') if isinstance(source.get('state'), str) and source['state'] in STATES else 'running'
    if parent in TERMINAL | {'queued', 'waiting', 'retrying', 'stopping', 'pausing'}:
        current = parent
    result = dict(schema=1, state=current, phase=_text(source.get('phase'), 100),
                  label=_text(source.get('label'), 160), detail=_text(source.get('detail')),
                  metrics=_metrics(source.get('metrics')))
    for name in ('completed', 'total', 'attempt', 'max_attempts', 'next_check_at'):
        result[name] = _number(source.get(name))
    for name in ('owner', 'job_id', 'run_id'):
        item = identity.get(name, source.get(name))
        if item is not None:
            result[name] = _text(item, 100) if isinstance(item, str) else item
    result['unit'] = _text(source.get('unit'), 30)
    start = _timestamp(source.get('started_at'))
    start = _timestamp(started_at) if start is None else start
    updated = _timestamp(source.get('updated_at'))
    updated = start if updated is None else updated
    end = _timestamp(finished_at)
    end = _timestamp(source.get('finished_at')) if end is None else end
    if current not in TERMINAL:
        end = None
    clock = time.time() if now is None else now
    if current in TERMINAL:
        clock = updated if end is None else end
    result.update(started_at=_iso(start) if start is not None else None,
                  updated_at=_iso(updated) if updated is not None else None,
                  finished_at=_iso(end) if end is not None else None,
                  elapsed_seconds=round(max(0, clock - start), 1) if clock is not None and start is not None else None,
                  inactive_seconds=round(max(0, clock - updated), 1) if clock is not None and updated is not None else None)
    # 单句等待使用服务端快照时钟，避免浏览器与后端时差；读取不刷新真实活动时间。
    sentence_start = _timestamp(result['metrics'].get('sentence_started_at'))
    if sentence_start is not None and clock is not None:
        result['metrics']['sentence_elapsed_seconds'] = round(max(0, clock - sentence_start), 1)
    else:
        result['metrics'].pop('sentence_elapsed_seconds', None)
    history = source.get('history')
    result['history'] = [dict(phase=_text(row.get('phase'), 100), label=_text(row.get('label'), 160),
        state=row.get('state') if isinstance(row.get('state'), str) and row['state'] in STATES else 'completed', detail=_text(row.get('detail')),
        started_at=_iso(_timestamp(row['started_at'])) if _timestamp(row.get('started_at')) is not None else None,
        finished_at=_iso(_timestamp(row['finished_at'])) if _timestamp(row.get('finished_at')) is not None else None)
        for row in (history[-HISTORY_LIMIT:] if isinstance(history, list) else []) if isinstance(row, dict)]
    return result


class Recorder:
    """阶段切换立即保存；流式活动与计量最多每秒一次，不持久化正文或思考。"""
    def __init__(self, emit=None, *, previous=None, clock=None, restart=False, **identity):
        self.emit, self.clock = emit, clock or time.time
        self.lock = threading.RLock()
        self.emit_lock = threading.Lock()
        self.data = normalize(previous, now=self.clock(), **identity)
        self.data['started_at'] = self.data['started_at'] or _iso(self.clock())
        if restart:
            # 明确继续属于新一轮执行，不能把两次尝试之间数小时的停顿计为本次耗时。
            # 旧阶段的起止时间仍保留在有界 history 中，不改写既有失败证据。
            restart_time = self.clock()
            history = self.data['history']
            if history and not history[-1].get('finished_at'):
                previous = history[-1]
                previous_state = self.data['state']
                if previous_state not in TERMINAL:
                    previous_state = previous['state'] if previous['state'] in TERMINAL else 'interrupted'
                start = _timestamp(previous.get('started_at'))
                updated = _timestamp(self.data.get('updated_at'))
                end = min(updated if updated is not None else start if start is not None else restart_time,
                          restart_time)
                # 系统时钟回退时，旧阶段最多收为零时长，不能生成结束早于开始的历史。
                end = max(start, end) if start is not None else end
                previous.update(state=previous_state, finished_at=_iso(end))
            stamp = _iso(restart_time)
            self.data.update(started_at=stamp, updated_at=stamp, finished_at=None)
        self.last_emit = float('-inf')
        self.requests = set()
        self.request_id = None

    def phase(self, code, label, detail='', state='running', **fields):
        return self.update(phase=code, label=label, detail=detail, state=state, **fields)

    def update(self, *, force=False, **fields):
        with self.lock:
            stamp = self.clock()
            code = _text(fields.get('phase', self.data.get('phase')), 100)
            state = fields.get('state', self.data.get('state', 'running'))
            state = state if isinstance(state, str) and state in STATES else 'running'
            changed = code != self.data.get('phase') or state != self.data.get('state')
            if code != self.data.get('phase') or (self.data['history'] and
                    self.data['history'][-1].get('finished_at') and state not in TERMINAL):
                if self.data['history'] and not self.data['history'][-1].get('finished_at'):
                    previous_state = self.data['history'][-1].get('state')
                    self.data['history'][-1].update(finished_at=_iso(stamp),
                        state=previous_state if previous_state in TERMINAL else 'completed')
                for name in ('completed', 'total', 'unit', 'next_check_at'):
                    self.data[name] = None
                self.data['metrics'] = {}
                self.data['history'].append(dict(phase=code, label=_text(fields.get('label'), 160),
                    detail=_text(fields.get('detail')), state=state, started_at=_iso(stamp), finished_at=None))
                self.data['history'] = self.data['history'][-HISTORY_LIMIT:]
            self.data.update({key: value for key, value in fields.items()
                              if key in {'phase', 'label', 'detail', 'completed', 'total', 'unit', 'attempt',
                                         'max_attempts', 'next_check_at', 'owner', 'job_id', 'run_id'}})
            if 'metrics' in fields:
                self.data['metrics'] = {**self.data.get('metrics', {}), **_metrics(fields['metrics'])}
            self.data.update(state=state, updated_at=_iso(stamp))
            if self.data['history']:
                self.data['history'][-1].update(state=state, label=_text(self.data.get('label'), 160),
                                              detail=_text(self.data.get('detail')))
            if state in TERMINAL:
                self.data['finished_at'] = _iso(stamp)
                if self.data['history']:
                    self.data['history'][-1].update(state=state, finished_at=_iso(stamp), detail=_text(self.data.get('detail')))
            else:
                self.data['finished_at'] = None
            snapshot = self.snapshot()
            should_emit = self.emit and (force or changed or state in TERMINAL or stamp - self.last_emit >= 1)
            if should_emit:
                self.last_emit = stamp
        # 数据库繁忙不能占用快照读取锁；并发回调按最新状态顺序落盘，
        # 等待写锁的旧阶段不能在新阶段之后覆盖回去。
        if should_emit:
            with self.emit_lock:
                snapshot = self.snapshot()
                try:
                    self.emit(copy.deepcopy(snapshot))
                except Exception:
                    # 诊断失败不能使正式产物或远端操作重复执行。
                    pass
        return snapshot

    def snapshot(self):
        with self.lock:
            return normalize(self.data, now=self.clock())

    def finish(self, state='completed', detail='', **fields):
        return self.update(state=state, detail=detail, force=True, **fields)

    def llm_trace(self, event, content='', **meta):
        """复用标准LLM事件，仅计数和固定文案；不复制提示词、正文或思考。"""
        with self.lock:
            metrics = dict(self.data.get('metrics') or {})
            for key in ('model', 'profile'):
                if meta.get(key):
                    metrics[key] = meta[key]
            attempt = meta.get('retry_attempt', meta.get('attempt', self.data.get('attempt')))
            maximum = meta.get('retry_total', meta.get('max_retries', self.data.get('max_attempts')))
            # 模型内部阶段仍属于当前分块/窗口，不能把外层真实工作量清空。
            args = dict(attempt=attempt, max_attempts=maximum,
                        completed=self.data.get('completed'), total=self.data.get('total'),
                        unit=self.data.get('unit'))
            request = meta.get('request_id')
            if event == 'request':
                if request and request == self.request_id:
                    return self.snapshot()
                self.request_id = request
                if request:
                    self.requests.add(request)
                metrics.update(request=len(self.requests), output_chars=0, reasoning_chars=0)
                phase, label, state = 'llm.waiting', '等待模型响应', 'waiting'
            elif event in {'reasoning_delta', 'assistant_delta', 'assistant_response'}:
                if not content:
                    return self.snapshot()
                thinking = event == 'reasoning_delta'
                key = 'reasoning_chars' if thinking else 'output_chars'
                metrics[key] = (max(metrics.get(key, 0), len(content)) if event == 'assistant_response'
                                else metrics.get(key, 0) + len(content))
                phase, label, state = ('llm.reasoning', '模型思考中', 'running') if thinking else ('llm.output', '接收模型输出', 'running')
            elif event in {'retry', 'provider_error', 'validation_error', 'json_repair'}:
                phase, label, state = 'llm.retry', '正在修正或重试模型请求', 'retrying'
            elif event in {'response_metadata', 'validation_success'}:
                phase, label, state = 'llm.validating', '校验模型结果', 'running'
            elif event in {'request_cancelled', 'request_aborted'}:
                phase, label, state = 'llm.stopping', '模型请求正在结束', 'stopping'
            else:
                return self.snapshot()
        return self.phase(phase, label, state=state, metrics=metrics, **args)


def report_stage(ctx, code, label, *, detail='', state='running', completed=None, total=None, unit=None, **metrics):
    """步骤协议的兼容入口；旧测试上下文也能按其已有能力报告阶段。"""
    reporter = getattr(ctx, 'report_execution', None)
    if callable(reporter):
        reporter(code, label, detail=detail, state=state, completed=completed, total=total, unit=unit, **metrics)
        return
    phase = getattr(ctx, 'set_progress_phase', None)
    if callable(phase):
        phase(label, indeterminate=completed is None or total is None)
    setter = getattr(ctx, 'set_progress_detail', None)
    if callable(setter):
        setter(dict(phase=code, label=label, detail=detail, state=state, completed=completed,
                    total=total, unit=unit, **metrics))
