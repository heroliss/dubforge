"""配音请求的服务占用记录：超时退出等待，不谎称已停止远端计算。"""
import hashlib
import json
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit

from dubforge import config
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes

_lock = threading.RLock()
_active = {}
RECOVERY_HINT = '服务端可能仍在生成。请等待本句结束；若持续不恢复，请在没有其他任务时停止并重新启动配音服务，再补齐未完成句子。'


class RequestTimeout(DubForgeError):
    code = 'TTS_REQUEST_TIMEOUT'
    suggestion = RECOVERY_HINT


class ServiceDraining(DubForgeError):
    code = 'TTS_SERVICE_DRAINING'
    suggestion = RECOVERY_HINT


def _path(url):
    try:
        parsed = urlsplit(url.rstrip('/'))
        host = parsed.hostname or ''
        if host in {'localhost', '127.0.0.1', '::1'}:
            host = 'loopback'
        # 不把服务凭据写入状态文件；同一服务的不同会话也不能重复排队。
        endpoint = urlunsplit((parsed.scheme, f'{host}:{parsed.port or (443 if parsed.scheme == "https" else 80)}',
                              parsed.path, '', ''))
    except ValueError:
        endpoint = url  # 错误 URL 的状态查询仍交由服务层返回原有中文校验提示。
    key = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
    return config.data_root() / 'logs' / f'tts-request-{key}.json'


def _read(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        return record if isinstance(record, dict) and isinstance(record.get('token'), str) and record['token'] else {'token': 'unknown'}
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        return {'token': 'unknown'}  # 记录损坏时不能冒险把未知的远端任务当空闲。


def needs_recovery(url):
    with _lock:
        path = _path(url)
        record = _read(path)
        return bool(record and (record.get('draining') or record.get('token') not in _active))


def reset(url):
    """仅在服务已确认停止或新进程已启动后调用；不提供盲目解除占用按钮。"""
    with _lock:
        _path(url).unlink(missing_ok=True)


class Request:
    def __init__(self, url):
        self.path = _path(url)
        self.token = uuid.uuid4().hex
        self.detached = False
        with _lock:
            if _read(self.path):
                raise ServiceDraining('配音服务仍有未结束或状态未知的请求，已阻止重复提交')
            self._write(False)
            _active[self.token] = self

    def _write(self, draining):
        atomic_write_bytes(self.path, json.dumps({'token': self.token, 'draining': draining}).encode())

    def finish(self):
        with _lock:
            record = _read(self.path)
            if record and record.get('token') == self.token:
                self.path.unlink(missing_ok=True)
            _active.pop(self.token, None)

    def detach(self, job, client, log):
        """保留连接观察真实结束；Gradio cancel() 不能保证停止同步模型计算。"""
        self.detached = True
        with _lock:
            record = _read(self.path)
            if record and record.get('token') == self.token:
                self._write(True)
        log('已停止等待本句；配音服务可能仍在收尾，期间不会继续提交新句。')

        def observe():
            try:
                # 后台只等待服务回执，不转换/登记迟到音频，不能覆盖正式结果。
                job.result()
            except Exception:
                # 断线或客户端取消并不证明远端已停止；持久记录留到服务重启。
                with _lock:
                    _active.pop(self.token, None)
            else:
                self.finish()
            finally:
                client.close()
        threading.Thread(target=observe, daemon=True, name='dubforge-tts-drain').start()
