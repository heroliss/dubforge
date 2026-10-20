"""步骤新鲜度的两个独立维度：本步设置变化与上游产物变化。

这些测试故意不检查 ``StepRun.status`` 的级联标记。设置变化只影响本步的
执行输入；下游是否需要更新只由它实际消费的上游文件摘要决定。
"""
import json

import pytest

from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline import stale_explanation
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint
from test_configuration import save


def _save_success_baseline(uid: str, key: str) -> None:
    project = crud.get_project(uid)
    deps = Dependencies(project)
    fingerprint = compute_fingerprint(project, key, dependencies=deps)
    snapshot = stale_explanation.capture(deps, key, fingerprint=fingerprint)
    row = crud.get_step(project.id, key)
    row.status = "done"
    row.input_fingerprint = fingerprint
    row.input_snapshot = json.dumps(snapshot, ensure_ascii=False)
    crud.save_step(row)


def test_step_setting_change_does_not_propagate_to_consumers(make_project):
    """修改翻译设置只标记翻译本步；下游仍可消费旧译文产物。"""
    uid = make_project()
    # ``mark_upstream_done`` seeds inputs before the requested step; use tts so
    # the translate output exists for the downstream readiness check.
    mark_upstream_done(uid, "tts")
    _save_success_baseline(uid, "translate")

    save("project:" + uid, {"translate.style_prompt": "更口语"})
    project = crud.get_project(uid)
    deps = Dependencies(project)

    assert deps.freshness_flags("translate")["settings_changed"] is True
    assert deps.freshness_flags("translate")["upstream_changed"] is False
    translate_input = next(item for item in deps.input_view("tts") if item["step"] == "translate")
    assert translate_input["ready"] is True


def test_replaced_upstream_artifact_marks_consumer_without_setting_change(make_project):
    """替换上游登记文件只标记消费者的上游维度。"""
    uid = make_project()
    # Seed a translate result before taking its successful input snapshot.
    mark_upstream_done(uid, "tts")
    _save_success_baseline(uid, "translate")

    project = crud.get_project(uid)
    path = Dependencies(project).path("split", "split.txt")
    path.write_text("1. 0.000-1.000: changed\n", encoding="utf-8")
    deps = Dependencies(crud.get_project(uid))
    flags = deps.freshness_flags("translate")

    assert flags["upstream_changed"] is True
    assert flags["settings_changed"] is False
    split_input = next(item for item in deps.input_view("translate") if item["step"] == "split")
    assert split_input["state"] == "stale"


def test_running_step_locks_only_its_configuration_paths(make_project):
    """运行步骤只冻结本步骤会消费的设置；其它步骤设置仍可保存。"""
    from dubforge import configuration
    from dubforge.errors import DubForgeError
    from dubforge.domain import crud

    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, "download")
    step.status = "running"
    crud.save_step(step)

    current = configuration.resolve(uid)
    # 连接参数属于下一次边界：当前下载继续使用快照，但可以提前准备下一轮代理。
    configuration.save("project:" + uid, {
        "revision": current["revision"], "fingerprint": current["fingerprint"],
        "updates": {"download.proxy": "http://proxy.local:7890"},
    })
    assert configuration.resolve(uid)["values"]["download.proxy"] == "http://proxy.local:7890"

    # 影响当前下载内容的分辨率仍在运行期间冻结。
    current = configuration.resolve(uid)
    with pytest.raises(DubForgeError) as caught:
        configuration.save("project:" + uid, {
            "revision": current["revision"], "fingerprint": current["fingerprint"],
            "updates": {"download.resolution": 720},
        })
    assert caught.value.code == "CONFIGURATION_BUSY"

    current = configuration.resolve(uid)
    configuration.save("project:" + uid, {
        "revision": current["revision"], "fingerprint": current["fingerprint"],
        "updates": {"assemble.crf": 19},
    })
    assert configuration.resolve(uid)["values"]["assemble.crf"] == 19


def test_queued_step_does_not_lock_settings(make_project):
    """排队中的步骤尚未冻结执行快照，设置仍可编辑。"""
    from dubforge import configuration
    from dubforge.domain import crud

    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, "download")
    step.status = "queued"
    crud.save_step(step)
    current = configuration.resolve(uid)
    configuration.save("project:" + uid, {
        "revision": current["revision"], "fingerprint": current["fingerprint"],
        "updates": {"download.proxy": "http://proxy.local:7890"},
    })
    assert configuration.resolve(uid)["values"]["download.proxy"] == "http://proxy.local:7890"


def test_inline_synthesis_locks_tts_but_not_other_steps(make_project):
    """未建 StepRun 的逐句合成也只冻结 TTS 设置。"""
    from dubforge import configuration
    from dubforge.errors import DubForgeError
    from dubforge.pipeline import dubbing_session

    uid = make_project()
    dubbing_session._active[uid] = {'active_line': 1}
    try:
        current = configuration.resolve(uid)
        with pytest.raises(DubForgeError) as caught:
            configuration.save("project:" + uid, {
                "revision": current["revision"], "fingerprint": current["fingerprint"],
                "updates": {"tts.indextts25.emo_weight": 0.5},
            })
        assert caught.value.code == "CONFIGURATION_BUSY"

        current = configuration.resolve(uid)
        configuration.save("project:" + uid, {
            "revision": current["revision"], "fingerprint": current["fingerprint"],
            "updates": {"assemble.crf": 19},
        })
        assert configuration.resolve(uid)["values"]["assemble.crf"] == 19
    finally:
        dubbing_session._active.pop(uid, None)


def test_deferred_execution_snapshot_uses_settings_saved_while_queued(make_project, pdir):
    """StepContext 延迟冻结时读取运行边界的最新项目设置。"""
    import threading
    from dubforge import configuration
    from dubforge.runner import StepContext

    uid = make_project()
    project = crud.get_project(uid)
    ctx = StepContext(project, "download", pdir(uid), pdir(uid) / "logs" / "download.log",
                      threading.Event(), defer_snapshot=True)
    current = configuration.resolve(uid)
    configuration.save("project:" + uid, {
        "revision": current["revision"], "fingerprint": current["fingerprint"],
        "updates": {"download.proxy": "http://proxy.local:7890"},
    })
    ctx.freeze_configuration()
    assert ctx.cfg("download.proxy") == "http://proxy.local:7890"
