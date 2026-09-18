# -*- coding: utf-8 -*-
"""M3a 配音链测试: dubtask 估算纯函数 / mock 合成全链 / workbench API"""
import json
import subprocess
import time

from conftest import wait_status
from dubforge.domain import crud
from dubforge.pipeline.step_dubtask import estimate_seconds, speed_flag
from dubforge.utils.srt import SubLine, write_srt


def _seed_transcribe(uid, lines, total=None):
    project = crud.get_project(uid)
    srt_dir = crud.project_dir(uid) / "srt"
    write_srt([SubLine(i + 1, s, e, t) for i, (s, e, t) in enumerate(lines)],
              srt_dir / "src.srt")
    words = []
    for s, e, t in lines:
        n = max(1, len(t.split()))
        span = (e - s) / n
        for j, w in enumerate(t.split()):
            words.append({"start": s + j * span, "end": s + (j + 1) * span, "word": w + " "})
    (srt_dir / "words.json").write_text(json.dumps(words), encoding="utf-8")
    total = total or max(e for _, e, _ in lines)
    (srt_dir / "transcribe.json").write_text(
        json.dumps({"language": "en", "duration": total, "lines": len(lines)}),
        encoding="utf-8")
    for name, kind in (("src.srt", "srt"), ("words.json", "json"),
                       ("transcribe.json", "json")):
        f = srt_dir / name
        crud.add_artifact(project.id, "transcribe", kind, name, f, f"srt/{name}")


def _run_until(uid, key, statuses={"done"}, timeout=60):
    _runner().start_step(uid, key)
    return wait_status(uid, key, statuses, timeout=timeout)


def _runner():
    from dubforge.runner import runner
    return runner


def test_estimate_and_speed_flagisolated_env():
    # 10 个 CJK 字 ≈ 2.13s; 10 个英文词 ≈ 3.33s
    assert 2.0 < estimate_seconds("一二三四五六七八九十") < 2.3
    assert 3.0 < estimate_seconds("one two three four five six seven eight nine ten") < 3.6
    assert speed_flag(5, 4, 0.4) == 1          # 需加速
    assert speed_flag(6, 4, 0.4) == 2          # 严重超速
    assert speed_flag(3.7, 4, 0.4) == 0        # 健康
    assert speed_flag(3, 4, 0.4) == -1         # 偏慢 (低于自然语速下限)
    assert speed_flag(2, 5, 0.4) == -1


def _mock_backend(monkeypatch, fail_ids=()):
    """mock IndexTTS synth: 生成 0.8s 正弦 wav; 指定句可失败"""
    from dubforge.backends import tts_indextts25

    def fake_synth(url, text, out_path, lang, **kw):
        if kw.get("line_id") in fail_ids:
            raise RuntimeError("mock 注入失败")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                        "-ar", "32000", "-ac", "1", "-f", "wav", str(out_path)],
                       check=True)
        return out_path

    # 包装原签名 (line_id 由 step_tts 通过 kwargs 传入? — 实现走 log, 这里简化:
    # 以 out_path 名判断句号)
    def synth_wrapper(url, text, out_path, lang, **kw):
        no = int(str(out_path).split(os.sep)[-1].split(".")[0])
        if no in fail_ids:
            raise RuntimeError("mock 注入失败")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                        "-ar", "32000", "-ac", "1", "-f", "wav",
                        str(out_path.with_suffix(".tmp"))], check=True)
        os.replace(out_path.with_suffix(".tmp"), out_path)
        return out_path

    monkeypatch.setattr(tts_indextts25, "synth", synth_wrapper)


import os  # noqa: E402


def test_dubtask_and_tts_chain(make_project, isolated_env, monkeypatch, pdir):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 3.0, "Hello world this is a test"),
                           (3.5, 6.0, "Second line for dubbing")])
    _mock_backend(monkeypatch)
    for key in ("split", "dubtask", "tts"):
        assert _run_until(uid, key) == "done"

    pdir = pdir(uid)
    task = json.loads((pdir / "artifacts" / "dubtask" / "dubtask.json")
                      .read_text(encoding="utf-8"))
    assert task["lines"][0]["est_dur"] > 0
    assert task["lines"][1]["gap"] >= 0
    # 逐句 wav
    tts_dir = pdir / "artifacts" / "tts"
    assert (tts_dir / "1.wav").exists() and (tts_dir / "2.wav").exists()

    # 句级缓存: 全部已存在 → tts 重跑所有句 skip
    runner = _runner()
    r = runner.start_step(uid, "tts", force=True)
    wait_status(uid, "tts", {"done"})
    assert r["status"] == "started"            # force 仍执行, 但每句 skipped


def test_workbench_api(make_project, isolated_env, monkeypatch):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 3.0, "Hello world this is a test"),
                           (3.5, 6.0, "Second line for dubbing")])
    _mock_backend(monkeypatch)
    for key in ("split", "dubtask"):
        assert _run_until(uid, key) == "done"

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get(f"/api/projects/{uid}/workbench")
        assert r.status_code == 200
        d = r.json()
        assert d["counts"]["total"] == 2
        assert d["lines"][0]["wav"]["exists"] is False

        # 单句合成 (mock 后端)
        r = client.post(f"/api/projects/{uid}/workbench/lines/1/synthesize",
                        json={"force": True})
        assert r.json()["ok"] is True
        assert r.json()["duration"] is not None

        # 采纳/取消
        r = client.post(f"/api/projects/{uid}/workbench/lines/1/confirm",
                        json={"confirmed": True})
        assert r.json()["count"] == 1
        d = client.get(f"/api/projects/{uid}/workbench").json()
        assert d["lines"][0]["confirmed"] is True
        assert d["counts"]["confirmed"] == 1

        # 音频流
        r = client.get(f"/api/projects/{uid}/workbench/audio/line/1")
        assert r.status_code == 200
        # 不存在的句
        r = client.post(f"/api/projects/{uid}/workbench/lines/99/synthesize", json={})
        assert r.status_code in (400, 404)    # 业务错误统一 400 (句号不存在)
