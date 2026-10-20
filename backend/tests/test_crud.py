"""M1 领域 CRUD 测试: 目录骨架 / 9 步骤行 / 删除联动 / manifest 双写"""
import json


def test_create_project_scaffolding(make_project, isolated_env, pdir):
    from dubforge.domain import crud
    from dubforge.pipeline.registry import STEP_KEYS

    uid = make_project(params={"fail_step": "translate"})
    pdir = pdir(uid)

    # 目录骨架
    for sub in ("media", "audio", "srt", "logs", "artifacts/download"):
        assert (pdir / sub).is_dir(), f"缺少目录 {sub}"
    # 9 个步骤行, 全部 pending
    steps = crud.get_steps(crud.get_project(uid).id)
    assert [s.step_key for s in steps] == STEP_KEYS
    assert all(s.status == "pending" for s in steps)
    # manifest 双写
    manifest = json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "测试项目"
    assert len(manifest["steps"]) == 9


def test_project_status_update_refreshes_manifest(make_project, pdir):
    from dubforge.domain import crud

    uid = make_project()
    crud.update_project_status(crud.get_project(uid).id, 'failed')
    assert json.loads((pdir(uid) / 'manifest.json').read_text(encoding='utf-8'))['status'] == 'failed'
    crud.update_project_status(crud.get_project(uid).id, 'created')
    assert json.loads((pdir(uid) / 'manifest.json').read_text(encoding='utf-8'))['status'] == 'created'


def test_delete_project_cascades(make_project, isolated_env, pdir):
    from dubforge.domain import crud
    uid = make_project()
    assert crud.delete_project(uid) is True
    assert crud.get_project(uid) is None
    assert not (pdir(uid)).exists()
    # 重复删除 → False (不抛错)
    assert crud.delete_project(uid) is False


def test_backfill_steps(isolated_env):
    """老项目缺失的步骤行在启动时自动补齐 (注册表随版本增长)"""
    import sqlmodel
    from dubforge.domain import crud
    from dubforge.domain.models import StepRun
    p = crud.create_project(title="老项目")
    with crud.session() as s:
        rows = s.exec(sqlmodel.select(StepRun).where(
            StepRun.project_id == p.id)).all()
        for r in rows[3:]:
            s.delete(r)
        s.commit()
    assert len(crud.get_steps(p.id)) == 3
    added = crud.backfill_steps()
    assert added >= 6
    assert len(crud.get_steps(p.id)) == 9


def test_repair_only_stale_steps_without_execution_or_artifact_evidence(make_project, pdir):
    """兼容修复只还原从未运行的旧错误状态，不能把真实过期结果说成首次运行。"""
    import datetime

    from dubforge.domain import crud
    from dubforge.domain.models import Artifact, Job

    uid = make_project()
    project = crud.get_project(uid)
    keys = ("split", "translate", "subtitles", "dubtask", "tts", "assemble")
    for key in keys:
        step = crud.get_step(project.id, key)
        step.status = "stale"
        step.error_message = "上游步骤 [download] 已重跑, 本步结果过期"
        if key == "translate":
            step.input_fingerprint = "old-input"
        elif key == "subtitles":
            step.started_at = datetime.datetime.now(datetime.timezone.utc)
        elif key == "assemble":
            step.progress = 10
        crud.save_step(step)
    with crud.session() as s:
        s.add(Artifact(project_id=project.id, step_key="dubtask", name="tasks.json", rel_path="tasks.json"))
        s.add(Job(project_id=project.id, step_key="tts", status="failed"))
        s.commit()
    assert crud.repair_unstarted_stale_steps() == 1
    steps = {s.step_key: s for s in crud.get_steps(project.id)}
    assert steps["split"].status == "pending"
    assert not steps["split"].error_message
    assert all(steps[key].status == "stale" for key in keys if key != "split")
    manifest = json.loads((pdir(uid) / "manifest.json").read_text(encoding="utf-8"))
    assert next(s for s in manifest["steps"] if s["key"] == "split")["status"] == "pending"
    assert crud.repair_unstarted_stale_steps() == 0


def test_rebuild_from_dirs(isolated_env):
    """DB 行被删后从 manifest 重建: 项目/步骤(状态+指纹)/产物;
    running 判中断; params (勾选链) 恢复; 幂等 —— 再跑一遍零新增"""
    import sqlmodel
    from dubforge.domain import crud
    from dubforge.domain.models import Artifact, Project, StepRun
    from dubforge.pipeline.registry import STEP_KEYS

    p = crud.create_project(title="重建项目", source_url="https://x.com/v",
                            source_kind="link", params={"auto_steps": ["download"]})
    with crud.session() as s:
        dl = s.exec(sqlmodel.select(StepRun).where(
            StepRun.project_id == p.id, StepRun.step_key == "download")).first()
        dl.status = "done"
        dl.input_fingerprint = "fp-abc"
        s.add(dl)
        s.add(Artifact(project_id=p.id, step_key="download",
                       name="video.mp4", rel_path="media/video.mp4", size=123))
        tt = s.exec(sqlmodel.select(StepRun).where(
            StepRun.project_id == p.id, StepRun.step_key == "tts")).first()
        tt.status = "running"                 # 崩溃残留 → 重建后应为 interrupted
        s.add(tt)
        s.commit()
    crud.write_manifest(p.uid)

    # 模拟数据库全部丢失 (目录与 manifest 保留)
    with crud.session() as s:
        for model in (Artifact, StepRun, Project):
            for row in s.exec(sqlmodel.select(model)).all():
                s.delete(row)
        s.commit()
    assert crud.get_project(p.uid) is None

    counts = crud.rebuild_from_dirs()
    assert counts["projects"] == 1
    assert counts["steps"] == len(STEP_KEYS)
    assert counts["artifacts"] == 1

    proj = crud.get_project(p.uid)
    assert proj.title == "重建项目"
    assert proj.source_kind == "link"
    assert json.loads(proj.params)["auto_steps"] == ["download"]
    steps = {st.step_key: st for st in crud.get_steps(proj.id)}
    assert steps["download"].status == "done"
    assert steps["download"].input_fingerprint == "fp-abc"
    assert steps["tts"].status == "interrupted"
    assert all(steps[k].status == "pending"
               for k in STEP_KEYS if k not in ("download", "tts"))
    arts = crud.latest_artifacts(proj.id, None)
    assert "download/video.mp4" in arts
    assert arts["download/video.mp4"].rel_path == "media/video.mp4"

    # 幂等: 已登记的项目不再重复收编
    assert crud.rebuild_from_dirs() == {"projects": 0, "steps": 0, "artifacts": 0}


def test_rebuild_admits_copied_dir(isolated_env):
    """手动拷进 projects/ 的项目文件夹 (带 manifest) 启动/重建时被收编"""
    from dubforge.domain import crud
    from dubforge.pipeline.registry import STEP_KEYS
    p = crud.create_project(title="被搬来的项目")
    src_dir = crud.project_dir(p.uid)
    crud.delete_project(p.uid)
    # delete_project 连目录一起删了 —— 手工重造一个"拷贝进来"的目录: 只有 manifest
    src_dir.mkdir(parents=True)
    manifest = {
        "uid": p.uid, "title": "被搬来的项目", "dir_name": src_dir.name,
        "source": {"kind": "link", "url": "https://x.com/v", "note": ""},
        "status": "created", "params": {},
        "steps": [{"key": "download", "status": "done", "fingerprint": "fp-1",
                   "error": None}],
        "artifacts": {},
    }
    (src_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding="utf-8")
    counts = crud.rebuild_from_dirs()
    assert counts["projects"] == 1
    proj = crud.get_project(p.uid)
    assert proj is not None and proj.title == "被搬来的项目"
    steps = {st.step_key: st for st in crud.get_steps(proj.id)}
    assert len(steps) == len(STEP_KEYS)          # 缺的步骤补 pending
    assert steps["download"].status == "done"


def test_step_param_change_alters_fingerprint(isolated_env):
    """步骤参数修改 → 输入指纹变化 (已完成步骤自动「已过期」, 重跑按新参数)"""
    from dubforge import config
    from dubforge.domain import crud
    from dubforge.runner import compute_fingerprint
    p = crud.create_project(title="指纹项目")
    fp1 = compute_fingerprint(p, "preprocess")
    config.set_key("separate.model", "htdemucs_ft")
    assert compute_fingerprint(p, "preprocess") == fp1
    from test_configuration import save
    save('project:' + p.uid, {'separate.model': 'htdemucs_ft'})
    assert compute_fingerprint(p, "preprocess") != fp1


def test_segmentation_mode_alters_split_fingerprint(isolated_env):
    """词级/短语级只让语义整理失效，不触发 Whisper 重新识别。"""
    import dubforge.config as cfgmod
    from dubforge.domain import crud
    from dubforge.runner import compute_fingerprint

    p = crud.create_project(title="mode-fingerprint")
    cfgmod.set_key("asr.segmentation_mode", "phrase")
    transcribe_fp = compute_fingerprint(p, "transcribe")
    phrase_fp = compute_fingerprint(p, "split")
    from test_configuration import save
    save('project:' + p.uid, {'asr.segmentation_mode': 'word'})
    assert compute_fingerprint(crud.get_project(p.uid), "transcribe") == transcribe_fp
    assert compute_fingerprint(crud.get_project(p.uid), "split") != phrase_fp
def test_download_source_change_alters_fingerprint(isolated_env, tmp_path):
    from dubforge.domain import crud
    from dubforge.runner import compute_fingerprint

    source = tmp_path / "a.mp4"
    source.write_bytes(b"a")
    p = crud.create_project(title="源指纹", source_kind="local",
                            source_url=str(source))
    fp1 = compute_fingerprint(p, "download")
    p.source_url = str(tmp_path / "b.mp4")
    crud.save_project(p)
    assert compute_fingerprint(crud.get_project(p.uid), "download") != fp1


def test_force_assemble_keeps_history_files(make_project, isolated_env, pdir):
    """重跑保留成片及索引，失败后用户仍可预览、定位和发布原成品。"""
    from dubforge.domain import crud

    uid = make_project()
    project = crud.get_project(uid)
    out = pdir(uid) / "output" / "final_old.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"old")
    crud.add_artifact(project.id, "assemble", "video", out.name, out,
                      str(out.relative_to(pdir(uid))))
    crud.delete_step_artifacts(project.id, "assemble")
    assert out.exists()
    assert f'assemble/{out.name}' in crud.latest_artifacts(project.id, ["assemble"])
