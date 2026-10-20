"""真实转录产物写入链配合本地模型桩，不调用ASR、VAD或外部服务。"""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from dubforge.pipeline import step_transcribe
from dubforge.pipeline.semantic_recognition_hints import recognition_hints
from dubforge.utils.srt import parse_srt


@pytest.fixture(autouse=True)
def isolated_transcribe_evidence_environment(isolated_env):
    """纯函数测试也不继承真实项目或配置环境。"""


def context(uid, pdir):
    from dubforge.domain import crud
    from dubforge.runner import StepContext

    project = crud.get_project(uid)
    step = crud.get_step(project.id, "transcribe")
    step.status = "running"
    crud.save_step(step)
    root = pdir(uid)
    raw = root / "audio" / "raw.wav"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"isolated fake audio; model stub never reads it")
    ctx = StepContext(project, "transcribe", root, root / "logs" / "transcribe.log",
                      threading.Event())
    ctx.upstream = lambda key, name: raw
    return ctx


def install_model(monkeypatch, segments):
    from dubforge import config, environment

    calls = []

    class FakeWhisper:
        def __init__(self, *args, **kwargs):
            calls.append(("init", args, kwargs))

        def transcribe(self, *args, **kwargs):
            calls.append(("transcribe", args, kwargs))
            assert kwargs == dict(language=None, word_timestamps=True, vad_filter=True,
                                  vad_parameters={"min_silence_duration_ms": 500})
            return iter(segments), SimpleNamespace(duration=8, language="en")

    monkeypatch.setattr(config, "ensure_hf_env", lambda: None)
    monkeypatch.setattr(environment, "require_component", lambda key: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisper))
    return calls


def test_real_run_preserves_optional_evidence_and_full_text_for_hints(make_project, pdir, monkeypatch):
    segments = [
        SimpleNamespace(id=7, end=0, no_speech_prob=.9, avg_logprob=-2.1,
                        words=[SimpleNamespace(start=0, end=0, word=" I", probability=.2)]),
        SimpleNamespace(id=8, end=2.2, no_speech_prob=.1, avg_logprob=-.2,
                        words=[SimpleNamespace(start=2, end=2.2, word=" Yes!", probability=.8)]),
    ]
    calls = install_model(monkeypatch, segments)
    ctx = context(make_project(), pdir)
    step_transcribe.run(ctx)

    words_path = ctx.pdir / "artifacts" / "transcribe" / "words.json"
    saved_bytes = words_path.read_bytes()
    words = json.loads(words_path.read_text(encoding="utf-8"))
    assert words == [
        dict(start=0, end=0, word=" I", probability=.2, no_speech_prob=.9,
             avg_logprob=-2.1, segment_id=7),
        dict(start=2, end=2.2, word=" Yes!", probability=.8, no_speech_prob=.1,
             avg_logprob=-.2, segment_id=8),
    ]
    lines = parse_srt(ctx.pdir / "srt" / "src.srt")
    assert [line.text for line in lines] == ["I", "Yes!"]
    assert lines[0].end == .4 and words[0]["end"] == 0  # 显示锚点不冒充有效词时长。
    assert len(recognition_hints(lines, words)[1]) == 2
    assert 2 not in recognition_hints(lines, words)
    assert words_path.read_bytes() == saved_bytes  # 提示 helper 不改产物。
    assert [call[0] for call in calls] == ["init", "transcribe"]
    assert not list(ctx.pdir.rglob("*.tmp"))


def test_real_run_omits_nonfinite_or_absent_optional_fields(make_project, pdir, monkeypatch):
    segments = [
        SimpleNamespace(id=1, end=.2, no_speech_prob=float("nan"), avg_logprob=float("inf"),
                        words=[SimpleNamespace(start=0, end=.2, word=" Yes!", probability=float("nan"))]),
        SimpleNamespace(end=2.2, words=[SimpleNamespace(start=2, end=2.2, word=" Hello.")]),
    ]
    install_model(monkeypatch, segments)
    ctx = context(make_project(), pdir)
    step_transcribe.run(ctx)
    words = json.loads((ctx.pdir / "artifacts" / "transcribe" / "words.json").read_text(encoding="utf-8"))
    assert words == [dict(start=0, end=.2, word=" Yes!", segment_id=1),
                     dict(start=2, end=2.2, word=" Hello.")]
    assert recognition_hints(parse_srt(ctx.pdir / "srt" / "src.srt"), words) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.1, True, ".2"])
def test_invalid_probabilities_are_not_serialized(value):
    assert step_transcribe._recognition_evidence(
        SimpleNamespace(probability=value), SimpleNamespace(no_speech_prob=value)) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), .1, True, "-.2"])
def test_invalid_log_probability_is_not_serialized(value):
    assert step_transcribe._recognition_evidence(SimpleNamespace(),
                                                SimpleNamespace(avg_logprob=value)) == {}


def test_platform_subtitles_never_fabricate_word_or_confidence_evidence(make_project, pdir, monkeypatch):
    from dubforge.publishing import imports

    calls = install_model(monkeypatch, [])
    ctx = context(make_project(), pdir)
    original_cfg = ctx.cfg
    ctx.cfg = lambda key, default=None: "platform_subtitle" if key == "asr.input_mode" else original_cfg(key, default)
    monkeypatch.setattr(imports, "selected", lambda root: dict(language="en", bvid="BVtest", cid=1,
                        lines=[dict(start=1, end=1.4, text="Yes!")]))
    step_transcribe.run(ctx)
    words = json.loads((ctx.pdir / "artifacts" / "transcribe" / "words.json").read_text(encoding="utf-8"))
    assert words == []
    assert calls == []
    assert recognition_hints(parse_srt(ctx.pdir / "srt" / "src.srt"), words) == {}
