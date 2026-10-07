"""产物依赖与按需工作流；所有数据均由隔离夹具提供，不调用外部模型。"""
import json
import hashlib

import pytest

from conftest import mark_upstream_done, wait_status
from dubforge import config
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.dependencies import Dependencies, project_status
from dubforge.runner import compute_fingerprint, runner


def seed(uid, step, name, text, rel=None):
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / (rel or f"artifacts/{step}/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    crud.add_artifact(project.id, step, "text", name, path, str(path.relative_to(root)))
    row = crud.get_step(project.id, step)
    row.status = "done"
    crud.save_step(row)
    return path


def source_project(make_project):
    uid = make_project(params={"auto_steps": ["subtitles"], "step_params": {
        "text.source": "transcribe", "subtitles.mode": "source"}})
    seed(uid, "transcribe", "src.srt", "1\n00:00:00,000 --> 00:00:00,200\nHello\n\n"
         "2\n00:00:01,000 --> 00:00:02,000\nWorld\n", "srt/src.srt")
    seed(uid, "split", "split.txt", "1. 0.000-0.200: Corrected hello\n2. 1.000-2.000: World\n")
    return uid


def test_source_export_uses_semantic_result_without_translation(make_project):
    uid = source_project(make_project)
    runner.start_step(uid, "subtitles")
    assert wait_status(uid, "subtitles", {"done"}) == "done"
    p = crud.get_project(uid)
    assert crud.get_step(p.id, "split").status == "done"
    assert crud.get_step(p.id, "translate").status == "pending"
    assert project_status(p) == "done"
    source = crud.project_dir(uid) / "srt" / "source.srt"
    assert "00:00:01,000" in source.read_text(encoding="utf-8")
    assert "Corrected hello" in source.read_text(encoding="utf-8")
    assert "00:00:00,200" in (source.parent / "src.srt").read_text(encoding="utf-8")
    assert not (source.parent / "trans.srt").exists()


def test_minimal_source_plan(make_project):
    config.set_key("text.source", "transcribe")
    config.set_key("subtitles.mode", "source")
    config.set_key("separate.enabled", False)
    p = crud.get_project(make_project())
    plan = Dependencies(p).plan(["subtitles"], True)
    assert plan["steps"] == ["download", "preprocess", "transcribe", "split", "subtitles"]
    assert not plan["missing"]
    assert not plan["blockers"]


def test_editor_sync_plan_updates_upstream_and_both_outputs(make_project):
    """上游失效后，字幕与配音的一次同步计划必须包含导出与逐句配音。"""
    uid = make_project(params={"auto_steps": ["subtitles", "tts"]})
    mark_upstream_done(uid, "tts")
    runner.start_step(uid, "subtitles")
    wait_status(uid, "subtitles", {"done"})
    project = crud.get_project(uid)
    for key in ("split", "translate"):
        row = crud.get_step(project.id, key)
        row.status = "done"
        row.input_fingerprint = compute_fingerprint(project, key)
        crud.save_step(row)
    tts = crud.get_step(project.id, "tts")
    tts.status = "done"
    tts.input_fingerprint = compute_fingerprint(project, "tts")
    crud.save_step(tts)

    # 让真正的上游识别结果变化：split 及其全部下游都必须进入同步计划。
    root = crud.project_dir(uid)
    source_path = root / "srt/src.srt"
    source_path.write_text("1\n00:00:00,000 --> 00:00:01,500\nchanged hello\n",
                           encoding="utf-8")
    crud.add_artifact(project.id, "transcribe", "text", "src.srt", source_path,
                      str(source_path.relative_to(root)))
    split = crud.get_step(project.id, "split")
    split.input_fingerprint = "previous-run"
    crud.save_step(split)

    plan = Dependencies(crud.get_project(uid)).plan(["subtitles", "tts"], True)
    assert plan["steps"] == ["split", "translate", "tts", "subtitles"]
    assert not plan["blockers"]
    assert any(i["step"] == "split" and i["state"] == "stale" for i in plan["requirements"])
    assert any(i["step"] == "translate" and i["state"] == "stale" for i in plan["requirements"])


def test_minimal_dubbing_plan_skips_subtitle_export(make_project):
    config.set_key("assemble.bgm_volume", 0)
    config.set_key("separate.enabled", False)
    p = crud.get_project(make_project())
    deps = Dependencies(p)
    assert "subtitles" not in deps.plan(["assemble"], True)["steps"]
    assert deps.inputs("tts") == deps.dubbing_inputs()
    assert "dubtask" not in deps.plan(["assemble"], True)["steps"]
    config.set_key("tts.indextts25.timbre_mode", "dynamic")
    assert Dependencies(p).plan(["tts"], True)["blockers"][0]["name"] == "vocal.wav"


def test_config_conflict_is_visible_before_running(make_project):
    config.set_key("assemble.burn_subtitles", True)
    config.set_key("assemble.subtitle_track", "bilingual")
    config.set_key("subtitles.mode", "source")
    p = crud.get_project(make_project())
    plan = Dependencies(p).plan(["assemble"], True)
    assert any(i["name"] == "bilingual.srt" and i["state"] == "blocked"
               and "请调整" in i["reason"] for i in plan["blockers"])
    assert not plan["missing"]


def test_missing_file_message_does_not_list_unrelated_steps(make_project):
    uid = make_project()
    with pytest.raises(DubForgeError) as exc:
        runner.start_step(uid, "dubtask")
    assert "translate.txt" in exc.value.message
    assert "split.txt" in exc.value.message
    assert "字幕导出" not in exc.value.message


def test_unrelated_settings_and_outputs_do_not_change_tts_fingerprint(make_project):
    uid = make_project()
    mark_upstream_done(uid, "tts")
    p = crud.get_project(uid)
    before = compute_fingerprint(p, "tts")
    config.set_key("subtitles.source_first", True)
    config.set_key("subtitles.min_duration", 2)
    seed(uid, "subtitles", "bilingual.srt", "新字幕")
    assert compute_fingerprint(p, "tts") == before
    assert Dependencies(p).downstream("subtitles") == []
    config.set_key("assemble.burn_subtitles", True)
    assert Dependencies(p).downstream("subtitles") == ["assemble"]


def test_stale_translation_blocks_dubbing(make_project):
    from fastapi import Response
    from app.api.steps import list_steps
    from app.api.projects import RunChain, run_chain

    uid = make_project()
    mark_upstream_done(uid, "tts")
    p = crud.get_project(uid)
    step = crud.get_step(p.id, "translate")
    step.input_fingerprint = compute_fingerprint(p, "translate")
    crud.save_step(step)
    config.set_key("translate.style_prompt", "使用自然的口语表达")
    deps = Dependencies(p)
    assert not deps.missing("tts")
    stale, = deps.unready("tts")
    assert stale["name"] == "translate.txt" and stale["state"] == "stale"
    assert "过期" in stale["reason"]
    plan = deps.plan(["tts"])
    assert not plan["missing"]
    assert plan["stale"] == plan["blockers"]
    assert plan["stale"][0]["name"] == "translate.txt"
    prepared = deps.plan(["tts"], True)
    assert prepared["added"] == ["translate"]
    assert prepared["requirements"] == [stale]
    assert not prepared["blockers"]
    view = next(s for s in list_steps(uid, Response()) if s["key"] == "tts")
    assert not view["missing_inputs"]
    assert view["unready_inputs"] == [stale]
    # 过期不能因不再属于缺失列表而绕过执行门禁。
    for run in (lambda: deps.require("tts"),
                lambda: run_chain(uid, RunChain(steps=["tts"]))):
        with pytest.raises(DubForgeError) as exc:
            run()
        assert exc.value.code == "DEPS_NOT_MET"
        assert "过期" in exc.value.message and "缺少" not in exc.value.message


@pytest.mark.parametrize("change,state", [
    ("none", "ready"), ("delete", "deleted"), ("edit", "stale"),
    ("stale", "stale"), ("running", "updating"),
])
def test_existing_dependencies_keep_distinct_states(make_project, change, state):
    uid = make_project()
    path = seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n")
    p = crud.get_project(uid)
    if change == "delete":
        path.unlink()
    elif change == "edit":
        path.write_text("修改后的原文", encoding="utf-8")
    elif change in {"stale", "running"}:
        row = crud.get_step(p.id, "split")
        row.status = change
        crud.save_step(row)
    deps = Dependencies(p)
    item, = deps.input_view("translate")
    assert item["state"] == state
    assert item["ready"] == (state == "ready")
    assert bool(deps.missing("translate")) == (state == "deleted")
    assert bool(deps.unready("translate")) == (state != "ready")


def test_plan_separates_missing_and_stale_dependencies(make_project):
    uid = make_project()
    seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n")
    p = crud.get_project(uid)
    row = crud.get_step(p.id, "split")
    row.status = "stale"
    crud.save_step(row)
    plan = Dependencies(p).plan(["dubtask"])
    assert len(plan["blockers"]) == 2
    assert [(i["name"], i["state"]) for i in plan["missing"]] == [("translate.txt", "missing")]
    assert [(i["name"], i["state"]) for i in plan["stale"]] == [("split.txt", "stale")]


def test_dubbing_uses_translation_and_original_timing(make_project):
    uid = make_project(params={"step_params": {"text.source": "transcribe"}})
    mark_upstream_done(uid, "dubtask")
    seed(uid, "split", "split.txt", "1. 0.000-0.200: hello\n2. 1.000-2.000: world\n")
    seed(uid, "translate", "translate.txt", "1. 你好\n2. 世界\n")
    runner.start_step(uid, "dubtask")
    wait_status(uid, "dubtask", {"done"})
    task = json.loads((crud.project_dir(uid) / "artifacts/dubtask/dubtask.json").read_text(encoding="utf-8"))
    assert task["lines"][0]["text"] == "你好"
    assert task["lines"][0]["end"] == 0.2
    assert task["lines"][0]["gap"] == 0.8


def test_dubbing_rejects_missing_translation_lines(make_project):
    uid = make_project()
    mark_upstream_done(uid, "dubtask")
    seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n2. 1.000-2.000: world\n")
    seed(uid, "translate", "translate.txt", "1. 你好\n")
    runner.start_step(uid, "dubtask")
    wait_status(uid, "dubtask", {"failed"})
    assert crud.get_step(crud.get_project(uid).id, "dubtask").error_code == "TEXT_ALIGNMENT"


def test_export_tracks_order_no_hard_wrap_and_remove_unselected(make_project):
    uid = make_project()
    mark_upstream_done(uid, "subtitles")
    seed(uid, "transcribe", "src.srt", "1\n00:00:00,000 --> 00:00:01,000\nhello\n", "srt/src.srt")
    seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n")
    target = "这是完整的一句译文" * 20
    seed(uid, "translate", "translate.txt", f"1. {target}\n")
    runner.start_step(uid, "subtitles")
    wait_status(uid, "subtitles", {"done"})
    root = crud.project_dir(uid) / "srt"
    assert (root / "trans.srt").read_text(encoding="utf-8").splitlines()[2] == target
    assert (root / "bilingual.srt").read_text(encoding="utf-8").splitlines()[2:4] == [target, "hello"]
    config.set_key("subtitles.source_first", True)
    runner.start_step(uid, "subtitles")
    wait_status(uid, "subtitles", {"done"})
    assert (root / "bilingual.srt").read_text(encoding="utf-8").splitlines()[2:4] == ["hello", target]
    config.set_key("subtitles.mode", "source")
    runner.start_step(uid, "subtitles")
    wait_status(uid, "subtitles", {"done"})
    assert (root / "source.srt").exists() and not (root / "trans.srt").exists()
    assert (root / "src.srt").exists()


def test_legacy_recognition_cache_remains_usable(make_project):
    uid = make_project()
    mark_upstream_done(uid, "split")
    p = crud.get_project(uid)
    step = crud.get_step(p.id, "transcribe")
    step.input_fingerprint = compute_fingerprint(p, "transcribe", legacy=True)
    crud.save_step(step)
    assert Dependencies(p).fresh("transcribe")
    assert runner.start_step(uid, "transcribe")["status"] == "skipped"
    config.set_key("asr.model", "different")
    assert not Dependencies(p).fresh("transcribe")


def test_missing_voice_and_same_size_changed_input(make_project):
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n2. 1.000-2.000: world\n")
    seed(uid, "translate", "translate.txt", "1. 你好\n2. 世界\n")
    # 模拟已经选用的配音文件丢失，不能悄悄改为原声。
    from dubforge.pipeline.line_settings import identity
    project = crud.get_project(uid)
    params = crud.project_params(project)
    line = Dependencies(project).dubbing_lines()[1]
    params['dubbing_edit'] = {'2': {'identity': identity(line), 'source': 'dub', 'take': 1}}
    project.params = json.dumps(params)
    crud.save_project(project)
    deps = Dependencies(crud.get_project(uid))
    assert any(i["name"] == "2.wav" for i in deps.missing("assemble"))
    path = deps.path("split", "split.txt")
    path.write_bytes(path.read_bytes().replace(b"hello", b"world"))
    with pytest.raises(DubForgeError, match="内容已变化"):
        deps.require("translate")


def test_non_contiguous_selected_chain_and_status(make_project, stub_heavy_steps):
    from app.api.projects import RunChain, run_chain, _auto_chain_of
    uid = make_project(params={"auto_steps": ["translate", "tts"]})
    mark_upstream_done(uid, "translate")
    stub_heavy_steps("translate", "tts")
    assert _auto_chain_of(crud.get_project(uid)) == ["translate", "tts"]
    result = run_chain(uid, RunChain(steps=["translate", "tts"]))
    assert result["started"] == "translate"
    wait_status(uid, "tts", {"done"})
    assert crud.get_step(crud.get_project(uid).id, "subtitles").status == "pending"
    assert project_status(crud.get_project(uid)) == "done"


def test_partial_success_and_unselected_failure(make_project):
    uid = source_project(make_project)
    runner.start_step(uid, "subtitles")
    wait_status(uid, "subtitles", {"done"})
    p = crud.get_project(uid)
    step = crud.get_step(p.id, "tts")
    step.status = "failed"
    crud.save_step(step)
    assert project_status(p) == "done"


def test_line_cache_tracks_voice_settings_not_subtitle_settings(make_project):
    from dubforge.pipeline.step_tts import line_fingerprint
    p = crud.get_project(make_project())
    line = {"id": 1, "start": 0, "end": 1, "text": "你好"}
    fp = line_fingerprint(p, line)
    config.set_key("subtitles.source_first", True)
    assert line_fingerprint(p, line) == fp
    config.set_key("tts.indextts25.emo_weight", 0.5)
    assert line_fingerprint(p, line) != fp


def test_manual_voice_artifacts_can_bypass_batch_step(make_project):
    from dubforge.pipeline.step_tts import line_fingerprint
    uid = make_project()
    mark_upstream_done(uid, "tts")
    config.set_key("assemble.bgm_volume", 0)
    p = crud.get_project(uid)
    deps = Dependencies(p)
    lines = deps.dubbing_lines(required=True)
    for line in lines:
        path = seed(uid, "tts", f"{line['id']}.wav", "voice")
        path.with_suffix(".input").write_text(line_fingerprint(p, line), encoding="utf-8")
    step = crud.get_step(p.id, "tts")
    step.status = "pending"
    crud.save_step(step)
    Dependencies(p).require("assemble")
    config.set_key("tts.indextts25.emo_weight", 0.2)
    deps = Dependencies(p)
    assert not deps.missing("assemble")
    assert any(i["step"] == "tts" and i["state"] == "stale" for i in deps.unready("assemble"))


@pytest.mark.parametrize("key", ["translate", "subtitles", "dubtask"])
def test_raw_recognition_does_not_replace_semantic_result(make_project, key):
    uid = make_project(params={"step_params": {"text.source": "transcribe"}})
    seed(uid, "transcribe", "src.srt", "1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    config.set_key("subtitles.mode", "source")
    deps = Dependencies(crud.get_project(uid))
    assert deps.inputs(key)[0][:2] == ("split", "split.txt")
    with pytest.raises(DubForgeError, match="split.txt"):
        deps.require(key)


def _old_source_choice_fingerprint(project, key, source):
    """按移除来源选项之前的存储格式构造指纹，验证升级不会误用旧产物。"""
    from dubforge.pipeline.text_prompts import (ASR_CORRECTION_VERSION, TRANSLATION_PROMPT_VERSION,
                                                TRANSLATION_WINDOW_VERSION)
    deps = Dependencies(project)
    old_keys = {"translate": ("translate", "llm"), "subtitles": ("subtitles",),
                "dubtask": ("tts.tolerance",)}
    cfg = {k: deps.cfg(k) for k in old_keys[key]}
    cfg["text.source"] = source
    inputs = [("split", "split.txt") if source == "split" else ("transcribe", "src.srt")]
    if key != "translate":
        inputs.append(("translate", "translate.txt"))
    payload = {"step": key, "cfg": cfg,
               "upstream": sorted(f"{up}/{name}:{deps.arts[f'{up}/{name}'].sha256}"
                                  for up, name in inputs)}
    if key == "translate":
        payload.update(correction_prompt=ASR_CORRECTION_VERSION,
                       translation_prompt=TRANSLATION_PROMPT_VERSION,
                       translation_windows=TRANSLATION_WINDOW_VERSION)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize("key", ["translate", "subtitles", "dubtask"])
@pytest.mark.parametrize("old_source", ["split", "transcribe"])
def test_old_semantic_caches_survive_but_raw_source_caches_expire(make_project, key, old_source):
    uid = make_project(params={"step_params": {"text.source": "transcribe"}})
    config.set_key("text.source", "transcribe")
    mark_upstream_done(uid, "assemble")
    p = crud.get_project(uid)
    row = crud.get_step(p.id, key)
    row.input_fingerprint = _old_source_choice_fingerprint(p, key, old_source)
    crud.save_step(row)
    # 字幕新增了原声审查与派生屏蔽，旧导出不能作为已处理结果继续复用。
    compatible = old_source == "split" and key != "subtitles"
    assert Dependencies(p).fresh(key) == compatible
    if compatible:
        assert runner.start_step(uid, key)["status"] == "skipped"


def test_source_choice_is_absent_from_project_and_global_forms(make_project):
    from app.api.projects import get_project_step_params
    from app.api.settings import step_params
    uid = make_project(params={"step_params": {"text.source": "transcribe"}})
    for response in (get_project_step_params(uid), step_params()):
        for key in ("translate", "subtitles", "dubtask"):
            assert "text.source" not in response["steps"][key]["values"]
            assert all(field["path"] != "text.source" for field in response["steps"][key]["fields"])


def test_partial_tts_retry_reuses_valid_lines(make_project, monkeypatch):
    from dubforge.backends import tts_indextts25
    uid = make_project()
    mark_upstream_done(uid, "tts")
    root = crud.project_dir(uid)
    ref = root / "reference.wav"
    ref.write_bytes(b"reference")
    config.set_key("tts.indextts25.timbre_audio", str(ref))
    config.set_key("tts.indextts25.emo_audio", str(ref))
    seed(uid, "split", "split.txt", "1. 0.000-1.000: hello\n2. 1.000-2.000: world\n")
    seed(uid, "translate", "translate.txt", "1. 你好\n2. 世界\n")
    calls = []
    fail_second = True

    def synth(url, text, out, lang, **kwargs):
        calls.append(text)
        if text == "世界" and fail_second:
            raise RuntimeError("模拟第二句失败")
        import wave
        with wave.open(str(out), 'wb') as stream:
            stream.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 16000)
        return out

    monkeypatch.setattr(tts_indextts25, "synth", synth)
    runner.start_step(uid, "tts")
    wait_status(uid, "tts", {"failed"})
    fail_second = False
    runner.start_step(uid, "tts")
    wait_status(uid, "tts", {"done"})
    assert calls == ["你好", "世界", "世界"]
