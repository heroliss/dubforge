"""开发所有权协议只使用隔离目录和假进程，不触碰常驻实例。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import time

import psutil
import pytest

from dubforge import dev_owner as owner


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
