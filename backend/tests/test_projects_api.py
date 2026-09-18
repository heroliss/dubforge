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
