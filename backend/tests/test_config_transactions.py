"""配置批量保存、请求快照及运行目录的一致性回归。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from dubforge import config, db
from dubforge.errors import ConfigError


def test_batch_update_writes_once_and_preserves_other_keys(isolated_env, monkeypatch):
    config.set_key("extension.note", "保留")
    writes = []
    original = config.atomic_write_bytes

    def tracked(path, data):
        writes.append(path)
        original(path, data)

    monkeypatch.setattr(config, "atomic_write_bytes", tracked)
    config.update_config({"server.port": 8611, "tts.backend": "gptsovits"})
    assert len(writes) == 1
    assert config.get("server.port") == 8611
    assert config.get("tts.backend") == "gptsovits"
    assert config.get("extension.note") == "保留"


@pytest.mark.parametrize("updates", [
    {"server.port": 8611, "tts": "不是对象"},
    {"server.port": 8611, "extension.value.child": 1},
    {"server.port": 8611, "": 1},
    {"server.port": 8611, "tts..backend": "gptsovits"},
    {"server": {}, "server.port": 8611},
    {"server.port": 8611, "server": {}},
])
def test_invalid_batch_does_not_partially_save(isolated_env, updates):
    config.set_key("extension.value", "原始值")
    before = config.config_path().read_bytes()
    with pytest.raises(ConfigError):
        config.update_config(updates)
    assert config.config_path().read_bytes() == before


def test_settings_api_rejects_whole_invalid_batch(isolated_env):
    from app.main import app
    from fastapi.testclient import TestClient

    config.set_key("server.port", 8611)
    before = config.config_path().read_bytes()
    response = TestClient(app).post("/api/settings", json={
        "updates": {"server.port": 8612, "tts": "不是对象"}})
    assert response.status_code == 400
    assert response.json()["code"] == "CONFIG_ERROR"
    assert config.config_path().read_bytes() == before


def test_concurrent_updates_do_not_lose_keys(isolated_env):
    ready = Barrier(8)

    def save(index):
        ready.wait(timeout=10)
        config.set_key(f"extension.key{index}", index)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(8)))
    assert config.get("extension") == {f"key{i}": i for i in range(8)}


@pytest.mark.parametrize("raw", ["false", "[]", "tts: broken", "tts:\n  indextts25: []"])
def test_invalid_config_structure_has_stable_error(isolated_env, raw):
    config.config_path().write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load_config()


def test_null_sections_legacy_settings_and_snapshot_isolation(isolated_env):
    config.config_path().write_text(
        "server: null\nsubtitles: null\ntts:\n  subtitles:\n    min_duration: 0\n",
        encoding="utf-8")
    settings = config.load_config()
    assert settings["server"]["port"] == config.DEFAULTS["server"]["port"]
    assert settings["subtitles"]["min_duration"] == 0
    settings["server"]["port"] = 1234
    assert config.get("server.port") != 1234
    config.config_path().write_text("server:\n  port: 8612\n", encoding="utf-8")
    assert config.get("server.port") == 8612
    assert config.get("server.port", settings=settings) == 1234


def test_failed_config_write_preserves_original(isolated_env, monkeypatch):
    config.set_key("server.port", 8611)
    before = config.config_path().read_bytes()

    def fail(*args):
        raise PermissionError("模拟文件占用")

    monkeypatch.setattr(config, "atomic_write_bytes", fail)
    with pytest.raises(ConfigError):
        config.set_key("server.port", 8612)
    assert config.config_path().read_bytes() == before


def test_global_subtitle_partial_update_validates_merged_widths_atomically(isolated_env):
    config.set_key('assemble.subtitle_shrink_width_percent', 75)
    before = config.config_path().read_bytes()
    with pytest.raises(ConfigError, match='缩小目标宽度不能大于换行上限宽度'):
        config.update_config({'server.port': 8611, 'assemble.subtitle_wrap_width_percent': 70})
    assert config.config_path().read_bytes() == before
    config.update_config({'assemble.subtitle_auto_fit': False, 'assemble.subtitle_wrap_width_percent': 70})
    assert config.get('assemble.subtitle_shrink_width_percent') == 75
    assert config.get('assemble.subtitle_wrap_width_percent') == 70
    before = config.config_path().read_bytes()
    with pytest.raises(ConfigError, match='缩小目标宽度不能大于换行上限宽度'):
        config.set_key('assemble.subtitle_auto_fit', True)
    assert config.config_path().read_bytes() == before
    config.update_config({'assemble.subtitle_auto_fit': True, 'assemble.subtitle_shrink_width_percent': 65})
    assert config.get('assemble.subtitle_auto_fit') is True


@pytest.mark.parametrize('value', [9, 101, None, True, '80'])
def test_global_subtitle_direct_writes_validate_numeric_values(isolated_env, value):
    config.set_key('extension.note', '保留')
    before = config.config_path().read_bytes()
    with pytest.raises(ConfigError):
        config.set_key('assemble.subtitle_shrink_width_percent', value)
    assert config.config_path().read_bytes() == before


@pytest.mark.parametrize('section', [
    {'subtitle_shrink_width_percent': 95, 'subtitle_wrap_width_percent': 80},
    {'subtitle_wrap_width_percent': None},
])
def test_global_assemble_section_update_also_validates_subtitle_widths(isolated_env, section):
    config.set_key('extension.note', '保留')
    before = config.config_path().read_bytes()
    with pytest.raises(ConfigError):
        config.set_key('assemble', section)
    assert config.config_path().read_bytes() == before


def test_data_directory_changes_only_with_database_restart(isolated_env, monkeypatch):
    from dubforge.domain import crud

    db.reset_engine()
    monkeypatch.delenv("DUBFORGE_DATA_ROOT")
    first = isolated_env / "first"
    second = isolated_env / "second"
    config.set_key("server.data_root", str(first))
    db.init_db()
    engine = db.get_engine()
    config.set_key("server.data_root", str(second))
    assert config.get("server.data_root") == str(second)
    assert config.data_root() == first
    assert config.db_path() == Path(engine.url.database)
    assert config.projects_dir().parent == first
    assert not second.exists()
    db.reset_engine()
    db.init_db()
    assert config.data_root() == second
    assert Path(db.get_engine().url.database) == second / "dubforge.db"
    assert crud.list_projects() == []


def test_concurrent_engine_initialization_uses_one_engine(isolated_env, monkeypatch):
    db.reset_engine()
    create_engine = db.sqlmodel.create_engine
    calls = []
    ready = Barrier(8)

    def tracked(*args, **kwargs):
        calls.append(args)
        return create_engine(*args, **kwargs)

    def initialize(_):
        ready.wait(timeout=10)
        return db.get_engine()

    monkeypatch.setattr(db.sqlmodel, "create_engine", tracked)
    with ThreadPoolExecutor(max_workers=8) as pool:
        engines = list(pool.map(initialize, range(8)))
    assert len(calls) == 1
    assert all(engine is engines[0] for engine in engines)
    with engines[0].connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
