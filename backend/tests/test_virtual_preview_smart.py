"""虚拟试听复用接缝编码、独立缓存与实际量化时间轴。"""
import copy
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from dubforge.pipeline import virtual_preview as vp, segment_cut
from test_segment_replace import media, frame_hashes


@pytest.fixture
def preview(media, tmp_path, monkeypatch):
    project = SimpleNamespace(uid='smart-preview-fixture')
    monkeypatch.setattr(vp.crud, 'get_project', lambda _uid: project)
    monkeypatch.setattr(vp.crud, 'project_dir_by_project', lambda _project: tmp_path)
    monkeypatch.setattr(vp, 'Dependencies', lambda _project: SimpleNamespace(root=tmp_path, project=project))
    monkeypatch.setattr(vp.output_cuts, 'source_context', lambda _deps: dict(duration=6., source_fingerprint='same'))
    monkeypatch.setattr(vp, 'resolve_video', lambda *_args: media)
    monkeypatch.setattr(vp, '_version_media', lambda *_args, **_kw: (media,
        dict(source_duration=6., source_fingerprint='same', ranges=[]),
        dict(version_id='base', artifact_hash=vp._path_hash(media)[1])))
    monkeypatch.setattr(vp.crud, 'add_artifact', lambda *a, **kw: pytest.fail('试听不能登记正式产物'))
    monkeypatch.setattr(vp.assembly_versions, 'record_result', lambda *a, **kw: pytest.fail('试听不能更新成片记录'))
    yield project, tmp_path, media
    for task_id, task in list(vp._tasks.items()):
        if task.get('uid') == project.uid:
            if task.get('status') in {'queued', 'running'} and task.get('cancel'):
                task['cancel'].set()
            if task.get('future'):
                task['future'].result(timeout=10)
            vp._tasks.pop(task_id, None)


def body(**changes):
    return dict(version_id='base', source_fingerprint='same', draft=dict(enabled=True,
        ranges=[dict(start=2.411, end=2.589)]), donor=dict(kind='source'), **changes)


def finish(uid, task):
    vp._tasks[task['task_id']]['future'].result(timeout=30)
    result = vp.status(uid, task['task_id'])
    assert result['status'] == 'done', result
    return result


def test_real_smart_preview_reuses_gops_and_completed_cache(preview, monkeypatch):
    project, root, media = preview
    result = finish(project.uid, vp.start(project.uid, body()))
    assert result['encoding']['method'] == 'smart_cut'
    assert result['encoding']['validated']
    assert result['frame_rate'] == pytest.approx(30.)
    assert result['encoding']['copied_seconds'] == 4
    assert result['duration'] == pytest.approx(5.8)
    assert [(row['source_start'], row['source_end']) for row in result['segments']] == [(0., 2.4), (2.6, 6.)]
    assert result['segments'][-1]['virtual_end'] == pytest.approx(result['duration'])
    path = vp._output_path(root, result['task_id'])
    source, output = frame_hashes(media), frame_hashes(path)
    assert len(output) == 174
    assert output[:60] == source[:60]
    assert output[114:] == source[120:]
    modified = path.stat().st_mtime_ns
    def no_render(*a, **kw):
        pytest.fail('同一有效缓存不应重新编码')
    monkeypatch.setattr(segment_cut, 'render', no_render)
    cached = vp.start(project.uid, body())
    assert cached['task_id'] == result['task_id']
    assert cached['cache_hit'] and cached['status'] == 'done'
    assert cached['frame_rate'] == pytest.approx(30.)
    assert path.stat().st_mtime_ns == modified
    # 进程内任务列表被清除（类似服务重启）也能读取持久化的已完成缓存。
    vp._tasks.pop(result['task_id'])
    cached = vp.start(project.uid, body())
    assert cached['task_id'] == result['task_id'] and cached['cache_hit']


def test_missing_cache_output_or_changed_file_version_is_not_reused(preview):
    project, root, media = preview
    first = finish(project.uid, vp.start(project.uid, body()))
    vp._output_path(root, first['task_id']).unlink()
    second = vp.start(project.uid, body())
    assert not second['cache_hit'] and second['task_id'] != first['task_id']
    second = finish(project.uid, second)
    media.touch()
    third = vp.start(project.uid, body())
    assert not third['cache_hit'] and third['cache_key'] != second['cache_key']
    finish(project.uid, third)


def test_short_source_window_uses_actual_quantized_coordinates(preview):
    project, root, _ = preview
    result = finish(project.uid, vp.start(project.uid, body(preview_range=dict(start=1.011, end=5.989))))
    assert result['preview_range'] == dict(start=1.011, end=5.989)
    assert result['applied_preview_range']['start'] == pytest.approx(31/30)
    assert result['applied_preview_range']['end'] == pytest.approx(179/30)
    assert result['duration'] == pytest.approx((72 - 31 + 179 - 78) / 30)
    assert result['segments'][0]['virtual_start'] == 0
    assert result['segments'][-1]['virtual_end'] == pytest.approx(result['duration'])
    assert vp._probe(vp._output_path(root, result['task_id']))['duration'] == pytest.approx(result['duration'], abs=.08)


def test_smart_cancel_stops_current_child_when_it_has_no_stdout(preview, monkeypatch):
    project, root, _ = preview
    started = threading.Event()
    real_popen = subprocess.Popen
    def silent(*_args, **_kw):
        proc = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started.set()
        return proc
    monkeypatch.setattr(vp, 'subprocess', SimpleNamespace(**{**vars(subprocess), 'Popen': silent}))
    task = vp.start(project.uid, body())
    assert started.wait(5)
    before = time.monotonic()
    vp.cancel(project.uid, task['task_id'])
    vp._tasks[task['task_id']]['future'].result(timeout=8)
    assert time.monotonic() - before < 6
    assert vp.status(project.uid, task['task_id'])['status'] == 'cancelled'
    assert not vp._output_path(root, task['task_id']).exists()


def test_quantized_plan_cache_key_includes_renderer_and_keeps_restore_on_full_path(preview, monkeypatch):
    project, _, _ = preview
    plan = vp._build_plan(project.uid, body())
    vp._prepare_render(plan)
    changed = copy.deepcopy(plan)
    changed['preview_range'] = dict(start=.1, end=6.)
    vp._prepare_render(changed)
    assert plan['cache_key'] != changed['cache_key']
    monkeypatch.setattr(vp, 'PREVIEW_RENDER_VERSION', vp.PREVIEW_RENDER_VERSION + 1)
    versioned = vp._build_plan(project.uid, body())
    vp._prepare_render(versioned)
    assert plan['cache_key'] != versioned['cache_key']
    restored = vp._build_plan(project.uid, body())
    restored['segments'][0]['media_kind'] = 'source'
    vp._prepare_render(restored)
    assert 'smart_cut' not in restored
    assert restored['encoding']['method'] == 'full_encode'


def test_smart_fallback_is_cached_but_source_change_never_triggers_full_render(preview, monkeypatch):
    from dubforge.pipeline.segment_replace import SegmentReplaceError
    project, root, _ = preview
    def incompatible(*a, **kw):
        raise SegmentReplaceError('编码头不一致', code='SEGMENT_REPLACE_INCOMPATIBLE')
    monkeypatch.setattr(segment_cut, 'render', incompatible)
    result = finish(project.uid, vp.start(project.uid, body()))
    assert result['encoding']['method'] == 'full_encode'
    assert result['encoding']['fallback_code'] == 'SEGMENT_REPLACE_INCOMPATIBLE'
    cached = vp.start(project.uid, body())
    assert cached['cache_hit'] and cached['encoding']['method'] == 'full_encode'
    def changed(*a, **kw):
        raise SegmentReplaceError('来源文件变化', code='SEGMENT_CUT_BASE_CHANGED')
    monkeypatch.setattr(segment_cut, 'render', changed)
    task = vp.start(project.uid, body(preview_range=dict(start=0., end=5.9)))
    vp._tasks[task['task_id']]['future'].result(timeout=15)
    failed = vp.status(project.uid, task['task_id'])
    assert failed['status'] == 'failed'
    assert failed['error_code'] == 'SEGMENT_CUT_BASE_CHANGED'
    assert not vp._output_path(root, task['task_id']).exists()
