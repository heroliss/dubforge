"""配音恢复预检应拦住未知请求，同时允许正常排队和复用音频。"""
import json
from types import SimpleNamespace

from dubforge import tts_requests, workflow_plan
from dubforge.utils.fs import atomic_write_bytes


def deps_for(url):
    values = {'tts.backend': 'indextts25', 'tts.indextts25.url': url}
    return SimpleNamespace(cfg=lambda key, default=None: values.get(key, default))


def test_orphan_request_blocks_before_start_without_model_probe(isolated_env, monkeypatch):
    url = 'http://127.0.0.1:17860'
    atomic_write_bytes(tts_requests._path(url), json.dumps({'token': 'old-backend'}).encode())
    def forbidden(*args, **kwargs):
        raise AssertionError('已知未知请求时，不应再请求配音模型或探测服务')
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _: 'installed')
    monkeypatch.setattr(workflow_plan, 'environment', SimpleNamespace(require_component=forbidden))
    from dubforge import tts_service
    monkeypatch.setattr(tts_service._BACKENDS['indextts25'], 'is_online', forbidden)
    issues = workflow_plan.production_checks(deps_for(url), ['tts'], online=True,
                                              dubbing_summary={'generate': 1})
    assert len(issues) == 1
    assert '配音服务需要恢复' in issues[0]
    assert '没有其他任务时' in issues[0]
    assert '重新启动配音服务' in issues[0]


def test_legitimate_active_request_can_queue_behind_current_sentence(isolated_env, monkeypatch):
    url = 'http://127.0.0.1:17861'
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _: 'installed')
    request = tts_requests.Request(url)
    try:
        assert workflow_plan.production_checks(deps_for(url), ['tts'], online=False,
                                               dubbing_summary={'generate': 1}) == []
    finally:
        request.finish()


def test_reuse_only_plan_does_not_require_idle_model(isolated_env, monkeypatch):
    url = 'http://127.0.0.1:17862'
    atomic_write_bytes(tts_requests._path(url), json.dumps({'token': 'old-backend'}).encode())
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _: 'installed')
    assert workflow_plan.production_checks(deps_for(url), ['tts'], online=False,
        dubbing_summary={'generate': 0, 'repair': 0}) == []


def test_confirmed_service_restart_unblocks_preflight(isolated_env, monkeypatch):
    url = 'http://127.0.0.1:17863'
    atomic_write_bytes(tts_requests._path(url), json.dumps({'token': 'old-backend'}).encode())
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _: 'installed')
    assert workflow_plan.production_checks(deps_for(url), ['tts'], online=False,
        dubbing_summary={'generate': 1})
    # 服务管理层只在确认进程停止/重新启动之后调用 reset；预检本身不能解除占用。
    tts_requests.reset(url)
    assert workflow_plan.production_checks(deps_for(url), ['tts'], online=False,
        dubbing_summary={'generate': 1}) == []
