"""修订入口使用隔离项目与真实 FFmpeg，不调用模型或操作平台。"""
import copy
import json
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from dubforge import comment_studio, config, configuration, video_info
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions as av, review_render as rr, dubbing_session
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing.models import Publication
from dubforge.utils.fs import atomic_write_bytes


def _run(args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def _fixture(make_project, *, comments=False):
    uid = make_project()
    project, root = crud.get_project(uid), crud.project_dir(uid)
    video = root / 'media/video.mp4'
    _run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:size=320x180:rate=10:duration=3',
          '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264', '-preset', 'ultrafast',
          '-c:a', 'aac', str(video)])
    crud.add_artifact(project.id, 'download', 'video', video.name, video, 'media/video.mp4')
    entries = [('split', 'split.txt', '1. 0.500-1.500: The Tiananmen Square massacre\n2. 2.100-2.900: New caption\n'),
               ('translate', 'translate.txt', '1. 天安门广场的悲剧\n2. NEW CAPTION\n'),
               ('transcribe', 'src.srt', '1\n00:00:00,500 --> 00:00:01,500\nThe Tiananmen Square massacre\n\n')]
    for step, name, text in entries:
        path = root / 'artifacts' / step / name
        atomic_write_bytes(path, text.encode())
        crud.add_artifact(project.id, step, 'srt' if name.endswith('.srt') else 'txt', name, path,
                          str(path.relative_to(root)))
    definition = dict(id='initial', name='旧成片', dubbing=True, burn_subtitles=True,
                      include_comments=comments, overrides={'assemble.subtitle_track': 'trans',
                          'assemble.subtitle_font': 'Arial', 'assemble.subtitle_size': 100,
                          'assemble.crf': 18, 'subtitles.min_duration': 0})
    av.save(uid, dict(revision=0, versions=[definition]))
    overlay = None
    if comments:
        path = root / 'artifacts/download/video_info.json'
        atomic_write_bytes(path, json.dumps({'comments': {'items': [
            dict(id='safe', text='God bless your family', author='Viewer'),
            dict(id='blocked', text='The Tiananmen Square massacre', author='Other')]}}).encode())
        # 模拟审查功能启用前已确认的旧评论方案。
        data = configuration.resolve(uid)
        configuration.save('project:' + uid, dict(revision=data['revision'],
            updates={'content_review.enabled': False}))
        state = comment_studio.view(uid)
        comment_studio.save(uid, dict(revision=state['overlay_revision'], settings={
            **comment_studio.DEFAULT_SETTINGS, 'start': 0, 'interval': 1, 'duration': 1},
            items=[dict(r, display_text='COMMENT') for r in state['rows']]))
        overlay = comment_studio.confirmed(uid)
        data = configuration.resolve(uid)
        configuration.save('project:' + uid, dict(revision=data['revision'],
            updates={'content_review.enabled': True}))
    snapshot = av.resolve(Dependencies(project), definition, comment_plan=overlay)
    old = root / 'output/final_version_initial.mp4'
    old.parent.mkdir(exist_ok=True)
    # 模拟不可去掉的旧烧录内容；旧成片的声音已混音，与原视频声音不同。
    _run(['ffmpeg', '-y', '-v', 'error', '-i', str(video), '-f', 'lavfi', '-i', 'sine=frequency=880:duration=3',
          '-map', '0:v:0', '-map', '1:a:0', '-vf', 'drawbox=x=0:y=0:w=iw:h=80:color=red:t=fill',
          '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-t', '3', str(old)])
    art = crud.add_artifact(project.id, 'assemble', 'video', old.name, old, str(old.relative_to(root)))
    with crud.session() as session:
        session.add(AssemblyResult(key=f'{uid}:initial', project_uid=uid, version_id='initial',
            artifact_id=art.id, artifact_hash=art.sha256, fingerprint='old-pipeline-fingerprint',
            snapshot=json.dumps(snapshot, ensure_ascii=False)))
        session.add(Publication(id='published-fixture', project_uid=uid, status='rejected',
            snapshot=json.dumps({'parts': [{'artifact_id': art.id, 'sha256': art.sha256}]}),
            receipt=json.dumps({'bvid': 'fixture'})))
        session.commit()
    for key in ('split', 'translate', 'tts', 'assemble'):
        step = crud.get_step(project.id, key)
        step.status, step.input_fingerprint = 'stale', 'old-' + key
        crud.save_step(step)
    return uid, root, art, snapshot


def _frame(path, seconds):
    raw = _run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(path), '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
    return np.frombuffer(raw, np.uint8).reshape((180, 320, 3))


def _audio(path):
    raw = _run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ar', '16000', '-ac', '1',
                '-f', 'f32le', 'pipe:1'])
    return np.frombuffer(raw, np.float32)


def _version(uid, ident):
    deps = Dependencies(crud.get_project(uid))
    definition = next(v for v in av.catalog(uid)['versions'] if v['id'] == ident)
    return deps, av.resolve(deps, definition, comment_plan=None)


def test_repair_rebuilds_source_frames_current_subtitles_and_mutes_existing_mix(make_project, monkeypatch):
    uid, root, art, _ = _fixture(make_project)
    old, source = root / art.rel_path, root / 'media/video.mp4'
    hashes = {p: rr._sha(p) for p in (old, source)}
    steps = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)]
    with crud.session() as session:
        publication = copy.deepcopy(session.get(Publication, 'published-fixture').model_dump())
    from dubforge.pipeline import step_assemble
    monkeypatch.setattr(step_assemble, '_mix_timeline', lambda *a, **k: pytest.fail('不能重新混音或跑TTS'))
    updates = []
    result = rr.repair(uid, [art.id], progress=updates.append)
    item = result['repaired'][0]
    repaired = Path(item['path'])
    assert item['version_id'].startswith('review-initial-') and not item['reused']
    assert repaired.is_file() and repaired.with_suffix('.review.json').is_file()
    assert all(rr._sha(path) == digest for path, digest in hashes.items())
    # 旧成片上部为红色，新成片恢复原视频蓝色，而非在旧画面追加字幕。
    assert _frame(old, 1)[40, 40, 0] > 200
    assert _frame(repaired, 1)[40, 40, 2] > 200 and _frame(repaired, 1)[40, 40, 0] < 20
    assert np.abs(_frame(repaired, 1).astype(float) - _frame(source, 1)).mean() < 1
    assert np.abs(_frame(repaired, 2.5).astype(float) - _frame(source, 2.5)).sum() > 1000
    audio = _audio(repaired)
    assert np.sqrt(np.mean(audio[int(.8*16000):int(1.2*16000)] ** 2)) < .001
    unmuted = audio[int(2.2*16000):int(2.6*16000)]
    assert np.sqrt(np.mean(unmuted ** 2)) > .04
    frequency = np.fft.rfftfreq(len(unmuted), 1/16000)[np.abs(np.fft.rfft(unmuted)).argmax()]
    assert abs(frequency - 880) < 5  # 沿用成片声音，没有错用440Hz原声。
    assert [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)] == steps
    with crud.session() as session:
        assert session.get(Publication, 'published-fixture').model_dump() == publication
        record = session.get(AssemblyResult, f'{uid}:{item["version_id"]}')
    snapshot = json.loads(record.snapshot)
    assert snapshot['content_review_receipt'] == item['receipt']
    assert snapshot['generated_at'] == snapshot['review_render']['created_at']
    assert snapshot['generated_at'].endswith('+00:00')
    assert av.artifact_metadata(uid)[item['artifact_id']]['generated_at'] == snapshot['generated_at']
    assert '未重新进行语义整理' in snapshot['review_render']['note']
    deps, version = _version(uid, item['version_id'])
    assert rr.is_current(deps, version, verify_hash=True)
    assert not av.is_current(deps, version)
    visible = next(v for v in av.view(uid)['versions'] if v['id'] == item['version_id'])
    assert visible['fresh'] and '审查修订有效' in visible['status'] and '复用旧文字与音轨' in visible['status']
    assert '审查修订' in av.artifact_metadata(uid)[item['artifact_id']]['version_summary']
    assert any(u['phase'] == 'rendering' and u['seconds'] > 0 for u in updates)
    assert updates[-1]['phase'] == 'complete'
    assert not list(root.rglob('.tmp_review_*'))


def test_identical_repair_reuses_result_without_encoding(make_project, monkeypatch):
    uid, root, art, _ = _fixture(make_project)
    first = rr.repair(uid, [art.id])['repaired'][0]
    recipe = json.loads(av.results(uid)[first['version_id']].snapshot)['review_render']['recipe']
    assert 'comment_width_rule' not in recipe
    before = configuration.resolve(uid)['revision']
    monkeypatch.setattr(rr, '_render', lambda *a, **k: pytest.fail('相同输入应复用已核对修订文件'))
    second = rr.repair(uid, [art.id])['repaired'][0]
    assert second['reused'] and second['artifact_id'] == first['artifact_id']
    assert second['path'] == first['path'] and configuration.resolve(uid)['revision'] == before
    assert len(list((root / 'output').glob('final_version_review-*.mp4'))) == 1


def _separated_background(uid, root, *, frequency=150):
    project = crud.get_project(uid)
    path = root / 'audio/background.wav'
    _run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
          f'sine=frequency={frequency}:duration=3', '-ar', '32000', '-ac', '1', '-f', 'wav', str(path)])
    crud.add_artifact(project.id, 'preprocess', 'audio', 'background.wav', path, 'audio/background.wav')
    step = crud.get_step(project.id, 'preprocess')
    step.status, step.input_fingerprint = 'done', ''
    crud.save_step(step)
    return path


def _tone(samples, start, end, frequency, rate=16000):
    part = samples[round(start * rate):round(end * rate)]
    return abs(np.mean(part * np.exp(-2j * np.pi * frequency * np.arange(len(part)) / rate)))


def test_review_revision_restores_separated_background_and_audio_mode_invalidates_cache(make_project):
    uid, root, art, _ = _fixture(make_project)
    _separated_background(uid, root)
    data = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=data['revision'],
        updates={'content_review.audio_mode': 'all_audio'}))
    first = rr.repair(uid, [art.id])['repaired'][0]
    assert _tone(_audio(first['path']), .8, 1.2, 150) < .001
    data = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=data['revision'],
        updates={'content_review.audio_mode': 'voice_only'}))
    deps, version = _version(uid, first['version_id'])
    assert not rr.is_current(deps, version)
    second = rr.repair(uid, [art.id])['repaired'][0]
    assert second['artifact_id'] != first['artifact_id'] and not second['reused']
    audio = _audio(second['path'])
    assert _tone(audio, .8, 1.2, 150) > .03
    assert _tone(audio, .8, 1.2, 880) < .0003
    assert _tone(audio, 2.2, 2.6, 880) > .03
    assert _tone(audio, 2.2, 2.6, 150) < .0003
    processing = second['receipt']['audio_processing']
    assert processing['requested_mode'] == processing['effective_mode'] == 'voice_only'
    assert processing['background_identity'] and not processing['fallback_reason']
    stored = json.loads(av.results(uid)[second['version_id']].snapshot)['review_render']['recipe']
    assert any(row['name'] == 'background.wav' for row in stored['inputs'])
    assert stored['audio_processing']['background_identity']
    assert Path(first['path']).is_file()


def test_review_background_resource_change_invalidates_prior_revision(make_project):
    uid, root, art, _ = _fixture(make_project)
    _separated_background(uid, root)
    first = rr.repair(uid, [art.id])['repaired'][0]
    assert first['receipt']['audio_processing']['effective_mode'] == 'voice_only'
    _separated_background(uid, root, frequency=180)
    deps, version = _version(uid, first['version_id'])
    assert not rr.is_current(deps, version)
    second = rr.repair(uid, [art.id])['repaired'][0]
    assert second['artifact_id'] != first['artifact_id']
    assert _tone(_audio(second['path']), .8, 1.2, 180) > .03


def test_current_review_changes_invalidate_old_revision_and_create_new_history(make_project):
    uid, root, art, _ = _fixture(make_project)
    first = rr.repair(uid, [art.id])['repaired'][0]
    data = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=data['revision'],
        updates={'content_review.blocked_ranges': '0-2:人工核定'}))
    deps, version = _version(uid, first['version_id'])
    assert not rr.is_current(deps, version)
    state = rr.revision_state(deps, version)
    assert {'REVIEW_MANUAL_RANGES_CHANGED', 'REVIEW_EFFECT_CHANGED'} <= {r['code'] for r in state['reasons']}
    assert '人工核定' not in json.dumps(state, ensure_ascii=False)
    second = rr.repair(uid, [art.id])['repaired'][0]
    assert second['artifact_id'] != first['artifact_id'] and second['version_id'] != first['version_id']
    assert Path(first['path']).is_file() and Path(second['path']).is_file()


def test_comment_revision_is_rescreened_and_changes_invalidate_render(make_project):
    uid, _, art, _ = _fixture(make_project, comments=True)
    result = rr.repair(uid, [art.id])['repaired'][0]
    stored = json.loads(av.results(uid)[result['version_id']].snapshot)
    assert [r['id'] for r in stored['comment_overlay']['items']] == ['safe']
    body = comment_studio.view(uid)
    item = body['overlay']['items'][0]
    item['display_text'] = 'NEW COMMENT'
    comment_studio.save(uid, dict(revision=body['overlay_revision'], settings=body['overlay']['settings'], items=[item]))
    deps, version = _version(uid, result['version_id'])
    assert not rr.is_current(deps, version)
    assert 'REVIEW_COMMENTS_CHANGED' in {r['code'] for r in rr.revision_state(deps, version)['reasons']}


def test_new_comment_revision_records_width_rule_and_auto_equals_full_width(make_project):
    uid, _, art, _ = _fixture(make_project, comments=True)
    result = rr.repair(uid, [art.id])['repaired'][0]
    recipe = json.loads(av.results(uid)[result['version_id']].snapshot)['review_render']['recipe']
    assert recipe['comment_width_rule'] == 'available-area-v1'
    full = copy.deepcopy(recipe)
    full['comment_overlay']['settings']['width_percent'] = 100
    full['values']['comments.width_percent'] = 100
    assert rr._revision_recipe(recipe) == rr._revision_recipe(full)
    deps, version = _version(uid, result['version_id'])
    assert rr.is_current(deps, version)
    version['values']['comments.width_percent'] = 100
    assert rr.is_current(deps, version)


@pytest.mark.parametrize(('width', 'fresh'), [(0, False), (100, True), (80, True)])
def test_historical_revision_only_invalidates_changed_auto_width(make_project, width, fresh):
    uid, _, art, _ = _fixture(make_project, comments=True)
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:initial')
        snapshot = json.loads(record.snapshot)
        snapshot['comment_overlay']['settings']['width_percent'] = width
        snapshot['values']['comments.width_percent'] = width
        record.snapshot = json.dumps(snapshot, ensure_ascii=False)
        session.add(record); session.commit()
    result = rr.repair(uid, [art.id])['repaired'][0]
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:{result["version_id"]}')
        snapshot = json.loads(record.snapshot)
        info, recipe = snapshot['review_render'], snapshot['review_render']['recipe']
        recipe.pop('comment_width_rule')
        # 先保存真实旧配方的摘要，确保测试走有效历史兼容而非损坏记录拒绝。
        info['fingerprint'] = record.fingerprint = av.digest(recipe)
        record.snapshot = json.dumps(snapshot, ensure_ascii=False)
        session.add(record); session.commit()
    deps, version = _version(uid, result['version_id'])
    state = rr.revision_state(deps, version)
    assert state['current'] is fresh
    if not fresh:
        assert {reason['code'] for reason in state['reasons']} == {'REVIEW_RECIPE_CHANGED'}


def test_unused_current_comment_duration_keeps_frozen_revision_current(make_project):
    uid, _, art, _ = _fixture(make_project, comments=True)
    result = rr.repair(uid, [art.id])['repaired'][0]
    record = av.results(uid)[result['version_id']]
    previous = record.snapshot, record.fingerprint
    data = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=data['revision'],
        updates={'comments.duration': 2, 'comments.interval': 2}))
    deps, version = _version(uid, result['version_id'])
    state = rr.revision_state(deps, version, verify_hash=True)
    assert state == dict(current=True, reasons=[])
    assert (av.results(uid)[result['version_id']].snapshot, av.results(uid)[result['version_id']].fingerprint) == previous


def _legacy_record(uid, ident):
    """模拟真实旧配方，保存原摘要后才允许兼容比较。"""
    with crud.session() as session:
        row = session.get(AssemblyResult, f'{uid}:{ident}')
        snapshot = json.loads(row.snapshot)
        info, recipe = snapshot['review_render'], snapshot['review_render']['recipe']
        info['schema'] = recipe['schema'] = 'review-render-v1'
        recipe.pop('audio_processing', None)
        recipe['review_receipt'].pop('audio_processing', None)
        recipe['values'].pop('content_review.audio_mode', None)
        info['fingerprint'] = row.fingerprint = av.digest(recipe)
        row.snapshot = json.dumps(snapshot, ensure_ascii=False)
        session.add(row); session.commit()


def test_legacy_all_audio_recipe_is_compatible_when_effect_is_unchanged(make_project):
    uid, _, art, _ = _fixture(make_project)
    result = rr.repair(uid, [art.id])['repaired'][0]
    _legacy_record(uid, result['version_id'])
    deps, version = _version(uid, result['version_id'])
    # 没有可用分离背景，新的 voice_only 安全降级为原有全静音。
    assert rr.revision_state(deps, version, verify_hash=True) == dict(current=True, reasons=[])


def test_legacy_recipe_detects_new_effective_voice_only_audio(make_project, monkeypatch):
    uid, _, art, _ = _fixture(make_project)
    result = rr.repair(uid, [art.id])['repaired'][0]
    _legacy_record(uid, result['version_id'])
    prepare = rr._prepare
    def voice_only(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared['recipe']['audio_processing'] = dict(requested_mode='voice_only', effective_mode='voice_only',
            background_identity=dict(sha256='isolated-background', size=100), bgm_volume=1)
        return prepared
    monkeypatch.setattr(rr, '_prepare', voice_only)
    deps, version = _version(uid, result['version_id'])
    state = rr.revision_state(deps, version)
    assert not state['current']
    reason = next(r for r in state['reasons'] if r['code'] == 'REVIEW_AUDIO_MODE_CHANGED')
    assert reason['changes'][0]['before'] == '全部声音静音'
    assert '仅屏蔽人声' in reason['changes'][0]['after']
    assert 'REVIEW_VERSION_SETTINGS_CHANGED' not in {r['code'] for r in state['reasons']}


def test_damaged_recipe_is_rejected_before_normalization_and_never_exposes_text(make_project):
    uid, _, art, _ = _fixture(make_project)
    result = rr.repair(uid, [art.id])['repaired'][0]
    with crud.session() as session:
        row = session.get(AssemblyResult, f'{uid}:{result["version_id"]}')
        snapshot = json.loads(row.snapshot)
        snapshot['review_render']['recipe']['current_comment_plan'] = {'private_text': '不应对外显示的正文'}
        row.snapshot = json.dumps(snapshot)
        session.add(row); session.commit()
    deps, version = _version(uid, result['version_id'])
    state = rr.revision_state(deps, version)
    assert not state['current'] and state['reasons'][0]['code'] == 'REVIEW_HISTORY_INVALID'
    assert '不应对外显示' not in json.dumps(state, ensure_ascii=False)


def test_revision_file_missing_has_specific_safe_reason(make_project):
    uid, _, art, _ = _fixture(make_project)
    result = rr.repair(uid, [art.id])['repaired'][0]
    Path(result['path']).unlink()
    deps, version = _version(uid, result['version_id'])
    state = rr.revision_state(deps, version)
    assert not state['current'] and state['reasons'][0]['code'] == 'REVIEW_FILE_MISSING_OR_CHANGED'
    assert str(Path(result['path']).parent) not in json.dumps(state, ensure_ascii=False)


@pytest.mark.parametrize('fault,code', [('snapshot', 'REVIEW_RENDER_SNAPSHOT'),
    ('hash', 'REVIEW_RENDER_SOURCE_CHANGED'), ('duration', 'REVIEW_RENDER_DURATION'), ('busy', 'PIPELINE_BUSY')])
def test_invalid_inputs_fail_before_encoding_and_preserve_old_files(make_project, monkeypatch, fault, code):
    uid, root, art, _ = _fixture(make_project)
    old_bytes = (root / art.rel_path).read_bytes()
    if fault == 'snapshot':
        with crud.session() as session:
            row = session.get(AssemblyResult, f'{uid}:initial'); row.snapshot = '{}'
            session.add(row); session.commit()
    elif fault == 'hash':
        atomic_write_bytes(root / 'media/video.mp4', b'invalid replacement')
    elif fault == 'duration':
        probe = rr._media
        monkeypatch.setattr(rr, '_media', lambda path: {**probe(path), 'duration': 1} if Path(path).name == art.name else probe(path))
    else:
        step = crud.get_step(art.project_id, 'tts'); step.status = 'running'; crud.save_step(step)
    monkeypatch.setattr(rr, '_render', lambda *a, **k: pytest.fail('输入未通过不能编码'))
    with pytest.raises(DubForgeError) as exc:
        rr.repair(uid, [art.id])
    assert exc.value.code == code
    assert (root / art.rel_path).read_bytes() == old_bytes
    assert not list((root / 'output').glob('final_version_review-*'))
    assert uid not in dubbing_session._active and uid not in video_info._active


def test_publication_copy_generation_blocks_review_revision_before_encoding(make_project, monkeypatch):
    from dubforge.publishing import project_copy
    uid, root, art, _ = _fixture(make_project)
    old_bytes = (root / art.rel_path).read_bytes()
    monkeypatch.setattr(rr, '_render', lambda *a, **k: pytest.fail('运行门禁前不能编码'))
    project_copy._suggesting.add(uid)
    try:
        with pytest.raises(DubForgeError) as caught:
            rr.repair(uid, [art.id])
        assert caught.value.code == 'CONFIGURATION_BUSY'
    finally:
        project_copy._suggesting.discard(uid)
    assert (root / art.rel_path).read_bytes() == old_bytes
    assert uid not in dubbing_session._active and uid not in video_info._active


def test_commit_failure_rolls_back_catalog_artifact_result_and_only_removes_new_files(make_project, monkeypatch):
    uid, root, art, _ = _fixture(make_project)
    before = av.catalog(uid)
    old_bytes = (root / art.rel_path).read_bytes()
    replace = rr.atomic_replace
    def fail_receipt(source, target):
        if str(target).endswith('.review.json'):
            raise OSError('模拟提交失败')
        replace(source, target)
    monkeypatch.setattr(rr, 'atomic_replace', fail_receipt)
    with pytest.raises(rr.ReviewRenderError) as exc:
        rr.repair(uid, [art.id])
    assert exc.value.code == 'REVIEW_RENDER_IO' and '模拟提交失败' in exc.value.detail
    assert av.catalog(uid) == before
    assert set(av.results(uid)) == {'initial'}
    assert len(crud.latest_artifacts(art.project_id, ['assemble'])) == 1
    assert (root / art.rel_path).read_bytes() == old_bytes
    assert not list((root / 'output').glob('final_version_review-*'))
    assert uid not in dubbing_session._active and uid not in video_info._active


def test_config_change_during_encoding_rejects_result(make_project, monkeypatch):
    from test_publishing_studio_review import _project_setting
    uid, root, art, _ = _fixture(make_project)
    render = rr._render
    def changed(*args, **kwargs):
        out = render(*args, **kwargs)
        _project_setting(uid, 'content_review.blocked_ranges', '0-2:中途修改', external=True)
        return out
    monkeypatch.setattr(rr, '_render', changed)
    with pytest.raises(rr.ReviewRenderError) as exc:
        rr.repair(uid, [art.id])
    assert exc.value.code == 'REVIEW_RENDER_CONFLICT'
    assert not list((root / 'output').glob('final_version_review-*'))
    assert set(av.results(uid)) == {'initial'}


def test_cancel_preserves_old_outputs_and_clears_reservation(make_project):
    uid, root, art, _ = _fixture(make_project)
    event = threading.Event()
    def progress(info):
        if info['phase'] == 'rendering':
            event.set()
    with pytest.raises(rr.ReviewRenderError) as exc:
        rr.repair(uid, [art.id], progress=progress, cancel=event)
    assert exc.value.code == 'REVIEW_RENDER_CANCELLED'
    assert (root / art.rel_path).is_file() and set(av.results(uid)) == {'initial'}
    assert uid not in dubbing_session._active and uid not in video_info._active


def test_hash_verification_detects_unregistered_same_size_audio_source_change(make_project):
    uid, root, art, _ = _fixture(make_project)
    result = rr.repair(uid, [art.id])['repaired'][0]
    path = root / art.rel_path
    data = bytearray(path.read_bytes()); data[-1] ^= 1
    atomic_write_bytes(path, bytes(data))
    deps, version = _version(uid, result['version_id'])
    assert rr.is_current(deps, version, verify_hash=False)
    assert not rr.is_current(deps, version, verify_hash=True)


def test_nvenc_selection_is_explicit_and_part_of_provenance_without_requiring_gpu(make_project, monkeypatch):
    uid, _, art, _ = _fixture(make_project)
    popen = rr.subprocess.Popen
    calls = []
    def substitute_cpu_for_gpu(cmd, **kwargs):
        if 'h264_nvenc' in cmd:
            calls.append(cmd.copy())
            cmd = cmd.copy()
            cmd[cmd.index('h264_nvenc')] = 'libx264'
            cmd[cmd.index('p4')] = 'ultrafast'
            for key in ('-rc', '-cq', '-b:v'):
                index = cmd.index(key); del cmd[index:index+2]
            cmd[-1:-1] = ['-crf', '18']
        return popen(cmd, **kwargs)
    monkeypatch.setattr(rr.subprocess, 'Popen', substitute_cpu_for_gpu)
    software = rr.repair(uid, [art.id])['repaired'][0]
    hardware = rr.repair(uid, [art.id], encoder='h264_nvenc')['repaired'][0]
    assert software['version_id'] != hardware['version_id']
    assert calls and calls[0][calls[0].index('-cq')+1] == '18'
    assert calls[0][calls[0].index('-rc')+1] == 'vbr' and calls[0][calls[0].index('-b:v')+1] == '0'
    snapshot = json.loads(av.results(uid)[hardware['version_id']].snapshot)
    assert snapshot['review_render']['encoder']['name'] == 'h264_nvenc'
    assert '不代表' in snapshot['review_render']['encoder']['note']
    deps, version = _version(uid, hardware['version_id'])
    assert rr.is_current(deps, version, verify_hash=True)
