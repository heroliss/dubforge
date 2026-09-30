"""评论 AI 的最新任务记录：评分与翻译分开保存，日志及对话不含连接凭据。"""
import copy
import json
import re
import threading
from datetime import datetime, timezone

from dubforge import config
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.logs import read_llm_trace, read_tail


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
    job = max((j for j in jobs if j), key=lambda j: j.get('started_at', ''), default=None)
    if job and job.get('status') == 'running':
        job.update(status='interrupted', cancelling=False, error='评论任务因服务重启中断，已保存的结果可在下次运行时复用。',
                   code='COMMENTS_INTERRUPTED', suggestion='重新检查并继续流程，或单独补充评分、翻译。')
    return job


class Diagnostics:
    def __init__(self, project, job):
        self.log_path, self.trace_path, self.status_path = paths(project, job['action'])
        self.lock = threading.RLock()
        self.job = job
        self.secrets = []
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if any(s in str(key).lower() for s in ('api_key', 'secret', 'password', 'token')) and isinstance(item, str) and item:
                        self.secrets.append(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(config.load_config().get('llm', {}))
        job['started_at'] = datetime.now(timezone.utc).isoformat()
        self.job['diagnostics_warning'] = ''
        # 初始记录原子替换；后续实时日志按完整行追加，读取端忽略尚未写完的半行。
        try:
            atomic_write_bytes(self.log_path, b'', create_parent=False)
            record = self.record('run_start', '', job_id=job['id'], action=job['action'])
            atomic_write_bytes(self.trace_path, (json.dumps(record, ensure_ascii=False) + '\n').encode(), create_parent=False)
        except OSError:
            self.unavailable()
        self.persist()

    def clean(self, value):
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items() if not any(s in str(k).lower() for s in ('api_key', 'secret', 'password', 'token'))}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, '[已隐藏]')
        value = re.sub(r'(?i)\b(?:sk-[\w-]+|ghp_[\w]+|[0-9a-f]{32}\.[A-Za-z0-9]{8})\b', '[已隐藏]', value)
        value = re.sub(r'(?i)(Bearer\s+)[^\s"\',;]+', r'\1[已隐藏]', value)
        value = re.sub(r'(https?://)[^/\s@]+@', r'\1[已隐藏]@', value)
        value = re.sub(r'(?i)([?&](?:api_key|token|key|secret)=)[^&\s]+', r'\1[已隐藏]', value)
        return value

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
        try:
            with self.lock, self.trace_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(self.record(event, content, **meta), ensure_ascii=False) + '\n')
        except OSError:
            self.unavailable()

    def persist(self):
        try:
            with self.lock:
                atomic_write_bytes(self.status_path, json.dumps(self.clean(copy.deepcopy(self.job)), ensure_ascii=False).encode(), create_parent=False)
        except OSError:
            self.unavailable()


def read(project, action, *, current=None, after=0, trace_id=''):
    log_path, trace_path, _ = paths(project, action)
    result = read_llm_trace(trace_path, after=after, trace_id=trace_id, tail=300)
    try:
        lines, truncated = read_tail(log_path, 300)
    except OSError:
        lines, truncated = [], False
    job = copy.deepcopy(current) if current and current.get('action') == action else restored_job(project, action)
    return dict(**result, lines=lines, truncated=truncated, job=job)
