"""模型缓存只按明确配置解析，不扫描其他应用、不移动或下载模型。"""
import os
from pathlib import Path

import pytest

from dubforge import config


@pytest.mark.parametrize('explicit,environment,expected', [
    (True, True, 'explicit'), (True, False, 'explicit'),
    (False, True, 'environment'), (False, False, 'default'),
])
def test_hf_cache_precedence_uses_explicit_configuration_then_environment(
        isolated_env, monkeypatch, explicit, environment, expected):
    configured = isolated_env / '共享模型/指定缓存'
    inherited = isolated_env / '环境缓存'
    config.set_key('paths.hf_home', str(configured) if explicit else '')
    monkeypatch.setenv('HF_HOME', str(inherited) if environment else '')
    paths = {'explicit': configured, 'environment': inherited, 'default': config.data_root() / 'hf'}
    assert config.resolve_hf_home() == paths[expected]
    assert not paths[expected].exists()  # 路径解析不下载模型，也不创建或迁移目录。


def test_hf_cache_does_not_adopt_another_applications_neighbor(isolated_env, monkeypatch):
    monkeypatch.setattr(config, 'REPO_ROOT', isolated_env / 'DubForge')
    monkeypatch.setenv('HF_HOME', '')
    neighbor = isolated_env / 'VideoLingo-Workspace/vl_cache/hf'
    neighbor.mkdir(parents=True)
    marker = neighbor / '保留模型.bin'
    marker.write_bytes(b'existing external model')
    assert config.resolve_hf_home() == config.data_root() / 'hf'
    assert marker.read_bytes() == b'existing external model'


def test_explicit_existing_cache_is_reused_without_touching_models(isolated_env, monkeypatch):
    external = isolated_env / 'VideoLingo-Workspace/vl_cache/hf'
    external.mkdir(parents=True)
    marker = external / '保留模型.bin'
    marker.write_bytes(b'existing external model')
    config.set_key('paths.hf_home', str(external))
    monkeypatch.setenv('HF_HOME', str(isolated_env / 'unused-env-cache'))
    assert config.ensure_hf_env() == external
    assert Path(os.environ['HF_HOME']) == external
    assert marker.read_bytes() == b'existing external model'
    assert sorted(path.name for path in external.iterdir()) == [marker.name]
