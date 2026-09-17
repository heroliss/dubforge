# -*- coding: utf-8 -*-
"""M1 领域 CRUD 测试: 目录骨架 / 9 步骤行 / 删除联动 / manifest 双写"""
import json


def test_create_project_scaffolding(make_project, isolated_env):
    from dubforge.domain import crud
    from dubforge.pipeline.registry import STEP_KEYS

    uid = make_project(params={"fail_step": "translate"})
    pdir = isolated_env / "data" / "projects" / uid

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


def test_delete_project_cascades(make_project, isolated_env):
    from dubforge.domain import crud
    uid = make_project()
    assert crud.delete_project(uid) is True
    assert crud.get_project(uid) is None
    assert not (isolated_env / "data" / "projects" / uid).exists()
    # 重复删除 → False (不抛错)
    assert crud.delete_project(uid) is False
