# -*- coding: utf-8 -*-
"""本地文件 API: 目录浏览 + 拖入上传"""
from fastapi.testclient import TestClient


def _client():
    from app.main import app
    return TestClient(app)


def test_list_drives_and_dir(isolated_env, tmp_path):
    with _client() as client:
        # 空路径 → 盘符列表
        r = client.get("/api/files/list")
        assert r.status_code == 200
        assert r.json()["drives"] is True

        (tmp_path / "子目录").mkdir()
        (tmp_path / "movie.mp4").write_bytes(b"x")
        (tmp_path / "note.txt").write_bytes(b"y")
        r = client.get("/api/files/list", params={"path": str(tmp_path)})
        entries = r.json()["entries"]
        # 目录全部排在文件前
        first_file = next(i for i, e in enumerate(entries) if not e["is_dir"])
        last_dir = max(i for i, e in enumerate(entries) if e["is_dir"])
        assert last_dir < first_file
        movie = next(e for e in entries if e["name"] == "movie.mp4")
        assert movie["video"] is True
        note = next(e for e in entries if e["name"] == "note.txt")
        assert note["video"] is False
        assert r.json()["parent"]                      # 有上级


def test_list_bad_path(isolated_env):
    with _client() as client:
        r = client.get("/api/files/list", params={"path": "Z:/不存在目录"})
        assert r.status_code == 400
        assert r.json()["code"] == "PATH_NOT_FOUND"


def test_upload_into_uploads(isolated_env):
    with _client() as client:
        r = client.post("/api/files/upload",
                        files={"file": ("我的视频.mp4", b"datadata")})
        assert r.status_code == 200
        j = r.json()
        assert j["name"] == "我的视频.mp4"
        assert j["path"].endswith("我的视频.mp4")
        # 同名再传 → 自动加序号, 不覆盖
        r2 = client.post("/api/files/upload",
                         files={"file": ("我的视频.mp4", b"2")})
        assert r2.json()["name"] == "我的视频_1.mp4"
