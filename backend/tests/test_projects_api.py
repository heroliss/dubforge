# -*- coding: utf-8 -*-
"""项目 API: 改名 / 标题自动命名 (链接兜底 & 本地文件名)"""
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
