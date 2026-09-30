"""获取视频页的来源元数据与中文视频信息整理。"""
import json

import pytest

from conftest import wait_status
from dubforge.domain import crud
from dubforge.pipeline import step_download
from dubforge import video_info


def _runner():
    from dubforge.runner import runner
    return runner


class FakeCtx:
    def __init__(self, output):
        self.output = output

    def log(self, message):
        pass

    def run_subprocess(self, cmd, on_stdout=None, on_stderr=None):
        for line in self.output.splitlines():
            if on_stdout:
                on_stdout(line)
        return 0, self.output, ""


def test_run_ytdlp_collects_structured_source_meta(monkeypatch):
    """JSON 标量输出要能解析多行描述，且不影响原标题解析。"""
    monkeypatch.setattr(step_download, "_ytdlp_cmd", lambda *args, **kwargs: [])
    output = "\n".join([
        "DUBFORGE_META_TITLE:\"Source Title\"",
        "DUBFORGE_META_UPLOADER:\"Author\"",
        "DUBFORGE_META_UPLOAD_DATE:\"20260102\"",
        "DUBFORGE_META_DURATION:183",
        "DUBFORGE_META_VIEW_COUNT:1234",
        "DUBFORGE_META_WEBPAGE_URL:\"https://example.com/watch?v=abc\"",
        "DUBFORGE_META_EXTRACTOR:\"youtube\"",
        "DUBFORGE_META_DESCRIPTION:\"first\\nsecond\"",
        "D:\\media\\video.mp4",
        "Source Title",
    ])
    info = step_download._run_ytdlp(FakeCtx(output), "https://example.com/watch?v=abc", "v")
    assert info["title"] == "Source Title"
    assert info["source_meta"]["description"] == "first\nsecond"
    source = video_info._source_meta(info["source_meta"],
                                        "https://example.com/watch?v=abc")
    assert source["platform"] == "YouTube"
    assert source["upload_date"] == "2026-01-02"
    assert source["view_count"] == 1234


def _fake_ytdlp(media):
    def run(ctx, url, template):
        from pathlib import Path
        media.mkdir(parents=True, exist_ok=True)
        video = Path(template.replace("%(ext)s", "mp4"))
        video.write_bytes(b"video")
        return {
            "title": "Source Title",
            "filepath": str(video),
            "source_meta": {
                "id": "abc", "title": "Source Title", "uploader": "Author",
                "upload_date": "20260102", "duration": 183, "view_count": 1234,
                "webpage_url": url, "extractor": "youtube",
                "description": "A useful description.",
            },
        }
    return run


def test_download_generates_video_info(make_project, isolated_env, monkeypatch, pdir):
    """网络视频下载后应生成中文标题、概要、来源和作者描述产物。"""
    uid = make_project()
    media = pdir(uid) / "media"
    calls = []

    def fake_chat(prompt, **kwargs):
        calls.append(prompt)
        return {"title_zh": "中文标题", "summary_zh": "这是可确认的内容概要。"}

    monkeypatch.setattr(step_download, "_run_ytdlp", _fake_ytdlp(media))
    monkeypatch.setattr(step_download, "_validate_download", lambda *args: None)
    monkeypatch.setattr(video_info, "chat_json", fake_chat)
    runner = _runner()
    runner.start_step(uid, "download")
    wait_status(uid, "download", {"done"})

    info_path = pdir(uid) / "artifacts" / "download" / "video_info.json"
    assert info_path.is_file()
    info = json.loads(info_path.read_text(encoding="utf-8"))
    assert info["title_original"] == "Source Title"
    assert info["title_zh"] == "中文标题"
    assert info["summary_zh"] == "这是可确认的内容概要。"
    assert info["description"] == "A useful description."
    assert info["summary_status"] == "generated"
    assert info["source"]["platform"] == "YouTube"
    assert info["source"]["uploader"] == "Author"
    assert calls

    project = crud.get_project(uid)
    art = crud.latest_artifacts(project.id, ["download"])["download/video_info.json"]
    assert art.name == "video_info.json"

    # 已有视频跳过下载；信息文件被误删时，仍应补生成并登记，避免要求强制重下。
    info_path.unlink()
    calls.clear()
    runner.start_step(uid, "download")
    wait_status(uid, "download", {"done"})
    assert info_path.is_file()
    assert calls


def test_download_video_info_survives_llm_failure(make_project, isolated_env, monkeypatch, pdir):
    """LLM 不可用是增强能力失败，不能让已经完成的视频获取变成失败。"""
    uid = make_project()
    media = pdir(uid) / "media"

    def fail_chat(*args, **kwargs):
        raise RuntimeError("LLM offline")

    monkeypatch.setattr(step_download, "_run_ytdlp", _fake_ytdlp(media))
    monkeypatch.setattr(step_download, "_validate_download", lambda *args: None)
    monkeypatch.setattr(video_info, "chat_json", fail_chat)
    runner = _runner()
    runner.start_step(uid, "download")
    wait_status(uid, "download", {"done"})
    info = json.loads((pdir(uid) / "artifacts" / "download" / "video_info.json")
                      .read_text(encoding="utf-8"))
    assert info["title_zh"] == "Source Title"
    assert info["summary_zh"] == ""
    assert info["summary_status"] == "llm_unavailable"
