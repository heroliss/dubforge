import time
# -*- coding: utf-8 -*-
import time
"""M2a 媒体链测试: SRT 读写 / 断句纯函数 / download 幂等与去重档案"""
import json

from conftest import wait_status
from dubforge.utils.srt import SubLine, parse_srt, write_srt


def test_srt_roundtrip(isolated_env):
    lines = [SubLine(1, 0.5, 2.1, "你好世界"), SubLine(2, 2.5, 5.0, "Hello world")]
    p = isolated_env / "t.srt"
    write_srt(lines, p)
    parsed = parse_srt(p)
    assert len(parsed) == 2
    assert parsed[0].text == "你好世界"
    assert abs(parsed[1].start - 2.5) < 1e-6
    assert parsed[1].text == "Hello world"


def test_segment_from_wordsisolated_env():
    from dubforge.pipeline.step_transcribe import _segment_from_words
    # 词序列: 停顿 1.2s 切句 + 句末标点切句
    words = [
        {"start": 0.0, "end": 0.4, "word": "今天"},
        {"start": 0.4, "end": 0.9, "word": "天气"},
        {"start": 2.1, "end": 2.6, "word": "很好"},        # gap 1.2s > 0.8 → 切
        {"start": 2.6, "end": 3.0, "word": "啊。"},        # 标点 → 切
    ]
    segs = _segment_from_words(words)
    assert len(segs) == 2
    assert segs[0][2] == "今天天气"
    assert segs[1][2] == "很好啊。"


def test_download_local_import_and_idempotent(make_project, isolated_env, pdir):
    """本地导入: 零拷贝 —— 原文件就地引用, 项目里不落副本; 重跑不碰原文件"""
    import subprocess
    from pathlib import Path
    src = isolated_env / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(src)], check=True)

    from dubforge.domain import crud
    p = crud.create_project(title="本地导入测试", source_kind="local",
                            source_url=str(src))
    uid = p.uid
    runner = _runner()
    runner.start_step(uid, "download")
    wait_status(uid, "download", {"done"})

    pdir = pdir(uid)
    assert not list((pdir / "media").glob("video.*"))       # 零拷贝: media/ 无副本
    mtime = src.stat().st_mtime
    arts = crud.latest_artifacts(p.id, ["download"])
    art = next(a for a in arts.values() if a.name == "video.mp4")
    assert Path(art.rel_path).is_absolute()                 # 外部引用登记绝对路径
    assert Path(art.rel_path) == src
    meta = json.loads((pdir / "artifacts" / "download" / "meta.json")
                      .read_text(encoding="utf-8"))
    assert meta["file"] == "sample.mp4"

    # force 重跑会先 delete_step_artifacts: 外部引用文件绝不能被删
    runner.start_step(uid, "download", force=True)
    wait_status(uid, "download", {"done"})
    assert src.stat().st_mtime == mtime
    assert not list((pdir / "media").glob("video.*"))


def test_preprocess_and_transcribe_local(make_project, isolated_env, pdir):
    """本地样片全链: download → preprocess → transcribe (demucs+whisper 真跑, 1s 样片)"""
    import subprocess
    # 2.5s 语音样片: 440Hz 正弦 (whisper 会输出空/噪声句, 断言的是链路与产物形态)
    src = isolated_env / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=2.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(src)], check=True)

    from dubforge.domain import crud
    p = crud.create_project(title="媒体链测试", source_kind="local",
                            source_url=str(src))
    uid = p.uid
    runner = _runner()
    for key in ("download", "preprocess"):
        runner.start_step(uid, key)
        wait_status(uid, key, {"done"}, timeout=300)

    audio = pdir(uid) / "audio"
    assert (audio / "raw.wav").exists()
    assert (audio / "vocal.wav").exists()
    assert (audio / "background.wav").exists()

    runner.start_step(uid, "transcribe")
    wait_status(uid, "transcribe", {"done"}, timeout=600)
    src_srt = pdir(uid) / "srt" / "src.srt"
    assert src_srt.exists()
    tr = json.loads((pdir(uid) / "artifacts"
                     / "transcribe" / "transcribe.json").read_text(encoding="utf-8"))
    assert tr["model"]
    assert tr["duration"] >= 2.0


def _runner():
    from dubforge.runner import runner
    return runner
