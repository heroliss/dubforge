"""复用只限本轮准备结果；真实短媒体验证字幕、评论与多版本编码合同。"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from dubforge.errors import DubForgeError
from dubforge.pipeline import subtitle_style as style, version_render as render
from dubforge.utils.srt import SubLine, write_srt


def _context(track='bilingual', **values):
    values = {**style.STYLE_DEFAULTS, 'assemble.subtitle_track': track,
              'assemble.subtitle_source_style_enabled': True,
              'assemble.subtitle_source_color': '#00FF00', **values}
    parent = SimpleNamespace(cfg=lambda key, default=None: values.get(key, default))
    version = dict(burn_subtitles=True, values=values)
    return parent, render.VersionContext(parent, version)


def test_canvas_cache_is_task_scoped_and_invalidates_changed_or_replaced_file(tmp_path, monkeypatch):
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'first')
    probes = []
    monkeypatch.setattr(style, 'video_canvas', lambda path: probes.append(path) or (1920, 1080))
    parent, first = _context()
    second = render.VersionContext(parent, first.version)
    assert style.render_canvas(first, video) == style.render_canvas(second, video) == (1920, 1080)
    assert len(probes) == 1

    previous = video.stat()
    video.write_bytes(b'other')
    os.utime(video, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    style.render_canvas(second, video)
    assert len(probes) == 2

    # 同路径、大小和修改时间的新实体也必须重新探测。
    previous = video.stat()
    replacement = video.with_suffix('.tmp')
    replacement.write_bytes(b'other')
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(replacement, video)
    style.render_canvas(first, video)
    assert len(probes) == 3
    _, next_task = _context()
    style.render_canvas(next_task, video)
    assert len(probes) == 4


def test_failed_canvas_probe_is_not_cached(tmp_path, monkeypatch):
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'media')
    calls = []
    def probe(path):
        calls.append(path)
        if len(calls) == 1:
            raise DubForgeError('暂时无法读取画幅', code='VIDEO_DIMENSIONS')
        return (1920, 1080)
    monkeypatch.setattr(style, 'video_canvas', probe)
    _, ctx = _context()
    with pytest.raises(DubForgeError, match='暂时无法读取画幅'):
        style.render_canvas(ctx, video)
    assert style.render_canvas(ctx, video) == (1920, 1080)
    assert len(calls) == 2


@pytest.mark.parametrize(('sar', 'rotation', 'width'), [('1:1', 0, 1920), ('2:1', 0, 3840), ('2:1', 90, 304)])
def test_cached_canvas_preserves_rotation_and_pixel_aspect(tmp_path, monkeypatch, sar, rotation, width):
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'media')
    probes = []
    def run(command):
        probes.append(command)
        return json.dumps({'streams': [dict(width=640, height=360, sample_aspect_ratio=sar,
                        side_data_list=[dict(rotation=rotation)])]}).encode()
    monkeypatch.setattr(style, '_run', run)
    _, ctx = _context()
    assert style.render_canvas(ctx, video) == style.render_canvas(ctx, video) == (width, 1080)
    assert len(probes) == 1


def test_prepared_subtitles_retain_language_parts_and_do_not_leak_between_versions(tmp_path, monkeypatch):
    from dubforge.pipeline import step_subtitles
    lines = [SubLine(1, 1, 2, '中文\nEnglish', (('trans', '中文'), ('source', 'English')))]
    builds = []
    monkeypatch.setattr(step_subtitles, 'build_tracks',
        lambda ctx, mode: (builds.append((ctx, mode)) or {'bilingual.srt': lines}, []))
    monkeypatch.setattr(style, 'video_canvas', lambda _: (1920, 1080))
    parent, ctx = _context()
    # 父任务的旧结果不得成为本版的缓存。
    parent._prepared_subtitle_lines = dict(track='bilingual', lines=[])
    ctx.subtitle_path = tmp_path / 'version.srt'
    prepared = render.subtitle_lines(ctx, ctx.version)
    write_srt(prepared, ctx.subtitle_path)
    original = ctx.subtitle_path.read_bytes()
    _, output = style.prepare_burn_filter(ctx, tmp_path / 'source.mp4', ctx.subtitle_path)
    assert len(builds) == 1
    assert output.read_text(encoding='utf-8-sig') == style.ass_document(lines, ctx.cfg, (1920, 1080))
    assert prepared[0].parts == (('trans', '中文'), ('source', 'English'))
    assert ctx.subtitle_path.read_bytes() == original
    output.unlink()

    sibling = render.VersionContext(parent, ctx.version)
    sibling.subtitle_path = ctx.subtitle_path
    _, output = style.prepare_burn_filter(sibling, tmp_path / 'source.mp4', sibling.subtitle_path)
    assert len(builds) == 2
    output.unlink()


def test_real_multi_version_render_builds_each_track_once_and_probes_canvas_once(make_project, monkeypatch):
    from dubforge import comment_studio
    from dubforge.pipeline import assembly_versions as versions, step_subtitles
    from dubforge.runner import runner
    from test_assemble import _prepare_outputs_project
    from test_assembly_versions import definition, wait
    from test_comment_studio import plan_body, seed
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    seed(uid, count=1)
    comment_studio.save(uid, plan_body(uid))
    definitions = [definition('translated', **{'assemble.subtitle_track': 'trans', 'assemble.final_loudness_mode': 'off'}),
                   definition('bilingual', **{'assemble.subtitle_track': 'bilingual', 'assemble.final_loudness_mode': 'off'})]
    definitions[0]['include_comments'] = True
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=definitions))
    tracks, probes = [], []
    original_tracks, original_canvas = step_subtitles.build_tracks, style.video_canvas
    def build(ctx, mode=None):
        # 预检和输入指纹需要独立读取文字；此断言约束一次真实渲染内部的复用。
        if getattr(getattr(ctx, 'parent', None), 'task_data', None) is not None:
            tracks.append((ctx.version['id'], mode))
        return original_tracks(ctx, mode)
    def canvas(video):
        probes.append(video)
        return original_canvas(video)
    monkeypatch.setattr(step_subtitles, 'build_tracks', build)
    monkeypatch.setattr(style, 'video_canvas', canvas)
    request = dict(version_ids=['translated', 'bilingual'], overlay_revision=comment_studio.view(uid)['overlay_revision'])
    plan = versions.plan(uid, request)
    assert not plan['blockers']
    runner.start_versions(uid, {**request, 'fingerprint': plan['fingerprint']})
    wait(uid)
    assert tracks == [('translated', 'target'), ('bilingual', 'bilingual')]
    assert len(probes) == 1
    for key in ('translated', 'bilingual'):
        output = root / 'output' / f'final_version_{key}.mp4'
        result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-f', 'null', '-'],
                                capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    assert all(version['fresh'] for version in versions.view(uid)['versions'])
    assert not list(root.rglob('.tmp*'))


def test_real_frame_with_subtitles_and_comments_shares_one_canvas_probe(make_project, monkeypatch):
    from dubforge import comment_studio
    from test_assemble import _prepare_outputs_project
    from test_assembly_versions import definition
    uid, _ = _prepare_outputs_project(make_project, monkeypatch)
    overlay = dict(settings=comment_studio.validate_settings(dict(start=0, interval=3, duration=3)),
                   items=[dict(id='one', author='观众', source_text='A clear comment', display_text='一条清晰的评论')])
    monkeypatch.setattr(comment_studio, 'for_task', lambda *args, **kwargs: overlay)
    probes = []
    original = style.video_canvas
    def canvas(video):
        probes.append(video)
        return original(video)
    monkeypatch.setattr(style, 'video_canvas', canvas)
    spec = {**definition('preview', **{'assemble.subtitle_track': 'bilingual'}), 'include_comments': True}
    data = render.frame(uid, dict(version=spec, seconds=1))
    assert data.startswith(b'\x89PNG')
    assert len(probes) == 1
