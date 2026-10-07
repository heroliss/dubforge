"""全文语义整理缓存；全部文件使用独立数据根，不执行模型。"""
import copy
from types import SimpleNamespace

import pytest

from dubforge.errors import StepFailedError
from dubforge.pipeline import semantic_cache as windows
from dubforge.runner import StepCancelled


@pytest.fixture(autouse=True)
def isolated_source_environment(isolated_env):
    """缓存测试遵守仓库的数据与配置隔离约定。"""


def key(**updates):
    values = dict(prompt="全文原始时间轴", source="全部原始台词", version="semantic-v2",
                  profile={"name": "固定配置", "base_url": "https://model.example/v1", "model": "model-a",
                           "temperature": .2, "params": {"reasoning_effort": "low", "max_tokens": 4000}},
                  settings={"pause": .6, "words": [{"word": "hello", "start": 1.0, "end": 1.5}]})
    values.update(updates)
    return windows.cache_key(**values)


@pytest.mark.parametrize("field,value", [("prompt", "邻句上下文已变化"), ("source", "源台词已变化"),
                                        ("version", "semantic-v3"), ("settings", {"words": [{"word": "hello", "start": 1.01, "end": 1.5}]})])
def test_input_prompt_word_anchors_and_rule_version_each_invalidate_cache(field, value):
    assert key(**{field: value}) != key()


def test_profile_secrets_and_connection_controls_are_omitted_recursively():
    profile = {"name": "fixed", "base_url": "https://user:password@model.example/v1?region=a&api_key=secret#token",
               "model": "model-a", "temperature": .2, "timeout": 30, "enabled": True, "api_key": "secret-a",
               "params": {"custom_vendor_parameter": 123, "max_tokens": 4000,
                          "nested": {"budget_tokens": 200, "apiKey": "secret-b", "apiToken": "secret-f",
                                     "authorization": "Bearer secret-c"},
                          "list": [{"token": "secret-d", "value": 2}], "extra_headers": {"X-Key": "secret-e"}}}
    public = copy.deepcopy(profile)
    public.update(base_url="https://model.example/v1?region=a", api_key="another-credential", timeout=600, enabled=False)
    public["params"]["nested"].update(apiKey="another-nested", apiToken="another-api-token", authorization="Bearer another")
    public["params"]["list"][0]["token"] = "another-token"
    public["params"]["extra_headers"]["X-Key"] = "another-header"
    assert key(profile=profile) == key(profile=public)
    assert len(key(profile=profile)) == 64
    public["params"]["nested"]["budget_tokens"] = 201
    assert key(profile=profile) != key(profile=public)
    public = copy.deepcopy(profile)
    public["params"]["custom_vendor_parameter"] = 124
    assert key(profile=profile) != key(profile=public)
    public = copy.deepcopy(profile)
    public["base_url"] = "https://model.example/v1?region=b&api_key=secret"
    assert key(profile=profile) != key(profile=public)


def test_profile_fallback_order_and_model_change_are_bound():
    profiles = [{"name": "a", "model": "model-a"}, {"name": "b", "model": "model-b"}]
    assert key(profile=profiles) != key(profile=list(reversed(profiles)))
    assert key(profile=profiles) != key(profile=[{**profiles[0], "model": "model-c"}, profiles[1]])
    assert key(profile="a") != key(profile="b")


@pytest.fixture
def ctx(make_project):
    from dubforge.domain import crud
    uid = make_project()
    return SimpleNamespace(pdir=crud.project_dir(uid), check_cancel=lambda: None, check_pause=lambda: None, force=True)


@pytest.mark.parametrize("value", ["1. 0.0-1.0: raw response", {"kind": "response", "raw": "1. 0.0-1.0: raw response"}])
def test_raw_response_round_trip_without_creating_artifacts(ctx, value):
    from dubforge.domain import crud
    digest = key()
    assert windows.read_cache(ctx, digest) is None
    assert not (ctx.pdir / "cache").exists()
    windows.write_cache(ctx, digest, value)
    assert windows.read_cache(ctx, digest) == value
    assert list((ctx.pdir / "cache/split").iterdir()) == [ctx.pdir / "cache/split" / f"{digest}.json"]
    with crud.session() as session:
        from dubforge.domain.models import Artifact
        from sqlmodel import select
        assert session.exec(select(Artifact)).all() == []
    # force是否绕过缓存由调用方决定，缓存工具本身没有隐藏的执行策略。
    assert ctx.force and windows.read_cache(ctx, digest) == value


@pytest.mark.parametrize("bytes_value", [b"{damaged json", b"\xff\xfe\x00", b""])
def test_corrupted_cache_is_a_non_mutating_miss(ctx, bytes_value):
    path = ctx.pdir / "cache/split" / f"{key()}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes_value)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert windows.read_cache(ctx, key()) is None
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert windows.read_cache(ctx, "../unsafe") is None


def test_atomic_failure_keeps_previous_cache_and_cleans_temporary_file(ctx, monkeypatch):
    from dubforge.utils import fs
    digest = key()
    windows.write_cache(ctx, digest, {"raw": "old"})
    path = ctx.pdir / "cache/split" / f"{digest}.json"
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("isolated disk failure")

    monkeypatch.setattr(fs, "atomic_replace", fail_replace)
    with pytest.raises(StepFailedError) as error:
        windows.write_cache(ctx, digest, {"raw": "new"})
    assert error.value.code == "SPLIT_CACHE_WRITE" and "写入失败" in error.value.message
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("guard", ["check_pause", "check_cancel"])
def test_cancellation_before_cache_write_preserves_old_value_and_error_class(ctx, guard):
    digest = key()
    windows.write_cache(ctx, digest, {"raw": "old"})
    path = ctx.pdir / "cache/split" / f"{digest}.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    def cancel():
        raise StepCancelled("步骤已取消")

    setattr(ctx, guard, cancel)
    with pytest.raises(StepCancelled):
        windows.write_cache(ctx, digest, {"raw": "new"})
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(path.parent.iterdir()) == [path]
