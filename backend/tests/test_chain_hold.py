"""自动续跑链「挂起可恢复」语义 (ADR-0072): 失败/取消/中断保留计划,
恢复必须显式; 只有明确放弃才销毁。"""
import time

from fastapi.testclient import TestClient

from conftest import mark_upstream_done, wait_status


def _client():
    from app.main import app
    return TestClient(app)


def _chain_of(client, uid):
    return client.get(f"/api/projects/{uid}").json()["chain"]


def _wait_held(client, uid, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        chain = _chain_of(client, uid)
        if chain and chain.get("state") == "held":
            return chain
        time.sleep(0.05)
    raise TimeoutError(f"链未在 {timeout}s 内挂起: {_chain_of(client, uid)}")


def test_plain_retry_success_keeps_chain_held(isolated_env, stub_heavy_steps, make_project):
    """普通重试 (不带 resume_chain) 成功后: 计划仍挂起, 不自动续跑。"""
    stub_heavy_steps("split", "translate")
    uid = make_project(params={"fail_step": "translate"})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/run_chain", json={"steps": ["split", "translate"]})
        wait_status(uid, "translate", {"failed"})
        chain = _wait_held(client, uid)
        assert chain["remaining"] == ["translate"] and chain["hold"]["step"] == "translate"

        from dubforge.domain import crud
        project = crud.get_project(uid)
        crud.project_params(project).pop("fail_step")
        # 去掉失败开关后直接重跑 (普通单步语义)。
        import json as _json
        params = crud.project_params(crud.get_project(uid))
        params.pop("fail_step", None)
        latest = crud.get_project(uid)
        latest.params = _json.dumps(params, ensure_ascii=False)
        crud.save_project(latest)

        client.post(f"/api/projects/{uid}/steps/translate/run")
        wait_status(uid, "translate", {"done"})
        # 成功后链不自动续跑 (已无剩余步骤可续也不会清理计划键之外的东西),
        # 但挂起状态必须保留到用户显式恢复/放弃。
        chain = _chain_of(client, uid)
        assert chain["state"] == "held"
        assert not any(s["status"] in ("running", "queued")
                       for s in client.get(f"/api/projects/{uid}/steps").json())


def test_retry_with_resume_chain_continues(isolated_env, stub_heavy_steps, make_project):
    """带 resume_chain 的重试成功后: 剩余计划自动续跑。"""
    stub_heavy_steps("split", "translate", "subtitles")
    uid = make_project(params={"fail_step": "translate"})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/run_chain",
                    json={"steps": ["split", "translate", "subtitles"]})
        wait_status(uid, "translate", {"failed"})
        chain = _wait_held(client, uid)
        assert chain["remaining"] == ["translate", "subtitles"]

        import json as _json
        from dubforge.domain import crud
        params = crud.project_params(crud.get_project(uid))
        params.pop("fail_step", None)
        latest = crud.get_project(uid)
        latest.params = _json.dumps(params, ensure_ascii=False)
        crud.save_project(latest)

        client.post(f"/api/projects/{uid}/steps/translate/run", json={"resume_chain": True})
        wait_status(uid, "translate", {"done"})
        wait_status(uid, "subtitles", {"done"})
        # 全部跑完: 计划键清理, 项目回到无链状态。
        deadline = time.time() + 10
        while time.time() < deadline and _chain_of(client, uid):
            time.sleep(0.05)
        assert _chain_of(client, uid) is None


def test_cancel_holds_chain(isolated_env, stub_heavy_steps, make_project):
    """链中步骤被用户取消 → 计划挂起 (kind=stopped), 可恢复。"""
    stub_heavy_steps("split", "translate")
    uid = make_project(params={"delay_step": {"split": 0.3}})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/run_chain", json={"steps": ["split", "translate"]})
        time.sleep(0.1)
        client.post(f"/api/projects/{uid}/steps/split/cancel")
        wait_status(uid, "split", {"failed"})
        chain = _wait_held(client, uid)
        assert chain["hold"]["code"] == "STEP_CANCELLED"
        assert chain["remaining"] == ["split", "translate"]


def test_chain_resume_and_stop_endpoints(isolated_env, stub_heavy_steps, make_project):
    """chain/resume 恢复挂起计划并从链头执行; chain/stop 明确放弃清空计划。"""
    stub_heavy_steps("split", "translate")
    uid = make_project(params={"fail_step": "translate"})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/run_chain", json={"steps": ["split", "translate"]})
        wait_status(uid, "translate", {"failed"})
        _wait_held(client, uid)

        # 没挂起计划时 resume 报错 (active/不存在)。
        r = client.post(f"/api/projects/does-not-exist/chain/resume")
        assert r.status_code == 400

        import json as _json
        from dubforge.domain import crud
        params = crud.project_params(crud.get_project(uid))
        params.pop("fail_step", None)
        latest = crud.get_project(uid)
        latest.params = _json.dumps(params, ensure_ascii=False)
        crud.save_project(latest)

        r = client.post(f"/api/projects/{uid}/chain/resume")
        assert r.status_code == 200, r.json()
        assert r.json()["started"] == "translate"
        wait_status(uid, "translate", {"done"})
        deadline = time.time() + 10
        while time.time() < deadline and _chain_of(client, uid):
            time.sleep(0.05)
        assert _chain_of(client, uid) is None

        # stop: 再造一条挂起链并放弃。
        params = crud.project_params(crud.get_project(uid))
        params["_chain"] = ["translate"]
        params["_chain_state"] = "held"
        latest = crud.get_project(uid)
        latest.params = _json.dumps(params, ensure_ascii=False)
        crud.save_project(latest)
        r = client.post(f"/api/projects/{uid}/chain/stop")
        assert r.status_code == 200
        assert _chain_of(client, uid) is None
        assert "_chain" not in crud.project_params(crud.get_project(uid))


def test_run_chain_replaces_held_plan(isolated_env, stub_heavy_steps, make_project):
    """挂起期间重新提交链 → 新计划覆盖旧计划并立即激活。"""
    stub_heavy_steps("split", "translate")
    uid = make_project()
    mark_upstream_done(uid, "split")
    with _client() as client:
        # 手工构造挂起链。
        import json as _json
        from dubforge.domain import crud
        project = crud.get_project(uid)
        params = crud.project_params(project)
        params.update(_chain=["translate"], _chain_state="held",
                      _chain_hold={"step": "translate", "code": "TEST", "message": "x"})
        project.params = _json.dumps(params, ensure_ascii=False)
        crud.save_project(project)

        client.post(f"/api/projects/{uid}/run_chain", json={"steps": ["split", "translate"]})
        wait_status(uid, "translate", {"done"})
        deadline = time.time() + 10
        while time.time() < deadline and _chain_of(client, uid):
            time.sleep(0.05)
        assert _chain_of(client, uid) is None


def test_single_step_failure_has_no_chain_semantics(isolated_env, stub_heavy_steps, make_project):
    """无链项目的普通单步失败: 不凭空造出挂起计划。"""
    stub_heavy_steps("split")
    uid = make_project(params={"fail_step": "split"})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/steps/split/run")
        wait_status(uid, "split", {"failed"})
        time.sleep(0.2)
        assert _chain_of(client, uid) is None
        from dubforge.domain import crud
        assert "_chain_state" not in crud.project_params(crud.get_project(uid))
