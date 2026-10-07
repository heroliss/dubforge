"""正式制作不允许把未生成的配音回退成原声；模型完全使用隔离桩。"""
import time
from types import SimpleNamespace

import pytest

from conftest import wait_status
from test_dubbing_preparation import prepared
from dubforge import config, workflow_plan
from dubforge.domain import crud
from dubforge.errors import DubbingIncompleteError, StepFailedError
from dubforge.pipeline import assembly_versions, dubbing_edit, step_assemble, version_render
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes


def finish(uid, status):
    wait_status(uid, 'tts', {status})
    deadline = time.monotonic() + 10
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)


def versions(project):
    deps = Dependencies(project)
    return [assembly_versions.resolve(deps, dict(id=key, name=key, dubbing=dub,
        burn_subtitles=False, include_comments=False, overrides={'assemble.bgm_volume': 0}))
        for key, dub in [('original', False), ('dubbed', True)]]


def test_partial_failed_batch_requires_missing_sentences_and_retry_only_fills_them(prepared, monkeypatch):
    from dubforge.backends import tts_gptsovits
    uid, project, calls = prepared
    original = tts_gptsovits.synth
    def fail_second(url, text, *args, **kwargs):
        if text == '世界':
            raise StepFailedError('模拟第二句合成失败')
        return original(url, text, *args, **kwargs)
    monkeypatch.setattr(tts_gptsovits, 'synth', fail_second)
    runner.start_step(uid, 'tts'); finish(uid, 'failed')
    deps = Dependencies(crud.get_project(uid))
    assert deps.dubbing_summary() == dict(known=True, total=2, required=2, ready=1,
        pending=1, pending_ids=[2], original=0, silent=0)
    assert not deps.fresh('tts')
    specs = versions(deps.project)
    assert [n for s, n, _ in deps.with_versions(specs).inputs('assemble') if s == 'tts'] == ['1.wav', '2.wav']
    assert 'tts' in deps.with_versions(specs).plan(['assemble'], True)['steps']
    assert 'tts' in deps.with_assembly_variants(['dub']).plan(['assemble'], True)['steps']
    assert 'tts' not in deps.with_versions(specs[:1]).plan(['assemble'], True)['steps']
    with pytest.raises(DubbingIncompleteError, match='待补 1 句'):
        deps.require_dubbing()
    # 工作台仍能试听原声，但正式制作的默认意图不随文件有无改变。
    line = deps.dubbing_lines()[1]
    assert dubbing_edit.selection(deps.project, line)['source'] == 'original'
    assert dubbing_edit.production_lines(deps.project, [line])[0]['source'] == 'dub'
    path = deps.path('tts', '1.wav'); before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(tts_gptsovits, 'synth', original)
    runner.start_step(uid, 'tts'); finish(uid, 'done')
    deps = Dependencies(crud.get_project(uid))
    assert deps.require_dubbing()['ready'] == 2
    assert deps.fresh('tts')
    assert 'tts' not in deps.with_versions(specs).plan(['assemble'], True)['steps']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert calls == ['你好', '世界']
    # 上一次的失败状态本身不否定已经逐句补齐的有效音频。
    step = crud.get_step(project.id, 'tts'); step.status = 'failed'; crud.save_step(step)
    assert Dependencies(crud.get_project(uid)).require_dubbing()['ready'] == 2


@pytest.mark.parametrize('source,take', [('original', None), ('dub', None)])
def test_explicit_original_and_silence_are_not_missing_dubbing(prepared, source, take):
    uid, project, calls = prepared
    for line in Dependencies(project).dubbing_lines():
        dubbing_edit.save_selection(project, line, source, take)
    deps = Dependencies(crud.get_project(uid))
    summary = deps.require_dubbing()
    assert summary['required'] == summary['pending'] == 0
    assert summary['original' if source == 'original' else 'silent'] == 2
    assert 'tts' not in deps.with_versions(versions(deps.project)).plan(['assemble'], True)['steps']
    assert not dubbing_edit.synthesis_lines(deps.project, deps.dubbing_lines(), deps.root)
    assert not calls


def test_missing_or_changed_selected_take_blocks_even_when_step_is_done(prepared):
    uid, project, _ = prepared
    runner.start_step(uid, 'tts'); finish(uid, 'done')
    deps = Dependencies(crud.get_project(uid))
    line = deps.dubbing_lines()[0]
    path = dubbing_edit.take_path(deps.root, 1, 2)
    atomic_write_bytes(path, deps.path('tts', '1.wav').read_bytes())
    crud.add_artifact(project.id, 'tts', 'audio', path.name, path, str(path.relative_to(deps.root)))
    dubbing_edit.record_take(project, line, deps.root, 2, 'fixture', {})
    dubbing_edit.save_selection(project, line, 'dub', 2)
    assert Dependencies(crud.get_project(uid)).require_dubbing()['ready'] == 2
    path.unlink()
    deps = Dependencies(crud.get_project(uid))
    assert deps.dubbing_summary()['pending_ids'] == [1]
    assert any(n == '1_v2.wav' for s, n, _ in deps.with_versions(versions(deps.project)).inputs('assemble') if s == 'tts')
    with pytest.raises(DubbingIncompleteError): deps.require_dubbing()
    # 文件恢复但快照不匹配，也不能因同大小或原版本仍在就放行。
    atomic_write_bytes(path, deps.path('tts', '1.wav').read_bytes())
    with pytest.raises(DubbingIncompleteError): Dependencies(crud.get_project(uid)).require_dubbing()


def test_runtime_guards_stop_before_any_mix_or_version_encoding(prepared, monkeypatch):
    uid, project, _ = prepared
    deps = Dependencies(project)
    ctx = SimpleNamespace(project=project, cfg=deps.cfg, upstream=deps.upstream, pdir=deps.root,
                          dependencies=lambda: deps,
                          task_data={'version_specs': versions(project)})
    monkeypatch.setattr(version_render, 'resolve_video', lambda *args: pytest.fail('配音不全不应开始编码原声版'))
    with pytest.raises(DubbingIncompleteError): version_render.run(ctx)
    with pytest.raises(DubbingIncompleteError): step_assemble._mix_timeline(ctx, deps.root, 6)


def test_preflight_explains_needed_dubbing_and_unknown_is_not_ready(prepared, monkeypatch):
    uid, project, _ = prepared
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    config.set_key('assemble.bgm_volume', 0)
    plan = workflow_plan.plan(uid, dict(mode='produce', variants=['dub']), online=False)
    assert plan['dubbing']['needed'] and plan['dubbing']['pending'] == 2 and 'tts' in plan['steps']
    plan = workflow_plan.plan(uid, dict(mode='produce', variants=['src']), online=False)
    assert plan['dubbing'] == {'needed': False} and 'tts' not in plan['steps']
    deps = Dependencies(project); deps.path('translate', 'translate.txt').unlink()
    assert not Dependencies(project).dubbing_summary()['known']
    with pytest.raises(DubbingIncompleteError, match='原文与译文'): Dependencies(project).require_dubbing()
