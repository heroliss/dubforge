"""开发服务身份登记；只恢复本工作区的明确启动记录，不按端口猜测所有权。"""
import argparse
import base64
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import time
import uuid

import psutil

from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes


class DevelopmentOwnerError(DubForgeError):
    code = 'DEV_PROCESS_OWNER_INVALID'


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _created(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise DevelopmentOwnerError('服务登记的进程创建时间无效。')
    return float(value)


def registry_path(workspace, backend_port, frontend_port, role):
    if role not in {'backend', 'frontend'} or any(type(port) is not int or not 1 <= port <= 65535
                                                 for port in (backend_port, frontend_port)):
        raise DevelopmentOwnerError('服务登记的角色或端口无效。')
    root = Path(workspace).resolve()
    path = root / 'data' / 'dev-logs' / f'{backend_port}-{frontend_port}' / f'{role}.process.json'
    if not path.resolve().is_relative_to(root):
        raise DevelopmentOwnerError('服务登记路径不在当前工作区内。')
    return path


@contextmanager
def _locked(path):
    # OS 文件锁随进程退出释放；不能用崩溃后残留的目录锁阻断下一次启动。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic() + 3
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise DevelopmentOwnerError('服务登记正在被其他控制台更新，请稍后重试。') from exc
                time.sleep(.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path, workspace, backend_port, frontend_port, role):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DevelopmentOwnerError('服务身份登记无法读取，请核对后重试；不会接管未知进程。') from exc
    if (not isinstance(value, dict) or value.get('schema') != 1
            or not isinstance(value.get('workspace'), str) or not value['workspace']
            or _path(value.get('workspace', '')) != _path(workspace)
            or value.get('backend_port') != backend_port or value.get('frontend_port') != frontend_port
            or value.get('role') != role or not isinstance(value.get('generation'), str)
            or not value['generation'] or type(value.get('pid')) is not int or value['pid'] <= 0
            or not isinstance(value.get('argv'), list) or not value['argv']
            or not all(isinstance(part, str) for part in value['argv'])
            or not isinstance(value.get('executable'), str) or not value['executable']):
        raise DevelopmentOwnerError('服务身份登记与当前工作区、端口或角色不一致，拒绝接管。')
    _created(value.get('created'))
    return value


def _live(value):
    try:
        process = psutil.Process(value['pid'])
        if (abs(process.create_time() - value['created']) > .01
                or _path(process.exe()) != _path(value['executable']) or process.cmdline() != value['argv']):
            return None
        return process if process.is_running() else None
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as exc:
        raise DevelopmentOwnerError('无法核验服务进程身份，拒绝接管。') from exc


def _intent(value):
    args = value['argv']
    try:
        if Path(value['executable']).stem.lower() not in {'powershell', 'pwsh'}:
            raise ValueError()
        index = [part.lower() for part in args].index('-encodedcommand')
        command = base64.b64decode(args[index + 1], validate=True).decode('utf-16-le')
        quoted = "'" + str(Path(value['workspace']) / value['role']).replace("'", "''") + "'"
        location = f'Set-Location -LiteralPath {quoted}; & '
        if location.replace('\\', '/').casefold() not in command.replace('\\', '/').casefold():
            raise ValueError()
        port = value[f'{value["role"]}_port']
        command_args = value.get('command')
        if not isinstance(command_args, list) or not all(isinstance(part, str) for part in command_args):
            raise ValueError()
        marker = command_args.index('--port')
        if command_args.count('--port') != 1 or command_args[marker + 1] != str(port):
            raise ValueError()
        if value['role'] == 'backend' and 'app.main:app' not in command_args:
            raise ValueError()
        if value['role'] == 'frontend' and command_args[:4] != ['npm', 'run', 'dev', '--']:
            raise ValueError()
        invocation = ' '.join("'" + part.replace("'", "''") + "'" for part in command_args)
        if '; & ' + invocation not in command:
            raise ValueError()
    except (ValueError, IndexError, UnicodeError) as exc:
        raise DevelopmentOwnerError('服务包装器的启动目录、命令或端口与登记不符，拒绝接管。') from exc


def resolve(workspace, backend_port, frontend_port, role, *, require_listener=False):
    path = registry_path(workspace, backend_port, frontend_port, role)
    value = _read(path, workspace, backend_port, frontend_port, role)
    if value is None:
        return None
    process = _live(value)
    if process is None:
        return None
    _intent(value)
    try:
        port = value[f'{role}_port']
        owners = {row.pid for row in psutil.net_connections(kind='tcp')
                  if row.status == psutil.CONN_LISTEN and row.laddr and row.laddr.port == port}
        if not owners:
            if require_listener:
                raise DevelopmentOwnerError('已登记服务尚未监听预期端口，暂不能接管控制台。')
            return {**value, 'state': 'starting', 'listener_pid': None}
        descendants = {process.pid, *(child.pid for child in process.children(recursive=True))}
        if len(owners) != 1 or not owners <= descendants:
            raise DevelopmentOwnerError('预期端口由登记树之外的进程占用，拒绝接管。')
        listener = psutil.Process(next(iter(owners)))
        args = listener.cmdline()
        index = args.index('--port')
        # Windows npm 的入口可能带 node_modules/.bin/../vite，先解析路径再核验精确项目入口。
        vite_entry = Path(workspace) / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
        valid_command = ('app.main:app' in args if role == 'backend' else
                         any(_path(part) == _path(vite_entry) for part in args))
        if (args.count('--port') != 1 or args[index + 1] != str(port) or not valid_command
                or _path(listener.cwd()) != _path(Path(workspace) / role)):
            raise DevelopmentOwnerError('端口监听者的工作区或服务命令不匹配，拒绝接管。')
        return {**value, 'state': 'listening', 'listener_pid': listener.pid}
    except (psutil.Error, ValueError, IndexError) as exc:
        raise DevelopmentOwnerError('无法完整核验服务监听树，拒绝接管。') from exc


def register(workspace, backend_port, frontend_port, role, pid, created, command):
    path = registry_path(workspace, backend_port, frontend_port, role)
    if type(pid) is not int or pid <= 0:
        raise DevelopmentOwnerError('待登记的进程 PID 无效。')
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - _created(created)) > .01:
            raise DevelopmentOwnerError('启动进程身份已变化，未写入服务登记。')
        value = dict(schema=1, workspace=str(Path(workspace).resolve()), backend_port=backend_port,
                     frontend_port=frontend_port, role=role, pid=pid, created=process.create_time(),
                     executable=process.exe(), argv=process.cmdline(), command=list(command), generation=uuid.uuid4().hex)
        _intent(value)
        with _locked(path):
            previous = _read(path, workspace, backend_port, frontend_port, role)
            if previous and _live(previous):
                if previous['pid'] == pid and abs(previous['created'] - value['created']) <= .01:
                    return previous
                raise DevelopmentOwnerError('已有同端口的已登记服务仍在运行，未覆盖其所有权。')
            atomic_write_bytes(path, json.dumps(value, ensure_ascii=False).encode('utf-8'))
        return value
    except psutil.Error as exc:
        raise DevelopmentOwnerError('无法核验新启动进程，未写入服务登记。') from exc
    except OSError as exc:
        raise DevelopmentOwnerError('服务身份登记未能原子保存；当前控制台仍管理已启动的进程。') from exc


def forget(workspace, backend_port, frontend_port, role, pid, created, generation):
    path = registry_path(workspace, backend_port, frontend_port, role)
    if not path.exists():
        return False
    with _locked(path):
        value = _read(path, workspace, backend_port, frontend_port, role)
        if (value is None or value['pid'] != pid or abs(value['created'] - _created(created)) > .01
                or value['generation'] != generation or _live(value)):
            return False
        path.unlink()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['register', 'resolve', 'forget'])
    parser.add_argument('workspace'); parser.add_argument('backend_port', type=int)
    parser.add_argument('frontend_port', type=int); parser.add_argument('role', choices=['backend', 'frontend'])
    parser.add_argument('--pid', type=int); parser.add_argument('--created', type=float)
    parser.add_argument('--generation'); parser.add_argument('--require-listener', action='store_true')
    args, command = parser.parse_known_args()
    try:
        base = (args.workspace, args.backend_port, args.frontend_port, args.role)
        if args.action == 'register':
            value = register(*base, args.pid, args.created, command[1:] if command[:1] == ['--'] else command)
        elif args.action == 'forget':
            value = {'removed': forget(*base, args.pid, args.created, args.generation)}
        else:
            value = resolve(*base, require_listener=args.require_listener)
        # 原生 PowerShell 管道与 Python locale 可能不同；协议用 ASCII JSON，中文由接收端解码。
        print(json.dumps(value, ensure_ascii=True))
        return 0
    except (DevelopmentOwnerError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({'message': str(exc), 'code': 'DEV_PROCESS_OWNER_INVALID'}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
