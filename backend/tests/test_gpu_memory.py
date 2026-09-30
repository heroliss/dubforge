"""资源交接只清理本进程空闲缓存，不加载 CUDA、不终止任何外部服务。"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dubforge.utils import gpu_memory


def test_does_not_import_torch_or_initialize_cuda(monkeypatch):
    monkeypatch.delitem(sys.modules, 'torch', raising=False)
    gpu_memory.release_unused()
    assert 'torch' not in sys.modules
    cuda = Mock()
    cuda.is_initialized.return_value = False
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda))
    gpu_memory.release_unused()
    cuda.empty_cache.assert_not_called()


def test_collects_released_models_then_returns_idle_cache(monkeypatch):
    events = []
    cuda = SimpleNamespace(is_initialized=lambda: True,
        memory_reserved=Mock(side_effect=[8 * 1024**2, 2 * 1024**2]),
        empty_cache=lambda: events.append('empty'))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda))
    monkeypatch.setattr(gpu_memory.gc, 'collect', lambda: events.append('gc'))
    logs = []
    gpu_memory.release_unused(logs.append)
    assert events == ['gc', 'empty']
    assert '6 MiB' in logs[0]


def test_cleanup_failure_does_not_replace_step_result(monkeypatch):
    cuda = Mock()
    cuda.is_initialized.return_value = True
    cuda.empty_cache.side_effect = RuntimeError('驱动失效')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda))
    logs = []
    gpu_memory.release_unused(logs.append)
    assert '未完成' in logs[0]


@pytest.mark.parametrize('fails', [False, True])
def test_runner_releases_gpu_before_returning_slot(make_project, isolated_env, monkeypatch, fails):
    from conftest import mark_upstream_done, wait_status
    from dubforge.pipeline import steps
    from dubforge.runner import runner
    uid = make_project()
    mark_upstream_done(uid, 'preprocess')
    events = []
    def run(ctx):
        events.append('run')
        if fails:
            raise RuntimeError('模拟计算失败')
    original = steps.get_runner
    monkeypatch.setattr(steps, 'get_runner', lambda key: run if key == 'preprocess' else original(key))
    monkeypatch.setattr(gpu_memory, 'release_unused', lambda log: events.append('release'))
    release_slot = runner.release_slot
    def release(key, slot_key=None):
        events.append('slot')
        release_slot(key, slot_key)
    monkeypatch.setattr(runner, 'release_slot', release)
    runner.start_step(uid, 'preprocess')
    wait_status(uid, 'preprocess', {'failed' if fails else 'done'})
    from dubforge.domain import crud
    from test_dubbing_tasks import until
    until(lambda: runner.current_job_id(crud.get_project(uid).id, 'preprocess') is None)
    assert events == ['run', 'release', 'slot'] + (['release'] if fails else [])
