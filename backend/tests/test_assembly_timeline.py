"""已有成片剪辑的真实媒体与冻结范围回归，不调用模型或B站。"""
import copy
import json
import shutil

import pytest

from dubforge import workflow_plan
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_timeline as timeline, assembly_versions as av, output_cuts
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assemble import _prepare_outputs_project
from test_assembly_versions import wait


@pytest.fixture
def timeline_project(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    definition = dict(id='base', name='旧成片', dubbing=False, burn_subtitles=True,
                      include_comments=True, overrides={})
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[definition]))
    deps = Dependencies(crud.get_project(uid))
    saved = av.resolve(deps, definition)
    source = deps.path('download', 'video.mp4')
    duration = output_cuts.source_context(deps)['duration']
    artifact = deps.arts['download/video.mp4']
    # 旧局部成片没有完整manifest，但保存了原视频字节身份和明确的空剪辑。
    saved['sentence_inputs'] = dict(schema=1, coverage='known', duration=duration,
        lines={}, global_inputs=dict(source=dict(sha256=artifact.sha256, size=artifact.size), output_cuts=None),
        comment_inputs=dict(known=False))
    out = root / 'output' / 'base.mp4'
    out.parent.mkdir(exist_ok=True)
    shutil.copyfile(source, out)
    art = crud.add_artifact(deps.project.id, 'assemble', 'video', out.name, out, 'output/' + out.name)
    with crud.session() as session:
        session.add(AssemblyResult(key=uid + ':base', project_uid=uid, version_id='base', artifact_id=art.id,
            artifact_hash=art.sha256, fingerprint='historic', snapshot=json.dumps(saved)))
        session.commit()
    return uid, root, definition, out


def request(base='base', ranges=None, donor=None):
    return dict(base_version_id=base, draft=dict(enabled=True,
        ranges=ranges if ranges is not None else [dict(start=1., end=2., enabled=True)]),
        donor=donor or dict(kind='source'))


def make_version(uid, body):
    plan = timeline.plan(uid, body)
    return timeline.create(uid, {**body, 'fingerprint': plan['fingerprint']})['version']


def save_child(uid, child):
    catalog = av.catalog(uid)
    av.save(uid, dict(revision=catalog['revision'], versions=[*catalog['versions'], child]))


def test_delete_restore_and_other_donor_use_real_media_and_are_idempotent(timeline_project, monkeypatch):
    uid, root, _, old = timeline_project
    old_hash = timeline._hash(old)
    child = make_version(uid, request())
    save_child(uid, child)
    # 本次只读旧成片，不允许重新确认评论或使用现有的字幕生成器。
    from dubforge import comment_studio
    from dubforge.pipeline import version_render
    monkeypatch.setattr(comment_studio, 'confirmed', lambda *a, **kw: pytest.fail('不应重新确认评论'))
    monkeypatch.setattr(version_render, 'subtitle_lines', lambda *a: pytest.fail('不应重绘当前字幕'))
    candidate = av.plan(uid, dict(version_ids=[child['id']]))
    assert not candidate['blockers']
    assert Dependencies(crud.get_project(uid)).with_versions(candidate['versions']).inputs('assemble') == []
    runner.start_versions(uid, dict(version_ids=[child['id']], fingerprint=candidate['fingerprint']))
    wait(uid)
    deps = Dependencies(crud.get_project(uid))
    artifact = av.result_artifact(deps, child)
    path = deps.path('assemble', artifact.name)
    from dubforge.pipeline.virtual_preview import _probe, context
    assert abs(_probe(path)['duration'] - 5.) < .08
    assert context(uid, child['id'])['artifact_cuts']['ranges'] == [dict(start=1., end=2.)]
    assert timeline._hash(old) == old_hash
    snapshot = json.loads(av.results(uid)[child['id']].snapshot)
    assert snapshot['output_cuts']['ranges'] == [dict(start=1., end=2.)]
    assert snapshot['sentence_inputs']['global_inputs']['output_cuts'] == snapshot['output_cuts']
    assert snapshot['content_review_receipt']['scope'] == 'timeline'
    assert not json.loads(crud.get_step(deps.project.id, 'assemble').input_snapshot)['incomplete_comments']
    stamp = path.stat().st_mtime_ns
    # 同一不可变计划再次运行，正常成片幂等分支直接复用。
    assert child['id'] in av.plan(uid, dict(version_ids=[child['id']]))['reusable']
    candidate = av.plan(uid, dict(version_ids=[child['id']]))
    runner.start_versions(uid, dict(version_ids=[child['id']], fingerprint=candidate['fingerprint']))
    wait(uid)
    assert path.stat().st_mtime_ns == stamp
    # 从另一个成片恢复删除范围；非恢复范围仍取已剪辑成片，不重混声音。
    restored = make_version(uid, request(child['id'], [], dict(kind='version', version_id='base')))
    save_child(uid, restored)
    value = timeline.load_plan(Dependencies(crud.get_project(uid)), restored, verify=True)
    assert value['restored_ranges'] == [dict(start=1., end=2.)]
    assert [row['media_kind'] for row in value['segments']] == ['target', 'version', 'target']
    candidate = av.plan(uid, dict(version_ids=[restored['id']]))
    runner.start_versions(uid, dict(version_ids=[restored['id']], fingerprint=candidate['fingerprint']))
    wait(uid)
    deps = Dependencies(crud.get_project(uid))
    restored_artifact = av.result_artifact(deps, restored)
    assert abs(_probe(deps.path('assemble', restored_artifact.name))['duration'] - 6.) < .08
    assert context(uid, restored['id'])['artifact_cuts']['ranges'] == []
    assert not list(root.rglob('.tmp_pending*'))


def test_plan_freezes_media_and_ignores_working_configuration(timeline_project):
    uid, _, base, old = timeline_project
    child = make_version(uid, request())
    save_child(uid, child)
    deps = Dependencies(crud.get_project(uid))
    before = av.fingerprint(deps, av.resolve(deps, child))
    old_bytes = old.read_bytes()
    pending = old.with_suffix('.replacement')
    pending.write_bytes(b'replaced existing version')
    pending.replace(old)
    value = timeline.load_plan(Dependencies(crud.get_project(uid)), child, verify=True)
    assert value['media'][0]['sha256'] == timeline._hash(value['media'][0]['path'])
    assert open(value['media'][0]['path'], 'rb').read() == old_bytes
    from test_configuration import save
    save('project:' + uid, {'assemble.subtitle_color': '#123456', 'assemble.final_loudness_lufs': -14})
    deps = Dependencies(crud.get_project(uid))
    assert av.fingerprint(deps, av.resolve(deps, child)) == before
    assert av.resolve(deps, child)['values']['assemble.subtitle_color'] != '#123456'


def test_render_uses_frozen_media_after_current_source_is_removed(timeline_project, monkeypatch):
    uid, _, _, _ = timeline_project
    child = make_version(uid, request())
    save_child(uid, child)
    deps = Dependencies(crud.get_project(uid))
    deps.path('download', 'video.mp4').unlink()
    from dubforge.pipeline import assembly_timeline
    seen = []
    render = assembly_timeline.render
    def capture(ctx, out):
        seen.append(True)
        return render(ctx, out)
    monkeypatch.setattr(assembly_timeline, 'render', capture)
    candidate = av.plan(uid, dict(version_ids=[child['id']]))
    assert not candidate['blockers']
    runner.start_versions(uid, dict(version_ids=[child['id']], fingerprint=candidate['fingerprint']))
    wait(uid)
    assert seen
    assert av.view(uid)['versions'][-1]['fresh']


def test_recheck_and_media_tamper_are_rejected(timeline_project):
    uid, root, _, _ = timeline_project
    body = request()
    with pytest.raises(timeline.TimelineError, match='重新检查'):
        timeline.create(uid, {**body, 'fingerprint': 'old'})
    assert not (root / 'output/.timeline-plans').exists()
    child = make_version(uid, body)
    value = timeline.load_plan(Dependencies(crud.get_project(uid)), child)
    path = value['media'][0]['path']
    with open(path, 'r+b') as stream:
        stream.seek(20); stream.write(b'changed!')
    with pytest.raises(timeline.TimelineError):
        timeline.load_plan(Dependencies(crud.get_project(uid)), child, verify=True)


def test_timeline_joins_auto_plan_without_comments_or_tts(timeline_project, monkeypatch):
    uid, _, _, _ = timeline_project
    child = make_version(uid, request())
    save_child(uid, child)
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *a, **kw: [])
    automatic = workflow_plan.plan(uid, dict(mode='produce', version_ids=[child['id']],
        production_steps=['assemble']), online=False)
    assert automatic['version_specs'][0]['timeline'] == child['timeline']
    assert not any(row.get('step') == 'comments' for row in automatic.get('blockers', []))
    assert not av.requires_dubbing(automatic['version_specs'][0])


def test_frozen_media_does_not_silently_accept_changed_dimensions(timeline_project):
    uid, _, _, _ = timeline_project
    child = make_version(uid, request())
    for patch in [dict(dubbing=True), dict(burn_subtitles=False), dict(include_comments=False),
                  dict(overrides={'assemble.crf': 30})]:
        with pytest.raises(timeline.TimelineError) as error:
            timeline.resolve(Dependencies(crud.get_project(uid)), {**child, **patch})
        assert error.value.code == 'ASSEMBLY_TIMELINE_FROZEN'


def test_cancel_before_encoding_leaves_old_output_and_no_completed_file(timeline_project):
    from types import SimpleNamespace
    uid, root, _, old = timeline_project
    child = make_version(uid, request())
    old_hash = timeline._hash(old)
    out = root / 'output' / 'never.mp4'
    def cancelled():
        raise DubForgeError('已取消', code='JOB_CANCELLED')
    ctx = SimpleNamespace(dependencies=lambda: Dependencies(crud.get_project(uid)), version=child,
                          check_cancel=cancelled)
    with pytest.raises(DubForgeError, match='已取消'):
        timeline.render(ctx, out)
    assert not out.exists()
    assert timeline._hash(old) == old_hash


def test_smart_cut_records_actual_strategy_and_frame_aligned_source_ranges(timeline_project, monkeypatch):
    from test_segment_replace import ff
    uid, root, _, old = timeline_project
    # 给已登记成片建立含完整 GOP 的封闭编码，仍为同一原片的 6 秒时间轴。
    pending = old.with_name('closed-gop.mp4')
    ff('-i', old, '-c:v', 'libx264', '-threads', '2', '-preset', 'medium', '-crf', '18',
       '-g', '20', '-keyint_min', '20', '-sc_threshold', '0', '-bf', '3',
       '-x264-params', 'open-gop=0:repeat-headers=1', '-c:a', 'copy', pending)
    pending.replace(old)
    deps = Dependencies(crud.get_project(uid))
    art = crud.add_artifact(deps.project.id, 'assemble', 'video', old.name, old, 'output/' + old.name,
                            replace_existing=True)
    with crud.session() as session:
        row = session.get(AssemblyResult, uid + ':base')
        row.artifact_hash, row.artifact_id = art.sha256, art.id
        session.add(row); session.commit()
    body = request(ranges=[dict(start=2.411, end=2.589)])
    value = timeline.plan(uid, body)
    assert value['encoding']['method'] == 'smart_cut'
    assert value['encoding']['copied_seconds'] == 4
    assert value['encoding']['encoded_seconds'] == pytest.approx(1.8)
    assert value['ranges'] == [dict(start=2.4, end=2.6)]
    assert value['output_duration'] == pytest.approx(5.8)
    assert value['requested_ranges'] != value['ranges']
    child = make_version(uid, body)
    save_child(uid, child)
    candidate = av.plan(uid, dict(version_ids=[child['id']]))
    runner.start_versions(uid, dict(version_ids=[child['id']], fingerprint=candidate['fingerprint']))
    wait(uid)
    row = av.view(uid)['versions'][-1]
    assert row['fresh']
    assert row['timeline_summary']['actual_encoding']['method'] == 'smart_cut'
    snapshot = json.loads(av.results(uid)[child['id']].snapshot)
    assert snapshot['actual_encoding']['validated']
    assert snapshot['output_cuts']['ranges'] == value['ranges']
    deps = Dependencies(crud.get_project(uid))
    artifact = av.result_artifact(deps, child)
    rendered_path = deps.path('assemble', artifact.name)
    modified = rendered_path.stat().st_mtime_ns
    reusable = av.plan(uid, dict(version_ids=[child['id']]))
    assert reusable['reusable'] == [child['id']]
    runner.start_versions(uid, dict(version_ids=[child['id']], fingerprint=reusable['fingerprint']))
    wait(uid)
    assert rendered_path.stat().st_mtime_ns == modified
    # 新建子剪辑按已实际量化的源坐标继续映射，不复用原来未对齐的草稿。
    next_value = timeline.plan(uid, request(child['id'], ranges=[dict(start=2.4, end=2.6), dict(start=4., end=4.5)]))
    assert next_value['output_duration'] == pytest.approx(5.3)
    assert next_value['ranges'] == [dict(start=2.4, end=2.6), dict(start=4., end=4.5)]


def test_smart_validation_failure_falls_back_but_cancel_and_source_change_do_not(timeline_project, monkeypatch):
    from types import SimpleNamespace
    from dubforge.pipeline import segment_cut, segment_replace
    uid, root, _, _ = timeline_project
    child = make_version(uid, request())
    deps = Dependencies(crud.get_project(uid))
    value = timeline.load_plan(deps, child)
    value['_smart_cut'] = {'method': 'smart_cut'}
    value['encoding'] = dict(method='smart_cut', copied_seconds=4., encoded_seconds=1., output_duration=5.)
    monkeypatch.setattr(timeline, 'load_plan', lambda *a, **kw: copy.deepcopy(value))
    called = []
    monkeypatch.setattr(timeline, '_render_full', lambda *a: called.append('full'))
    monkeypatch.setattr(timeline, '_finish', lambda *a: None)
    def incompatible(*a, **kw):
        raise segment_replace.SegmentReplaceError('SPS 不同', code='SEGMENT_REPLACE_INCOMPATIBLE')
    monkeypatch.setattr(segment_cut, 'render', incompatible)
    ctx = SimpleNamespace(dependencies=lambda: deps, version=child, check_cancel=lambda: None,
        cfg=lambda _key, default=None: default, log=lambda _text: None)
    timeline.render(ctx, root / 'output' / 'fallback.mp4')
    assert called == ['full']
    assert ctx.version['actual_encoding']['method'] == 'full_encode'
    assert ctx.version['actual_encoding']['fallback_code'] == 'SEGMENT_REPLACE_INCOMPATIBLE'
    def source_changed(*a, **kw):
        raise segment_replace.SegmentReplaceError('来源变化', code='SEGMENT_CUT_BASE_CHANGED')
    monkeypatch.setattr(segment_cut, 'render', source_changed)
    with pytest.raises(segment_replace.SegmentReplaceError):
        timeline.render(ctx, root / 'output' / 'never.mp4')
    assert called == ['full']
