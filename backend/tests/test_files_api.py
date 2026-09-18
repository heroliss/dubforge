# -*- coding: utf-8 -*-
"""本地文件 API: 目录浏览 + 拖入导入 (ingest)"""
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


def test_ingest_into_project_dir(isolated_env):
    """拖入导入: 内容直接写进项目目录 media/, source_url 记项目内相对路径"""
    from dubforge.domain import crud
    from dubforge.pipeline.video_source import resolve_video
    with _client() as client:
        p = client.post("/api/projects", json={
            "title": "拖入项目", "source_kind": "local", "source_url": ""}).json()
        uid = p["uid"]
        r = client.post(f"/api/projects/{uid}/ingest",
                        params={"filename": "我的视频.mp4"}, content=b"datadata")
        assert r.status_code == 200
        assert r.json()["file"] == "media/我的视频.mp4"

        # 副本就在项目目录内, 且 resolve_video 能按相对 source_url 定位
        hits = list((crud.config.projects_dir()).glob("*/media/我的视频.mp4"))
        assert len(hits) == 1
        project = crud.get_project(uid)
        assert project.source_url == "media/我的视频.mp4"
        assert resolve_video(project, crud.project_dir(uid)) == hits[0]

        # 删项目 → 副本随项目目录一起消失
        assert client.delete(f"/api/projects/{uid}").status_code == 200
        assert not hits[0].exists()
        assert not (crud.config.data_root() / "uploads").exists()


def test_ingest_replaces_previous_copy(isolated_env):
    """换文件重传: 清掉旧视频副本, 保持一个项目一个源视频"""
    from dubforge.domain import crud
    with _client() as client:
        p = client.post("/api/projects", json={
            "title": "重传项目", "source_kind": "local"}).json()
        uid = p["uid"]
        assert client.post(f"/api/projects/{uid}/ingest",
                           params={"filename": "a.mp4"}, content=b"old").status_code == 200
        assert client.post(f"/api/projects/{uid}/ingest",
                           params={"filename": "b.mkv"}, content=b"new").status_code == 200
        media = crud.project_dir(uid) / "media"
        assert [f.name for f in media.iterdir()] == ["b.mkv"]


def test_ingest_guards(isolated_env, tmp_path):
    """不存在 / 空内容 / 链接项目 → 明确报错"""
    with _client() as client:
        assert client.post("/api/projects/nope/ingest",
                           params={"filename": "a.mp4"}, content=b"x").status_code == 400
        p = client.post("/api/projects", json={
            "title": "链接项目", "source_kind": "link", "source_url": "https://x.com/v"}).json()
        r = client.post(f"/api/projects/{p['uid']}/ingest",
                        params={"filename": "a.mp4"}, content=b"x")
        assert r.status_code == 400
        assert r.json()["code"] == "INGEST_KIND_INVALID"
        p2 = client.post("/api/projects", json={
            "title": "空内容", "source_kind": "local"}).json()
        r2 = client.post(f"/api/projects/{p2['uid']}/ingest",
                         params={"filename": "a.mp4"}, content=b"")
        assert r2.status_code == 400
        assert r2.json()["code"] == "INGEST_EMPTY"


def test_external_file_survives_project_delete(isolated_env, tmp_path):
    """引用的外部视频 (项目目录外) 删项目时绝不受伤"""
    from dubforge import config
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"original")
    with _client() as client:
        p = client.post("/api/projects", json={
            "title": "引用项目", "source_kind": "local",
            "source_url": str(src)}).json()
        assert client.delete(f"/api/projects/{p['uid']}").status_code == 200
    assert src.read_bytes() == b"original"


def test_find_final_video_skips_partial(tmp_path):
    """yt-dlp 过程文件 (.part / .fNNN 单流中间产物) 不算源视频 —— 否则下载中途
    预览会拿到无音轨的纯视频流"""
    from dubforge.pipeline.video_source import find_final_video
    media = tmp_path / "media"
    media.mkdir()
    assert find_final_video(media) is None                       # 空目录
    (media / "video.f137.mp4.part").write_bytes(b"x")            # 下载中的单流
    (media / "video.f251.webm").write_bytes(b"x")                # 合并前的单流
    (media / "video.mp4.ytdl").write_bytes(b"x")
    assert find_final_video(media) is None                       # 全是过程文件 → None
    (media / "video.mp4").write_bytes(b"final")
    assert find_final_video(media) == media / "video.mp4"        # 完成后精确名优先
