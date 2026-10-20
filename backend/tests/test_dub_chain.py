"""M3a 配音链测试: dubtask 估算纯函数 / mock 合成全链 / workbench API"""
import json
import subprocess
import wave
from pathlib import Path

import pytest

from conftest import mark_upstream_done, synthesize, wait_status
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


def _run_until(uid, key, statuses=None, timeout=60):
    statuses = statuses or {"done"}
    mark_upstream_done(uid, key)   # 链式测试手工播种产物, 上游状态直接置 done 越过门禁
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


@pytest.mark.parametrize("global_value,project_value,expected", [
    (None, None, 0.4), (0, None, 0), (0.8, None, 0.8), (None, 0, 0),
])
def test_dubtask_recovers_null_tolerance_and_preserves_zero(make_project, global_value, project_value, expected):
    from app.api.projects import get_project_step_params
    from dubforge import config
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import compute_fingerprint

    config.set_key("tts.tolerance", global_value)
    uid = make_project(params={"step_params": {"tts.tolerance": project_value}})
    mark_upstream_done(uid, "dubtask")
    p = crud.get_project(uid)
    view = get_project_step_params(uid)
    assert 'tts.tolerance' not in view["steps"]["dubtask"]["values"]
    assert 'tts.tolerance' not in view["steps"]["tts"]["values"]
    assert Dependencies(p).cfg("tts.tolerance") == expected
    if project_value is None:
        assert 'tts.tolerance' not in view["overridden"]
    fingerprint = compute_fingerprint(p, "dubtask")
    _runner().start_step(uid, "dubtask")
    wait_status(uid, "dubtask", {"done"})
    path = crud.project_dir(uid) / "artifacts" / "dubtask" / "dubtask.json"
    assert json.loads(path.read_text(encoding="utf-8"))["tolerance"] == expected
    assert Dependencies(crud.get_project(uid)).fresh("dubtask")
    if global_value is None:
        config.set_key("tts.tolerance", 0.4)
        assert compute_fingerprint(p, "dubtask") == fingerprint


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize("value", [None, "", "abc", True])
def test_numeric_parameter_validation_rejects_bad_batch_before_writing(make_project, scope, value):
    from fastapi.testclient import TestClient
    from app.main import app
    from dubforge import config

    uid = make_project()
    url = "/api/settings" if scope == "global" else f"/api/projects/{uid}/step_params"
    method = "post" if scope == "global" else "put"
    with TestClient(app) as client:
        response = getattr(client, method)(url, json={"updates": {
            "translate.style_prompt": "不应部分保存", "tts.tolerance": value}})
        assert response.status_code == 400
        assert response.json()["code"] == "CONFIG_ERROR"
        assert "时长偏差容差" in response.json()["message"]
        assert config.get("translate.style_prompt") == ""
        from dubforge.configuration import resolve
        assert not resolve(uid)['overrides']
        response = getattr(client, method)(url, json={"updates": {"tts.tolerance": 0}})
        assert response.status_code == 200
        assert (config.get("tts.tolerance") if scope == "global" else
                crud.project_step_params(crud.get_project(uid))["tts.tolerance"]) == 0


def _mock_backend(monkeypatch, ref_dir: Path | None = None, fail_ids=()):
    """mock IndexTTS synth: 生成 0.8s 正弦 wav; 指定句可失败"""
    from dubforge.backends import tts_indextts25
    if ref_dir is not None:
        from test_configuration import save
        ref = ref_dir / "tts_ref.wav"
        with wave.open(str(ref), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(b'\0\0' * 32000)
        project = next(p for p in crud.list_projects()
                       if crud.project_dir_by_project(p).resolve() == ref_dir.resolve())
        save('project:' + project.uid, {'tts.indextts25.timbre_audio': str(ref),
                                       'tts.indextts25.emo_audio': str(ref)})

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
    # 桩已经提供全部语音输出，服务探测也必须隔离，不能尝试启动真实模型。
    monkeypatch.setattr(tts_indextts25, "is_online", lambda *args, **kwargs: True)


import os


def test_dubtask_and_tts_chain(make_project, isolated_env, monkeypatch, pdir):
    # 本测试聚焦配音链; 语义整理走本地规则 (未配置 LLM 也不再静默回退)
    from dubforge import config as _cfg
    _cfg.update_config({'split.use_llm': False})
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 3.0, "Hello world this is a test"),
                           (3.5, 6.0, "Second line for dubbing")])
    _mock_backend(monkeypatch, crud.project_dir(uid))
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
    from dubforge import config as _cfg
    _cfg.update_config({'split.use_llm': False})
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 3.0, "Hello world this is a test"),
                           (3.5, 6.0, "Second line for dubbing")])
    _mock_backend(monkeypatch, crud.project_dir(uid))
    for key in ("split", "dubtask"):
        assert _run_until(uid, key) == "done"

    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get(f"/api/projects/{uid}/workbench")
        assert r.status_code == 200
        d = r.json()
        assert d["counts"]["total"] == 2
        assert d["lines"][0]["wav"]["exists"] is False

        # 单句合成 (mock 后端)
        r = synthesize(client, f"/api/projects/{uid}/workbench/lines/1/synthesize",
                        json={"force": True})
        assert r.json()["status"] == "started"
        assert client.get(f"/api/projects/{uid}/workbench?probe=false").json()["lines"][0]["wav"]["seconds"] > 0

        # 标记满意/取消
        r = client.post(f"/api/projects/{uid}/workbench/lines/1/confirm",
                        json={"confirmed": True})
        assert r.json()["count"] == 1
        d = client.get(f"/api/projects/{uid}/workbench").json()
        assert d["lines"][0]["confirmed"] is True
        assert d["counts"]["confirmed"] == 1

        # 单句试配是配音页内的操作，不再是额外步骤或确认门禁。
        steps = client.get(f"/api/projects/{uid}/steps").json()
        keys = [s["key"] for s in steps]
        assert "workbench" not in keys and "dubtask" not in keys
        assert keys.index("tts") < keys.index("assemble")
        assert steps[keys.index("tts")]["label"] == "字幕与配音"
        assert keys.index('tts') < keys.index('subtitles') < keys.index('assemble')

        # 音频流
        r = client.get(f"/api/projects/{uid}/workbench/audio/line/1")
        assert r.status_code == 200
        # 不存在的句
        r = synthesize(client, f"/api/projects/{uid}/workbench/lines/99/synthesize", json={})
        assert r.status_code in (400, 404)    # 业务错误统一 400 (句号不存在)


def test_workbench_confirm_requires_existing_project(isolated_env):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.post("/api/projects/missing/workbench/lines/1/confirm",
                        json={"confirmed": True})
        assert r.status_code == 400
        assert r.json()["code"] == "PROJECT_NOT_FOUND"


def test_pipeline_contains_only_one_dubbing_step(make_project, isolated_env):
    """逐句与批量合成共享一个配音步骤。"""
    uid = make_project()
    from app.main import app
    from dubforge.domain import crud
    from fastapi.testclient import TestClient

    mark_upstream_done(uid, "assemble")
    project = crud.get_project(uid)
    tts = crud.get_step(project.id, "tts")
    tts.status = "done"
    crud.save_step(tts)
    with TestClient(app) as client:
        steps = client.get(f"/api/projects/{uid}/steps").json()
    assert not any(s["key"] in {"workbench", "dubtask"} for s in steps)
    assert next(s for s in steps if s["key"] == "tts")["status"] == "done"
