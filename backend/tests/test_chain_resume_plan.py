"""挂起链恢复预检、确认与去重；隔离产物，不执行模型、编码或投稿。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import mark_upstream_done, wait_status
from dubforge import config, configuration
from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.errors import DubForgeError
from dubforge.publishing.models import Publication
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint, runner


def _client():
    from app.main import app
    return TestClient(app)


def _save_params(uid, **updates):
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params.update(updates)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)


def _held_translation(make_project, *, stale=False, steps=None):
    uid = make_project(params={"auto_steps": ["translate", "subtitles"]})
    mark_upstream_done(uid, "translate")
    project = crud.get_project(uid)
    split = crud.get_step(project.id, "split")
    split.input_fingerprint = "old-successful-input" if stale else compute_fingerprint(project, "split")
    crud.save_step(split)
    translation = crud.get_step(project.id, "translate")
    translation.status = "failed"
    translation.error_code = "STEP_INTERRUPTED"
    translation.error_message = "隔离测试：上次制作被中断"
    translation.input_snapshot = json.dumps({"previous_success": "保留原生成输入"}, ensure_ascii=False)
    crud.save_step(translation)
    _save_params(uid, _chain=steps or ["translate", "subtitles"], _chain_state="held",
                 _chain_hold={"step": "translate", "code": "STEP_INTERRUPTED", "message": "原计划已挂起"})
    return uid


def _plan(uid):
    response = _client().post(f"/api/projects/{uid}/chain/plan")
    assert response.status_code == 200, response.text
    result = response.json()
    assert isinstance(result["steps"], list)
    assert isinstance(result["labels"], dict)
    assert isinstance(result["version_specs"], list)
    assert isinstance(result["effects"], list)
    assert isinstance(result["blockers"], list)
    assert all(isinstance(message, str) for message in result["blockers"])
    assert isinstance(result["fingerprint"], str) and result["fingerprint"]
    return result


def _body(plan, request_id=None):
    return {"confirmed": True, "fingerprint": plan["fingerprint"], "request_id": request_id or str(uuid4())}


def _resume(uid, body):
    return _client().post(f"/api/projects/{uid}/chain/resume", json=body)


def _persisted(uid):
    """只核对持久化记录及项目文件，不以SQLite的WAL页变化判定业务写入。"""
    with crud.session() as session:
        rows = {model.__name__: [row.model_dump() for row in session.exec(select(model)).all()]
                for model in (Project, StepRun, Artifact, Job, Publication)}
    root = crud.project_dir(uid)
    files = {str(path.relative_to(root)): (path.stat().st_mtime_ns,
             hashlib.sha256(path.read_bytes()).hexdigest()) for path in root.rglob("*") if path.is_file()}
    return rows, files


@pytest.fixture
def no_external(monkeypatch):
    """任何跨出预检边界的调用立即失败，防止测试意外访问真实服务。"""
    from dubforge.publishing import service, transport
    from dubforge import workflows

    def forbidden(*args, **kwargs):
        pytest.fail("恢复普通制作链不得创建流程、草稿或调用外部发布")

    monkeypatch.setattr(transport, "call", forbidden)
    monkeypatch.setattr(service, "create", forbidden)
    monkeypatch.setattr(service, "enqueue", forbidden)
    monkeypatch.setattr(workflows, "start", forbidden)


def test_plan_adds_earlier_stale_dependency_without_writes_or_execution(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, stale=True)
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda *args, **kwargs: calls.append(args))
    before = _persisted(uid)
    plan = _plan(uid)
    assert _persisted(uid) == before
    assert _plan(uid) == plan
    assert calls == []
    assert plan["steps"] == ["split", "translate", "subtitles"]
    assert not plan["blockers"] and not plan["version_specs"]
    recovery = plan["recovery"]
    assert recovery["start_step"] == "split"
    assert recovery["previous_step"] == "translate"
    assert recovery["changed"] is True
    assert recovery["reason"] and recovery["start_label"] and recovery["previous_label"]
    assert recovery["execute_steps"] == plan["steps"]
    rows = {row["key"]: row for row in recovery["rows"]}
    assert rows["split"]["action"] == "update"
    assert rows["translate"]["action"] == "retry"
    assert all(row["label"] and row["reason"] for row in rows.values())
    assert not any(key in plan["steps"] for key in ("tts", "assemble"))


def test_missing_earlier_file_is_replanned_but_legacy_resume_keeps_old_scope(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project)
    project = crud.get_project(uid)
    artifact = crud.latest_artifacts(project.id, ["split"])["split/split.txt"]
    (crud.project_dir(uid) / artifact.rel_path).unlink()
    plan = _plan(uid)
    assert plan["steps"] == ["split", "translate", "subtitles"]
    assert "split" in plan["recovery"]["execute_steps"]
    row = next(row for row in plan["recovery"]["rows"] if row["key"] == "split")
    assert row["action"] in {"create", "update"} and row["reason"]
    calls = []
    def require_old_scope(project_uid, key, **kwargs):
        calls.append((project_uid, key))
        Dependencies(crud.get_project(project_uid)).require(key)
        pytest.fail("缺失依赖不能进入实际制作")

    monkeypatch.setattr(runner, "start_step", require_old_scope)
    response = _client().post(f"/api/projects/{uid}/chain/resume")
    assert response.status_code == 200 and response.json()["stopped"], response.text
    assert calls == [(uid, "translate")]
    params = crud.project_params(crud.get_project(uid))
    assert params["_chain"] == ["translate", "subtitles"] and params["_chain_state"] == "held"
    assert params["_chain_hold"]["code"] == "DEPS_NOT_MET"


def test_confirmed_resume_runs_added_dependency_but_keeps_original_terminal(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, stale=True)
    plan = _plan(uid)
    calls = []

    def start(project_uid, key, **kwargs):
        calls.append((project_uid, key, kwargs))
        return {"status": "started"}

    monkeypatch.setattr(runner, "start_step", start)
    response = _resume(uid, _body(plan))
    assert response.status_code == 200, response.text
    assert response.json()["started"] == "split"
    params = crud.project_params(crud.get_project(uid))
    assert params["_chain"] == ["split", "translate", "subtitles"]
    assert params["_chain_state"] == "active"
    assert calls[0][:2] == (uid, "split") and len(calls) == 1
    assert not calls[0][2].get("force", False)
    assert crud.get_step(crud.get_project(uid).id, "translate").input_snapshot == json.dumps(
        {"previous_success": "保留原生成输入"}, ensure_ascii=False)
    with crud.session() as session:
        assert session.exec(select(Publication)).all() == []


def test_legacy_resume_still_works_when_plan_does_not_add_scope(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["translate"])
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    response = _client().post(f"/api/projects/{uid}/chain/resume")
    assert response.status_code == 200, response.text
    assert response.json()["started"] == "translate"
    assert calls == [(uid, "translate")]
    assert crud.project_params(crud.get_project(uid))["_chain"] == ["translate"]


def test_fresh_target_is_reused_and_resume_starts_first_needed_step(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["split", "translate"])
    plan = _plan(uid)
    assert "split" in plan["recovery"]["reusable_steps"]
    assert "translate" not in plan["recovery"]["reusable_steps"]
    assert plan["recovery"]["execute_steps"] == ["translate"]
    assert plan["recovery"]["start_step"] == "translate"
    assert next(row for row in plan["recovery"]["rows"] if row["key"] == "split")["action"] == "reuse"
    calls = []

    def start_with_normal_cache(project_uid, key, **kwargs):
        if Dependencies(crud.get_project(project_uid)).fresh(key):
            return {"status": "skipped"}
        calls.append((project_uid, key))
        return {"status": "started"}

    monkeypatch.setattr(runner, "start_step", start_with_normal_cache)
    response = _resume(uid, _body(plan))
    assert response.status_code == 200 and response.json()["started"] == "translate", response.text
    assert calls == [(uid, "translate")]


@pytest.mark.parametrize("drift", ["configuration", "remaining_steps", "hold_reason", "step_state"])
def test_preflight_change_requires_new_confirmation_without_mutation(make_project, monkeypatch, no_external, drift):
    uid = _held_translation(make_project)
    plan = _plan(uid)
    if drift == "configuration":
        config.set_key("translate.style_prompt", "新的隔离翻译风格")
    elif drift == "remaining_steps":
        _save_params(uid, _chain=["translate"])
    elif drift == "hold_reason":
        _save_params(uid, _chain_hold={"step": "translate", "code": "STEP_FAILED", "message": "失败原因发生变化"})
    else:
        row = crud.get_step(crud.get_project(uid).id, "translate")
        row.status = "pending"
        crud.save_step(row)
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda *args, **kwargs: calls.append(args))
    before = _persisted(uid)
    response = _resume(uid, _body(plan))
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "CHAIN_PLAN_CHANGED"
    assert calls == [] and _persisted(uid) == before


def _source_versions(uid, names=("原声字幕",)):
    versions = [dict(id=f"source-{index}", name=name, dubbing=False, burn_subtitles=False,
                     include_comments=False, overrides={}) for index, name in enumerate(names)]
    current = configuration.resolve(uid)
    configuration.save("project:" + uid, dict(revision=current["revision"], updates={"assembly.versions": versions}))
    return versions


def test_actual_saved_version_scope_is_visible_and_version_drift_rejected(make_project, monkeypatch, no_external):
    uid = make_project()
    mark_upstream_done(uid, "translate")
    versions = _source_versions(uid, ("原声成品", "另一原声成品"))
    _save_params(uid, _chain=["assemble"], _chain_state="held",
                 _chain_hold={"step": "assemble", "code": "STEP_INTERRUPTED", "message": "合成中断"})
    plan = _plan(uid)
    assert plan["steps"] == ["assemble"]
    assert [version["id"] for version in plan["version_specs"]] == [version["id"] for version in versions]
    assert all(version["name"] in "\n".join(plan["effects"]) for version in versions)
    _source_versions(uid, ("改变后的成品",))
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda *args, **kwargs: calls.append(args))
    before = _persisted(uid)
    response = _resume(uid, _body(plan))
    assert response.status_code == 400 and response.json()["code"] == "CHAIN_PLAN_CHANGED", response.text
    assert calls == [] and _persisted(uid) == before


@pytest.mark.parametrize("drift", ["configuration", "version"])
def test_change_after_confirmation_holds_before_remaining_step(make_project, monkeypatch, no_external, drift):
    uid = _held_translation(make_project, stale=True)
    if drift == "version":
        _source_versions(uid, ("原声成品",))
        _save_params(uid, _chain=["split", "assemble"])
    plan = _plan(uid)
    assert plan["recovery"]["start_step"] == "split"
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    response = _resume(uid, _body(plan))
    assert response.status_code == 200 and response.json()["started"] == "split", response.text
    if drift == "configuration":
        config.set_key("translate.style_prompt", "确认后修改的隔离翻译风格")
    else:
        _source_versions(uid, ("确认后改名的原声版本",))
    # 前一步成功后正常交还续跑器；此处只模拟完成通知，不触发实际制片。
    result = runner.continue_chain(crud.get_project(uid), SimpleNamespace(step_key="split", log=lambda _: None))
    assert result["started"] is None and result["stopped"]
    params = crud.project_params(crud.get_project(uid))
    assert params["_chain_state"] == "held"
    assert params["_chain_hold"]["code"] == "CHAIN_PLAN_CHANGED"
    assert params["_chain"][0] == ("translate" if drift == "configuration" else "assemble")
    assert calls == [(uid, "split")]


def test_irrelevant_setting_change_does_not_stop_remaining_production(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, stale=True)
    plan = _plan(uid)
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    response = _resume(uid, _body(plan))
    assert response.status_code == 200 and response.json()["started"] == "split", response.text
    config.set_key("server.port", 8611)
    result = runner.continue_chain(crud.get_project(uid), SimpleNamespace(step_key="split", log=lambda _: None))
    assert result["started"] == "translate" and result["stopped"] is None
    assert calls == [(uid, "split"), (uid, "translate")]


def _manual_project(make_project, *, key="assemble", track="trans", dubbing=False, comments=False):
    uid = make_project()
    mark_upstream_done(uid, "tts")
    if key == "assemble":
        current = configuration.resolve(uid)
        configuration.save("project:" + uid, dict(revision=current["revision"], updates={"assembly.versions": [
            dict(id="reviewed", name="隔离已确认版本", dubbing=dubbing, burn_subtitles=not dubbing,
                 include_comments=comments, overrides={"assemble.subtitle_track": track, "assemble.bgm_volume": 0})]}))
    _save_params(uid, _chain=["subtitles", "assemble"] if key == "assemble" else [key], _chain_state="held",
                 _chain_hold={"step": "subtitles" if key == "assemble" else key,
                              "code": "STEP_INTERRUPTED", "message": "隔离计划已挂起"})
    return uid


def _confirm_without_work(uid, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    plan = _plan(uid)
    response = _resume(uid, _body(plan))
    assert response.status_code == 200, response.text
    return plan, calls


def _manual_text(uid, values):
    from dubforge.pipeline import subtitle_document
    deps = Dependencies(crud.get_project(uid))
    line = subtitle_document.read(deps, translation=False, strict=False)[0]
    response = _client().put(f"/api/projects/{uid}/workbench/lines/{line['id']}/text", json={
        "revision": line["edit_revision"], "values": values})
    assert response.status_code == 200, response.text
    assert crud.project_params(crud.get_project(uid))["subtitle_edits"][str(line["id"])]["values"]


@pytest.mark.parametrize("track,field", [("source", "source_text"), ("trans", "text"),
                                        ("bilingual", "source_text"), ("bilingual", "text")])
def test_actual_subtitle_manual_save_stops_confirmed_version_before_encoding(make_project, monkeypatch,
                                                                           no_external, track, field):
    config.set_key("content_review.enabled", False)
    uid = _manual_project(make_project, track=track)
    plan, calls = _confirm_without_work(uid, monkeypatch)
    assert plan["recovery"]["start_step"] == "subtitles"
    _manual_text(uid, {field: "人工复核后的新文字"})
    result = runner.continue_chain(crud.get_project(uid), SimpleNamespace(step_key="subtitles", log=lambda _: None))
    assert result["started"] is None and result["stopped"]
    params = crud.project_params(crud.get_project(uid))
    assert params["_chain"] == ["assemble"] and params["_chain_state"] == "held"
    assert params["_chain_hold"]["code"] == "CHAIN_PLAN_CHANGED"
    assert calls == [(uid, "subtitles")]


@pytest.mark.parametrize("mode,field,blocked", [("source", "source_text", True), ("source", "text", False),
                                                ("target", "source_text", False), ("target", "text", True)])
def test_manual_subtitle_save_is_bound_only_when_selected_export_consumes_it(make_project, monkeypatch,
                                                                          no_external, mode, field, blocked):
    from dubforge.chain_recovery import check_execution
    config.set_key("content_review.enabled", False)
    config.set_key("subtitles.mode", mode)
    uid = _manual_project(make_project, key="subtitles")
    plan, _ = _confirm_without_work(uid, monkeypatch)
    _manual_text(uid, {field: "人工复核后的新文字"})
    if blocked:
        with pytest.raises(DubForgeError) as error:
            check_execution(uid, plan["execution"], ["subtitles"])
        assert error.value.code == "CHAIN_PLAN_CHANGED"
    else:
        check_execution(uid, plan["execution"], ["subtitles"])


def test_actual_voice_selection_save_stops_remaining_dubbing_version(make_project, monkeypatch, no_external):
    uid = _manual_project(make_project, dubbing=True)
    plan, calls = _confirm_without_work(uid, monkeypatch)
    response = _client().put(f"/api/projects/{uid}/workbench/lines/1/selection", json={"source": "original", "take": None})
    assert response.status_code == 200, response.text
    assert crud.project_params(crud.get_project(uid))["dubbing_edit"]["1"]["source"] == "original"
    # 配音是否排在字幕之前由真实依赖计划决定；原scope中的用户声源编辑必须在下一阶段阻断。
    from dubforge.chain_recovery import check_execution
    with pytest.raises(DubForgeError) as error:
        check_execution(uid, plan["execution"], ["assemble"])
    assert error.value.code == "CHAIN_PLAN_CHANGED"
    assert len(calls) == 1


def test_actual_line_parameter_save_stops_confirmed_tts(make_project, monkeypatch, no_external):
    from dubforge.chain_recovery import check_execution
    config.set_key("tts.backend", "indextts25")
    uid = _manual_project(make_project, key="tts")
    plan, calls = _confirm_without_work(uid, monkeypatch)
    response = _client().put(f"/api/projects/{uid}/workbench/lines/1/params", json={
        "updates": {"tts.indextts25.duration_factor": 1.15}, "reset": []})
    assert response.status_code == 200, response.text
    assert crud.project_params(crud.get_project(uid))["tts_lines"]["1"]["settings"]["tts.indextts25.duration_factor"] == 1.15
    with pytest.raises(DubForgeError) as error:
        check_execution(uid, plan["execution"], ["tts"])
    assert error.value.code == "CHAIN_PLAN_CHANGED" and len(calls) == 1


def test_normal_upstream_generation_is_not_treated_as_manual_edit(make_project, monkeypatch, no_external):
    from dubforge.chain_recovery import check_execution
    uid = _manual_project(make_project, key="tts")
    plan, _ = _confirm_without_work(uid, monkeypatch)
    root = crud.project_dir(uid)
    project = crud.get_project(uid)
    artifact = crud.latest_artifacts(project.id, ["translate"])["translate/translate.txt"]
    path = root / artifact.rel_path
    path.write_text("1. 正常翻译阶段生成的新译文\n", encoding="utf-8")
    crud.add_artifact(project.id, "translate", "text", "translate.txt", path, artifact.rel_path)
    check_execution(uid, plan["execution"], ["tts"])


def _seed_comment(uid):
    from dubforge.utils.fs import atomic_write_bytes
    root = crud.project_dir(uid)
    path = root / "artifacts/download/video_info.json"
    atomic_write_bytes(path, json.dumps({"title_original": "隔离素材", "comments": {"items": [
        {"id": "safe-comment", "text": "I enjoyed this travel video", "author": "观众", "like_count": 1,
         "url": "https://www.youtube.com/watch?v=isolated123&lc=safe-comment"}]}}, ensure_ascii=False).encode())
    crud.add_artifact(crud.get_project(uid).id, "download", "json", "video_info.json", path, str(path.relative_to(root)))


def _save_comment(uid, text):
    state = _client().get(f"/api/projects/{uid}/comments").json()
    row = state["rows"][0]
    response = _client().put(f"/api/projects/{uid}/comments/overlay", json={"revision": state["overlay_revision"],
        "settings": state["defaults"], "items": [{"id": row["id"], "source_hash": row["source_hash"],
        "source_text": row["text"], "display_text": text}]})
    assert response.status_code == 200, response.text
    assert response.json()["overlay"]["items"][0]["display_text"] == text


def test_actual_confirmed_comment_save_stops_remaining_comment_version(make_project, monkeypatch, no_external):
    from dubforge.chain_recovery import check_execution
    uid = _manual_project(make_project, comments=True)
    _seed_comment(uid)
    _save_comment(uid, "旅行视频很好看")
    plan, _ = _confirm_without_work(uid, monkeypatch)
    _save_comment(uid, "风景和旅行讲解很有趣")
    with pytest.raises(DubForgeError) as error:
        check_execution(uid, plan["execution"], ["assemble"])
    assert error.value.code == "CHAIN_PLAN_CHANGED"


def test_initial_auto_comment_creation_does_not_invalidate_confirmed_auto_plan(make_project, monkeypatch, no_external):
    from dubforge.chain_recovery import check_execution
    uid = _manual_project(make_project, comments=True)
    project = crud.get_project(uid)
    project.source_url = "https://www.youtube.com/watch?v=isolated123"
    crud.save_project(project)
    current = configuration.resolve(uid)
    configuration.save("project:" + uid, dict(revision=current["revision"], updates={"comments.mode": "auto"}))
    _seed_comment(uid)
    plan, _ = _confirm_without_work(uid, monkeypatch)
    assert any("自动采集" in effect for effect in plan["effects"])
    # 自动评论阶段最终也通过此保存入口产生confirmed方案；本次授权已经说明会自动准备。
    _save_comment(uid, "自动阶段准备的正常旅行评论")
    check_execution(uid, plan["execution"], ["assemble"])


def test_concurrent_same_uuid_creates_one_resume_receipt_and_one_start(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["translate"])
    body = _body(_plan(uid))
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: _resume(uid, body), range(4)))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert all(response.json()["started"] == "translate" for response in responses)
    assert calls == [(uid, "translate")]
    params = crud.project_params(crud.get_project(uid))
    receipts = params["_chain_resume_receipts"]
    assert len(receipts) == 1 and body["request_id"] in json.dumps(receipts)
    assert params["_chain_state"] == "active"


def test_response_lost_and_fast_failure_replay_receipt_without_running_again(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["translate"])
    plan = _plan(uid)
    body = _body(plan)
    calls = []
    original_snapshot = crud.get_step(crud.get_project(uid).id, "translate").input_snapshot

    def fail_immediately(project_uid, key, **kwargs):
        calls.append((project_uid, key))
        raise DubForgeError("隔离测试：任务无法启动", code="TEST_START_FAILED")

    monkeypatch.setattr(runner, "start_step", fail_immediately)
    first = _resume(uid, body)
    assert first.status_code == 200 and first.json()["stopped"], first.text
    assert crud.project_params(crud.get_project(uid))["_chain_state"] == "held"
    after = _persisted(uid)
    replay = _resume(uid, body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["started"] == first.json()["started"]
    assert replay.json()["stopped"] == first.json()["stopped"]
    assert calls == [(uid, "translate")] and _persisted(uid) == after
    assert crud.get_step(crud.get_project(uid).id, "translate").input_snapshot == original_snapshot
    # 用户重新检查并明确发起另一次恢复，才允许再次尝试。
    new_attempt = _resume(uid, _body(_plan(uid)))
    assert new_attempt.status_code == 200, new_attempt.text
    assert calls == [(uid, "translate"), (uid, "translate")]


def test_actual_failed_resume_keeps_success_snapshot_and_does_not_retry_same_receipt(make_project, stub_heavy_steps,
                                                                                   monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["translate"])
    config.set_key("runner.step_retries", {})
    _save_params(uid, fail_step="translate")
    stub_heavy_steps("translate")
    row = crud.get_step(crud.get_project(uid).id, "translate")
    snapshot = row.input_snapshot
    futures = []
    submit = runner._pool.submit

    def tracked_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future

    monkeypatch.setattr(runner._pool, "submit", tracked_submit)
    body = _body(_plan(uid))
    response = _resume(uid, body)
    assert response.status_code == 200, response.text
    wait_status(uid, "translate", {"failed"})
    # 状态与运行槽先释放，manifest及聚合状态稍后写完；等待整个worker才冻结比较基线。
    assert len(futures) == 1
    futures[0].result(timeout=15)
    project = crud.get_project(uid)
    assert crud.get_step(project.id, "translate").input_snapshot == snapshot
    assert crud.project_params(project)["_chain_state"] == "held"
    before = _persisted(uid)
    replay = _resume(uid, body)
    assert replay.status_code == 200, replay.text
    assert _persisted(uid) == before
    with crud.session() as session:
        assert len(session.exec(select(Job)).all()) == 1
        assert session.exec(select(Publication)).all() == []


def test_used_uuid_cannot_be_rebound_to_different_fingerprint(make_project, monkeypatch, no_external):
    uid = _held_translation(make_project, steps=["translate"])
    body = _body(_plan(uid))
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda project_uid, key, **kwargs:
                        calls.append((project_uid, key)) or {"status": "started"})
    first = _resume(uid, body)
    assert first.status_code == 200, first.text
    before = _persisted(uid)
    conflicting = _resume(uid, {**body, "fingerprint": "different-confirmed-plan"})
    assert conflicting.status_code == 400, conflicting.text
    assert conflicting.json()["code"] == "REQUEST_CONFLICT"
    assert calls == [(uid, "translate")] and _persisted(uid) == before


@pytest.mark.parametrize("updates", [{"confirmed": False}, {"request_id": "bad-id"}, {"fingerprint": ""}])
def test_unconfirmed_or_invalid_request_never_activates_plan(make_project, monkeypatch, no_external, updates):
    uid = _held_translation(make_project)
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda *args, **kwargs: calls.append(args))
    body = {**_body(_plan(uid)), **updates}
    before = _persisted(uid)
    response = _resume(uid, body)
    assert response.status_code in {400, 422}, response.text
    assert calls == [] and _persisted(uid) == before


def test_failure_without_chain_does_not_invent_resume_or_publishing_history(make_project, monkeypatch, no_external):
    uid = make_project()
    row = crud.get_step(crud.get_project(uid).id, "split")
    row.status = "failed"
    crud.save_step(row)
    calls = []
    monkeypatch.setattr(runner, "start_step", lambda *args, **kwargs: calls.append(args))
    before = _persisted(uid)
    response = _client().post(f"/api/projects/{uid}/chain/plan")
    assert response.status_code == 400 and response.json()["code"] == "CHAIN_NOT_HELD", response.text
    response = _client().post(f"/api/projects/{uid}/chain/resume")
    assert response.status_code == 400 and response.json()["code"] == "CHAIN_NOT_HELD", response.text
    assert calls == [] and _persisted(uid) == before
