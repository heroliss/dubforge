# -*- coding: utf-8 -*-
"""M3b 成片测试: 铺轨纯函数 + 合成样片端到端 (mock TTS → assemble 真实 ffmpeg)"""
import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import mark_upstream_done, wait_status
from dubforge.domain import crud
from dubforge.utils import audio as au


def test_mix_timeline(isolated_env):
    sr = au.SR
    clip_a = np.ones(sr, dtype=np.float32)            # 1s 满幅
    clip_b = np.zeros(sr // 2, dtype=np.float32) + 0.5
    mixed = au.mix_over_timeline([(0.0, clip_a), (2.0, clip_b)], total_seconds=4.0)
    assert len(mixed) == 4 * sr
    assert abs(mixed[0]) == 1.0                       # 句 a 起点
    assert abs(mixed[int(1.5 * sr)]) < 1e-6           # 间隙静音
    assert abs(mixed[int(2.2 * sr)] - 0.5) < 1e-6     # 句 b
    norm = au.peak_normalize(mixed, -6.0)
    assert abs(np.max(np.abs(norm)) - 10 ** (-6 / 20)) < 1e-3


def _make_fixture_media(uid_dir: Path):
    """合成 6s 视频 + 两句配音 wav (0.8s sine) + 背景音"""
    media = uid_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    video = media / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=200:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(video)], check=True)
    return video


def _mock_tts(monkeypatch, pdir: Path):
    """TTS 后端 mock: 按句号生成不同频率的 0.8s wav (32k mono)"""
    from dubforge.backends import tts_indextts25

    def fake_synth(url, text, out_path, lang, **kw):
        no = int(str(out_path).split(os.sep)[-1].split(".")[0])
        freq = 440 + no * 110
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.8",
                        "-ar", "32000", "-ac", "1", "-f", "wav",
                        str(out_path.with_suffix(".tmp"))], check=True)
        os.replace(out_path.with_suffix(".tmp"), out_path)
        return out_path

    monkeypatch.setattr(tts_indextts25, "synth", fake_synth)


def _seed_project(uid: str):
    """伪造 transcribe/split/dubtask 三级产物 (2 句, 5s 视频时间轴)"""
    project = crud.get_project(uid)
    pdir = crud.project_dir(uid)
    (pdir / "srt").mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts" / "dubtask").mkdir(parents=True, exist_ok=True)
    (pdir / "srt" / "transcribe.json").write_text(
        json.dumps({"language": "en", "duration": 6.0}), encoding="utf-8")
    crud.add_artifact(project.id, "transcribe", "json", "transcribe.json",
                      pdir / "srt" / "transcribe.json", "srt/transcribe.json")
    (pdir / "artifacts" / "split").mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts" / "split" / "split.json").write_text(
        json.dumps({"count": 2, "lines": [
            {"id": 1, "text": "第一句", "start": 0.5, "end": 2.0},
            {"id": 2, "text": "第二句", "start": 3.0, "end": 4.5}]}), encoding="utf-8")
    crud.add_artifact(project.id, "split", "json", "split.json",
                      pdir / "artifacts" / "split" / "split.json",
                      "artifacts/split/split.json")
    task = {"total_duration": 6.0, "tolerance": 0.4, "lines": [
        {"id": 1, "text": "第一句", "start": 0.5, "end": 2.0, "duration": 1.5,
         "est_dur": 1.0, "gap": 1.0, "speed_flag": 0},
        {"id": 2, "text": "第二句", "start": 3.0, "end": 4.5, "duration": 1.5,
         "est_dur": 1.0, "gap": 1.5, "speed_flag": 0}]}
    (pdir / "artifacts" / "dubtask" / "dubtask.json").write_text(
        json.dumps(task, ensure_ascii=False), encoding="utf-8")
    crud.add_artifact(project.id, "dubtask", "json", "dubtask.json",
                      pdir / "artifacts" / "dubtask" / "dubtask.json",
                      "artifacts/dubtask/dubtask.json")
    return pdir


def test_assemble_full_mux(make_project, isolated_env, monkeypatch):
    """端到端: mock TTS → tts → assemble (背景+ducking, 不烧录) → 成片存在"""
    uid = make_project()
    pdir = _seed_project(uid)
    _make_fixture_media(pdir)
    (pdir / "audio").mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=150:duration=6",
                    str(pdir / "audio" / "background.wav")], check=True)
    _mock_tts(monkeypatch, pdir)

    mark_upstream_done(uid, "tts")   # 手工播种产物越过依赖门禁
    for key in ("tts", "assemble"):
        _runner().start_step(uid, key)
        wait_status(uid, key, {"done"}, timeout=180)

    outs = list((pdir / "output").glob("final_*.mp4"))
    assert outs, "成片未生成"
    # 音频流存在且时长≈视频
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(outs[0])],
                       capture_output=True, text=True)
    assert abs(float(r.stdout.strip()) - 6.0) < 1.0


def test_assemble_burn_subtitles(make_project, isolated_env, monkeypatch):
    """烧录分支: 重编码 + 字幕滤镜跑通"""
    uid = make_project()
    pdir = _seed_project(uid)
    _make_fixture_media(pdir)
    _mock_tts(monkeypatch, pdir)
    # 需要 trans.srt
    (pdir / "srt" / "trans.srt").write_text(
        "1\n00:00:00,500 --> 00:00:02,000\n第一句\n\n"
        "2\n00:00:03,000 --> 00:00:04,500\n第二句\n", encoding="utf-8")
    from dubforge import config as cfgmod
    cfgmod.set_key("assemble.burn_subtitles", True)
    cfgmod.set_key("assemble.loudnorm", False)   # 加速测试

    mark_upstream_done(uid, "tts")   # 手工播种产物越过依赖门禁
    for key in ("tts", "assemble"):
        _runner().start_step(uid, key)
        wait_status(uid, key, {"done"}, timeout=180)
    assert list((pdir / "output").glob("final_*.mp4"))
    assert list((pdir / "output").glob("final_*.srt"))   # 伴生字幕 (trans.srt 存在)


def _runner():
    from dubforge.runner import runner
    return runner
