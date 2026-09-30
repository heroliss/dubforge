import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dubforge import config, environment
from dubforge.errors import DubForgeError


def test_missing_optional_component_is_actionable(isolated_env, monkeypatch):
    monkeypatch.setattr(environment, "module_available", lambda name: False)
    for key in ("asr", "separation"):
        with pytest.raises(DubForgeError) as exc:
            environment.require_component(key)
        assert exc.value.code == "OPTIONAL_COMPONENT_MISSING"
        assert "install.ps1" in exc.value.suggestion


def test_environment_api_works_without_models_or_services(isolated_env, monkeypatch):
    from app.main import app
    monkeypatch.setattr(environment, "module_available", lambda name: False)
    monkeypatch.setattr(config, "REPO_ROOT", isolated_env / "fresh-repo")
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)
    data = TestClient(app).get("/api/settings/environment").json()
    assert all(not row["installed"] for row in data["components"] + data["services"])
    assert all(row["path"] is None for row in data["tools"])
    assert "api_key" not in str(data)


def test_base_install_excludes_ml_dependencies():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert not any(dep.startswith(("torch", "demucs", "faster-whisper")) for dep in project["dependencies"])
    assert any(dep.startswith("faster-whisper") for dep in project["optional-dependencies"]["asr"])
    assert any(dep.startswith("demucs") for dep in project["optional-dependencies"]["separation"])
