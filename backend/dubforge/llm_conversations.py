"""统一的模型对话诊断：完整事件落盘，业务进度与正式产物保持独立。"""
import contextvars
import copy
import functools
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from dubforge import config
from dubforge.errors import DubForgeError

logger = logging.getLogger(__name__)
_scope = contextvars.ContextVar('dubforge_llm_scope', default=None)
_capture = contextvars.ContextVar('dubforge_llm_capture', default=None)
_gate = threading.RLock()
_active = {}
_recent = {}
_process = uuid.uuid4().hex
_CONTENT_CHUNK = 8192
_READ_BUDGET = 512 * 1024
_LINE_BUDGET = 128 * 1024
_WARNING = '模型对话记录未能完整保存，请检查数据目录权限和磁盘空间；正式任务不受影响。'
_SECRET_KEY = re.compile(r'[^a-z0-9]')
_CREDENTIAL = re.compile(r'(?i)\b(?:sk-[\w-]{4,}|ghp_[\w]{4,}|[0-9a-f]{32}\.[A-Za-z0-9]{8})\b')
_META = {'request_id', 'role', 'profile', 'model', 'retry_group', 'retry_attempt', 'retry_total',
         'input_chars', 'output_chars', 'received_chars', 'finish_reason', 'code', 'suggestion',
         'mode', 'server_confirmed', 'elapsed_ms', 'choice_count', 'selected_choice',
         'done_received', 'terminal_received', 'job_id', 'action', 'unit', 'batch', 'count'}
_COLUMNS = ('id', 'project_uid', 'operation', 'label', 'owner_id', 'status', 'started_at',
            'updated_at', 'model', 'warning', 'process')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _credential_key(key):
    label = _SECRET_KEY.sub('', str(key).lower())
    return label in {'key', 'token', 'passwd', 'cookie', 'cookies', 'csrf', 'bilijct', 'sessdata'} or label.endswith(
        ('apikey', 'apitoken', 'authorization', 'password', 'secret', 'accesstoken',
         'refreshtoken', 'bearertoken', 'securitytoken', 'privatekey', 'credentials'))


def collect_secrets(value):
    """只提取认证字段的精确值供内存脱敏，绝不返回或保存原始配置。"""
    secrets, seen = set(), set()

    def collect(item, depth=0):
        if depth > 20 or id(item) in seen:
            return
        if isinstance(item, (dict, list, tuple)):
            seen.add(id(item))
        if isinstance(item, dict):
            for key, child in item.items():
                if _credential_key(key) and isinstance(child, str) and child:
                    secrets.add(child)
                else:
                    collect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, depth + 1)

    collect(value)
    return tuple(sorted(secrets, key=len, reverse=True))


def redact(value, secrets=()):
    """递归清洗诊断内容；普通 max_tokens 等模型参数不会被当成认证字段。"""
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items() if not _credential_key(key)}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        value = value.replace(secret, '[已隐藏]')
    value = _CREDENTIAL.sub('[已隐藏]', value)
    value = re.sub(r'(?i)(Bearer\s+)[^\s"\',;]+', r'\1[已隐藏]', value)
    value = re.sub(r'(https?://)[^/\s@]+@', r'\1[已隐藏]@', value)
    value = re.sub(r'(?i)([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)[^&\s"\']+',
                   r'\1[已隐藏]', value)
    value = re.sub(r'(?i)((?:api[_-]?key|authorization|password|secret|access[_-]?token)\s*[=:]\s*)[^\s,;"\'}]+',
                   r'\1[已隐藏]', value)
    return value


@contextmanager
def context(project_uid='', operation='', label='', owner_id=''):
    """只绑定业务归属；真正调用模型时才创建会话，不产生空对话。"""
    metadata = dict(_scope.get() or {})
    metadata.update({key: str(value) for key, value in dict(project_uid=project_uid, operation=operation,
                    label=label, owner_id=owner_id).items() if value is not None and value != ''})
    token = _scope.set(metadata)
    try:
        yield
    finally:
        _scope.reset(token)


def _root():
    return config.data_root() / 'llm_conversations'


def _connect(root, *, create=False):
    if create:
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(root / 'index.sqlite3', timeout=.2)
    else:
        path = root / 'index.sqlite3'
        if not path.is_file():
            return None
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute('''CREATE TABLE IF NOT EXISTS conversation (
            id TEXT PRIMARY KEY, project_uid TEXT, operation TEXT, label TEXT, owner_id TEXT,
            status TEXT, started_at TEXT, updated_at TEXT, model TEXT, warning TEXT, process TEXT)''')
        connection.execute('CREATE INDEX IF NOT EXISTS conversation_project_time ON conversation(project_uid, started_at DESC)')
        connection.execute('CREATE INDEX IF NOT EXISTS conversation_time ON conversation(started_at DESC)')
    return connection


def _remember(root, metadata):
    with _gate:
        _recent[(str(root), metadata['id'])] = copy.deepcopy(metadata)
        while len(_recent) > 200:
            candidate = next((key for key in _recent if key not in _active), None)
            if candidate is None:
                break
            del _recent[candidate]


def _write_index(root, metadata):
    with _gate:
        connection = _connect(root, create=True)
        try:
            connection.execute('INSERT OR REPLACE INTO conversation (' + ','.join(_COLUMNS)
                               + ') VALUES (' + ','.join('?' for _ in _COLUMNS) + ')',
                               tuple(metadata[key] for key in _COLUMNS))
            connection.commit()
        finally:
            connection.close()


class _Capture:
    def __init__(self, fallback='unclassified'):
        self.lock = threading.RLock()
        self.root = _root()
        self.secrets = ()
        self.streams = {}
        self.last_persist = 0.
        try:
            self.secrets = collect_secrets(config.load_config().get('llm', {}))
        except Exception:
            pass
        now, metadata = _now(), redact(_scope.get() or {}, self.secrets)
        self.data = dict(id=uuid.uuid4().hex, project_uid=metadata.get('project_uid', '')[:160],
            operation=metadata.get('operation', fallback)[:100],
            label=metadata.get('label') or ('模型连接测试' if fallback == 'provider_test' else '未归类模型调用'),
            owner_id=metadata.get('owner_id', '')[:160], status='running', started_at=now, updated_at=now,
            model='', warning='', process=_process)
        self.data['label'] = self.data['label'][:200]
        self.path = self.root / (self.data['id'] + '.jsonl')
        with _gate:
            _active[(str(self.root), self.data['id'])] = self
        try:
            self.emit('run_start')
        except Exception:
            self.warn()

    def warn(self):
        if not self.data['warning']:
            logger.warning(_WARNING)
        self.data['warning'] = _WARNING
        _remember(self.root, self.data)

    def protect(self, provider):
        self.secrets = tuple(sorted(set(self.secrets) | set(collect_secrets(provider)), key=len, reverse=True))

    def persist(self, force=False):
        _remember(self.root, self.data)
        now = time.monotonic()
        if not force and now - self.last_persist < 1:
            return
        try:
            _write_index(self.root, self.data)
            self.last_persist = now
        except Exception:
            self.warn()

    def _write(self, event, content='', **meta):
        safe = redact(dict(ts=_now(), event=str(event)[:100], content=str(content or ''),
                           **{key: value for key, value in meta.items() if key in _META}), self.secrets)
        for key in set(safe) - {'content'}:
            if isinstance(safe[key], str):
                safe[key] = safe[key][:1600]
            elif not isinstance(safe[key], (bool, int, float, type(None))):
                safe[key] = str(safe[key])[:400]
            elif isinstance(safe[key], float) and not math.isfinite(safe[key]):
                safe[key] = None
        text = safe.pop('content')
        count = max(1, (len(text) + _CONTENT_CHUNK - 1) // _CONTENT_CHUNK)
        message_id = uuid.uuid4().hex[:16] if count > 1 else ''
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8', newline='\n') as stream:
                for index in range(count):
                    record = dict(safe, content=text[index * _CONTENT_CHUNK:(index + 1) * _CONTENT_CHUNK])
                    if count > 1:
                        record.update(message_id=message_id, content_part=index, content_parts=count, continuation=index > 0)
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
        except Exception:
            self.warn()

    def _flush(self, request_id=None):
        for key in list(self.streams):
            if request_id is not None and key[0] != request_id:
                continue
            text, meta = self.streams.pop(key)
            if text:
                self._write(key[1], text, **meta)

    def emit(self, event, content='', **meta):
        """流式末尾短暂缓冲，防止已知密钥横跨两个增量记录而漏过脱敏。"""
        with self.lock:
            self.data['updated_at'] = _now()
            if meta.get('model'):
                self.data['model'] = redact(str(meta['model']), self.secrets)[:200]
            request = str(meta.get('request_id') or '')
            if event in {'assistant_delta', 'reasoning_delta'}:
                key = (request, event)
                old, _ = self.streams.get(key, ('', {}))
                combined = redact(old + str(content or ''), self.secrets)
                hold = max(256, max(map(len, self.secrets), default=0))
                if len(combined) > hold:
                    self._write(event, combined[:-hold], **meta)
                    combined = combined[-hold:]
                self.streams[key] = (combined, meta)
            else:
                if event in {'request', 'response_metadata', 'assistant_response', 'request_cancelled',
                             'request_aborted', 'provider_error', 'error', 'run_end'}:
                    self._flush(request or None)
                self._write(event, content, **meta)
            self.persist(force=event in {'run_start', 'request', 'error', 'run_end'})

    def finish(self, status='completed', error=None):
        try:
            with self.lock:
                if error is not None:
                    self.emit('error', getattr(error, 'message', str(error)), code=getattr(error, 'code', ''))
                self._flush()
                self.data.update(status=status, updated_at=_now())
                self.emit('run_end', {'completed': '模型调用完成', 'cancelled': '模型调用已停止',
                                      'interrupted': '模型调用已中断'}.get(status, '模型调用失败'))
                self.persist(force=True)
        finally:
            with _gate:
                _active.pop((str(self.root), self.data['id']), None)


def trace(event, content='', **meta):
    """给无 trace 参数的内部入口（连接测试）复用当前统一记录。"""
    current = _capture.get()
    if current is not None:
        try:
            current.emit(event, content, **meta)
        except Exception:
            current.warn()


def protect(provider):
    """在发送请求前加入本轮实际配置中的精确密钥，不向诊断传递配置。"""
    current = _capture.get()
    if current is not None:
        try:
            current.protect(provider)
        except Exception:
            current.warn()


def capture(function):
    """公共 LLM 入口自动记录，嵌套调用与校验重试共用一个会话。"""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if _capture.get() is not None:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        try:
            current = _Capture('provider_test' if function.__name__ == 'test_provider' else 'unclassified')
        except Exception:
            logger.warning(_WARNING)
            return function(*args, **kwargs)
        token = _capture.set(current)
        if 'trace' in signature.parameters:
            previous = bound.arguments.get('trace')

            def tee(event, content='', **meta):
                trace(event, content, **meta)
                if previous and previous is not trace:
                    # 旧进度消费者保留原有事件节奏；清洗只改变诊断，不改变模型业务返回值。
                    try:
                        safe = redact(dict(content=content, **meta), current.secrets)
                    except Exception:
                        current.warn()
                        safe = {'content': '[该条诊断内容无法安全显示]'}
                    try:
                        previous(event, safe.pop('content', ''), **safe)
                    except (OSError, sqlite3.Error):
                        # 兼容旧的文件诊断消费者；业务取消等领域异常仍原样传播。
                        current.warn()

            bound.arguments['trace'] = tee
        try:
            result = function(*bound.args, **bound.kwargs)
        except BaseException as error:
            code = getattr(error, 'code', '')
            status = ('cancelled' if any(word in code for word in ('CANCELLED', 'PAUSED', 'STOPPED'))
                      else 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) or 'INTERRUPTED' in code else 'failed')
            try:
                current.finish(status, error)
            except Exception:
                logger.warning(_WARNING)
            raise
        else:
            try:
                failed = isinstance(result, dict) and result.get('ok') is False
                if failed:
                    current.emit('error', str(result.get('error') or '模型连接测试失败'))
                current.finish('failed' if failed else 'completed')
            except Exception:
                logger.warning(_WARNING)
            return result
        finally:
            _capture.reset(token)

    return wrapped


def _public(root, row):
    value = dict(row)
    with _gate:
        active = (str(root), value['id']) in _active
    if value['status'] == 'running' and not active:
        value.update(status='interrupted', warning=value.get('warning') or '上次模型调用随程序中断；此前已记录的内容仍可查看。')
    value.pop('process', None)
    value.update(active=active, project_title='')
    return value


def _titles(root, rows):
    ids = sorted({row['project_uid'] for row in rows if row.get('project_uid')})
    database = root.parent / config.DB_FILE_NAME
    if not ids or not database.is_file():
        return
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
        try:
            titles = dict(connection.execute('SELECT uid,title FROM project WHERE uid IN ('
                                            + ','.join('?' for _ in ids) + ')', ids))
        finally:
            connection.close()
        for row in rows:
            row['project_title'] = titles.get(row['project_uid'], '')
    except sqlite3.Error:
        pass


def list_conversations(*, project_uid='', operation='', owner_id='', limit=30, before=''):
    root, clauses, values, rows = _root(), [], [], []
    for key, value in (('project_uid', project_uid), ('operation', operation), ('owner_id', owner_id)):
        if value:
            clauses.append(key + '=?')
            values.append(value)
    if before:
        stamp, _, identity = before.partition('|')
        clauses.append('(started_at < ? OR (started_at = ? AND id < ?))')
        values.extend((stamp, stamp, identity))
    warning = ''
    try:
        connection = _connect(root)
        if connection:
            try:
                rows = [dict(row) for row in connection.execute('SELECT ' + ','.join(_COLUMNS) + ' FROM conversation'
                    + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
                    + ' ORDER BY started_at DESC,id DESC LIMIT ?', (*values, limit + 1))]
            finally:
                connection.close()
    except Exception:
        warning = _WARNING
    # 索引失败或写入节流期间，仍显示当前进程已经掌握的最新诊断元信息。
    indexed = {row['id']: row for row in rows}
    with _gate:
        local = [copy.deepcopy(row) for (path, _), row in _recent.items() if path == str(root)]
    for row in local:
        if (project_uid and row['project_uid'] != project_uid or operation and row['operation'] != operation
                or owner_id and row['owner_id'] != owner_id or before and (row['started_at'] + '|' + row['id']) >= before):
            continue
        indexed[row['id']] = row
    ordered = sorted(indexed.values(), key=lambda row: (row['started_at'], row['id']), reverse=True)
    more = len(ordered) > limit
    result = [_public(root, row) for row in ordered[:limit]]
    _titles(root, result)
    return dict(items=result, more=more, next_cursor=(result[-1]['started_at'] + '|' + result[-1]['id']) if more else '', warning=warning)


def read_conversation(identity, *, after=0, trace_id='', limit=200):
    if not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise DubForgeError('模型对话记录不存在', code='LLM_CONVERSATION_NOT_FOUND')
    root, row = _root(), None
    try:
        connection = _connect(root)
        if connection:
            try:
                found = connection.execute('SELECT ' + ','.join(_COLUMNS) + ' FROM conversation WHERE id=?', (identity,)).fetchone()
                row = dict(found) if found else None
            finally:
                connection.close()
    except Exception:
        pass
    with _gate:
        row = copy.deepcopy(_recent.get((str(root), identity), row))
    if row is None:
        raise DubForgeError('模型对话记录不存在或索引暂不可读取', code='LLM_CONVERSATION_NOT_FOUND')
    result = _public(root, row)
    _titles(root, [result])
    events, cursor, consumed, more, reset = [], after, 0, False, bool(trace_id and trace_id != identity)
    try:
        with (root / (identity + '.jsonl')).open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if reset or after > size:
                cursor, reset = 0, True
            stream.seek(cursor)
            for _ in range(limit):
                position = stream.tell()
                line = stream.readline(_LINE_BUDGET + 1)
                if not line:
                    break
                if not line.endswith(b'\n') or len(line) > _LINE_BUDGET:
                    break
                # 给元信息和 JSON 数组结构预留空间，整个 API 响应也保持有界。
                if consumed + len(line) > _READ_BUDGET - 8192:
                    stream.seek(position)
                    break
                consumed += len(line)
                cursor = stream.tell()
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except (ValueError, UnicodeError):
                    continue
            more = cursor < size
    except OSError:
        result['warning'] = _WARNING
    return dict(result, events=events, cursor=cursor, trace_id=identity, more=more, reset=reset)
