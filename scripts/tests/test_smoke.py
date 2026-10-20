"""只使用临时文本和进程/HTTP替身；不启动服务或运行产品测试。"""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('dubforge_smoke', Path(__file__).parents[1] / 'smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def test_environment_copies_only_example_and_replaces_all_shared_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo'
            repo.mkdir()
            (repo / 'config.example.yaml').write_text('server: {}\n', encoding='utf-8')
            (repo / 'config.yaml').write_text('DO NOT COPY USER CONFIG', encoding='utf-8')
            root = Path(temporary) / 'isolated'
            root.mkdir()
            inherited = {key: 'old-shared-path' for key in ['DUBFORGE_CONFIG', 'DUBFORGE_DATA_ROOT', 'HF_HOME',
                'DUBFORGE_DEBUG_DATADIR', 'PYTHONPATH', 'PYTHONHOME', 'TEMP', 'TMP', 'TMPDIR', 'TORCH_HOME', 'HUGGINGFACE_HUB_CACHE', 'TRANSFORMERS_CACHE']}
            with mock.patch.object(smoke, 'ROOT', repo):
                env = smoke.isolated_environment(root, inherited)
            self.assertEqual((root / 'config.yaml').read_text(), 'server: {}\n')
            self.assertNotIn('DUBFORGE_DEBUG_DATADIR', env)
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('PYTHONHOME', env)
            for key in ['DUBFORGE_CONFIG', 'DUBFORGE_DATA_ROOT', 'HF_HOME', 'TEMP', 'TMP', 'TMPDIR',
                        'TORCH_HOME', 'HUGGINGFACE_HUB_CACHE', 'TRANSFORMERS_CACHE', 'PYTHONPYCACHEPREFIX']:
                self.assertTrue(Path(env[key]).is_relative_to(root), key)
            self.assertEqual(env['HF_HUB_OFFLINE'], '1')
            self.assertEqual(env['DUBFORGE_BACKEND_PORT'], '8611')
            self.assertEqual(inherited['HF_HOME'], 'old-shared-path')

    def test_occupied_port_rejects_without_launch_or_stop(self):
        with mock.patch.object(smoke, 'assert_port_free', side_effect=smoke.SmokeError('occupied')), \
             mock.patch.object(smoke.subprocess, 'Popen') as launch, mock.patch.object(smoke, 'stop_owned') as stop:
            with self.assertRaisesRegex(smoke.SmokeError, 'occupied'):
                smoke.health_check({}, Path('unused'), 1)
            launch.assert_not_called()
            stop.assert_not_called()

    def test_port_probe_checks_only_fixed_local_8611_and_refuses_occupancy(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError('occupied')
        with mock.patch.object(smoke.socket, 'socket', return_value=probe):
            with self.assertRaisesRegex(smoke.SmokeError, '8611 已被占用'):
                smoke.assert_port_free()
        probe.bind.assert_called_once_with(('127.0.0.1', 8611))

    def test_health_http_has_no_proxy_and_no_production_address(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"ok":true,"app":"DubForge"}')
        with mock.patch.object(smoke.urllib.request, 'ProxyHandler', return_value='direct') as proxy, \
             mock.patch.object(smoke.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(smoke.read_health()['app'], 'DubForge')
        proxy.assert_called_once_with({})
        build.assert_called_once_with('direct')
        opener.open.assert_called_once_with('http://127.0.0.1:8611/api/health', timeout=2)

    def run_health(self, listener, health, projects=None):
        child = mock.Mock(pid=98765)
        child.poll.return_value = None
        listener = [(98765, 123) if value is True else None if value is False else value for value in listener]
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(smoke, 'assert_port_free'), \
             mock.patch.object(smoke.subprocess, 'Popen', return_value=child) as launch, \
             mock.patch.object(smoke, 'expected_version', return_value='1.0.0'), \
             mock.patch.object(smoke, 'creation_time', return_value=123), \
             mock.patch.object(smoke, 'listener_owned', side_effect=listener), \
             mock.patch.object(smoke, 'read_health', return_value=health) as read, \
             mock.patch.object(smoke, 'read_projects', return_value=[] if projects is None else projects), \
             mock.patch.object(smoke.time, 'monotonic', side_effect=[0, 0, 2]), \
             mock.patch.object(smoke.time, 'sleep'), mock.patch.object(smoke, 'stop_owned') as stop, \
             mock.patch.object(smoke, 'assert_stopped') as stopped:
            try:
                result = smoke.health_check({'TEMP': temporary}, Path(temporary), 1)
                return result, launch.call_args, read.call_count
            finally:
                stop.assert_called_once()
                self.assertEqual(stop.call_args.args[:2], (child, 123))
                self.assertEqual(stop.call_args.args[2], launch.call_args.args[0])
                self.assertEqual(stop.call_args.args[3], {})
                stopped.assert_called_once_with(child, {})

    def test_health_requires_owned_listener_and_exact_app(self):
        result, call, count = self.run_health([True, True, True], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'})
        self.assertEqual(result['port'], 8611)
        self.assertEqual(result['version'], '1.0.0')
        self.assertTrue(result['empty_projects'])
        self.assertEqual(count, 1)
        command = call.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[command.index('--port') + 1], '8611')
        self.assertNotIn('--reload', command)
        self.assertEqual(command[command.index('--host') + 1], '127.0.0.1')

    def test_foreign_app_is_not_health_success(self):
        with self.assertRaisesRegex(smoke.SmokeError, '超时'):
            self.run_health([True], {'ok': True, 'app': 'Other'})

    def test_unowned_listener_is_not_read(self):
        child = mock.Mock()
        child.poll.return_value = None
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke, 'assert_port_free'), \
             mock.patch.object(smoke.subprocess, 'Popen', return_value=child), \
             mock.patch.object(smoke, 'expected_version', return_value='1.0.0'), \
             mock.patch.object(smoke, 'creation_time', return_value=1), \
             mock.patch.object(smoke, 'listener_owned', return_value=None), \
             mock.patch.object(smoke, 'read_health') as read, \
             mock.patch.object(smoke.time, 'monotonic', side_effect=[0, 0, 2]), \
             mock.patch.object(smoke.time, 'sleep'), mock.patch.object(smoke, 'stop_owned'), \
             mock.patch.object(smoke, 'assert_stopped'):
            with self.assertRaises(smoke.SmokeError):
                smoke.health_check({}, Path(temporary), 1)
            read.assert_not_called()

    def test_changed_identity_after_response_fails_and_still_stops_owned_handle(self):
        with self.assertRaisesRegex(smoke.SmokeError, '身份已变化'):
            self.run_health([True, False], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'})

    def test_wrong_version_cannot_pass_even_with_owned_healthy_listener(self):
        with self.assertRaisesRegex(smoke.SmokeError, '版本与源码'):
            self.run_health([True], {'ok': True, 'app': 'DubForge', 'version': '0.1.0'})

    def test_nonempty_projects_cannot_pass_and_no_project_details_are_printed(self):
        with self.assertRaisesRegex(smoke.SmokeError, '隔离数据库的项目列表非空') as error:
            self.run_health([True, True], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'}, [{'uid': 'private'}])
        self.assertNotIn('private', str(error.exception))

    def test_changed_identity_after_project_read_cannot_pass(self):
        with self.assertRaisesRegex(smoke.SmokeError, '项目列表返回时进程身份已变化'):
            self.run_health([True, True, False], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'})

    def test_different_verified_listener_cannot_replace_response_owner(self):
        with self.assertRaisesRegex(smoke.SmokeError, '身份已变化'):
            self.run_health([(200, 123.1), (201, 123.2)], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'})

    def test_redirector_health_reports_actual_listener_and_launcher_separately(self):
        identity = (200, 123.1)
        result, _, count = self.run_health([identity, identity, identity], {'ok': True, 'app': 'DubForge', 'version': '1.0.0'})
        self.assertEqual((result['pid'], result['launcher_pid'], result['listener_created']), (200, 98765, 123.1))
        self.assertEqual(count, 1)

    def test_expected_version_is_read_from_metadata_without_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / 'backend/dubforge/__init__.py'
            module.parent.mkdir(parents=True)
            module.write_text('__version__ = "1.0.0"\nraise RuntimeError("not imported")\n', encoding='utf-8')
            with mock.patch.object(smoke, 'ROOT', root):
                self.assertEqual(smoke.expected_version(), '1.0.0')
                module.write_text('__version__ = "a"\n__version__ = "b"\n', encoding='utf-8')
                with self.assertRaises(smoke.SmokeError):
                    smoke.expected_version()

    def test_early_exit_does_not_query_api_and_waits_exact_owned_child(self):
        child = mock.Mock(returncode=3)
        child.poll.return_value = 3
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke, 'assert_port_free'), \
             mock.patch.object(smoke.subprocess, 'Popen', return_value=child), \
             mock.patch.object(smoke, 'expected_version', return_value='1.0.0'), \
             mock.patch.object(smoke, 'creation_time', return_value=1), \
             mock.patch.object(smoke, 'read_health') as read, mock.patch.object(smoke, 'stop_owned') as stop, \
             mock.patch.object(smoke, 'assert_stopped'):
            with self.assertRaisesRegex(smoke.SmokeError, '提前退出'):
                smoke.health_check({}, Path(temporary), 1)
            read.assert_not_called()
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[:2], (child, 1))

    def test_creation_inspection_failure_still_closes_original_handle(self):
        child = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke, 'assert_port_free'), \
             mock.patch.object(smoke.subprocess, 'Popen', return_value=child), \
             mock.patch.object(smoke, 'expected_version', return_value='1.0.0'), \
             mock.patch.object(smoke, 'creation_time', side_effect=OSError('denied')), \
             mock.patch.object(smoke, 'stop_owned') as stop, mock.patch.object(smoke, 'assert_stopped'):
            with self.assertRaises(OSError):
                smoke.health_check({}, Path(temporary), 1)
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[:2], (child, None))

    def test_health_failure_always_closes_retained_children_even_when_shutdown_fails(self):
        for shutdown_error in (None, OSError('shutdown failed')):
            with self.subTest(shutdown_error=bool(shutdown_error)):
                child = mock.Mock(pid=100)
                child.poll.return_value = None
                held = FakeHeldHandle()
                def listener(_process, _created, _command, owned):
                    owned[(200, 123.5)] = held
                    return (200, 123.5)
                with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke, 'assert_port_free'), \
                     mock.patch.object(smoke.subprocess, 'Popen', return_value=child), \
                     mock.patch.object(smoke, 'expected_version', return_value='1.0.0'), \
                     mock.patch.object(smoke, 'creation_time', return_value=123), \
                     mock.patch.object(smoke, 'listener_owned', side_effect=listener), \
                     mock.patch.object(smoke, 'read_health', return_value={'ok': True, 'app': 'DubForge', 'version': 'old'}), \
                     mock.patch.object(smoke.time, 'monotonic', side_effect=[0, 0]), \
                     mock.patch.object(smoke, 'stop_owned', side_effect=shutdown_error) as stop, \
                     mock.patch.object(smoke, 'assert_stopped') as stopped:
                    with self.assertRaises(OSError if shutdown_error else smoke.SmokeError):
                        smoke.health_check({}, Path(temporary), 1)
                    self.assertIs(stop.call_args.args[3][(200, 123.5)], held)
                    self.assertEqual(held.closed, 1)
                    stopped.assert_called_once_with(child, {(200, 123.5): held})

    def test_stop_gracefully_signals_only_original_process(self):
        child = mock.Mock()
        child.poll.side_effect = [None, 0]
        with mock.patch.object(smoke.os, 'name', 'nt'), mock.patch.object(smoke, 'same_process', return_value=True), \
             mock.patch.object(smoke.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            smoke.stop_owned(child, 123)
        child.send_signal.assert_called_once_with(1)
        child.terminate.assert_not_called()
        child.wait.assert_called_once_with(timeout=10)

    def test_shutdown_timeout_uses_original_popen_handle_without_tree_cleanup(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('owned', 10), 0]
        with mock.patch.object(smoke.os, 'name', 'nt'), mock.patch.object(smoke, 'same_process', return_value=True), \
             mock.patch.object(smoke.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            smoke.stop_owned(child, 123)
        child.terminate.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=10), mock.call(timeout=5)])

    def test_exited_child_is_never_signalled(self):
        child = mock.Mock()
        child.poll.return_value = 0
        smoke.stop_owned(child, 123)
        child.send_signal.assert_not_called()
        child.terminate.assert_not_called()

    def test_main_skip_flags_only_skip_checks_named_and_clean_new_workspace(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke.tempfile, 'gettempdir', return_value=temporary), \
             mock.patch.object(smoke, 'assert_port_free'), mock.patch.object(smoke, 'run_stage') as stage, \
             mock.patch.object(smoke, 'health_check', return_value={'port':8611}) as health, \
             contextlib.redirect_stdout(io.StringIO()):
            result = smoke.main(['--skip-tests', '--skip-frontend-build'])
            self.assertEqual(result, 0)
            stage.assert_not_called()
            env, root, timeout = health.call_args.args
            self.assertEqual(env['DUBFORGE_CONFIG'], str(root / 'config.yaml'))
            self.assertFalse(root.exists())

    def test_main_default_runs_full_pytest_and_build_in_isolated_environment(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke.tempfile, 'gettempdir', return_value=temporary), \
             mock.patch.object(smoke, 'assert_port_free'), mock.patch.object(smoke, 'run_stage') as stage, \
             mock.patch.object(smoke, 'health_check', return_value={'port':8611}), \
             mock.patch.object(smoke.shutil, 'which', return_value='npm.cmd'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main([]), 0)
            self.assertEqual([row.args[0] for row in stage.call_args_list], ['backend-pytest','frontend-build'])
            self.assertEqual(stage.call_args_list[0].args[1], [sys.executable, '-m', 'pytest', '-q'])
            self.assertEqual(stage.call_args_list[0].args[3], stage.call_args_list[1].args[3])
            build = stage.call_args_list[1].args
            self.assertEqual(build[1][-3:], ['--', '--outDir', str(build[4] / 'frontend-dist')])

    def test_failure_preserves_isolated_diagnostics_and_never_builds(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke.tempfile, 'gettempdir', return_value=temporary), \
             mock.patch.object(smoke, 'assert_port_free'), mock.patch.object(smoke, 'run_stage') as stage, \
             mock.patch.object(smoke, 'health_check', side_effect=smoke.SmokeError('fixture')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.main(['--skip-tests']), 1)
            stage.assert_not_called()
            remaining = list(Path(temporary).glob('dubforge-smoke-*'))
            self.assertEqual(len(remaining), 1)
            self.assertTrue((remaining[0] / 'config.yaml').exists())

    def test_cleanup_refuses_any_non_owned_directory(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke.shutil, 'rmtree') as remove:
            parent = Path(temporary).resolve()
            for root in [parent, parent / 'other-name', parent.parent / 'dubforge-smoke-other']:
                with self.assertRaises(smoke.SmokeError):
                    smoke.cleanup_workspace(root, parent)
            remove.assert_not_called()


class FakeInspection:
    def __init__(self, pid, created, command, table, parent=None, listening=False):
        self.pid, self.created, self.args, self.table = pid, created, list(command), table
        self.parent_pid, self.listening = parent, listening
        self.executable = sys.executable
        self.directory = str(smoke.ROOT / 'backend')
        self.descendants = []
        self.connection_reads = 0

    def create_time(self): return self.created
    def exe(self): return self.executable
    def cmdline(self): return self.args
    def cwd(self): return self.directory
    def parent(self): return self.table.get(self.parent_pid)
    def children(self, recursive):
        assert recursive is True
        return self.descendants
    def net_connections(self, kind):
        assert kind == 'tcp'
        self.connection_reads += 1
        return [SimpleNamespace(status='LISTEN', laddr=SimpleNamespace(port=8611, ip='127.0.0.1'))] if self.listening else []


class FakeHeldHandle:
    def __init__(self):
        self.running = True
        self.closed = 0
        self.forced = 0
    def alive(self): return self.running
    def wait(self, timeout): return not self.running
    def terminate(self):
        self.forced += 1
        self.running = False
    def close(self): self.closed += 1


class RedirectorTests(unittest.TestCase):
    def setUp(self):
        self.command = [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                        '--port', '8611', '--loop', 'app.event_loop:create_event_loop', '--timeout-graceful-shutdown', '5']
        self.process = mock.Mock(pid=100)
        self.process.poll.return_value = None
        self.table = {}
        self.parent = FakeInspection(100, 1000., self.command, self.table)
        self.child = FakeInspection(200, 1000.5, self.command, self.table, parent=100, listening=True)
        self.table.update({100: self.parent, 200: self.child})
        self.parent.descendants = [self.child]
        self.handles = []
        class InspectionError(Exception): pass
        def inspect(pid):
            if pid not in self.table: raise InspectionError('gone')
            return self.table[pid]
        fake_psutil = SimpleNamespace(Process=inspect, Error=InspectionError, CONN_LISTEN='LISTEN')
        self.module = mock.patch.dict(sys.modules, {'psutil': fake_psutil})
        self.module.start()
        self.addCleanup(self.module.stop)
        self.platform = mock.patch.object(smoke.os, 'name', 'nt')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        def hold(*_identity):
            handle = FakeHeldHandle()
            self.handles.append(handle)
            return handle
        self.factory = mock.patch.object(smoke, 'WindowsChildHandle', side_effect=hold)
        self.opened = self.factory.start()
        self.addCleanup(self.factory.stop)

    def test_redirector_listener_requires_real_child_identity_and_held_handle(self):
        owned = {}
        self.assertEqual(smoke.listener_owned(self.process, 1000., self.command, owned), (200, 1000.5))
        self.opened.assert_called_once_with(200, 1000.5)
        self.assertIs(owned[(200, 1000.5)], self.handles[0])

    def test_direct_python_listener_needs_no_extra_child_handle(self):
        self.parent.listening = True
        self.parent.descendants = []
        self.assertEqual(smoke.listener_owned(self.process, 1000., self.command, {}), (100, 1000.))
        self.opened.assert_not_called()

    def test_verified_multilevel_chain_is_supported(self):
        self.child.listening = False
        grandchild = FakeInspection(300, 1000.7, self.command, self.table, parent=200, listening=True)
        self.table[300] = grandchild
        self.parent.descendants.append(grandchild)
        owned = {}
        self.assertEqual(smoke.listener_owned(self.process, 1000., self.command, owned), (300, 1000.7))
        self.assertEqual(set(owned), {(200, 1000.5), (300, 1000.7)})

    def test_foreign_listener_is_not_adopted_or_inspected(self):
        self.child.listening = False
        foreign = FakeInspection(900, 1000.6, self.command, self.table, listening=True)
        self.table[900] = foreign
        self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, {}))
        self.assertEqual(foreign.connection_reads, 0)

    def test_wrong_parent_command_executable_cwd_or_birth_is_not_owned(self):
        cases = [('parent_pid', 900), ('args', self.command + ['--reload']),
                 ('executable', str(smoke.ROOT / 'foreign/python.exe')),
                 ('directory', str(smoke.ROOT / 'other')), ('created', 999.)]
        for attr, value in cases:
            with self.subTest(field=attr):
                previous = getattr(self.child, attr)
                setattr(self.child, attr, value)
                try:
                    self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, {}))
                finally:
                    setattr(self.child, attr, previous)
        self.opened.assert_not_called()

    def test_reused_root_pid_is_not_a_valid_ancestor(self):
        self.parent.created = 1001.
        self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, {}))
        self.opened.assert_not_called()

    def test_child_parent_changes_during_handle_acquisition_is_rejected_and_closed(self):
        handle = FakeHeldHandle()
        def changed(*_args):
            self.child.parent_pid = 900
            return handle
        self.opened.side_effect = changed
        owned = {}
        self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, owned))
        self.assertEqual(owned, {})
        self.assertEqual(handle.closed, 1)
        self.assertEqual(handle.forced, 0)

    def test_child_pid_birth_changes_during_handle_acquisition_is_rejected(self):
        handle = FakeHeldHandle()
        def changed(*_args):
            self.child.created = 1001.
            return handle
        self.opened.side_effect = changed
        self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, {}))
        self.assertEqual(handle.closed, 1)
        self.assertEqual(handle.forced, 0)

    def test_listener_identity_changed_during_connection_read_is_not_returned(self):
        read = self.child.net_connections
        def changed(kind):
            result = read(kind)
            self.child.created = 1001.
            return result
        self.child.net_connections = changed
        owned = {}
        self.assertIsNone(smoke.listener_owned(self.process, 1000., self.command, owned))
        self.assertEqual(set(owned), {(200, 1000.5)})  # 原句柄仍留给 finally 清理。
        self.assertEqual(self.handles[0].forced, 0)

    def test_exited_launcher_cleanup_still_closes_already_held_child(self):
        self.process.poll.return_value = 0
        held = FakeHeldHandle()
        smoke.stop_owned(self.process, 1000., self.command, {(200, 1000.5): held})
        self.process.send_signal.assert_not_called()
        self.process.terminate.assert_not_called()
        self.assertEqual(held.forced, 1)

    def test_one_child_cleanup_error_does_not_skip_other_handles_or_launcher(self):
        broken = FakeHeldHandle()
        broken.terminate = mock.Mock(side_effect=OSError('denied'))
        other = FakeHeldHandle()
        self.process.wait.side_effect = [subprocess.TimeoutExpired('owned', 10), 0]
        with mock.patch.object(smoke.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            with self.assertRaisesRegex(smoke.SmokeError, '隔离进程清理失败'):
                smoke.stop_owned(self.process, 1000., owned={(200, 1000.5): broken, (300, 1000.6): other})
        self.assertEqual(other.forced, 1)
        self.process.terminate.assert_called_once_with()
        self.assertEqual(self.process.wait.call_args_list,
                         [mock.call(timeout=10), mock.call(timeout=5)])

    def test_startup_failure_cleanup_captures_child_before_group_signal_and_uses_retained_handle(self):
        owned = {}
        self.process.wait.side_effect = [subprocess.TimeoutExpired('owned', 10), 0]
        with mock.patch.object(smoke.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            smoke.stop_owned(self.process, 1000., self.command, owned)
        self.process.send_signal.assert_called_once_with(1)
        self.assertEqual(self.handles[0].forced, 1)
        self.assertEqual(len(owned), 1)
        self.process.terminate.assert_called_once_with()

    def test_reused_child_pid_never_becomes_the_force_target(self):
        held = FakeHeldHandle()
        self.child.parent_pid = 900
        self.child.created = 1002.
        self.parent.descendants = []
        self.process.poll.side_effect = [None, None]
        self.process.wait.side_effect = [subprocess.TimeoutExpired('owned', 10), 0]
        with mock.patch.object(smoke.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            smoke.stop_owned(self.process, 1000., owned={(200, 1000.5): held})
        self.assertEqual(held.forced, 1)
        self.opened.assert_not_called()  # 关闭旧句柄，不重新按同一 PID 取得目标。

    def test_stopped_owned_processes_do_not_touch_new_foreign_listener(self):
        self.process.poll.return_value = 0
        held = FakeHeldHandle()
        held.running = False
        with mock.patch.object(smoke, 'assert_port_free', side_effect=smoke.SmokeError('foreign')):
            with self.assertRaisesRegex(smoke.SmokeError, '未操作未知监听'):
                smoke.assert_stopped(self.process, {(200, 1000.5): held})
        self.process.terminate.assert_not_called()
        self.assertEqual(held.forced, 0)

    def test_live_owned_handle_cannot_be_reported_stopped(self):
        self.process.poll.return_value = 0
        with mock.patch.object(smoke, 'assert_port_free') as probe:
            with self.assertRaisesRegex(smoke.SmokeError, '本轮进程仍未退出'):
                smoke.assert_stopped(self.process, {(200, 1000.5): FakeHeldHandle()})
        probe.assert_not_called()


class WindowsHandleTests(unittest.TestCase):
    def api(self, created):
        def times(_handle, creation, *_rest):
            stamp = int(created * 10000000) + 116444736000000000
            creation._obj.dwLowDateTime = stamp & 0xffffffff
            creation._obj.dwHighDateTime = stamp >> 32
            return True
        return SimpleNamespace(OpenProcess=mock.Mock(return_value=1234),
            GetProcessTimes=mock.Mock(side_effect=times), WaitForSingleObject=mock.Mock(return_value=258),
            TerminateProcess=mock.Mock(return_value=True), CloseHandle=mock.Mock(return_value=True))

    def test_native_handle_birth_is_checked_and_force_uses_that_handle(self):
        import ctypes
        api = self.api(1000.5)
        with mock.patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            held = smoke.WindowsChildHandle(200, 1000.5)
            held.terminate()
            held.close()
        api.OpenProcess.assert_called_once_with(0x1000 | 0x00100000 | 0x0001, False, 200)
        api.TerminateProcess.assert_called_once_with(1234, 1)
        api.CloseHandle.assert_called_once_with(1234)

    def test_reused_pid_at_open_is_closed_without_terminating_new_process(self):
        import ctypes
        api = self.api(1001.)
        with mock.patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            with self.assertRaisesRegex(smoke.SmokeError, 'PID 已换代'):
                smoke.WindowsChildHandle(200, 1000.5)
        api.TerminateProcess.assert_not_called()
        api.CloseHandle.assert_called_once_with(1234)


if __name__ == '__main__':
    unittest.main()
