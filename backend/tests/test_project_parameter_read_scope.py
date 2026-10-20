"""合成大参数的解析计数及读取等价；不读取生产媒体或启动服务。"""
import json
import time

import pytest

from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, line_settings
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_fingerprint


@pytest.fixture
def document(make_project):
    lines = [dict(id=i, text=f'第{i}句', start=float(i), end=float(i + 1)) for i in range(1, 513)]
    params = dict(dubbing_edit={str(row['id']): dict(identity=line_settings.identity(row),
        source='dub' if row['id'] % 2 else 'original', take=1 if row['id'] % 2 else None) for row in lines},
        tts_lines={str(row['id']): dict(identity=line_settings.identity(row), settings={
            'tts.indextts25.emo_weight': .2 if row['id'] % 2 else .8}) for row in lines},
        unrelated_notes='仅用于隔离大参数测试' * 8192)
    project = crud.get_project(make_project(params))
    return project, lines


def counter(monkeypatch):
    original = crud.project_params
    calls = []
    def read(project):
        calls.append(project.params)
        return original(project)
    monkeypatch.setattr(crud, 'project_params', read)
    return calls


def test_bulk_selection_and_line_overrides_parse_once_with_identical_results(document, monkeypatch):
    project, lines = document
    deps = Dependencies(project)
    calls = counter(monkeypatch)
    start = time.perf_counter()
    # 原有逐句入口就是优化前render_lines和effective_config的调用方式。
    old_rows = [{**line, **dubbing_edit.selection(project, line, deps.root, artifacts=deps.arts)} for line in lines]
    old_values = [line_settings.effective_config(project, line, deps.cfg)('tts.indextts25.emo_weight') for line in lines]
    old_elapsed = time.perf_counter() - start
    old_calls = len(calls)
    assert old_calls == len(lines) * 2
    assert old_values[:2] == [.2, .8]
    calls.clear()
    start = time.perf_counter()
    rows = dubbing_edit.render_lines(project, lines, deps.root, dependencies=deps)
    values = [line_settings.effective_config(project, line, deps.cfg,
        params=line_settings.project_parameters(project, deps))('tts.indextts25.emo_weight') for line in lines]
    elapsed = time.perf_counter() - start
    assert rows == old_rows and values == old_values
    assert len(calls) == 1
    # 实际依赖解析中的逐句effective_config也必须使用同一个请求内参数，不只优化测试循环。
    monkeypatch.setattr(deps, 'dubbing_lines', lambda: lines)
    deps.inputs('tts')
    assert len(calls) == 1
    print('PARAMETER_READ_PROOF=' + json.dumps(dict(lines=len(lines), json_bytes=len(project.params.encode()),
        before_parses=old_calls, after_parses=len(calls), before_seconds=round(old_elapsed, 6),
        after_seconds=round(elapsed, 6), identical=True)))


def test_standalone_render_call_parses_once_without_retaining_mutable_result(document, monkeypatch):
    project, lines = document
    calls = counter(monkeypatch)
    first = dubbing_edit.render_lines(project, lines)
    assert len(calls) == 1
    first[0]['source'] = 'original'
    second = dubbing_edit.render_lines(project, lines)
    assert len(calls) == 2 and second[0]['source'] == 'dub'


def test_same_snapshot_rereads_changed_json_and_different_frozen_project(document, monkeypatch):
    project, lines = document
    deps = Dependencies(project)
    calls = counter(monkeypatch)
    first = deps.read_project_params()
    assert first is deps.read_project_params() and len(calls) == 1
    changed = json.loads(project.params)
    changed['dubbing_edit']['1']['source'] = 'original'
    frozen = project.model_copy(update={'params': json.dumps(changed, ensure_ascii=False)})
    assert dubbing_edit.render_lines(frozen, lines[:1], dependencies=deps)[0]['source'] == 'original'
    assert len(calls) == 2
    assert dubbing_edit.render_lines(project, lines[:1], dependencies=deps)[0]['source'] == 'dub'
    assert len(calls) == 3
    project.params = frozen.params
    assert dubbing_edit.render_lines(project, lines[:1], dependencies=deps)[0]['source'] == 'original'
    assert len(calls) == 4


def test_new_dependency_snapshot_reads_saved_changes_and_fingerprints_are_unchanged(document, monkeypatch):
    project, lines = document
    deps = Dependencies(project)
    expected = [line_fingerprint(project, line, dependencies=deps) for line in lines[:3]]
    fresh_reader = lambda candidate=None: crud.project_params(candidate if candidate is not None else project)
    monkeypatch.setattr(deps, 'read_project_params', fresh_reader)
    assert [line_fingerprint(project, line, dependencies=deps) for line in lines[:3]] == expected
    params = crud.project_params(project)
    params['tts_lines']['1']['settings']['tts.indextts25.emo_weight'] = .9
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    updated = crud.get_project(project.uid)
    next_deps = Dependencies(updated)
    assert line_fingerprint(updated, lines[0], dependencies=next_deps) != expected[0]
    assert line_fingerprint(updated, lines[1], dependencies=next_deps) == expected[1]
    # 读取者只能得到当前内容的参数，没有修改缓存命中或正式产物记录。
    assert not crud.latest_artifacts(project.id, ['tts'])


def test_identity_mismatch_and_malformed_parameters_keep_existing_fallbacks(document):
    project, lines = document
    deps = Dependencies(project)
    changed_line = {**lines[0], 'text': '已经换成另一句话'}
    assert line_settings.effective_config(project, changed_line, deps.cfg,
        params=deps.read_project_params())('tts.indextts25.emo_weight') == deps.cfg('tts.indextts25.emo_weight')
    project.params = '{broken'
    assert deps.read_project_params() == {}
    assert line_settings.effective_config(project, lines[0], deps.cfg,
        params=deps.read_project_params())('tts.indextts25.emo_weight') == deps.cfg('tts.indextts25.emo_weight')
