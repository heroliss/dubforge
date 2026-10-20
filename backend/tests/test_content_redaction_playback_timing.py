"""内容命中绑定源句，配音静音范围必须覆盖自动/人工最终播放位置。"""
import json

import numpy as np
import pytest
import soundfile as sf

from dubforge.domain import crud
from dubforge.pipeline import content_redaction, dubbing_edit, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.line_settings import identity
from test_content_redaction import MediaContext, _media, _rms, _samples, _seed_text


@pytest.fixture
def reviewed_voices(make_project, monkeypatch):
    project, root, paths = _seed_text(make_project(), source=[
        dict(id=1, start=2., end=3., text='A safe sentence'),
        dict(id=2, start=3., end=4., text='A blocked sentence'),
    ], target={1: '普通介绍', 2: '屏蔽词'})
    ctx = MediaContext(project, root, paths, values={
        'content_review.blocked_terms': '屏蔽词', 'assemble.bgm_volume': 0.,
        'assemble.ducking': False})
    deps = Dependencies(project)
    lines = deps.dubbing_lines(required=True)
    for row, duration in zip(lines, (2., 1.5), strict=True):
        path = dubbing_edit.take_path(root, row['id'], 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        t = np.arange(round(duration * 32000)) / 32000
        sf.write(path, .2 * np.sin(2 * np.pi * 700 * t), 32000)
        dubbing_edit.record_take(project, row, root, 1, 'test-input', {})
        crud.add_artifact(project.id, 'tts', 'audio', path.name, path, str(path.relative_to(root)))
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 8.)
    return ctx, lines


def layout(ctx):
    deps = Dependencies(ctx.project)
    rows = dubbing_edit.production_lines(ctx.project, deps.dubbing_lines(), deps.root, dependencies=deps)
    return voice_timing.plan(ctx.project, ctx.pdir, rows, ctx.cfg, 8.)


def manual(ctx, rows):
    params = crud.project_params(ctx.project)
    params['playback_timing_edits'] = {'2': {'identity': identity(rows[1]), 'start': 6.}}
    ctx.project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(ctx.project)


def covers(ranges, start, end):
    return any(row['start'] <= start and row['end'] >= end for row in ranges)


@pytest.mark.parametrize('mode', ['automatic', 'manual'])
def test_source_hit_mutes_complete_final_voice_after_any_sliding(reviewed_voices, mode):
    ctx, rows = reviewed_voices
    if mode == 'manual':
        manual(ctx, rows)
    ctx._voice_layout = layout(ctx)
    blocked = next(row for row in ctx._voice_layout['timing']['lines'] if row['id'] == 2)
    assert blocked['applied_start'] != rows[1]['start']
    current = content_redaction.audio_ranges(ctx)
    assert covers(current, 3., 4.)
    assert covers(current, blocked['applied_start'], blocked['audio_end'])
    assert covers(current, blocked['audio_end'], blocked['audio_end'] + .14)


def test_cached_voice_other_version_lazily_uses_same_final_clock(reviewed_voices):
    from dubforge.pipeline.version_render import VersionContext
    ctx, rows = reviewed_voices
    manual(ctx, rows)
    version = {'dubbing': True, 'values': {}}
    first = VersionContext(ctx, version)
    first._voice_layout = layout(ctx)
    second = VersionContext(ctx, version)
    assert not hasattr(second, '_voice_layout')
    current = content_redaction.audio_ranges(second)
    assert covers(current, 6., 7.5)
    assert second._content_redaction_voice_layout['timing']['lines'][1]['applied_start'] == 6.
    assert content_redaction.audio_ranges(second) == current


def test_original_version_does_not_mute_distant_dubbing_positions(reviewed_voices):
    from dubforge.pipeline.version_render import VersionContext
    ctx, rows = reviewed_voices
    manual(ctx, rows)
    ctx._voice_layout = layout(ctx)
    original = VersionContext(ctx, {'dubbing': False, 'values': {}})
    current = content_redaction.audio_ranges(original)
    assert covers(current, 3., 4.)
    assert not covers(current, 6., 7.5)


def test_subtitles_still_omit_blocked_identity_after_manual_movement(reviewed_voices):
    from dubforge.pipeline.step_subtitles import build_tracks
    ctx, rows = reviewed_voices
    manual(ctx, rows)
    tracks, _ = build_tracks(ctx)
    assert tracks
    assert all(2 not in {row.index for row in track} for track in tracks.values())
    assert all(1 in {row.index for row in track} for track in tracks.values())


def test_real_mux_mutes_moved_blocked_voice_and_keeps_safe_voice(reviewed_voices):
    from dubforge.pipeline.step_assemble import _mux
    ctx, rows = reviewed_voices
    manual(ctx, rows)
    ctx._voice_layout = layout(ctx)
    video, _, _ = _media(ctx, duration=8)
    mixed = np.zeros(8 * 32000)
    for row in ctx._voice_layout['inputs']:
        audio, rate = sf.read(dubbing_edit.take_path(ctx.pdir, row['id'], row['take']))
        assert rate == 32000
        offset = round(row['start'] * rate)
        mixed[offset:offset + len(audio)] += audio
    voice = ctx.pdir / 'audio' / 'shifted_dub.wav'
    sf.write(voice, mixed, 32000)
    output = ctx.pdir / 'output' / 'reviewed_shifted.mp4'
    _mux(ctx, ctx.pdir, video, voice, output, 8.)
    samples, rate = _samples(output)
    assert _rms(samples, rate, 6.3, 7.2) < .0001
    assert _rms(samples, rate, 2.2, 2.6) > .01
