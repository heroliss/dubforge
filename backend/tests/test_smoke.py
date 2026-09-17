# -*- coding: utf-8 -*-
"""M0 冒烟测试: 配置读写 / DB 建表 / FastAPI health (夹具隔离 DATA_ROOT)"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """把 DATA_ROOT/CONFIG 指到临时目录, 隔离真实数据"""
    monkeypatch.setenv("DUBFORGE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("DUBFORGE_CONFIG", str(tmp_path / "config.yaml"))
    # 重置已缓存的单例 (config 没有 engine 缓存, db 有)
    import dubforge.config as cfg
    import dubforge.db as db
    from dubforge.domain import models  # noqa: F401
    db.reset_engine()
    yield tmp_path
    db.reset_engine()


def test_config_defaults_and_roundtrip(isolated_env):
    from dubforge import config
    # 缺 config.yaml → 返回默认值
    assert config.get("tts.indextts25.url") == "http://127.0.0.1:7860/"
    assert config.get("server.port") == 8600
    # set_key → 落盘 → 重读生效
    config.set_key("tts.indextts25.timbre_mode", "dynamic")
    assert config.get("tts.indextts25.timbre_mode") == "dynamic"
    # 未触及的键保留
    assert config.get("tts.indextts25.emo_mode") == "preset"


def test_db_creates_tables(isolated_env):
    import sqlmodel
    from dubforge import config
    from dubforge.db import get_engine, init_db
    from dubforge.domain.models import Project
    init_db()
    assert config.db_path().exists()
    with sqlmodel.Session(get_engine()) as s:
        p = Project(uid="t1", title="测试项目")
        s.add(p)
        s.commit()
        s.refresh(p)
        assert p.id is not None
        rows = s.exec(sqlmodel.select(Project)).all()
        assert len(rows) == 1


def test_registry_steps(isolated_env):
    from dubforge.pipeline.registry import STEP_KEYS, downstream_keys
    assert len(STEP_KEYS) == 9
    assert downstream_keys("download")[0] == "preprocess"
    assert downstream_keys("assemble") == []


def test_app_health(isolated_env):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # settings 接口: 密钥脱敏
        r2 = client.get("/api/settings")
        assert r2.status_code == 200
        assert isinstance(r2.json()["llm"]["api_key"], dict)
        # 更新接口
        r3 = client.post("/api/settings", json={"updates": {"server.port": 8601}})
        assert r3.json()["applied"]["server.port"] == 8601
