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


def test_segment_from_words_marks_soft_punctuation_anchor():
    """软标点先形成短语锚点，后续语义整理仍可根据上下文合并。"""
    from dubforge.pipeline.step_transcribe import _segment_from_words

    words = [
        {"start": 0.0, "end": 0.4, "word": "今天"},
        {"start": 0.4, "end": 0.8, "word": "天气，"},
        {"start": 0.8, "end": 1.2, "word": "很好。"},
    ]
    segs = _segment_from_words(words)
    assert len(segs) == 2
    assert segs[0][2] == "今天天气，"
    assert segs[1][2] == "很好。"


def test_segment_from_words_honors_fine_duration_anchor():
    """无标点连续语音也按粗句时长上限保留更细的时间锚点。"""
    from dubforge.pipeline.step_transcribe import _segment_from_words

    words = [
        {"start": float(i), "end": float(i + 0.8), "word": f"词{i}"}
        for i in range(6)
    ]
    segs = _segment_from_words(words, max_line=4.0)
    assert len(segs) == 2
    assert segs[0][2] == "词0词1词2词3"
    assert segs[1][2] == "词4词5"


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


def test_preprocess_and_transcribe_local(make_project, isolated_env, pdir, monkeypatch):
    """真实本地媒体链；只替换模型边界，不下载权重、不使用 GPU 或真实推理。"""
    import subprocess
    import sys
    from types import SimpleNamespace
    import torch
    from demucs import pretrained
    import demucs.apply
    import soundfile as sf
    from dubforge import config
    from dubforge.utils.srt import parse_srt

    calls = []
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)

    class ModelBoundary:
        samplerate = 44100
        segment = 1.

        def eval(self):
            return self

    def model(name):
        calls.append(('separation_model', name))
        return ModelBoundary()

    def apply_model(model, mix, **kwargs):
        assert mix.device.type == 'cpu' and kwargs['device'] == 'cpu'
        assert kwargs['split'] is True and kwargs['progress'] is False
        callback = kwargs['callback']
        for offset in (0, 44100):
            callback({'state': 'start', 'segment_offset': offset, 'shift_idx': 0})
            callback({'state': 'end', 'segment_offset': offset, 'shift_idx': 0})
        sources = torch.zeros((1, 4, *mix.shape[1:]), dtype=mix.dtype)
        sources[:, -1] = mix
        calls.append(('separation', mix.shape[-1]))
        return sources

    class WhisperBoundary:
        def __init__(self, name, **kwargs):
            assert kwargs == {'device': 'cpu', 'compute_type': 'int8'}
            calls.append(('transcribe_model', name))

        def transcribe(self, wav, **kwargs):
            info = sf.info(wav)
            assert info.samplerate == 16000 and info.channels == 1
            assert kwargs['word_timestamps'] and kwargs['vad_filter']
            calls.append(('transcribe', info.duration))
            return iter([SimpleNamespace(start=0., end=2., words=[
                SimpleNamespace(start=.1, end=.7, word='Hello'),
                SimpleNamespace(start=1.1, end=1.8, word=' world.'),
            ])]), SimpleNamespace(duration=info.duration, language='en')

    monkeypatch.setattr(pretrained, 'get_model', model)
    monkeypatch.setattr(demucs.apply, 'apply_model', apply_model)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=WhisperBoundary))
    config.update_config({'asr.model': 'isolated-asr-boundary', 'separate.model': 'isolated-separation-boundary'})
    # 音轨与全部文件封装仍由真实 FFmpeg / WAV 实现处理。
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
    assert tr["model"] == 'isolated-asr-boundary'
    assert tr["duration"] >= 2.0
    assert [line.text for line in parse_srt(src_srt)] == ['Hello world.']
    assert [row[0] for row in calls] == ['separation_model', 'separation', 'transcribe_model', 'transcribe']
    assert sf.info(audio / 'vocal.wav').samplerate == 44100
    assert sf.info(audio / 'background.wav').samplerate == 44100
    assert not list(pdir(uid).rglob('*.tmp'))
    before = {key: crud.latest_job(p.id, key).id for key in ('preprocess', 'transcribe')}
    saved = src_srt.read_bytes()
    for key in before:
        assert runner.start_step(uid, key)['status'] == 'skipped'
        assert crud.latest_job(p.id, key).id == before[key]
    assert src_srt.read_bytes() == saved and len(calls) == 4


def _runner():
    from dubforge.runner import runner
    return runner


def test_transcribe_phases_and_eta_exclude_model_preparation(make_project, pdir, monkeypatch):
    """可控模型逐批返回，覆盖加载、首批等待、真实进度和尾部静音的收尾。"""
    import sys
    import threading
    from types import SimpleNamespace

    from dubforge.domain import crud
    from dubforge.pipeline import step_transcribe
    from dubforge.runner import StepContext, runner

    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, "transcribe")
    step.status = "running"
    crud.save_step(step)
    ctx = StepContext(project, "transcribe", pdir(uid), pdir(uid) / "logs" / "transcribe.log", threading.Event())
    clock = [0.0]
    ctx.active_seconds = lambda: clock[0]
    ctx.upstream = lambda *args: pdir(uid) / "audio" / "raw.wav"
    monkeypatch.setitem(runner._ctxs, (project.id, "transcribe"), ctx)

    def visible():
        return runner.progress_eta(project.id, "transcribe")

    def segments():
        assert visible()["progress_phase"] == "识别首批语音，等待结果"
        assert visible()["progress_indeterminate"] is True
        assert ctx.eta_seconds() == 0
        clock[0] = 90
        yield SimpleNamespace(start=0, end=30, words=[SimpleNamespace(start=0, end=2, word="Hello.")])
        assert visible()["progress_indeterminate"] is False
        assert crud.get_step(project.id, "transcribe").progress == 24.8
        assert ctx.eta_seconds() == 30  # 仅识别耗时10秒，前80秒准备不计入速度。
        clock[0] = 95
        assert ctx.eta_seconds() == 25
        clock[0] = 100
        yield SimpleNamespace(start=30, end=60, words=[SimpleNamespace(start=30, end=32, word="World.")])
        assert ctx.eta_seconds() == 20

    class FakeWhisper:
        def __init__(self, *args, **kwargs):
            assert visible()["progress_phase"] == "加载语音识别模型"
            assert visible()["progress_indeterminate"] is True
            clock[0] = 60
            assert visible()["elapsed_seconds"] == 60

        def transcribe(self, *args, **kwargs):
            assert visible()["progress_phase"] == "准备音频并检测人声与语言"
            clock[0] = 80
            return segments(), SimpleNamespace(duration=120, language="en")

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisper))
    step_transcribe.run(ctx)
    assert visible()["progress_phase"] == "整理并保存字幕与词级时间戳"
    assert crud.get_step(project.id, "transcribe").progress == 99
    assert ctx.eta_seconds() == 0
    assert [line.text for line in parse_srt(pdir(uid) / "srt" / "src.srt")] == ["Hello.", "World."]
