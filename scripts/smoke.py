"""隔离冒烟执行器；只启动并关闭自己持有句柄的 8611 后端。"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PORT = 8611


class SmokeError(RuntimeError):
    pass


def isolated_environment(root: Path, inherited=None):
    """只复制示例配置；不读取当前用户配置或继承调试数据目录。"""
    env = dict(os.environ if inherited is None else inherited)
    for key in ('DUBFORGE_DEBUG_DATADIR', 'PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    config_file = root / 'config.yaml'
    shutil.copyfile(ROOT / 'config.example.yaml', config_file)
    locations = {'DUBFORGE_DATA_ROOT': root / 'data', 'HF_HOME': root / 'hf',
                 'TEMP': root / 'tmp', 'TMP': root / 'tmp', 'TMPDIR': root / 'tmp',
                 'TORCH_HOME': root / 'torch', 'HUGGINGFACE_HUB_CACHE': root / 'hf/hub',
                 'TRANSFORMERS_CACHE': root / 'transformers', 'PYTHONPYCACHEPREFIX': root / 'pycache'}
    for key, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(DUBFORGE_CONFIG=str(config_file), DUBFORGE_BACKEND_PORT=str(PORT),
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONUNBUFFERED='1',
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return env


def assert_port_free():
    """端口已占用则拒绝，不能把别人的健康响应当成此次验证。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(('127.0.0.1', PORT))
        except OSError as exc:
            raise SmokeError('8611 已被占用；未启动或停止任何现有服务，请先结束原验证实例。') from exc


def creation_time(process):
    import psutil
    return psutil.Process(process.pid).create_time()


def same_process(process, created):
    if created is None or process.poll() is not None:
        return False
    try:
        return abs(creation_time(process) - created) < .00001
    except Exception:
        return False


class WindowsChildHandle:
    """保留已核验解释器的原生句柄；超时关闭不能重新按 PID 寻找目标。"""

    def __init__(self, pid, created):
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.GetProcessTimes.restype = wintypes.BOOL
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateProcess.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x1000 | 0x00100000 | 0x0001, False, pid)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), '无法持有隔离解释器子进程句柄')
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle, *[ctypes.byref(value) for value in times]):
                raise OSError(ctypes.get_last_error(), '无法核对隔离解释器出生时间')
            stamp = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            actual = (stamp - 116444736000000000) / 10000000
            if abs(actual - created) >= .00001:
                raise SmokeError('解释器 PID 已换代，未接管新进程')
        except BaseException:
            self.close()
            raise

    def wait(self, timeout):
        value = self.api.WaitForSingleObject(self.handle, int(timeout * 1000))
        if value not in (0, 258):
            raise OSError('无法等待已持有的隔离解释器句柄')
        return value == 0

    def alive(self):
        return not self.wait(0)

    def terminate(self):
        if self.alive() and not self.api.TerminateProcess(self.handle, 1):
            raise OSError('无法结束已持有的隔离解释器句柄')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _path_identity(value):
    return os.path.normcase(os.path.realpath(value))


def _backend_identity(candidate, command):
    allowed = {_path_identity(sys.executable), _path_identity(getattr(sys, '_base_executable', None) or sys.executable)}
    args = candidate.cmdline()
    return (_path_identity(candidate.exe()) in allowed and bool(args)
            and _path_identity(args[0]) in allowed and args[1:] == command[1:]
            and _path_identity(candidate.cwd()) == _path_identity(ROOT / 'backend'))


def _owned_chain(candidate, process, created, command):
    """逐层核验父子关系；虚拟环境 redirector 与真实解释器须同属此次命令。"""
    visited = set()
    while candidate is not None and candidate.pid not in visited:
        visited.add(candidate.pid)
        stamp = candidate.create_time()
        if stamp < created - .00001 or not _backend_identity(candidate, command):
            return False
        if candidate.pid == process.pid:
            return abs(stamp - created) < .00001
        parent = candidate.parent()
        if parent is None or parent.create_time() > stamp + .00001:
            return False
        candidate = parent
    return False


def owned_candidates(process, created, command, owned):
    """仅枚举本轮根进程后代；Windows 后代在返回前取得不可换代的句柄。"""
    if not same_process(process, created):
        return []
    import psutil
    found = []
    try:
        parent = psutil.Process(process.pid)
        candidates = [parent, *parent.children(recursive=True)]
    except (psutil.Error, OSError):
        return found
    for candidate in candidates:
        try:
            candidate = psutil.Process(candidate.pid)
            if not _owned_chain(candidate, process, created, command):
                continue
            identity = (candidate.pid, candidate.create_time())
            if candidate.pid != process.pid:
                if os.name != 'nt':
                    continue
                if identity not in owned:
                    handle = WindowsChildHandle(*identity)
                    try:
                        fresh = psutil.Process(candidate.pid)
                        if (not handle.alive() or abs(fresh.create_time() - identity[1]) >= .00001
                                or not _owned_chain(fresh, process, created, command)
                                or not same_process(process, created)):
                            handle.close()
                            continue
                    except BaseException:
                        handle.close()
                        raise
                    owned[identity] = handle
                if not owned[identity].alive():
                    continue
            found.append((candidate, identity))
        except (psutil.Error, OSError):
            continue
    return found


def listener_owned(process, created, command, owned):
    """返回固定监听身份，不能把另一代已核验后代当作同一次 HTTP 的服务。"""
    import psutil
    listeners = []
    for candidate, identity in owned_candidates(process, created, command, owned):
        try:
            if any(row.status == psutil.CONN_LISTEN and row.laddr.port == PORT
                   and row.laddr.ip == '127.0.0.1' for row in candidate.net_connections(kind='tcp')):
                fresh = psutil.Process(identity[0])
                if (abs(fresh.create_time() - identity[1]) >= .00001
                        or not _owned_chain(fresh, process, created, command)
                        or not same_process(process, created)
                        or identity[0] != process.pid and not owned[identity].alive()):
                    continue
                listeners.append(identity)
        except (psutil.Error, OSError):
            continue
    if len(listeners) > 1:
        raise SmokeError('本轮出现多个监听身份，拒绝混用健康响应')
    return listeners[0] if listeners else None


def expected_version():
    """只读统一版本常量，不导入应用或加载用户配置。"""
    source = ast.parse((ROOT / 'backend/dubforge/__init__.py').read_text(encoding='utf-8-sig'))
    values = [node.value.value for node in source.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(values) != 1:
        raise SmokeError('源码缺少唯一的 __version__，不能核验健康响应版本')
    return values[0]


def read_json(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{PORT}{path}', timeout=2) as response:
        return json.load(response)


def read_health():
    return read_json('/api/health')


def read_projects():
    return read_json('/api/projects')


def stop_owned(process, created, command=None, owned=None):
    """先关闭原进程组；强制关闭仅使用原 Popen 或已持有的真实解释器句柄。"""
    owned = {} if owned is None else owned
    if command is not None:
        try:
            # 启动未完成也要保留已验证后代，不能只在健康成功后才负责退出。
            owned_candidates(process, created, command, owned)
        except (SmokeError, OSError):
            pass  # 仍关闭原组；最后的无监听检查不能因归属检查失败而省略。
    if process.poll() is None:
        try:
            if os.name == 'nt':
                # 原 Popen 句柄仍存活时 PID 不能复用；信号只发送给本轮创建的组。
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    failure = None
    for handle in owned.values():
        try:
            if not handle.wait(5):
                handle.terminate()
                if not handle.wait(5):
                    raise SmokeError('本轮解释器句柄未退出，拒绝报告冒烟通过')
        except (SmokeError, OSError) as exc:
            failure = exc  # 单个关闭失败不能跳过其余自有句柄或原 launcher。
    try:
        if process.poll() is None:
            process.terminate()  # Windows 使用原 Popen 的进程句柄，不按 PID 重新寻找。
            process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = exc
    if failure is not None:
        raise SmokeError('本轮隔离进程清理失败，拒绝报告冒烟通过') from failure


def assert_stopped(process, owned):
    """本轮句柄全部退出后，8611 仍占用也只拒绝通过，绝不触碰其他进程。"""
    if process.poll() is None or any(handle.alive() for handle in owned.values()):
        raise SmokeError('本轮进程仍未退出，隔离监听尚不能确认已清理')
    try:
        assert_port_free()
    except SmokeError as exc:
        raise SmokeError('本轮进程已退出，但8611仍被占用；未操作未知监听，拒绝验收') from exc


def health_check(env, root, timeout):
    assert_port_free()
    version = expected_version()
    command = [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
               '--port', str(PORT), '--loop', 'app.event_loop:create_event_loop',
               '--timeout-graceful-shutdown', '5']
    process = None
    created = None
    owned = {}
    log = root / 'health.log'
    with log.open('wb') as output:
        try:
            process = subprocess.Popen(command, cwd=ROOT / 'backend', env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
            created = creation_time(process)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise SmokeError(f'隔离后端提前退出（{process.returncode}），日志：{log}')
                listener = listener_owned(process, created, command, owned)
                if listener is not None:
                    try:
                        value = read_health()
                    except (OSError, ValueError):
                        value = None
                    if isinstance(value, dict) and value.get('ok') is True and value.get('app') == 'DubForge':
                        if value.get('version') != version:
                            raise SmokeError('健康响应版本与源码 __version__ 不一致，拒绝验收。')
                        if listener_owned(process, created, command, owned) != listener:
                            raise SmokeError('健康响应返回时进程身份已变化，拒绝验收。')
                        if read_projects() != []:
                            raise SmokeError('全新隔离数据库的项目列表非空，拒绝验收（未输出项目资料）。')
                        if listener_owned(process, created, command, owned) != listener:
                            raise SmokeError('项目列表返回时进程身份已变化，拒绝验收。')
                        return dict(port=PORT, pid=listener[0], launcher_pid=process.pid,
                            listener_created=listener[1], app=value['app'], version=version, empty_projects=True)
                time.sleep(.2)
            raise SmokeError(f'隔离后端健康检查超时，日志：{log}')
        finally:
            if process is not None:
                try:
                    try:
                        stop_owned(process, created, command, owned)
                    finally:
                        assert_stopped(process, owned)
                finally:
                    for handle in owned.values():
                        handle.close()


def run_stage(name, command, cwd, env, root):
    log = root / f'{name}.log'
    print(f'开始 {name}，日志：{log}', flush=True)
    with log.open('wb') as output:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        with log.open('rb') as output:
            output.seek(max(0, log.stat().st_size - 65536))
            print(output.read().decode('utf-8', errors='replace'), flush=True)
        raise SmokeError(f'{name} 失败（{result.returncode}），日志已保留：{log}')


def cleanup_workspace(root, parent):
    # 清理前再次解析，目录若被替换成外部链接就保留，不递归删除其他位置。
    if root.resolve() != root or root.parent != parent or not root.name.startswith('dubforge-smoke-'):
        raise SmokeError('临时目录身份已变化，未递归清理')
    shutil.rmtree(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--skip-frontend-build', action='store_true')
    parser.add_argument('--startup-timeout', type=int, default=90)
    parser.add_argument('--keep-temporary-data', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.startup_timeout <= 300:
        parser.error('启动等待时间必须为 1–300 秒')
    parent = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix='dubforge-smoke-', dir=parent)).resolve()
    # 递归清理仅限本次新建目录，不能跟随环境变量删除任意位置。
    if root.parent != parent or not root.name.startswith('dubforge-smoke-'):
        raise SmokeError('临时目录身份无效，拒绝继续')
    passed = False
    try:
        assert_port_free()
        env = isolated_environment(root)
        if not args.skip_tests:
            run_stage('backend-pytest', [sys.executable, '-m', 'pytest', '-q'], ROOT / 'backend', env, root)
        result = health_check(env, root, args.startup_timeout)
        print('隔离健康检查通过：' + json.dumps(result, ensure_ascii=False), flush=True)
        if not args.skip_frontend_build:
            npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
            if not npm:
                raise SmokeError('未找到 npm，请先安装 Node.js')
            run_stage('frontend-build', [npm, 'run', 'build', '--', '--outDir', str(root / 'frontend-dist')],
                      ROOT / 'frontend', env, root)
        passed = True
        print('冒烟通过；跳过的检查不计入本次通过范围。', flush=True)
        return 0
    except (SmokeError, OSError, subprocess.SubprocessError) as exc:
        print(f'冒烟失败：{exc}\n隔离诊断目录保留：{root}', file=sys.stderr, flush=True)
        return 1
    finally:
        if passed and not args.keep_temporary_data:
            cleanup_workspace(root, parent)
        else:
            print(f'隔离临时目录：{root}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
