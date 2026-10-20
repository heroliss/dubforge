"""版本级成片剪辑方案与历史基准的隔离合同。"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from dubforge.pipeline import output_cuts as cuts
from dubforge.pipeline.dependencies import Dependencies


@pytest.fixture()
def project(isolated_env, monkeypatch):
    from dubforge.domain import crud
    project = crud.create_project('版本剪辑测试', source_kind='local', source_url='media/video.mp4')
    (crud.project_dir(project.uid) / 'media/video.mp4').write_bytes(b'isolated source video')
    monkeypatch.setattr(cuts, '_duration', lambda *_: 100.)
    return project


def _body(uid, version_id=None, ranges=None, **extra):
    state = cuts.view(uid, version_id=version_id)
    return {
        'revision': state['revision'], 'source_fingerprint': state['source_fingerprint'],
        'enabled': True, 'ranges': ranges if ranges is not None else [
            {'id': f'{version_id or "project"}-cut', 'start': 10, 'end': 20,
             'reason': '测试删除', 'enabled': True}],
        **({'version_id': version_id} if version_id else {}), **extra,
    }


def test_version_plans_are_isolated_and_disabled_plan_does_not_fallback(project):
    from dubforge.domain import crud
    uid = project.uid
    cuts.save(uid, _body(uid, ranges=[{'id': 'global', 'start': 40, 'end': 50}]))
    cuts.save(uid, _body(uid, 'version-a', ranges=[{'id': 'a', 'start': 1, 'end': 2}]))
    cuts.save(uid, _body(uid, 'version-b', ranges=[{'id': 'b', 'start': 3, 'end': 4}]))

    deps = Dependencies(crud.get_project(uid))
    assert cuts.snapshot(deps, 'version-a')['ranges'] == [{'start': 1.0, 'end': 2.0}]
    assert cuts.snapshot(deps, 'version-b')['ranges'] == [{'start': 3.0, 'end': 4.0}]
    assert cuts.snapshot(deps)['ranges'] == [{'start': 40.0, 'end': 50.0}]
    assert cuts.plan_scope(deps, 'version-a') == 'version'

    disabled = _body(uid, 'version-b', enabled=False, ranges=[])
    cuts.save(uid, disabled)
    assert cuts.snapshot(deps, 'version-b') is None
    assert cuts.snapshot(deps, 'version-a')['ranges'] == [{'start': 1.0, 'end': 2.0}]
    persisted = json.loads((crud.project_dir(uid) / 'output_cuts.json').read_text(encoding='utf-8'))
    assert persisted['schema'] == 2 and set(persisted['versions']) == {'version-a', 'version-b'}
    cuts.save(uid, _body(uid, ranges=[{'id': 'global-new', 'start': 60, 'end': 70}]))
    persisted = json.loads((crud.project_dir(uid) / 'output_cuts.json').read_text(encoding='utf-8'))
    assert set(persisted['versions']) == {'version-a', 'version-b'}
    assert cuts.snapshot(Dependencies(crud.get_project(uid)), 'version-a')['ranges'] == [
        {'start': 1.0, 'end': 2.0}]


def test_old_assembly_can_seed_version_plan_and_keeps_baseline_identity(project, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline import assembly_versions
    from dubforge.pipeline.dependencies import Dependencies
    uid = project.uid
    artifact_path = crud.project_dir(uid) / 'output/old.mp4'
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b'old artifact')
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    global_state = cuts.view(uid)
    old_snapshot = {
        'name': '旧成片',
        'generated_at': '2026-01-01T00:00:00+00:00',
        'output_cuts': {'source_fingerprint': global_state['source_fingerprint'],
                        'ranges': [{'start': 10, 'end': 20}]},
    }
    monkeypatch.setattr(assembly_versions, 'results', lambda _uid: {
        'old': SimpleNamespace(artifact_hash=artifact_hash, snapshot=json.dumps(old_snapshot))})
    monkeypatch.setattr(assembly_versions, 'result_artifact', lambda *_args, **_kwargs:
                        SimpleNamespace(id=7, step_key='assemble', name='old.mp4',
                                         sha256=artifact_hash, size=artifact_path.stat().st_size))
    monkeypatch.setattr(Dependencies, 'path', lambda self, step, name: artifact_path)

    saved = cuts.save(uid, _body(uid, 'replacement', base_version_id='old'))
    assert saved['scope'] == 'version'
    assert saved['baseline']['version_id'] == 'old'
    assert saved['baseline']['artifact_id'] == 7
    assert saved['ranges'][0]['start'] == 10


def test_get_view_can_preview_an_old_baseline_without_persisting(project, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline import assembly_versions
    from dubforge.pipeline.dependencies import Dependencies
    uid = project.uid
    artifact_path = crud.project_dir(uid) / 'output/old.mp4'
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b'old artifact')
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    state = cuts.view(uid)
    old_snapshot = {'output_cuts': {'source_fingerprint': state['source_fingerprint'],
                                    'ranges': [{'start': 31.666, 'end': 35.5}]}}
    monkeypatch.setattr(assembly_versions, 'results', lambda _uid: {
        'old': SimpleNamespace(artifact_hash=artifact_hash, snapshot=json.dumps(old_snapshot))})
    monkeypatch.setattr(assembly_versions, 'result_artifact', lambda *_args, **_kwargs:
                        SimpleNamespace(id=8, step_key='assemble', name='old.mp4',
                                         sha256=artifact_hash, size=artifact_path.stat().st_size))
    monkeypatch.setattr(Dependencies, 'path', lambda self, step, name: artifact_path)
    preview = cuts.view(uid, version_id='new', baseline_version_id='old')
    assert preview['scope'] == 'project'
    assert preview['ranges'][0]['start'] == pytest.approx(31.666)
    assert not (crud.project_dir(uid) / 'output_cuts.json').exists()


def test_output_timeline_can_map_back_to_source_after_deletions():
    value = {'source_duration': 100., 'output_duration': 90.,
             'ranges': [{'start': 10., 'end': 20.}, {'start': 50., 'end': 60.}]}
    assert cuts.unmap_time(value, 9.5) == pytest.approx(9.5)
    assert cuts.unmap_time(value, 10.) == pytest.approx(20.)
    assert cuts.unmap_time(value, 10., bias='previous') == pytest.approx(10.)
    assert cuts.unmap_time(value, 40.) == pytest.approx(60.)
    assert cuts.unmap_time(value, 40., bias='previous') == pytest.approx(50.)
    assert cuts.map_interval(value, 5., 25.) == [
        {'start': 5., 'end': 10., 'output_start': 5., 'output_end': 10.},
        {'start': 20., 'end': 25., 'output_start': 10., 'output_end': 15.},
    ]


def test_version_cannot_use_itself_as_cut_baseline(project):
    uid = project.uid
    with pytest.raises(cuts.OutputCutsError) as exc:
        cuts.save(uid, _body(uid, 'version-a', base_version_id='version-a'))
    assert exc.value.code == 'OUTPUT_CUTS_BASE_INVALID'
