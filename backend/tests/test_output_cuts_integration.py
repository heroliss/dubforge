"""真实编码验证删除画面、音轨与烧录字幕共用时间轴，运行中编辑不热切换。"""
import json
import subprocess

import numpy as np
import pytest

from conftest import wait_status
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions, output_cuts, step_assemble
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assemble import _prepare_outputs_project


def _save_cut(uid, start=2, end=4):
    current = output_cuts.view(uid)
    return output_cuts.save(uid, dict(revision=current['revision'],
        source_fingerprint=current['source_fingerprint'], enabled=True,
        ranges=[dict(id='advert', start=start, end=end, reason='测试删除', enabled=True)]))


def _version(key='edited', **options):
    return dict(id=key, name='剪辑测试', dubbing=False, burn_subtitles=False,
                include_comments=False, overrides={}, **options)


def _start(uid, spec, preview=None):
    if preview:
        runner.start_versions(uid, dict(version=spec, start=preview[0], end=preview[1]), preview=True)
    else:
        versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[spec]))
        plan = versions.plan(uid, dict(version_ids=[spec['id']]))
        assert not plan['blockers']
        runner.start_versions(uid, dict(version_ids=[spec['id']], fingerprint=plan['fingerprint']))
    wait_status(uid, 'assemble', {'done'}, timeout=30)


def _rgb(path, seconds):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(path),
        '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(240, 320, 3)


def _colored_source(root):
    path = root / 'media/video.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=red:s=320x240:r=10:d=2',
        '-f', 'lavfi', '-i', 'color=green:s=320x240:r=10:d=2',
        '-f', 'lavfi', '-i', 'color=blue:s=320x240:r=10:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
        '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
        '-map', '[v]', '-map', '3:a', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'aac', str(path)], check=True, timeout=30)
    return path


def test_plain_source_version_becomes_edited_output_with_synchronized_audio(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    source = _colored_source(root)
    crud.add_artifact(crud.get_project(uid).id, 'download', 'video', 'video.mp4', source, 'media/video.mp4')
    before = source.read_bytes()
    _save_cut(uid)
    spec = _version()
    plan = versions.resolve(Dependencies(crud.get_project(uid)), spec)
    from dubforge.pipeline.version_render import uses_source_directly
    assert not uses_source_directly(Dependencies(crud.get_project(uid)), plan)
    _start(uid, spec)
    output = root / 'output/final_version_edited.mp4'
    assert abs(step_assemble._ffprobe_duration(output) - 4) < .15
    assert np.argmax(_rgb(output, .5).mean(axis=(0, 1))) == 0
    assert np.argmax(_rgb(output, 2.5).mean(axis=(0, 1))) == 2
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                               '-of', 'json', str(output)]))
    assert any(row['codec_type'] == 'audio' for row in probe['streams'])
    assert source.read_bytes() == before
    assert versions.view(uid)['versions'][0]['fresh']
    saved = json.loads(versions.results(uid)['edited'].snapshot)
    assert saved['output_cuts']['ranges'] == [dict(start=2, end=4)]
    assert not list(root.rglob('.tmp*'))


def test_preview_intersects_source_range_and_rejects_fully_deleted_selection(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    _save_cut(uid)
    _start(uid, _version(), preview=(1, 5))
    output = next((root / 'output').glob('preview_version_edited_*.mp4'))
    assert abs(step_assemble._ffprobe_duration(output) - 2) < .15
    previous_status = crud.get_step(crud.get_project(uid).id, 'assemble').status
    from dubforge.errors import DubForgeError
    with pytest.raises(DubForgeError) as failure:
        runner.start_versions(uid, dict(version=_version(), start=2.5, end=3), preview=True)
    assert failure.value.code == 'OUTPUT_CUTS_PREVIEW_EMPTY'
    assert crud.get_step(crud.get_project(uid).id, 'assemble').status == previous_status


def test_running_edit_is_saved_for_next_output_but_current_record_keeps_frozen_cut(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    _save_cut(uid)
    original_mux = step_assemble._mux_original_sub
    observed = []
    def save_during_encoding(ctx, *args, **kwargs):
        observed.append(ctx.version['output_cuts'])
        _save_cut(uid, 1, 2)
        assert ctx.version['output_cuts']['ranges'] == [dict(start=2, end=4)]
        return original_mux(ctx, *args, **kwargs)
    monkeypatch.setattr(step_assemble, '_mux_original_sub', save_during_encoding)
    _start(uid, _version())
    assert observed and observed[0]['ranges'] == [dict(start=2, end=4)]
    saved = json.loads(versions.results(uid)['edited'].snapshot)
    assert saved['output_cuts']['ranges'] == [dict(start=2, end=4)]
    assert output_cuts.snapshot(Dependencies(crud.get_project(uid)))['ranges'] == [dict(start=1, end=2)]
    assert not versions.view(uid)['versions'][0]['fresh']


@pytest.mark.parametrize('burn', [False, True])
def test_dub_mix_and_burned_subtitles_are_cut_in_the_same_mux(make_project, monkeypatch, burn):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    runner.start_step(uid, 'tts')
    wait_status(uid, 'tts', {'done'}, timeout=30)
    _save_cut(uid, 2, 3)
    spec = _version()
    spec.update(dubbing=True, burn_subtitles=burn)
    _start(uid, spec)
    output = root / 'output/final_version_edited.mp4'
    assert abs(step_assemble._ffprobe_duration(output) - 5) < .15
    assert versions.view(uid)['versions'][0]['fresh']


def test_changed_source_keeps_editor_available_but_blocks_generation(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    _save_cut(uid)
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[_version()]))
    (root / 'media/video.mp4').touch()
    assert output_cuts.view(uid)['source_changed']
    state = versions.view(uid)
    assert state['output_cuts_error'] and state['versions'][0]['status'] == '剪辑方案需核对'
    with pytest.raises(output_cuts.OutputCutsError, match='源视频已变化'):
        versions.plan(uid, dict(version_ids=['edited']))


def test_queued_task_adopts_latest_saved_cuts_when_execution_slot_is_granted(make_project, monkeypatch):
    import threading
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    _save_cut(uid)
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[_version()]))
    entered, proceed = threading.Event(), threading.Event()
    acquire = runner.acquire_slot
    def wait_for_edit(step, cancel):
        if step == 'assemble':
            entered.set()
            assert proceed.wait(10)
        return acquire(step, cancel)
    monkeypatch.setattr(runner, 'acquire_slot', wait_for_edit)
    plan = versions.plan(uid, dict(version_ids=['edited']))
    runner.start_versions(uid, dict(version_ids=['edited'], fingerprint=plan['fingerprint']))
    try:
        assert entered.wait(5)
        assert crud.get_step(crud.get_project(uid).id, 'assemble').status == 'queued'
        _save_cut(uid, 1, 2)
    finally:
        proceed.set()
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    saved = json.loads(versions.results(uid)['edited'].snapshot)
    assert saved['output_cuts']['ranges'] == [dict(start=1, end=2)]
    assert abs(step_assemble._ffprobe_duration(root / 'output/final_version_edited.mp4') - 5) < .15


def test_only_assembly_fingerprint_changes_when_saving_cuts(make_project, monkeypatch):
    from dubforge.runner import compute_fingerprint
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    project = crud.get_project(uid)
    before = {key: compute_fingerprint(project, key) for key in ('split', 'translate', 'tts', 'subtitles', 'assemble')}
    _save_cut(uid)
    after = {key: compute_fingerprint(project, key) for key in before}
    assert after['assemble'] != before['assemble']
    assert all(after[key] == before[key] for key in before if key != 'assemble')


def test_automatic_plan_keeps_plain_source_version_as_assembly_when_cutting(make_project, monkeypatch):
    from dubforge import workflow_plan
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    _save_cut(uid)
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[_version()]))
    plan = workflow_plan.plan(uid, dict(mode='produce', version_ids=['edited'],
        comments_mode='saved', production_steps=['assemble']), online=False)
    assert 'assemble' in plan['steps']
    assert plan['version_specs'][0]['output_cuts']['ranges'] == [dict(start=2, end=4)]


def test_silent_original_can_be_cut_without_inventing_an_audio_stream(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    source = root / 'media/video.mp4'
    silent = source.with_name('silent.tmp.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-an', '-c:v', 'copy', str(silent)],
                   check=True, timeout=30)
    silent.replace(source)
    crud.add_artifact(crud.get_project(uid).id, 'download', 'video', 'video.mp4', source, 'media/video.mp4')
    _save_cut(uid)
    _start(uid, _version())
    output = root / 'output/final_version_edited.mp4'
    assert abs(step_assemble._ffprobe_duration(output) - 4) < .15
    assert not step_assemble._has_audio_stream(output)
