"""真实预设文件的往返、项目参数优先级和合成参数传递。"""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from dubforge import config, indextts_presets as presets
from dubforge.errors import ConfigError, PresetError
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates

P = presets.PREFIX


@pytest.fixture()
def preset_env(isolated_env, monkeypatch, tmp_path):
    root = tmp_path / "index-tts"
    root.mkdir()
    (root / "webui.py").touch()
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "DubForge")
    config.set_key(P + "project_dir", str(root))
    voice = tmp_path / "voice.wav"
    emotion = tmp_path / "emotion.wav"
    voice.write_bytes(b"different voice audio")
    emotion.write_bytes(b"different emotion audio")
    values = {P + k: copy.deepcopy(v) for k, v in config.DEFAULTS["tts"]["indextts25"].items()}
    values.update({P + "timbre_audio": str(voice), P + "emo_audio": str(emotion),
                   P + "emo_vector": [0.2, 0, 0, 0, 0, 0, 0.1, 0],
                   P + "lang": "EN", P + "duration_factor": 1.2})
    return root, values


@pytest.mark.parametrize("method,mode", [(0, "same"), (1, "preset"), (2, "vector"), (3, "text")])
def test_webui_native_preset_mapping_is_read_only(preset_env, method, mode):
    root, _ = preset_env
    folder = root / "outputs/presets/原生预设"
    folder.mkdir(parents=True)
    (folder / "voice.wav").write_bytes(b"voice")
    (folder / "emotion.wav").write_bytes(b"emotion")
    payload = {"version": "1.0", "prompt_audio": "voice.wav", "emo_audio": "emotion.wav",
               "emo_control_method": method, "emo_weight": 0.7, "emo_vector": [0.1] * 8,
               "emo_text": "轻快", "emo_random": True, "advanced_params": {"temperature": 1.1}}
    (folder / "preset.json").write_text(json.dumps(payload), encoding="utf-8")
    before = config.config_path().read_bytes()
    loaded = presets.load("shared", "原生预设", str(root))["values"]
    assert loaded[P + "emo_mode"] == mode
    assert loaded[P + "temperature"] == 1.1
    assert loaded[P + "emo_audio"] != loaded[P + "timbre_audio"]
    assert P + "lang" not in loaded and P + "duration_factor" not in loaded
    assert config.config_path().read_bytes() == before


@pytest.mark.parametrize("storage", ["local", "shared"])
def test_roundtrip_copies_audio_and_all_voice_parameters(preset_env, storage):
    root, values = preset_env
    values.update({P + "do_sample": False, P + "top_k": 12, P + "temperature": 1.2,
                   P + "emo_text": "好奇", P + "emo_random": True})
    result = presets.save(storage, "双参考", values, project_dir=str(root))
    path = Path(result["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == "1.0" and payload["emo_control_method"] == 1
    assert payload["prompt_audio"] != payload["emo_audio"]
    assert not Path(payload["prompt_audio"]).is_absolute()
    assert (path.parent / payload["prompt_audio"]).read_bytes() == b"different voice audio"
    assert "url" not in payload["dubforge"] and "project_dir" not in payload["dubforge"]
    loaded = presets.load(storage, "双参考", str(root))["values"]
    for key in (*presets.ADVANCED, *presets.EXTENSION, "emo_weight", "emo_vector", "emo_text", "emo_random"):
        assert loaded[P + key] == values[P + key]
    Path(values[P + "timbre_audio"]).unlink()
    assert Path(presets.load(storage, "双参考", str(root))["values"][P + "timbre_audio"]).is_file()


def test_overwrite_requires_choice_and_failed_commit_preserves_old(preset_env, monkeypatch):
    root, values = preset_env
    path = Path(presets.save("shared", "保留", values, project_dir=str(root))["path"])
    before = path.read_bytes()
    with pytest.raises(PresetError) as exc:
        presets.save("shared", "保留", values, project_dir=str(root))
    assert exc.value.code == "PRESET_EXISTS"
    original = presets.atomic_write_bytes
    def failing(target, data):
        if Path(target).name == "preset.json":
            raise OSError("模拟中断")
        return original(target, data)
    monkeypatch.setattr(presets, "atomic_write_bytes", failing)
    Path(values[P + "timbre_audio"]).write_bytes(b"new voice")
    with pytest.raises(PresetError, match="模拟中断"):
        presets.save("shared", "保留", values, project_dir=str(root), overwrite=True)
    assert path.read_bytes() == before
    assert Path(presets.load("shared", "保留", str(root))["values"][P + "timbre_audio"]).read_bytes() == b"different voice audio"


@pytest.mark.parametrize("name", ["../outside", "..", "CON", "nul.wav", "x/y", "x.", "x "])
def test_unsafe_names_rejected(preset_env, name):
    root, values = preset_env
    with pytest.raises(PresetError):
        presets.save("shared", name, values, project_dir=str(root))


def test_preset_cannot_read_audio_outside_its_directory(preset_env):
    root, values = preset_env
    path = Path(presets.save("shared", "路径", values, project_dir=str(root))["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["prompt_audio"] = values[P + "timbre_audio"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PresetError, match="目录之外"):
        presets.load("shared", "路径", str(root))


def test_dynamic_reference_is_local_only(preset_env):
    root, values = preset_env
    values.update({P + "timbre_mode": "dynamic", P + "emo_mode": "stable"})
    with pytest.raises(PresetError, match="视频原声"):
        presets.save("shared", "视频参考", values, project_dir=str(root))
    presets.save("local", "视频参考", values, project_dir=str(root))
    loaded = presets.load("local", "视频参考", str(root))["values"]
    assert loaded[P + "timbre_mode"] == "dynamic" and loaded[P + "emo_mode"] == "stable"


@pytest.mark.parametrize("value", [[0] * 7, [float('nan')] * 8, [1.1] * 8, [None] * 8, "0,0,0,0,0,0,0,0"])
def test_bad_vector_rejected_before_saving(value):
    with pytest.raises(ConfigError):
        validate_numeric_updates({P + "emo_vector": value})


def test_all_effective_indextts_settings_have_controls_and_help():
    fields = {f.path: f for f in STEP_PARAM_SCHEMA["tts"]}
    for key in config.DEFAULTS["tts"]["indextts25"]:
        if key not in {"source_preset", "preset_baseline"}:
            assert P + key in fields, key
            assert fields[P + key].help, key


def test_new_default_emotion_fields_preserve_existing_audio_fingerprint(make_project, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline.step_tts import line_fingerprint
    from dubforge.runner import compute_fingerprint
    uid = make_project()
    project = crud.get_project(uid)
    line = {"id": 1, "text": "你好", "start": 0, "end": 1}
    current = line_fingerprint(project, line), compute_fingerprint(project, "tts")
    old = copy.deepcopy(config.DEFAULTS["tts"]["indextts25"])
    old.pop("emo_text")
    old.pop("emo_random")
    monkeypatch.setitem(config.DEFAULTS["tts"], "indextts25", old)
    assert (line_fingerprint(project, line), compute_fingerprint(project, "tts")) == current


def test_api_load_save_do_not_change_project_or_global_and_saved_values_win(preset_env, make_project, monkeypatch):
    from app.main import app
    from dubforge.domain import crud
    from dubforge.pipeline import step_tts
    root, values = preset_env
    values[P + "emo_mode"] = "vector"
    uid = make_project(params={"step_params": {P + "temperature": 0.4}})
    client = TestClient(app)
    before = config.config_path().read_bytes()
    saved = client.post("/api/workbench-presets/save", json={"name": "候选", "storage": "shared", "project_dir": str(root), "values": values})
    assert saved.status_code == 200, saved.json()
    loaded = client.post("/api/workbench-presets/load", json={"name": "候选", "storage": "shared", "project_dir": str(root)}).json()["values"]
    assert crud.project_step_params(crud.get_project(uid))[P + "temperature"] == 0.4
    # 载入后继续调整表单，再保存；运行时只读取最后保存的值。
    loaded[P + "temperature"] = 1.3
    assert client.put(f"/api/projects/{uid}/step_params", json={"updates": loaded}).status_code == 200
    seen = {}
    def synth(url, text, out, lang, **kwargs):
        seen.update(kwargs)
        import io
        import wave
        from dubforge.utils.fs import atomic_write_bytes
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as stream:
            stream.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 32000)
        atomic_write_bytes(out, audio.getvalue())
    monkeypatch.setattr(step_tts.tts_indextts25, "synth", synth)
    project = crud.get_project(uid)
    step_tts.synth_line(project, {"id": 1, "start": 0, "end": 2, "text": "你好"}, crud.project_dir(uid))
    assert seen["advanced"]["temperature"] == 1.3
    assert seen["emo_vector"] == loaded[P + "emo_vector"]
    assert config.config_path().read_bytes() == before


@pytest.mark.parametrize("mode,label", [("vector", "使用情感向量控制"), ("text", "使用情感描述文本控制"), ("same", "与音色参考音频相同")])
def test_gradio_receives_explicit_current_parameters(preset_env, monkeypatch, tmp_path, mode, label):
    import gradio_client
    from dubforge.backends import tts_indextts25 as backend
    output = tmp_path / "returned.wav"
    output.write_bytes(b"audio")
    client = Mock()
    client.submit.return_value.status.return_value.progress_data = None
    client.submit.return_value.result.return_value = str(output)
    monkeypatch.setattr(gradio_client, "Client", Mock(return_value=client))
    monkeypatch.setattr(gradio_client, "handle_file", lambda path: path)
    monkeypatch.setattr(backend, "is_online", lambda url: True)
    def ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"converted")
    monkeypatch.setattr(backend.subprocess, "run", ffmpeg)
    backend.synth("http://localhost:7860", "你好", tmp_path / "final.wav", "ZH", timbre_ref=output,
                  emo_mode=mode, emo_text="开心", emo_random=True, emo_vector=[0.3] * 8,
                  advanced={"temperature": 1.1})
    args = client.submit.call_args.kwargs
    client.predict.assert_not_called()
    client.close.assert_called_once()
    assert args["emo_control_method"] == label
    assert args["emo_ref_path"] is None
    assert args["emo_text"] == ("开心" if mode == "text" else "")
    assert args["vec1"] == (0.3 if mode == "vector" else 0)
    assert args["param_21"] == 1.1
