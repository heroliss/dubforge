"""M0 冒烟测试: 配置读写 / DB 建表 / FastAPI health (夹具隔离 DATA_ROOT, 见 conftest)"""
import pytest


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


def test_malformed_config_uses_domain_error(isolated_env):
    """损坏的 YAML 必须走稳定错误体系，不能把 yaml 异常直接冒到 API。"""
    from dubforge import config
    from dubforge.errors import ConfigError

    config.config_path().write_text("llm: [", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        config.get("llm")
    assert exc.value.code == "CONFIG_ERROR"


def test_db_creates_tables(isolated_env):
    import sqlmodel
    from dubforge import config
    from dubforge.db import init_db
    from dubforge.domain import crud
    from dubforge.domain.models import Project
    init_db()
    assert config.db_path().exists()
    with crud.session() as s:
        p = Project(uid="t1", title="测试项目")
        s.add(p)
        s.commit()
        s.refresh(p)
        assert p.id is not None
        rows = list(s.exec(sqlmodel.select(Project)).all())
        assert len(rows) == 1


def test_registry_steps(isolated_env):
    from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_KEYS
    assert len(STEP_KEYS) == 9
    assert len(PIPELINE_KEYS) == 8
    assert "workbench" not in PIPELINE_KEYS and "dubtask" not in PIPELINE_KEYS
    assert PIPELINE_KEYS.index("tts") < PIPELINE_KEYS.index("assemble")


def test_app_health(isolated_env):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # settings 接口: 本机应用, key 明文回传 (前端密码框显示)
        r2 = client.get("/api/settings")
        assert r2.status_code == 200
        assert "profiles" in r2.json()["llm"]    # 配置池字段存在
        # 更新接口
        r3 = client.post("/api/settings", json={"updates": {"server.port": 8601}})
        assert r3.json()["applied"]["server.port"] == 8601


def test_llm_display_settings_are_bounded_and_explainable(isolated_env):
    from app.main import app
    from dubforge import config
    from fastapi.testclient import TestClient

    assert config.get("ui.llm_stream.reference_distance_chars") == 500
    config.set_key("ui.llm_stream.reference_distance_chars", 800)
    with TestClient(app) as client:
        r = client.get("/api/settings/llm_display")
        assert r.status_code == 200
        assert r.json()["reference_distance_chars"] == 800
        config.set_key("ui.llm_stream.reference_distance_chars", 99999)
        assert client.get("/api/settings/llm_display").json()["reference_distance_chars"] == 5000


def test_progress_timing_settings_are_transparent(isolated_env):
    """设置页与估算器共用截尾平均规则和首次默认耗时。"""
    from app.main import app
    from dubforge.progress import _save_sample
    from fastapi.testclient import TestClient

    for seconds in (100, 110, 900):
        _save_sample("group", "语义整理 / 测试模型", 1000, seconds)
    with TestClient(app) as client:
        r = client.get("/api/settings/progress_timing")
        assert r.status_code == 200
        body = r.json()
        assert body["download_speed"] == "cumulative_average"
        assert body["llm_default_seconds"] == 600
        assert body["sample_count"] == 3
        assert body["profiles"][0]["trimmed_mean_seconds"] == 110
        assert "estimate" not in body["profiles"][0]


def test_step_status_redacts_llm_keys(isolated_env):
    """步骤轮询需要展示配置快照，但不能把 LLM 密钥回传到浏览器。"""
    from app.main import app
    from dubforge import config
    from dubforge.domain import crud
    from fastapi.testclient import TestClient

    config.set_key("llm.profiles", [{"name": "test", "base_url": "http://llm",
                                     "api_key": "super-secret", "model": "m"}])
    project = crud.create_project(title="脱敏测试")
    with TestClient(app) as client:
        r = client.get(f"/api/projects/{project.uid}/steps")
        assert r.status_code == 200
        payload = r.json()
        assert "super-secret" not in r.text
        llm = next(s["cfg"]["llm"] for s in payload if "llm" in s["cfg"])
        assert llm["profiles"][0]["api_key"] == "***"
