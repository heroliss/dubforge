"""开发所有权协议只使用隔离目录和假进程，不触碰常驻实例。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import time

import psutil
import pytest

from dubforge import dev_owner as owner


def _native_owner_lock(handle, *, unlock=False):
    """用独立句柄核对真实 OS 锁，不依赖被测上下文的重试逻辑。"""
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _hold_owner_lock_in_child(path, connection):
    """仅持有隔离锁文件；管道握手固定顺序，不启动任何服务。"""
    try:
        with owner._locked(Path(path)):
            connection.send('locked')
            if connection.recv() == 'exit':
                # 故意跳过 finally，核对进程退出也由 OS 释放锁。
                os._exit(0)
        connection.send('released')
    finally:
        connection.close()


def test_empty_owner_lock_retries_without_writing_into_a_held_byte(tmp_path, monkeypatch, isolated_env):
    path = tmp_path / 'backend.process.json'
    lock_path = path.with_suffix('.lock')
    sleeps = []
    with lock_path.open('a+b') as held:
        _native_owner_lock(held)
        try:
            def release_after_contention(delay):
                # 第一次申请必定遇到已持有的空文件锁，才允许释放供重试取得。
                sleeps.append(delay)
                assert sleeps == [.02]
                _native_owner_lock(held, unlock=True)

            monkeypatch.setattr(owner, 'time', SimpleNamespace(
                monotonic=time.monotonic, sleep=release_after_contention))
            with owner._locked(path):
                assert sleeps == [.02]
                assert lock_path.stat().st_size == 0
                with lock_path.open('a+b') as contender:
                    with pytest.raises(OSError):
                        _native_owner_lock(contender)
        finally:
            # 旧实现会在进入重试前写入受保护的字节；失败时也需释放测试自己的句柄。
            if not sleeps:
                _native_owner_lock(held, unlock=True)
    assert lock_path.read_bytes() == b''


def test_owner_lock_preserves_three_second_wait_deadline(tmp_path, monkeypatch, isolated_env):
    path = tmp_path / 'backend.process.json'
    ticks = iter((100., 102.99, 103.))
    sleeps = []
    with path.with_suffix('.lock').open('a+b') as held:
        _native_owner_lock(held)
        try:
            monkeypatch.setattr(owner, 'time', SimpleNamespace(
                monotonic=lambda: next(ticks), sleep=sleeps.append))
            with pytest.raises(owner.DevelopmentOwnerError, match='其他控制台更新') as failure:
                with owner._locked(path):
                    pytest.fail('锁仍由另一句柄持有，不能进入登记事务')
            assert failure.value.code == 'DEV_PROCESS_OWNER_INVALID'
            assert isinstance(failure.value.__cause__, OSError)
            assert sleeps == [.02], '3 秒截止前重试，达到原截止时间后立即结束'
            assert path.with_suffix('.lock').stat().st_size == 0
        finally:
            _native_owner_lock(held, unlock=True)


@pytest.mark.parametrize('contents', [b'', b'0'], ids=['empty', 'legacy-byte'])
@pytest.mark.parametrize('release', ['normal', 'exit'])
def test_owner_lock_cross_process_exclusion_and_release(tmp_path, isolated_env, contents, release):
    path = tmp_path / 'backend.process.json'
    lock_path = path.with_suffix('.lock')
    lock_path.write_bytes(contents)
    context = multiprocessing.get_context('spawn')
    connection, child_connection = context.Pipe()
    process = context.Process(target=_hold_owner_lock_in_child, args=(str(path), child_connection))
    process.start()
    child_connection.close()
    try:
        assert connection.poll(15), '测试子进程未能取得隔离锁'
        assert connection.recv() == 'locked'
        assert lock_path.stat().st_size == len(contents)
        with lock_path.open('a+b') as contender:
            with pytest.raises(OSError):
                _native_owner_lock(contender)
        connection.send(release)
        if release == 'normal':
            assert connection.poll(15), '测试子进程未能释放隔离锁'
            assert connection.recv() == 'released'
        process.join(15)
        assert not process.is_alive()
        assert process.exitcode == 0
        # 普通退出与跳过 finally 的退出都必须允许同一偏移重新取得锁。
        with owner._locked(path):
            assert lock_path.stat().st_size == len(contents)
            with lock_path.open('a+b') as contender:
                with pytest.raises(OSError):
                    _native_owner_lock(contender)
        assert lock_path.read_bytes() == contents
    finally:
        connection.close()
        if process.is_alive():
            process.terminate()
            process.join(15)
        process.close()


def test_owner_lock_releases_after_transaction_error(tmp_path, isolated_env):
    path = tmp_path / 'backend.process.json'
    with pytest.raises(RuntimeError, match='隔离事务失败'):
        with owner._locked(path):
            raise RuntimeError('隔离事务失败')
    with path.with_suffix('.lock').open('a+b') as contender:
        _native_owner_lock(contender)
        _native_owner_lock(contender, unlock=True)


@pytest.fixture
def service(tmp_path, monkeypatch, isolated_env):
    workspace = tmp_path / '工作区 空格'
    for role in ('backend', 'frontend'):
        (workspace / role).mkdir(parents=True)
    processes, connections = {}, []

    class Process:
        def __init__(self, pid, command, cwd, executable, created=10):
            self.pid, self.command, self.directory, self.executable, self.created = pid, command, cwd, executable, created
            self.descendants, self.alive = [], True
        def create_time(self): return self.created
        def exe(self): return self.executable
        def cmdline(self): return self.command
        def cwd(self): return str(self.directory)
        def is_running(self): return self.alive
        def children(self, recursive=False): return self.descendants

    def process(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]
    monkeypatch.setattr(owner.psutil, 'Process', process)
    monkeypatch.setattr(owner.psutil, 'net_connections', lambda kind: connections)

    def prepare(role='backend', pid=51, created=10):
        port = 8611 if role == 'backend' else 15273
        command = (['uv', 'run', 'uvicorn', 'app.main:app', '--port', str(port)] if role == 'backend'
                   else ['npm', 'run', 'dev', '--', '--port', str(port), '--strictPort'])
        quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        encoded = f"Set-Location -LiteralPath {quote(workspace / role)}; & " + ' '.join(map(quote, command))
        wrapper = Process(pid, ['powershell', '-EncodedCommand', base64.b64encode(encoded.encode('utf-16-le')).decode()],
                          workspace, str(tmp_path / 'powershell.exe'), created)
        args = (['python', 'uvicorn', 'app.main:app', '--port', str(port)] if role == 'backend'
                else ['node', str(workspace / 'frontend/node_modules/vite/bin/vite.js'), '--port', str(port)])
        listener = Process(pid + 1, args, workspace / role, str(tmp_path / 'python.exe'), created + 1)
        wrapper.descendants = [listener]
        processes.update({pid: wrapper, pid + 1: listener})
        connections[:] = [SimpleNamespace(pid=listener.pid, status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=port))]
        return wrapper, listener, command

    wrapper, listener, command = prepare()
    return SimpleNamespace(workspace=workspace, processes=processes, connections=connections,
                           wrapper=wrapper, listener=listener, command=command, prepare=prepare,
                           path=lambda role='backend': owner.registry_path(workspace, 8611, 15273, role),
                           register=lambda: owner.register(workspace, 8611, 15273, 'backend', 51, 10, command))


@pytest.mark.parametrize('role', ['backend', 'frontend'])
def test_owner_atomic_registration_and_strict_listener_tree(service, role):
    s = service
    wrapper, listener, command = s.prepare(role)
    value = owner.register(s.workspace, 8611, 15273, role, wrapper.pid, wrapper.created, command)
    assert json.loads(s.path(role).read_text(encoding='utf-8')) == value
    assert len(value['generation']) == 32
    assert not list(s.path(role).parent.glob('*.tmp'))
    resolved = owner.resolve(s.workspace, 8611, 15273, role, require_listener=True)
    assert resolved['pid'] == wrapper.pid and resolved['listener_pid'] == listener.pid
    assert resolved['state'] == 'listening'
    assert owner.register(s.workspace, 8611, 15273, role, wrapper.pid, wrapper.created, command)['generation'] == value['generation']


@pytest.mark.parametrize('changed', ['created', 'executable', 'argv', 'dead'])
def test_owner_never_restores_reused_or_changed_process(service, changed):
    s = service; s.register()
    if changed == 'created': s.wrapper.created = 200
    elif changed == 'executable': s.wrapper.executable += '.changed'
    elif changed == 'argv': s.wrapper.command += ['unexpected']
    else: s.wrapper.alive = False
    assert owner.resolve(s.workspace, 8611, 15273, 'backend') is None


@pytest.mark.parametrize('changed', ['workspace', 'role', 'backend_port', 'frontend_port'])
def test_owner_rejects_cross_workspace_and_registry_scope(service, changed):
    s = service; value = s.register()
    value[changed] = str(s.workspace.parent / '另一工作区') if changed == 'workspace' else 'frontend' if changed == 'role' else 12345
    s.path().write_text(json.dumps(value))
    with pytest.raises(owner.DevelopmentOwnerError, match='不一致'):
        owner.resolve(s.workspace, 8611, 15273, 'backend')


@pytest.mark.parametrize('changed', ['unrelated', 'cwd', 'application', 'port', 'unknown_pid'])
def test_owner_rejects_foreign_listener_even_with_valid_wrapper(service, changed):
    s = service; s.register()
    if changed == 'unrelated': s.wrapper.descendants = []
    elif changed == 'cwd': s.listener.directory = s.workspace.parent
    elif changed == 'application': s.listener.command[2] = 'other.main:app'
    elif changed == 'port': s.listener.command[-1] = '8600'
    else: s.connections[0].pid = None
    with pytest.raises(owner.DevelopmentOwnerError, match='拒绝接管'):
        owner.resolve(s.workspace, 8611, 15273, 'backend')


def test_starting_registered_owner_blocks_duplicate_start_but_attach_requires_listener(service):
    s = service; s.register(); s.connections.clear()
    assert owner.resolve(s.workspace, 8611, 15273, 'backend')['state'] == 'starting'
    with pytest.raises(owner.DevelopmentOwnerError, match='尚未监听'):
        owner.resolve(s.workspace, 8611, 15273, 'backend', require_listener=True)


def test_frontend_accepts_windows_npm_relative_bin_entry_but_rejects_other_project_vite(service):
    s = service
    wrapper, listener, command = s.prepare('frontend')
    owner.register(s.workspace, 8611, 15273, 'frontend', wrapper.pid, wrapper.created, command)
    listener.command[1] = str(s.workspace / 'frontend/node_modules/.bin/../vite/bin/vite.js')
    assert owner.resolve(s.workspace, 8611, 15273, 'frontend', require_listener=True)['listener_pid'] == listener.pid
    listener.command[1] = str(s.workspace.parent / 'other/frontend/node_modules/vite/bin/vite.js')
    with pytest.raises(owner.DevelopmentOwnerError, match='工作区或服务命令不匹配'):
        owner.resolve(s.workspace, 8611, 15273, 'frontend', require_listener=True)


def test_registration_does_not_overwrite_live_owner_and_stale_cleanup_preserves_new_generation(service):
    s = service; first = s.register(); wrapper, _, command = s.prepare(pid=61, created=30)
    with pytest.raises(owner.DevelopmentOwnerError, match='仍在运行'):
        owner.register(s.workspace, 8611, 15273, 'backend', 61, 30, command)
    s.processes[51].alive = False
    second = owner.register(s.workspace, 8611, 15273, 'backend', 61, 30, command)
    assert not owner.forget(s.workspace, 8611, 15273, 'backend', 51, 10, first['generation'])
    assert not owner.forget(s.workspace, 8611, 15273, 'backend', 61, 30, 'old-generation')
    assert not owner.forget(s.workspace, 8611, 15273, 'backend', 61, 30, second['generation']), '活进程登记不能被退出清理抹掉'
    wrapper.alive = False
    assert owner.forget(s.workspace, 8611, 15273, 'backend', 61, 30, second['generation'])
    assert not s.path().exists()


def test_registration_rejects_wrong_wrapper_intent_before_writing(service):
    s = service
    s.wrapper.command[-1] = base64.b64encode(b'not-our-command').decode()
    with pytest.raises(owner.DevelopmentOwnerError, match='启动目录'):
        s.register()
    assert not s.path().exists()


def test_failed_atomic_write_keeps_previous_complete_owner(service, monkeypatch):
    s = service; first = s.register(); s.wrapper.alive = False
    s.prepare(pid=61, created=30)
    monkeypatch.setattr(owner, 'atomic_write_bytes', lambda *args: (_ for _ in ()).throw(PermissionError('fixture')))
    with pytest.raises(owner.DevelopmentOwnerError, match='原子保存'):
        owner.register(s.workspace, 8611, 15273, 'backend', 61, 30, s.command)
    assert json.loads(s.path().read_text(encoding='utf-8')) == first


def test_concurrent_registrations_keep_exactly_one_live_generation(service, monkeypatch):
    s = service
    s.prepare(pid=61, created=30)
    atomic_write = owner.atomic_write_bytes

    def slow_write(*args):
        # 扩大竞争窗口，证明文件锁保护读取旧代和原子替换这一整个事务。
        time.sleep(.05)
        atomic_write(*args)

    def register(pid, created):
        try:
            return owner.register(s.workspace, 8611, 15273, 'backend', pid, created, s.command)
        except owner.DevelopmentOwnerError as exc:
            return exc

    monkeypatch.setattr(owner, 'atomic_write_bytes', slow_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register, 51, 10)
        second = pool.submit(register, 61, 30)
        results = [first.result(), second.result()]
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1
    assert json.loads(s.path().read_text(encoding='utf-8')) == winners[0]
    assert '仍在运行' in str(next(value for value in results if isinstance(value, Exception)))


def test_cli_registration_preserves_service_arguments_and_returns_stable_error(service, monkeypatch, capsys):
    s = service
    args = ['dev_owner', 'register', str(s.workspace), '8611', '15273', 'backend', '--pid', '51', '--created', '10', '--', *s.command]
    monkeypatch.setattr('sys.argv', args)
    assert owner.main() == 0
    assert json.loads(capsys.readouterr().out)['command'] == s.command
    monkeypatch.setattr('sys.argv', ['dev_owner', 'resolve', str(s.workspace), '8611', '15273', 'backend', '--require-listener'])
    s.wrapper.descendants = []
    assert owner.main() == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'DEV_PROCESS_OWNER_INVALID'
