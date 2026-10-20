"""评论 AI 的最新任务记录：评分与翻译分开保存，日志及对话不含连接凭据。"""
import copy
import json
import threading
import time
from datetime import datetime, timezone

from dubforge import config, llm_conversations
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.logs import read_llm_trace, read_tail


PHASE_LABELS = {
    'preparing': '准备评论批次', 'waiting': '等待模型响应', 'thinking': '模型思考',
    'output': '接收模型结果', 'validating': '校验并保存结果', 'retrying': '定向纠错与重试',
    'finalizing': '整理处理结果', 'done': '处理完成', 'partial': '部分完成',
    'failed': '处理失败', 'cancelled': '已停止', 'interrupted': '任务已中断',
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value or '')
        return result if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def started_at_key(job):
    """旧日志可能使用本地时区；比较真实时刻，不能按 ISO 字符串排序。"""
    return _timestamp(job.get('started_at')) or datetime.min.replace(tzinfo=timezone.utc)


def snapshot(job):
    """只投影短小状态，耗时在读取时计算；没有事件证据的旧任务不猜阶段。"""
    if not job:
        return None
    value = copy.deepcopy(job)
    now = datetime.now(timezone.utc)
    def stamp(key):
        return _timestamp(value.get(key))
    # 老终态只有开始时刻时无法知道运行多久，保留未知，不能显示为零秒。
    ended = stamp('finished_at') or stamp('updated_at')
    clock = now if value.get('status') == 'running' else ended
    for field, start_key in [('elapsed_seconds', 'started_at'),
                             ('request_elapsed_seconds', 'request_started_at'),
                             ('inactive_seconds', 'updated_at')]:
        start = stamp(start_key)
        value[field] = round(max(0.0, (clock - start).total_seconds()), 1) if clock and start else None
    return value


def paths(project, action):
    if action not in {'filter', 'translate'}:
        raise DubForgeError('未知的评论操作', code='COMMENTS_INVALID')
    root = crud.project_dir_by_project(project) / 'logs'
    return [root / f'comments_{action}.{suffix}' for suffix in ('log', 'llm.jsonl', 'status.json')]


def read_job(project, action):
    try:
        value = json.loads(paths(project, action)[2].read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def restored_job(project, action=None):
    jobs = [read_job(project, key) for key in ([action] if action else ['filter', 'translate'])]
    job = max((j for j in jobs if j), key=started_at_key, default=None)
    if job and job.get('status') == 'running':
        job.update(status='interrupted', cancelling=False, error='评论任务因服务重启中断，已保存的结果可在下次运行时复用。',
                   code='COMMENTS_INTERRUPTED', suggestion='重新检查并继续流程，或单独补充评分、翻译。',
                   phase='interrupted', phase_label=PHASE_LABELS['interrupted'])
    return snapshot(job)


class Diagnostics:
    def __init__(self, project, job):
        self.log_path, self.trace_path, self.status_path = paths(project, job['action'])
        self.lock = threading.RLock()
        self.job = job
        self.project_uid = project.uid
        self.requests = set()
        self.last_persisted = 0.0
        self.secrets = llm_conversations.collect_secrets(config.load_config().get('llm', {}))
        started = _utc_now()
        job.update(started_at=started, updated_at=started, finished_at=None,
                   phase='preparing', phase_label=PHASE_LABELS['preparing'], phase_at=started,
                   request=0, request_id='', request_started_at=None, attempt=0, attempts=0,
                   model='', profile='', output_chars=0, reasoning_chars=0, last_event='run_start',
                   batches=0, batch_size=0, diagnostics_warning='')
        # 初始记录原子替换；后续实时日志按完整行追加，读取端忽略尚未写完的半行。
        try:
            atomic_write_bytes(self.log_path, b'', create_parent=False)
            record = self.record('run_start', '', job_id=job['id'], action=job['action'])
            atomic_write_bytes(self.trace_path, (json.dumps(record, ensure_ascii=False) + '\n').encode(), create_parent=False)
        except OSError:
            self.unavailable()
        self.persist()

    def clean(self, value):
        return llm_conversations.redact(value, secrets=self.secrets)

    def unavailable(self):
        self.job['diagnostics_warning'] = '诊断文件写入失败，请检查项目日志目录权限和磁盘空间。'

    def record(self, event, content='', **meta):
        return self.clean(dict(ts=datetime.now().astimezone().isoformat(timespec='seconds'),
                               event=event, content=str(content or ''), **meta))

    def log(self, message):
        try:
            with self.lock, self.log_path.open('a', encoding='utf-8') as stream:
                stamp = datetime.now().astimezone().strftime('%H:%M:%S')
                stream.write(f'[{stamp}] {self.clean(str(message))}\n')
        except OSError:
            self.unavailable()

    def trace(self, event, content='', **meta):
        with self.lock:
            previous_phase = self.job['phase']
            self._project_event(event, content, meta)
            try:
                with self.trace_path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(self.record(event, content, **meta), ensure_ascii=False) + '\n')
            except OSError:
                self.unavailable()
            # 流式正文和思考只更新内存；阶段切换立即落盘，持续输出最多每秒一次。
            self.persist(force=previous_phase != self.job['phase'])

    def _phase(self, phase):
        if phase != self.job.get('phase'):
            self.job.update(phase=phase, phase_label=PHASE_LABELS[phase], phase_at=_utc_now())

    def _project_event(self, event, content, meta):
        self.job.update(last_event=event, updated_at=_utc_now())
        request_id = meta.get('request_id')
        if event == 'request':
            # 一个 HTTP 请求会分别记录 system/user 消息，只算一次且不重置首字计时。
            if request_id and request_id not in self.requests:
                self.requests.add(request_id)
                self.job.update(request=len(self.requests), request_id=str(request_id),
                                request_started_at=_utc_now(), output_chars=0, reasoning_chars=0)
                self._phase('waiting')
            for key in ('model', 'profile'):
                if isinstance(meta.get(key), str):
                    self.job[key] = self.clean(meta[key])[:160]
        for source, target in [('retry_attempt', 'attempt'), ('retry_total', 'attempts')]:
            if type(meta.get(source)) is int and meta[source] >= 0:
                self.job[target] = meta[source]
        if event == 'batch_start':
            self.job.update(request_started_at=None, output_chars=0, reasoning_chars=0, attempt=0, attempts=0)
            self._phase('preparing')
        elif event == 'reasoning_delta' and content:
            self.job['reasoning_chars'] += len(content)
            if self.job['phase'] != 'output':
                self._phase('thinking')
        elif event == 'assistant_delta' and content:
            self.job['output_chars'] += len(content)
            self._phase('output')
        elif event == 'assistant_response':
            self.job['output_chars'] = max(self.job['output_chars'], len(content or ''))
            self._phase('validating')
        elif event in {'response_metadata', 'validation_success'}:
            self._phase('validating')
        elif event in {'validation_error', 'retry', 'retry_strategy', 'provider_error'}:
            self._phase('retrying')
        elif event in {'recovery_stop', 'run_end'}:
            self._phase('finalizing')

    def snapshot(self):
        with self.lock:
            return snapshot(self.job)

    def persist(self, *, force=True):
        try:
            with self.lock:
                now = time.monotonic()
                if not force and now - self.last_persisted < 1.0:
                    return
                if self.job.get('status') != 'running':
                    self._phase(self.job['status'])
                    if not self.job.get('finished_at'):
                        self.job['finished_at'] = _utc_now()
                atomic_write_bytes(self.status_path, json.dumps(self.clean(copy.deepcopy(self.job)), ensure_ascii=False).encode(), create_parent=False)
                self.last_persisted = now
        except OSError:
            self.unavailable()


def read(project, action, *, current=None, after=0, trace_id=''):
    log_path, trace_path, _ = paths(project, action)
    result = read_llm_trace(trace_path, after=after, trace_id=trace_id, tail=300)
    try:
        lines, truncated = read_tail(log_path, 300)
    except OSError:
        lines, truncated = [], False
    job = snapshot(current) if current and current.get('action') == action else restored_job(project, action)
    return dict(**result, lines=lines, truncated=truncated, job=job)
