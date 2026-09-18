# -*- coding: utf-8 -*-
"""项目 API: 改名 / 标题自动命名 / 语义化目录名 / manifest 自描述 / 总目录解析"""
import os
from pathlib import Path

from fastapi.testclient import TestClient


def _client():
    from app.main import app
    return TestClient(app)


def test_rename_project(isolated_env):
    with _client() as client:
        uid = client.post("/api/projects", json={"title": "旧标题"}).json()["uid"]
        r = client.patch(f"/api/projects/{uid}", json={"title": "新标题"})
        assert r.status_code == 200
        assert r.json()["title"] == "新标题"
        # manifest 双写同步
        import json as _json
        from dubforge.domain import crud
        m = _json.loads((crud.project_dir(uid) / "manifest.json").read_text("utf-8"))
        assert m["title"] == "新标题"
        # 空白标题 → 400 TITLE_EMPTY
        r = client.patch(f"/api/projects/{uid}", json={"title": "   "})
        assert r.status_code == 400
        assert r.json()["code"] == "TITLE_EMPTY"


def test_auto_title_local_uses_filename(isolated_env):
    with _client() as client:
        data = client.post("/api/projects", json={
            "source_kind": "local", "source_url": "D:/videos/我的旅行日记.mp4"}).json()
        assert data["title"] == "我的旅行日记"          # 文件名, 不是随机 uid
        client.delete(f"/api/projects/{data['uid']}")


def test_auto_title_link_fallback_uses_site_and_id(isolated_env):
    # 元数据取不到 (域名不存在) → 兜底「站点_ID」, 依然可读、非随机
    with _client() as client:
        data = client.post("/api/projects", json={
            "source_kind": "link",
            "source_url": "https://nonexistent.invalid/video/abc123"}).json()
        assert data["title"] == "nonexistent.invalid_abc123"
        assert data["source_kind"] == "link"
        client.delete(f"/api/projects/{data['uid']}")


def test_semantic_dir_name_and_manifest(isolated_env):
    """新项目目录名 = 日期_时间_标题 (Windows 非法字符被清理); manifest 完全自描述"""
    import json as _json
    import re as _re
    from dubforge import config
    from dubforge.domain import crud
    with _client() as client:
        p = client.post("/api/projects", json={
            "title": '我的 视频:第1集?', "source_kind": "local",
            "source_url": "D:/videos/v1.mp4"}).json()
        uid = p["uid"]
        pdir = crud.project_dir(uid)
        dirname = pdir.name
        assert _re.match(r"^\d{8}_\d{4}_", dirname)               # 日期_时间_ 前缀
        assert "我的 视频第1集" in dirname                         # 非法字符清理, 中文保留
        assert dirname != uid and not (config.projects_dir() / uid).exists()

        m = _json.loads((pdir / "manifest.json").read_text("utf-8"))
        assert m["dir_name"] == dirname
        assert m["source"]["note"]                                # 零拷贝引用语义说明
        assert "artifacts" in m and isinstance(m["artifacts"], dict)

        # 重命名只改展示名, 目录名保持稳定 (不破坏产物路径)
        client.patch(f"/api/projects/{uid}", json={"title": "完全不同的新名字"})
        assert crud.project_dir(uid).name == dirname

        # 删除: 项目目录整体消失 (外部引用文件另测)
        client.delete(f"/api/projects/{uid}")
        assert not pdir.exists()


def test_legacy_uid_dir_fallback(isolated_env):
    """旧项目 (dir_name 为空 + uid 目录) 沿用原布局 —— 升级兼容"""
    from dubforge.domain import crud
    p = crud.create_project(title="旧项目")
    uid = p.uid
    old = crud.project_dir(uid)         # 新语义化目录
    legacy = old.parent / uid
    old.rename(legacy)                  # 模拟旧布局
    row = crud.get_project(uid)
    row.dir_name = ""
    crud.save_project(row)
    assert crud.project_dir(uid) == legacy
    assert (legacy / "manifest.json").exists()
    crud.delete_project(uid)            # 删除也能正确定位旧目录
    assert not legacy.exists()


def test_data_root_config_resolution(isolated_env):
    """总工作目录: 环境变量优先; config server.data_root 次之; 默认 <repo>/data"""
    from dubforge import config
    # isolated_env 走环境变量 → 优先级最高
    assert Path(config.data_root()) == Path(os.environ["DUBFORGE_DATA_ROOT"])
    # 无环境变量时读 config (相对路径相对程序目录解析)
    config.set_key("server.data_root", "mydata")
    os.environ.pop("DUBFORGE_DATA_ROOT")
    try:
        assert config.data_root() == config.REPO_ROOT / "mydata"
    finally:
        os.environ["DUBFORGE_DATA_ROOT"] = str(isolated_env / "data")
        config.set_key("server.data_root", "")
    assert Path(config.data_root()) == Path(os.environ["DUBFORGE_DATA_ROOT"])


def test_delete_running_project_guard(isolated_env, stub_heavy_steps, make_project):
    """删除运行中项目: 默认拒绝 (PROJECT_BUSY); force 先取消等待再删"""
    from conftest import mark_upstream_done
    stub_heavy_steps("assemble")
    uid = make_project(params={"delay_step": {"assemble": 0.4}})
    mark_upstream_done(uid, "assemble")
    with _client() as client:
        client.post(f"/api/projects/{uid}/steps/assemble/run")
        r = client.delete(f"/api/projects/{uid}")
        assert r.status_code == 400 and r.json()["code"] == "PROJECT_BUSY"
        r = client.delete(f"/api/projects/{uid}?force=true")
        assert r.status_code == 200, r.json()
        rr = client.get(f"/api/projects/{uid}")
        assert rr.json()["code"] == "PROJECT_NOT_FOUND"


def test_run_chain_auto_continue(isolated_env, stub_heavy_steps, make_project):
    """勾选链: split 完成后自动续跑 translate, 链空后清空"""
    import time
    from conftest import mark_upstream_done
    stub_heavy_steps("split", "translate")
    uid = make_project()
    mark_upstream_done(uid, "split")
    with _client() as client:
        r = client.post(f"/api/projects/{uid}/run_chain",
                        json={"steps": ["split", "translate"]})
        assert r.status_code == 200, r.json()
        st = {}
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            st = {s["key"]: s["status"] for s in steps}
            if st["split"] == "done" and st["translate"] == "done":
                break
            time.sleep(0.05)
        assert st["split"] == "done" and st["translate"] == "done"
        p = client.get(f"/api/projects/{uid}").json()
        assert not p["params"].get("_chain")          # 链跑完清空


def test_run_chain_stops_on_failure(isolated_env, stub_heavy_steps, make_project):
    """链中步骤失败 → 自动续跑停止, _chain 清空"""
    import time
    from conftest import mark_upstream_done
    stub_heavy_steps("split", "translate")
    uid = make_project(params={"fail_step": "translate"})
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.post(f"/api/projects/{uid}/run_chain",
                    json={"steps": ["split", "translate"]})
        st = {}
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            st = {s["key"]: s["status"] for s in steps}
            if st["translate"] == "failed":
                break
            time.sleep(0.05)
        assert st["translate"] == "failed"
        p = client.get(f"/api/projects/{uid}").json()
        assert not p["params"].get("_chain")


def test_auto_steps_and_run_many(isolated_env, stub_heavy_steps, make_project):
    """勾选集持久化; run_many 按勾选链启动并自动续跑; 空链/已完成跳过"""
    import time
    from conftest import mark_upstream_done
    stub_heavy_steps("split", "translate")
    uid = make_project()
    mark_upstream_done(uid, "split")
    with _client() as client:
        client.put(f"/api/projects/{uid}/auto_steps",
                   json={"steps": ["split", "translate"]})
        r = client.post("/api/projects/run_many", json={"uids": [uid]})
        assert r.status_code == 200
        assert r.json()["results"][0]["started"] == "拆分句子"
        st = {}
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            st = {s["key"]: s["status"] for s in steps}
            if st["translate"] == "done":
                break
            time.sleep(0.05)
        assert st["translate"] == "done"
        # 全部完成 → 再 run_many 跳过
        r = client.post("/api/projects/run_many", json={"uids": [uid]})
        res = r.json()["results"][0]
        assert res.get("skipped") or "跳过" in res.get("started", "")


def test_project_step_params_layering(isolated_env):
    """项目级参数: 覆盖生效并标记 overridden; 与全局同值或 reset 后指纹恢复 ——
    「改参数→过期, 改回原值→过期自动撤销」语义 (指纹实时重算, 非固定事件)"""
    from fastapi.testclient import TestClient
    from app.main import app
    from dubforge.domain import crud
    from dubforge.runner import compute_fingerprint
    p = crud.create_project(title="参数项目")
    fp0 = compute_fingerprint(p, "preprocess")
    with TestClient(app) as client:
        j = client.get(f"/api/projects/{p.uid}/step_params").json()
        assert j["overridden"]["separate.model"] is False
        assert j["steps"]["preprocess"]["values"]["separate.model"] == "htdemucs"
        assert j["defaults"]["separate.model"] == "htdemucs"

        # 项目覆盖 → 生效值变化 + overridden 标记
        r = client.put(f"/api/projects/{p.uid}/step_params", json={
            "updates": {"separate.model": "htdemucs_ft"}, "reset": []})
        assert r.status_code == 200
        j = client.get(f"/api/projects/{p.uid}/step_params").json()
        assert j["overridden"]["separate.model"] is True
        assert j["steps"]["preprocess"]["values"]["separate.model"] == "htdemucs_ft"
    assert compute_fingerprint(crud.get_project(p.uid), "preprocess") != fp0

    # 覆盖值改回与全局相同 → 指纹立即恢复 (即使覆盖条目还在)
    with TestClient(app) as client:
        client.put(f"/api/projects/{p.uid}/step_params", json={
            "updates": {"separate.model": "htdemucs"}, "reset": []})
    assert compute_fingerprint(crud.get_project(p.uid), "preprocess") == fp0

    # 显式 reset 清掉覆盖条目 → 标记回到「跟随全局」, 指纹不变
    with TestClient(app) as client:
        client.put(f"/api/projects/{p.uid}/step_params", json={
            "updates": {}, "reset": ["separate.model"]})
        j = client.get(f"/api/projects/{p.uid}/step_params").json()
        assert j["overridden"]["separate.model"] is False
        assert j["steps"]["preprocess"]["values"]["separate.model"] == "htdemucs"
    assert compute_fingerprint(crud.get_project(p.uid), "preprocess") == fp0


def test_missing_dir_flag_and_delete(isolated_env, tmp_path):
    """目录被外部移走: 列表标 dir_exists=false; 删除仍成功 (跳过回收站只清登记)"""
    import shutil as _shutil
    from dubforge.domain import crud
    p = crud.create_project(title="目录丢失项目")
    _shutil.rmtree(crud.project_dir(p.uid))
    with _client() as client:
        j = client.get(f"/api/projects/{p.uid}").json()
        assert j["dir_exists"] is False
        assert client.delete(f"/api/projects/{p.uid}").status_code == 200
        assert crud.get_project(p.uid) is None


def test_ghost_project_cannot_run(isolated_env):
    """目录缺失的项目不允许启动 (否则运行会失败并"补回"空目录骨架)"""
    import shutil as _shutil
    from dubforge.domain import crud
    p = crud.create_project(title="幽灵项目")
    _shutil.rmtree(crud.project_dir(p.uid))
    with _client() as client:
        r = client.post(f"/api/projects/{p.uid}/steps/translate/run",
                        json={"force": False})
        assert r.status_code == 400
        assert r.json()["code"] == "PROJECT_DIR_MISSING"
    assert not crud.project_dir(p.uid).exists()      # 没有补回目录


def test_dir_name_trailing_space_regression(isolated_env):
    """回归: 40 字截断后残留尾部空格 → Windows mkdir 报"找不到路径", 项目创建 500。
    目录名必须剥掉首尾空白 (含全角), 且创建成功落登记。"""
    from dubforge.domain import crud
    title = "I'm Finally Playing Black Myth Wukong ｜ Black Myth Wukong 上手体验"
    p = crud.create_project(title=title)
    name = crud.project_dir(p.uid).name
    assert name == name.strip() and not name.endswith(" ")
    assert len(name.split("_", 2)[2]) <= 40         # 标题片段不超 40 且无尾部空白
    steps = crud.get_steps(p.id)
    assert len(steps) == 9                          # 登记完整 (骨架+步骤行都在)
