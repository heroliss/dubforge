"""M3b 成片测试: 铺轨纯函数 + 合成样片端到端 (mock TTS → assemble 真实 ffmpeg)"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import numpy as np
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
    from dubforge import config
    from dubforge.backends import tts_indextts25

    # 真实默认 preset 要求有效参考音频；桩只关心输出，因此放一个占位文件
    # 让参数校验通过，避免测试依赖 Path("") 恰好是当前目录的旧行为。
    ref = pdir / "tts_ref.wav"
    ref.write_bytes(b"test reference")
    config.set_key("tts.indextts25.timbre_audio", str(ref))
    config.set_key("tts.indextts25.emo_audio", str(ref))

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
    (pdir / "artifacts" / "split" / "split.txt").write_text(
        "1. 0.500-2.000: 第一句\n"
        "2. 3.000-4.500: 第二句\n", encoding="utf-8")
    crud.add_artifact(project.id, "split", "txt", "split.txt",
                      pdir / "artifacts" / "split" / "split.txt",
                      "artifacts/split/split.txt")
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
        _runner().start_assemble_outputs(uid,['dub']) if key=='assemble' else _runner().start_step(uid,key)
        wait_status(uid, key, {"done"}, timeout=180)

    outs = list((pdir / "output").glob("final_*.mp4"))
    assert outs, "成片未生成"
    # 默认 (不烧录) 组合命名: final_dub_<时间戳>.mp4
    assert all(re.fullmatch(r"final_dub_\d{8}_\d{6}_\d+\.mp4", o.name) for o in outs)
    # 音频流存在且时长≈视频
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(outs[0])],
                        capture_output=True, text=True, check=False)
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
    cfgmod.set_key("assemble.bgm_volume", 0)  # 此场景不制作背景音
    cfgmod.set_key("assemble.loudnorm", False)   # 加速测试

    mark_upstream_done(uid, "tts")   # 手工播种产物越过依赖门禁
    project = crud.get_project(uid)
    subtitle = pdir / 'srt/trans.srt'
    crud.add_artifact(project.id, 'subtitles', 'srt', subtitle.name, subtitle, 'srt/trans.srt')
    step = crud.get_step(project.id, 'subtitles')
    step.status = 'done'
    crud.save_step(step)
    for key in ("tts", "assemble"):
        _runner().start_assemble_outputs(uid,['dub_hardsub']) if key=='assemble' else _runner().start_step(uid,key)
        wait_status(uid, key, {"done"}, timeout=180)
    outs = list((pdir / "output").glob("final_*.mp4"))
    assert all(re.fullmatch(r"final_dub_hardsub_\d{8}_\d{6}_\d+\.mp4", o.name)
               for o in outs)
    assert (pdir / "srt" / "trans.srt").is_file()  # 外挂文件由字幕导出步骤独立提供


def _runner():
    from dubforge.runner import runner
    return runner


def test_assemble_preview_range_creates_short_clip_without_marking_full_output(
        make_project, isolated_env, monkeypatch):
    """区域试合成只生成 preview_*.mp4，且不把该步骤误标为正式成片最新。"""
    from dubforge import config
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner

    uid = make_project()
    pdir = _seed_project(uid)
    _make_fixture_media(pdir)
    _mock_tts(monkeypatch, pdir)
    config.set_key("assemble.bgm_volume", 0)
    config.set_key("assemble.loudnorm", False)
    config.set_key("assemble.burn_subtitles", True)
    subtitle = pdir / "srt" / "trans.srt"
    subtitle.write_text("1\n00:00:00,500 --> 00:00:02,000\n第一句\n\n"
                        "2\n00:00:03,000 --> 00:00:04,500\n第二句\n", encoding="utf-8")

    mark_upstream_done(uid, "tts")
    project = crud.get_project(uid)
    crud.add_artifact(project.id, "subtitles", "srt", subtitle.name, subtitle, "srt/trans.srt")
    subtitle_step = crud.get_step(project.id, "subtitles")
    subtitle_step.status = "done"
    crud.save_step(subtitle_step)
    runner.start_step(uid, "tts")
    wait_status(uid, "tts", {"done"}, timeout=180)
    assemble_step = crud.get_step(crud.get_project(uid).id, "assemble")
    assemble_step.input_fingerprint = "previous-full-run"
    crud.save_step(assemble_step)
    result = runner.start_assemble_preview(uid, 2.2, 4.2)
    assert result["status"] == "started"
    assert (result["start"], result["end"]) == (2.2, 4.2)
    wait_status(uid, "assemble", {"done"}, timeout=180)

    previews = list((pdir / "output").glob("preview_*.mp4"))
    assert len(previews) == 1
    assert not list((pdir / "output").glob("final_*.mp4"))
    assert not list((pdir / "srt").glob(".preview_*.srt"))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(previews[0])],
                           capture_output=True, text=True, check=True)
    assert abs(float(probe.stdout.strip()) - 2.0) < 0.35
    project = crud.get_project(uid)
    finished = crud.get_step(project.id, "assemble")
    assert finished.status == "done"
    assert finished.input_fingerprint == "previous-full-run"
    assert not Dependencies(project).fresh("assemble")


def test_preview_srt_is_clamped_and_shifted(tmp_path, isolated_env):
    from dubforge.pipeline.step_assemble import _write_preview_srt
    from dubforge.utils.srt import SubLine, parse_srt, write_srt

    source = tmp_path / "source.srt"
    write_srt([
        SubLine(1, 0, 1, "before"),
        SubLine(2, 1.5, 3.5, "first\nsecond"),
        SubLine(3, 4.1, 5, "after"),
    ], source)
    target = tmp_path / "preview.srt"
    _write_preview_srt(source, 2.0, 4.0, target)
    rows = parse_srt(target)
    assert [(row.start, row.end, row.text) for row in rows] == [(0.0, 1.5, "first\nsecond")]


def _prepare_outputs_project(make_project, monkeypatch):
    """结算组合用例公共前置: 2 句工程 + 视频 + TTS 桩 + 字幕产物登记。"""
    uid = make_project()
    pdir = _seed_project(uid)
    _make_fixture_media(pdir)
    _mock_tts(monkeypatch, pdir)
    from dubforge import config
    config.set_key("assemble.bgm_volume", 0)
    config.set_key("assemble.loudnorm", False)
    config.set_key("assemble.burn_subtitles", False)
    subtitle = pdir / "srt" / "trans.srt"
    subtitle.write_text("1\n00:00:00,500 --> 00:00:02,000\n第一句\n\n"
                        "2\n00:00:03,000 --> 00:00:04,500\n第二句\n", encoding="utf-8")
    mark_upstream_done(uid, "tts")
    project = crud.get_project(uid)
    crud.add_artifact(project.id, "subtitles", "srt", subtitle.name, subtitle,
                      "srt/trans.srt")
    step = crud.get_step(project.id, "subtitles")
    step.status = "done"
    crud.save_step(step)
    return uid, pdir


def test_assemble_outputs_combos_replace_current_output(
        make_project, isolated_env, monkeypatch):
    """不同组合并存，同组合替换文件并保持产物 ID，不无限增加文件和记录。"""
    runner = _runner()
    uid, pdir = _prepare_outputs_project(make_project, monkeypatch)
    runner.start_step(uid, "tts")
    wait_status(uid, "tts", {"done"}, timeout=180)

    runner.start_assemble_outputs(uid, ["dub", "dub_hardsub"])
    wait_status(uid, "assemble", {"done"}, timeout=300)
    names = [p.name for p in (pdir / "output").iterdir()]
    assert any(re.fullmatch(r"final_dub_\d{8}_\d{6}_\d+\.mp4", n) for n in names)
    assert any(re.fullmatch(r"final_dub_hardsub_\d{8}_\d{6}_\d+\.mp4", n) for n in names)
    assert (pdir / "audio" / "dub_full.wav").is_file()

    # 补生成原声烧录版，不影响另外两个组合。
    runner.start_assemble_outputs(uid, ["src_hardsub"])
    wait_status(uid, "assemble", {"done"}, timeout=300)
    names = [p.name for p in (pdir / "output").iterdir()]
    assert any(re.fullmatch(r"final_src_hardsub_\d{8}_\d{6}_\d+\.mp4", n) for n in names)
    assert any(re.fullmatch(r"final_dub_\d{8}_\d{6}_\d+\.mp4", n) for n in names)
    # 原声版保留视频自带音轨
    src_out = next(p for p in (pdir / "output").iterdir()
                   if re.fullmatch(r"final_src_hardsub_\d{8}_\d{6}_\d+\.mp4", p.name))
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            str(src_out)], capture_output=True, text=True, check=True)
    assert "audio" in probe.stdout

    project = crud.get_project(uid)
    before = crud.latest_artifacts(project.id, ['assemble'])
    original = before[f'assemble/{src_out.name}']
    runner.start_assemble_outputs(uid, ['src_hardsub'])
    wait_status(uid, 'assemble', {'done'}, timeout=300)
    assert sorted(p.name for p in (pdir / 'output').iterdir()) == sorted(names)
    after = crud.latest_artifacts(project.id, ['assemble'])
    assert len(after) == len(before)
    assert after[f'assemble/{src_out.name}'].id == original.id
    assert after[f'assemble/{src_out.name}'].created_at > original.created_at

    # 新版失败时，不覆盖上次成品，也不遗留暂存文件。
    from dubforge.pipeline import step_assemble
    from dubforge.errors import DubForgeError
    previous_bytes = src_out.read_bytes()
    def fail(ctx, video, out, *args, **kwargs):
        out.write_bytes(b'incomplete-video')
        raise DubForgeError('模拟封装失败', code='FFMPEG_FAIL')
    monkeypatch.setattr(step_assemble, '_mux_original_sub', fail)
    runner.start_assemble_outputs(uid, ['src_hardsub'])
    wait_status(uid, 'assemble', {'failed'}, timeout=30)
    assert src_out.read_bytes() == previous_bytes
    assert sorted(p.name for p in (pdir / 'output').iterdir()) == sorted(names)
    assert crud.latest_artifacts(project.id, ['assemble'])[f'assemble/{src_out.name}'].id == original.id


def test_assemble_outputs_rejects_invalid_variants(make_project, isolated_env):
    """原片组合、未知组合、空清单一律拒绝, 不启动任务。"""
    from dubforge.errors import DubForgeError
    runner = _runner()
    uid = make_project()
    for bad in (["src"], [], ["dub", "nonsense"], "dub"):
        with pytest.raises(DubForgeError) as exc:
            runner.start_assemble_outputs(uid, bad)
        assert exc.value.code == "ASSEMBLE_VARIANTS"
