"""局部修订计划、项目归属和普通版本流程；不调用真实模型或发布。"""
import copy
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import configuration, workflow_plan
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_partial as partial, assembly_versions as av
from dubforge.pipeline import segment_replace
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from test_assemble import _prepare_outputs_project
from test_configuration import save

_REAL_INSPECT = segment_replace.inspect_base
_REAL_WINDOWS = segment_replace.plan_windows


@pytest.fixture
def partial_project(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    definition = dict(id='base', name='原声基准', dubbing=False, burn_subtitles=True,
                      include_comments=False, overrides={})
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[definition]))
    deps = Dependencies(crud.get_project(uid))
    snapshot = av.resolve(deps, definition)
    source = deps.path('download', 'video.mp4')
    output = root / 'output' / 'base.mp4'
    output.parent.mkdir(exist_ok=True)
    shutil.copyfile(source, output)
    art = crud.add_artifact(deps.project.id, 'assemble', 'video', output.name, output, 'output/' + output.name)
    with crud.session() as session:
        session.add(AssemblyResult(key=uid + ':base', project_uid=uid, version_id='base', artifact_id=art.id,
                                   artifact_hash=art.sha256, fingerprint='old', snapshot=json.dumps(snapshot)))
        session.commit()
    duration = partial._source(deps)['duration']
    info = dict(duration=duration, fps_num=10, fps_den=1, frame_count=round(duration * 10), width=320, height=240,
                keyframe_frames=[0], x264_options={}, streams=[])
    monkeypatch.setattr(segment_replace, 'inspect_base', lambda path: copy.deepcopy(info))
    monkeypatch.setattr(segment_replace, 'plan_windows', lambda info, ranges: dict(expanded_ranges=ranges,
                                               encode_seconds=sum(r['end'] - r['start'] for r in ranges)))
    return uid, root, output, definition


def body():
    return dict(base_version_id='base', line_ids=[1], ranges=[dict(start=1, end=3.5)], replace_audio=False)


def create(uid, request=None):
    request = request or body()
    plan = partial.plan(uid, request)
    return partial.create(uid, {**request, 'fingerprint': plan['fingerprint']})


def test_ranges_merge_sentence_selection_and_context(partial_project):
    uid, _, _, _ = partial_project
    context = partial.context(uid)
    assert [row['version_id'] for row in context['baselines']] == ['base']
    assert [row['id'] for row in context['lines']] == [1, 2]
    plan = partial.plan(uid, body())
    assert plan['ranges'] == [dict(start=.5, end=3.5)]
    assert plan['encode_seconds'] == 3
    assert '范围外保留基准成片' in plan['summary']


@pytest.mark.parametrize(('known', 'old', 'expected'), [
    (True, dict(start=8, end=11), dict(start=8, end=15)),
    (False, dict(start=8, end=11), dict(start=12, end=15)),
    (True, dict(start=float('nan'), end=11), dict(start=12, end=15)),
    (True, dict(start=8, end=21), dict(start=12, end=15)),
])
def test_sentence_ranges_include_actual_known_baseline_location(monkeypatch, known, old, expected):
    from dubforge.pipeline import subtitle_document
    monkeypatch.setattr(subtitle_document, 'read', lambda *a, **kw: [dict(id=1, start=12, end=15)])
    monkeypatch.setattr(partial, '_subtitle_ranges', lambda *a: {1: dict(start=12, end=15)})
    saved = dict(dubbing=False, sentence_inputs=dict(schema=1, lines={
        '1': dict(id=1, known=known, range=old)}))
    assert partial._ranges(dict(line_ids=[1]), None, saved, 20) == [expected]


def test_update_all_plan_requires_recheck_if_actual_inputs_change(partial_project, monkeypatch):
    from dubforge.pipeline import assembly_sentence_changes
    from test_subtitle_document import edit
    uid, _, _, _ = partial_project
    monkeypatch.setattr(assembly_sentence_changes, 'view', lambda *a: dict(suggestion=dict(
        can_partial=True, replace_audio=False, ranges=[dict(start=.5, end=2)])))
    child = create(uid, {**body(), 'update_all': True})['version']
    deps = Dependencies(crud.get_project(uid))
    captured = partial.prepare(deps, child)
    assert captured['update_all'] and captured['expected_current_inputs_hash']
    assert edit(TestClient(app), uid, {'source_text': '未烧录的原文变化'}).status_code == 200
    assert partial.prepare(Dependencies(crud.get_project(uid)), child)['id'] == captured['id']
    assert edit(TestClient(app), uid, {'text': '后来再次修改的译文字幕'}).status_code == 200
    deps = Dependencies(crud.get_project(uid))
    assert partial.load_plan(deps, child)['id'] == captured['id'], '普通列表读取不增加逐句全量比较'
    with pytest.raises(partial.PartialError) as error:
        partial.prepare(deps, child)
    assert error.value.code == 'ASSEMBLY_UPDATE_RECHECK'


def test_manual_partial_keeps_explicit_scope_after_unrelated_edit(partial_project):
    from test_subtitle_document import edit
    uid, _, _, _ = partial_project
    child = create(uid)['version']
    assert edit(TestClient(app), uid, {'source_text': '手工局部之外的新字幕'}, line_id=2).status_code == 200
    assert not partial.prepare(Dependencies(crud.get_project(uid)), child).get('update_all')


def test_old_source_identity_ignores_later_display_metadata(partial_project):
    uid, root, _, _ = partial_project
    child = create(uid)['version']
    deps = Dependencies(crud.get_project(uid))
    value = partial.load_plan(deps, child)
    value['source_identity'].pop('frame_rate', None)
    value['fingerprint'] = partial._digest({key: item for key, item in value.items() if key != 'fingerprint'})
    path = root / 'output' / '.partial-plans' / (child['partial']['plan_id'] + '.json')
    path.write_text(json.dumps(value), encoding='utf-8')
    assert partial.load_plan(deps, child)['source_identity']['source_fingerprint']


def test_update_all_rejects_missing_changed_range(partial_project, monkeypatch):
    from dubforge.pipeline import assembly_sentence_changes
    uid, _, _, _ = partial_project
    monkeypatch.setattr(assembly_sentence_changes, 'view', lambda *a: dict(suggestion=dict(
        can_partial=True, replace_audio=False, ranges=[dict(start=4, end=5)])))
    with pytest.raises(partial.PartialError) as error:
        partial.plan(uid, {**body(), 'update_all': True})
    assert error.value.code == 'ASSEMBLY_UPDATE_RECHECK'


def test_force_full_rebuild_rejects_fixed_scope_partial_version(partial_project):
    uid, _, _, base = partial_project
    child = create(uid)['version']
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[base, child]))
    with pytest.raises(av.VersionError) as error:
        av.plan(uid, dict(version_ids=[child['id']], force_rebuild=True))
    assert error.value.code == 'ASSEMBLY_FORCE_FULL_ONLY'
    assert av.plan(uid, dict(version_ids=[child['id']]))['force_rebuild'] is False


def test_create_keeps_baseline_independent_and_uses_ordinary_version(partial_project):
    uid, root, output, _ = partial_project
    child = create(uid)['version']
    assert child['partial']['plan_id'] in child['id']
    assert [v['id'] for v in av.catalog(uid)['versions']] == ['base'], '添加计划不应隐式保存版本表单'
    deps = Dependencies(crud.get_project(uid))
    plan = partial.load_plan(deps, child, verify_hash=True)
    before = open(plan['base_path'], 'rb').read()
    replacement = output.with_suffix('.tmp')
    replacement.write_bytes(b'new base file')
    replacement.replace(output)
    assert open(plan['base_path'], 'rb').read() == before
    assert not list(root.rglob('.tmp-*'))
    assert plan['base_sha256'] == partial._hash(plan['base_path'])


def test_stale_confirmation_and_invalid_ranges_do_not_create_plan(partial_project):
    uid, root, _, _ = partial_project
    with pytest.raises(PartialError := partial.PartialError, match='重新检查'):
        partial.create(uid, {**body(), 'fingerprint': 'old'})
    assert not (root / 'output/.partial-plans').exists()
    for request in [{**body(), 'ranges': [{'start': -1, 'end': 1}]},
                    {**body(), 'line_ids': [999]}, {**body(), 'replace_audio': 'false'}]:
        with pytest.raises(PartialError):
            partial.plan(uid, request)


def test_plan_is_project_bound_and_integrity_checked(partial_project, make_project):
    uid, root, _, _ = partial_project
    child = create(uid)['version']
    other = Dependencies(crud.get_project(make_project()))
    with pytest.raises(partial.PartialError, match='跨项目'):
        partial.load_plan(other, child)
    plan_path = root / 'output/.partial-plans' / (child['partial']['plan_id'] + '.json')
    value = json.loads(plan_path.read_text(encoding='utf-8'))
    value['replace_audio'] = True
    plan_path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(partial.PartialError, match='有效'):
        partial.load_plan(Dependencies(crud.get_project(uid)), child)


def test_global_and_template_cannot_store_partial_project_ref(partial_project):
    uid, _, _, _ = partial_project
    child = create(uid)['version']
    for key in ['global', configuration.create_template('示例模板')['key']]:
        with pytest.raises(DubForgeError, match='不能保存到全局'):
            save(key, {'assembly.versions': [child]})


def test_partial_preserves_baseline_audio_without_tts_dependency(partial_project, monkeypatch):
    uid, _, _, base = partial_project
    # 模拟已有配音基准的成功快照；字幕修订不要求它的旧逐句音频仍然就绪。
    with crud.session() as session:
        row = session.get(AssemblyResult, uid + ':base')
        snapshot = json.loads(row.snapshot)
        snapshot['dubbing'] = True
        row.snapshot = json.dumps(snapshot)
        session.add(row)
        session.commit()
    child = create(uid)['version']
    assert child['dubbing'] is True
    deps = Dependencies(crud.get_project(uid))
    resolved = av.resolve(deps, child)
    assert not av.requires_dubbing(resolved)
    inputs = deps.for_version(resolved).inputs('assemble')
    assert not any(step in {'tts', 'preprocess'} for step, _, _ in inputs)
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[base, child]))
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *a, **kw: [])
    plan = workflow_plan.plan(uid, dict(mode='produce', production_steps=['assemble'], version_ids=[child['id']]), online=False)
    assert plan['version_specs'][0]['partial'] == child['partial']
    assert not plan['dubbing']['needed']
    assert 'tts' not in plan['steps']


def test_partial_cannot_mislabel_retained_audio_kind(partial_project):
    uid, _, _, _ = partial_project
    child = create(uid)['version']
    child['dubbing'] = True
    with pytest.raises(av.VersionError, match='保留基准'):
        av.resolve(Dependencies(crud.get_project(uid)), child)


def test_api_create_plan_and_fingerprint_are_shared(partial_project):
    uid, _, _, _ = partial_project
    client = TestClient(app)
    base = f'/api/projects/{uid}/assembly-versions'
    assert client.get(base + '/partial-context').status_code == 200
    response = client.post(base + '/partial-plan', json=body())
    assert response.status_code == 200, response.text
    created = client.post(base + '/partial-create', json={**body(), 'fingerprint': response.json()['fingerprint']})
    assert created.status_code == 200, created.text
    assert created.json()['version']['partial']


def test_sentence_ranges_include_actual_subtitle_minimum_duration(partial_project):
    uid, _, _, _ = partial_project
    save('project:' + uid, {'subtitles.min_duration': 2.4})
    plan = partial.plan(uid, dict(base_version_id='base', line_ids=[1], replace_audio=False))
    assert plan['ranges'] == [dict(start=.5, end=2.9)]
    row = partial.context(uid)['lines'][0]
    assert row['end'] == 2.9 and row['source_end'] == 2


def test_source_frame_geometry_mismatch_is_actionable(partial_project, monkeypatch):
    uid, _, _, _ = partial_project
    monkeypatch.setattr(partial, '_source_media', lambda *a: dict(width=640, height=240, fps_num=10, fps_den=1))
    with pytest.raises(partial.PartialError, match='画幅或帧率'):
        partial.plan(uid, body())


def test_missing_plan_still_leaves_version_workspace_repairable(partial_project):
    uid, root, _, base = partial_project
    child = create(uid)['version']
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[base, child]))
    (root / 'output/.partial-plans' / (child['partial']['plan_id'] + '.json')).unlink()
    view = av.view(uid)
    row = next(v for v in view['versions'] if v['id'] == child['id'])
    assert row['status'] == '局部修订需核对' and not row['fresh']
    assert '找不到' in row['partial_summary']['summary']


def test_baseline_tamper_is_rechecked_at_execution(partial_project):
    uid, _, _, _ = partial_project
    child = create(uid)['version']
    deps = Dependencies(crud.get_project(uid))
    value = partial.load_plan(deps, child)
    with open(value['base_path'], 'wb') as stream:
        stream.write(b'broken baseline')
    with pytest.raises(partial.PartialError, match='基准副本已变化'):
        partial.prepare(deps, child)


def test_partial_caption_fingerprint_tracks_events_but_ignores_audio_controls(partial_project):
    uid, _, _, _ = partial_project
    child = create(uid)['version']
    deps = Dependencies(crud.get_project(uid))
    before = av.fingerprint(deps, av.resolve(deps, child))
    save('project:' + uid, {'assemble.voice_volume': .3, 'assemble.bgm_volume': .8,
                          'assemble.final_loudness_lufs': -14})
    deps = Dependencies(crud.get_project(uid))
    assert av.fingerprint(deps, av.resolve(deps, child)) == before
    save('project:' + uid, {'subtitles.min_duration': 2.4})
    deps = Dependencies(crud.get_project(uid))
    assert av.fingerprint(deps, av.resolve(deps, child)) != before


def test_real_media_inspector_and_window_contract(partial_project, monkeypatch):
    uid, _, _, _ = partial_project
    monkeypatch.setattr(segment_replace, 'inspect_base', _REAL_INSPECT)
    monkeypatch.setattr(segment_replace, 'plan_windows', _REAL_WINDOWS)
    partial._inspect.cache_clear()
    plan = partial.plan(uid, body())
    assert plan['base_spec']['frame_count'] == 60
    assert plan['expanded_ranges'] == [dict(start=0., end=6.)]
    assert plan['encode_seconds'] == 6


def test_child_pins_base_crf_and_rejects_changed_encoder_quality(partial_project):
    uid, _, _, _ = partial_project
    save('project:' + uid, {'assemble.crf': 28})
    child = create(uid)['version']
    assert child['overrides']['assemble.crf'] == 18
    resolved = av.resolve(Dependencies(crud.get_project(uid)), child)
    assert resolved['values']['assemble.crf'] == 18
    child['overrides']['assemble.crf'] = 28
    with pytest.raises(av.VersionError, match='编码画质'):
        av.resolve(Dependencies(crud.get_project(uid)), child)


def test_valid_base_audio_report_is_frozen_with_base_hash(partial_project):
    uid, _, output, _ = partial_project
    from dubforge.pipeline.audio_quality import write_report
    write_report(output, dict(has_audio=True, integrated_lufs=-16., true_peak_db=-1.5))
    child = create(uid)['version']
    plan = partial.load_plan(Dependencies(crud.get_project(uid)), child)
    report = plan['baseline_audio_report']
    assert report['base_sha256'] == plan['base_sha256']
    assert report['report']['integrated_lufs'] == -16.


def test_original_update_maps_only_unique_selected_revision_by_exact_baseline_identity(partial_project, monkeypatch):
    uid, _, _, base = partial_project
    from dubforge import workflow_update
    from dubforge.publishing import updates
    deps = Dependencies(crud.get_project(uid))
    child = av.resolve(deps, create(uid)['version'])
    plan = child['partial_plan']
    old = dict(artifact_id=plan['base_artifact_id'], sha256=plan['base_sha256'], version_id='base')
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {7: (old, {})})
    remote = dict(videos=[dict(cid=7)])
    assert workflow_update._versions(uid, {}, remote, [child]) == {7: child['id']}
    assert workflow_update._versions(uid, {}, remote, [base, child]) == {7: 'base'}
    second = av.resolve(deps, create(uid)['version'])
    assert workflow_update._versions(uid, {}, remote, [child, second]) == {7: ''}
    old['sha256'] = 'wrong-baseline'
    assert workflow_update._versions(uid, {}, remote, [child]) == {7: ''}
    old['sha256'] = plan['base_sha256']
    old['artifact_id'] += 1
    assert workflow_update._versions(uid, {}, remote, [child]) == {7: ''}
