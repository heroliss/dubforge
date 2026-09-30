"""有界 JSONL 工作进程；凭据只经过匿名管道，不进入命令行或日志。"""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from dubforge import config
from dubforge.errors import DubForgeError

WORKER = config.REPO_ROOT / 'workers/bilibili/worker.mjs'


class TransportError(DubForgeError):
    code = 'PUBLISH_TRANSPORT'
    suggestion = '请检查网络和登录状态；遇到验证或风控请前往B站网页处理。'


def _error_detail(value):
    """仅允许有界、非敏感的合集诊断字段经过工作进程边界。"""
    if not isinstance(value, dict) or value.get('stage') not in ('auth', 'input', 'cover', 'create', 'receipt',
            'join_auth', 'join_season', 'join_section', 'join_input', 'join_add', 'join_verify'):
        return None
    result = {'stage': value['stage']}
    for key in ('remote_code', 'http_status'):
        if type(value.get(key)) is int and abs(value[key]) < 2**31:
            result[key] = value[key]
    if value.get('network_code') in ('ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED', 'ENOTFOUND', 'EAI_AGAIN',
            'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'DEPTH_ZERO_SELF_SIGNED_CERT',
            'ERR_TLS_CERT_ALTNAME_INVALID', 'SELF_SIGNED_CERT_IN_CHAIN'):
        result['network_code'] = value['network_code']
    return result


def available():
    return bool(shutil.which('node') and (WORKER.parent / 'node_modules/@renmu/bili-api').is_dir())


def call(operation: str, payload=None, *, credential=None, on_event=None,
         cancel=None, timeout=180):
    if not available():
        raise TransportError('发布组件未安装，请在 workers/bilibili 中运行 npm ci --ignore-scripts',
                             code='PUBLISH_WORKER_MISSING')
    # stderr 可能包含第三方请求头，禁止写进日志或回传页面。
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NODE_OPTIONS',
                                          'NODE_TLS_REJECT_UNAUTHORIZED'}}
    process = subprocess.Popen([shutil.which('node'), str(WORKER)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding='utf-8', env=environment,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    expired = threading.Event()
    stopped = threading.Event()

    def watch():
        import time
        deadline = time.monotonic() + timeout
        while not stopped.wait(.2):
            if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                expired.set()
                process.kill()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        process.stdin.write(json.dumps({'operation': operation, 'payload': payload or {},
                                       'credential': credential}, ensure_ascii=False) + '\n')
        process.stdin.close()
        result = None
        for _ in range(100000):
            line = process.stdout.readline(1 << 20)
            if not line:
                break
            if len(line) >= 1 << 20:
                raise TransportError('发布组件返回的数据过大')
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') == 'error':
                # 工作者只输出脱敏后的中文错误；不接受底层原始异常。
                raise TransportError(event.get('message', '发布接口请求失败'),
                                     code=event.get('code', 'PUBLISH_TRANSPORT'), detail=_error_detail(event.get('detail')))
            if event.get('event') == 'result':
                result = event.get('data')
            elif on_event:
                on_event(event)
        if expired.is_set():
            raise TransportError('操作已暂停' if cancel and cancel.is_set() else '发布接口请求超时')
        if process.wait(timeout=5) != 0 or result is None:
            raise TransportError('发布组件未返回完整结果')
        return result
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        watcher.join(timeout=1)
