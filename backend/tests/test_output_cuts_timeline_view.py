"""冻结剪辑版的编辑器初值：只读、量化坐标及显式草稿优先级。"""
import copy
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dubforge.pipeline import assembly_timeline, assembly_versions, output_cuts as cuts


@pytest.fixture()
def timeline_project(isolated_env, monkeypatch):
    from dubforge.domain import crud
    project = crud.create_project('剪辑版初值测试', source_kind='local', source_url='media/video.mp4')
    root = crud.project_dir(project.uid)
    (root / 'media/video.mp4').write_bytes(b'not a media file; no ffmpeg in this test')
    monkeypatch.setattr(cuts, '_duration', lambda *_: 100.)
    monkeypatch.setattr(cuts, '_frame_rate', lambda *_: 30.)
    version = dict(id='timeline-example', name='原声剪辑版', dubbing=False,
                   burn_subtitles=True, include_comments=True, overrides={},
                   timeline={'plan_id': 'a' * 32})
    monkeypatch.setattr(assembly_versions, 'catalog', lambda _uid: {'versions': [version]})
    monkeypatch.setattr(assembly_versions, 'results', lambda _uid: {})
    source_fingerprint = cuts.view(project.uid)['source_fingerprint']
    frozen = {'source_fingerprint': source_fingerprint, 'output_cuts': {
        'source_fingerprint': source_fingerprint, 'source_duration': 100.,
        'output_duration': 95.633333, 'ranges': [{'start': 10.0, 'end': 14.366667}]
    }}
    monkeypatch.setattr(assembly_timeline, 'load_plan', lambda *_args, **_kwargs: copy.deepcopy(frozen))
    return project, root, version, frozen


def test_get_timeline_seeds_quantized_ranges_with_stable_ids_without_writing(timeline_project):
    from app.api.output_cuts import router
    project, root, version, frozen = timeline_project
    before = {str(path.relative_to(root)): path.read_bytes()
              for path in root.rglob('*') if path.is_file()}
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        first = client.get(f'/api/projects/{project.uid}/output-cuts', params={'version_id': version['id']})
        second = client.get(f'/api/projects/{project.uid}/output-cuts', params={'version_id': version['id']})
    assert first.status_code == second.status_code == 200
    value = first.json()
    assert value['scope'] == 'version' and value['enabled'] is True
    assert value['effective_ranges'] == frozen['output_cuts']['ranges']
    assert value['output_duration'] == pytest.approx(95.633333)
    assert value['ranges'] == second.json()['ranges']
    assert value['ranges'][0]['id'] == 'timeline-' + 'a' * 32 + '-1'
    assert value['source_changed'] is False and value['revision'] == '0'
    assert not (root / 'output_cuts.json').exists()
    assert before == {str(path.relative_to(root)): path.read_bytes()
                      for path in root.rglob('*') if path.is_file()}


def test_timeline_editor_uses_frozen_range_instead_of_project_default(timeline_project):
    project, root, version, frozen = timeline_project
    raw = {'schema': 1, 'enabled': True, 'source_fingerprint': frozen['source_fingerprint'],
           'ranges': [{'id': 'project-ad', 'start': 40, 'end': 50, 'enabled': True}]}
    path = root / 'output_cuts.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    before = path.read_bytes()
    value = cuts.view(project.uid, version['id'])
    assert value['scope'] == 'version'
    assert value['effective_ranges'] == frozen['output_cuts']['ranges']
    assert cuts.view(project.uid)['effective_ranges'] == [{'start': 40., 'end': 50.}]
    assert path.read_bytes() == before


def test_user_saved_version_draft_takes_priority_over_frozen_timeline(timeline_project, monkeypatch):
    project, root, version, _ = timeline_project
    current = cuts.view(project.uid, version['id'])
    cuts.save(project.uid, {'version_id': version['id'], 'revision': current['revision'],
        'source_fingerprint': current['source_fingerprint'], 'enabled': True,
        'ranges': [{'id': 'manual-change', 'start': 12, 'end': 17, 'reason': '手动修改', 'enabled': True}]})
    monkeypatch.setattr(assembly_timeline, 'load_plan', lambda *_args, **_kwargs:
                        pytest.fail('显式版本草稿不应读取冻结方案'))
    value = cuts.view(project.uid, version['id'])
    assert value['scope'] == 'version'
    assert value['effective_ranges'] == [{'start': 12., 'end': 17.}]
    assert value['ranges'][0]['id'] == 'manual-change'
    assert json.loads((root / 'output_cuts.json').read_text(encoding='utf-8'))['versions'][version['id']]['ranges'][0]['reason'] == '手动修改'


def test_explicit_baseline_preview_takes_priority_and_does_not_seed_timeline(timeline_project, monkeypatch):
    project, root, version, frozen = timeline_project
    monkeypatch.setattr(assembly_timeline, 'load_plan', lambda *_args, **_kwargs:
                        pytest.fail('显式载入其他成片时不应读取当前冻结方案'))
    baseline = {'version_id': 'another-version', 'enabled': True,
        'source_fingerprint': frozen['source_fingerprint'],
        'ranges': [{'id': 'baseline-cut', 'start': 22., 'end': 25., 'enabled': True}]}
    monkeypatch.setattr(cuts, '_baseline_for', lambda *_args, **_kwargs: copy.deepcopy(baseline))
    value = cuts.view(project.uid, version['id'], baseline_version_id='another-version')
    assert value['effective_ranges'] == [{'start': 22., 'end': 25.}]
    assert value['baseline']['version_id'] == 'another-version'
    assert not (root / 'output_cuts.json').exists()


def test_timeline_without_deletions_does_not_inherit_project_deletion(timeline_project, monkeypatch):
    project, root, version, frozen = timeline_project
    (root / 'output_cuts.json').write_text(json.dumps({'schema': 1, 'enabled': True,
        'ranges': [{'id': 'project-ad', 'start': 40, 'end': 50}]}), encoding='utf-8')
    monkeypatch.setattr(assembly_timeline, 'load_plan', lambda *_args, **_kwargs:
                        {'source_fingerprint': frozen['source_fingerprint'], 'output_cuts': None})
    value = cuts.view(project.uid, version['id'])
    assert value['scope'] == 'version' and value['enabled'] is False
    assert value['ranges'] == [] and value['effective_ranges'] == []
    assert value['output_duration'] == 100.


def test_invalid_timeline_does_not_seed_or_write_an_editor_plan(timeline_project, monkeypatch):
    project, root, version, _ = timeline_project
    def missing(*_args, **_kwargs):
        raise assembly_timeline.TimelineError('无效剪辑方案')
    monkeypatch.setattr(assembly_timeline, 'load_plan', missing)
    value = cuts.view(project.uid, version['id'])
    assert value['scope'] == 'project' and value['ranges'] == []
    assert not (root / 'output_cuts.json').exists()
