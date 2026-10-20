"""实际时长排布与明确的试片裁剪，不调用模型或移动源台词。"""
import math

import pytest

from dubforge.errors import DubForgeError
from dubforge.pipeline.dubbing_placement import (
    allocate_voice_windows, build_plan, region_entries, require_video_fit,
)


def cue(id, start, end, seconds=None, source='dub', take=1):
    return dict(id=id, start=start, end=end, source=source, take=take,
                seconds=seconds, media_key=f'{id}-v{take}')


def test_overlap_uses_complete_selected_audio_and_never_delays_next_line(isolated_env):
    plan = build_plan([cue(2, 2, 4, 1), cue(1, 0, 2, 3)], 6)
    assert [(row['id'], row['start'], row['audio_end']) for row in plan['lines']] == [(1, 0, 3), (2, 2, 3)]
    assert [issue for issue in plan['issues'] if issue['kind'] == 'overlap'] == [
        dict(kind='overlap', id=1, next_id=2, available=2, excess=1)]
    require_video_fit(plan)
    entries = region_entries(plan, 0, 6)
    assert [(row['offset'], row['audio_offset'], row['clip_end']) for row in entries] == [(0, 0, 3), (2, 0, 3)]


def test_silent_slots_do_not_overlap_but_original_audio_does(isolated_env):
    lines = [cue(1, 0, 1, 4), cue(2, 2, 3, source='silent', take=None),
             cue(3, 3, 5, source='original', take=None)]
    plan = build_plan(lines, 8)
    overlap = [issue for issue in plan['issues'] if issue['kind'] == 'overlap']
    assert [(issue['id'], issue['next_id'], issue['excess']) for issue in overlap] == [(1, 3, 1)]
    assert [row['id'] for row in region_entries(plan, 0, 8)] == [1, 3]


def test_long_clip_reports_all_collisions_without_cumulative_shifts(isolated_env):
    plan = build_plan([cue(1, 0, 1, 7), cue(2, 2, 3, 3), cue(3, 4, 5, 1)], 10)
    overlaps = [issue for issue in plan['issues'] if issue['kind'] == 'overlap']
    assert [(issue['id'], issue['next_id'], issue['excess']) for issue in overlaps] == [(1, 2, 3), (1, 3, 1), (2, 3, 1)]
    assert [row['start'] for row in plan['lines']] == [0, 2, 4]


def test_subtitle_end_is_soft_target_not_video_end(isolated_env):
    plan = build_plan([cue(1, 3, 5, 3)], 8)
    assert [issue['kind'] for issue in plan['issues']] == ['target_overrun']
    require_video_fit(plan)
    assert region_entries(plan, 0, 8)[0]['clip_end'] == 6


def test_real_video_overrun_fails_without_trimming_existing_audio(isolated_env):
    plan = build_plan([cue(1, 3, 5, 3)], 5.5)
    with pytest.raises(DubForgeError) as exc:
        require_video_fit(plan)
    assert exc.value.code == 'DUB_AUDIO_OUTSIDE_VIDEO'
    assert '第 1 句超出视频结尾 0.50 秒' in str(exc.value)
    assert plan['lines'][0]['audio_end'] == 6
    # 明确选择的试片范围允许裁掉左右两边，不修改整个视频的计划。
    entry = region_entries(plan, 4, 5)[0]
    assert (entry['offset'], entry['audio_offset'], entry['clip_end']) == (0, 1, 5)


@pytest.mark.parametrize('total', [None, 0, math.nan, math.inf, -1])
def test_unknown_video_duration_never_uses_last_subtitle_as_video_boundary(isolated_env, total):
    plan = build_plan([cue(1, 3, 5, 9)], total)
    require_video_fit(plan)
    assert plan['video_duration'] is None
    assert not any(issue['kind'] == 'video_overrun' for issue in plan['issues'])


def test_millisecond_tolerance_and_selection_signature(isolated_env):
    first = build_plan([cue(1, 0, 2, 2.05)], 2)
    assert not first['issues']
    require_video_fit(first)
    second = build_plan([cue(1, 0, 2, 2.051, take=2)], 2)
    assert second['signature'] != first['signature']
    with pytest.raises(DubForgeError):
        require_video_fit(second)


@pytest.mark.parametrize('start,end,seconds', [(math.nan, 1, 1), (0, -1, 1), (2, 1, 1), (0, 1, math.inf)])
def test_invalid_cue_time_is_explicit_error(isolated_env, start, end, seconds):
    with pytest.raises(DubForgeError) as exc:
        build_plan([cue(1, start, end, seconds)], 5)
    assert exc.value.code == 'DUB_TIMELINE_INVALID'


def assert_safe_window(rows, gap=.08):
    for row in rows:
        assert abs(row['allocated_start'] - row['start']) <= 2 + 1e-7
        assert row['allocated_end'] - row['allocated_start'] == pytest.approx(row['audio_duration'])
        assert row['actual_end'] == pytest.approx(row['allocated_end'])
    for previous, current in zip(rows, rows[1:]):
        assert previous['allocated_end'] + gap <= current['allocated_start'] + 1e-7


def test_allocator_borrows_nearby_blank_before_resynthesis():
    rows = allocate_voice_windows([cue(1, 0, 1, 1.8), cue(2, 2, 3, .5)])
    assert all(row['window_ready'] and not row['needs_scale'] for row in rows)
    assert_safe_window(rows)


@pytest.mark.parametrize('count', [3, 4, 5])
def test_allocator_moves_dense_group_into_blank_on_both_sides(count):
    source = [cue(i + 1, 10 + i, 11 + i, 1.3) for i in range(count)]
    rows = allocate_voice_windows(source, video_duration=25)
    assert all(row['window_ready'] for row in rows)
    assert rows[0]['allocated_start'] < source[0]['start']
    assert rows[-1]['allocated_start'] > source[-1]['start']
    assert_safe_window(rows)


def test_allocator_moves_group_left_when_right_side_is_fixed():
    source = [cue(i + 1, 10 + i, 11 + i, 1.3) for i in range(5)]
    source.append(cue(6, 15.2, 16.2, source='original'))
    rows = allocate_voice_windows(source, video_duration=25)
    assert all(row['window_ready'] for row in rows[:5])
    assert rows[0]['allocated_start'] < 10
    assert rows[4]['allocated_end'] <= 15.2 + 1e-7
    assert_safe_window(rows[:5])


def test_allocator_moves_group_right_when_left_side_is_fixed():
    source = [cue(1, 9, 10, source='original')]
    source.extend(cue(i + 2, 10 + i, 11 + i, 1.3) for i in range(5))
    rows = allocate_voice_windows(source, video_duration=25)
    assert all(row['window_ready'] for row in rows[1:])
    assert rows[1]['allocated_start'] >= 10
    assert rows[-1]['allocated_start'] > 14
    assert_safe_window(rows[1:])


def test_allocator_proposes_shortening_long_sentence_without_compressing_short_one():
    rows = allocate_voice_windows([cue(1, 0, 1, 2), cue(2, 1, 2, 1)])
    assert rows[0]['needs_scale']
    assert rows[0]['available_seconds'] == pytest.approx(.92)
    assert rows[0]['actual_end'] > rows[0]['allocated_end']
    assert not rows[1]['needs_scale']
    assert rows[1]['scale_factor'] == pytest.approx(1)
    assert not any(row['window_ready'] for row in rows)
    assert rows[0]['allocated_end'] + .08 <= rows[1]['allocated_start'] + 1e-7


def test_allocator_uses_early_short_sentence_slack_for_late_long_sentence():
    rows = allocate_voice_windows([cue(1, 0, 2, .5), cue(2, 2, 3, 1.5)])
    assert all(row['window_ready'] for row in rows)
    # 新策略允许进入前句的空闲源字幕范围，原声与已播放音频均不被挤占。
    assert rows[1]['allocated_start'] < 2
    assert_safe_window(rows)


def test_allocator_borrows_bounded_part_of_long_pause_not_asr_pause_threshold():
    rows = allocate_voice_windows([cue(1, 0, 1, 1.5), cue(2, 5, 6, 1.5)], island_gap=.1)
    assert all(row['window_ready'] for row in rows)
    assert rows[0]['allocated_end'] > 1
    assert rows[1]['allocated_start'] >= 3
    assert_safe_window(rows)


def test_allocator_does_not_guess_video_tail_but_uses_known_tail_blank():
    source = [cue(1, 1, 3, 4)]
    unknown = allocate_voice_windows(source)
    assert unknown[0]['needs_scale']
    assert unknown[0]['allocated_end'] <= 3 + 1e-7
    known = allocate_voice_windows(source, video_duration=8)
    assert known[0]['window_ready']
    assert known[0]['allocated_end'] == pytest.approx(5)
    assert source == [cue(1, 1, 3, 4)]


def test_allocator_zero_shift_preserves_start_and_requests_actual_needed_capacity():
    rows = allocate_voice_windows([cue(1, 1, 2, 1.5), cue(2, 2, 3, 1)],
                                  max_shift_seconds=0, video_duration=4)
    assert [row['allocated_start'] for row in rows] == [1, 2]
    assert rows[0]['needs_scale']
    assert rows[0]['available_seconds'] == pytest.approx(.92)
    assert not rows[1]['needs_scale']


def test_allocator_keeps_original_silent_and_unknown_rows_as_fixed_obstacles():
    source = [cue(1, 0, 1, 2.5), cue(2, 2, 3, source='original'),
              cue(3, 3, 4, source='silent'), cue(4, 4, 5), cue(5, 5, 6, 1.2)]
    rows = allocate_voice_windows(source, video_duration=8)
    for i in (1, 2, 3):
        assert (rows[i]['allocated_start'], rows[i]['allocated_end']) == (source[i]['start'], source[i]['end'])
        assert rows[i]['window_id'] is None
    assert rows[0]['allocated_end'] <= 2
    assert rows[0]['needs_scale']
    assert rows[-1]['allocated_start'] >= 5
    assert rows[-1]['window_ready']


def test_allocator_bounds_shortening_windows_when_whole_segment_cannot_fit():
    rows = allocate_voice_windows([cue(i + 1, i, i + 1, 1.5) for i in range(12)],
                                  window_lines=9, video_duration=20)
    assert [row['window_id'] for row in rows] == [1] * 9 + [2] * 3
    assert not rows[0]['window_ready']
    assert rows[-1]['window_ready']
    assert rows[8]['allocated_end'] + .08 <= rows[9]['allocated_start'] + 1e-7
    assert all(row['window_lines'] <= 9 for row in rows)


def test_allocator_avoids_false_shortening_at_artificial_window_boundary():
    source = [cue(i + 1, i, i + 1, 1) for i in range(10)]
    rows = allocate_voice_windows(source, window_lines=9, video_duration=12)
    # 固定分窗会让前 9 句仅获 8.96 秒，原速加间隔却需要 9.64 秒。
    # 整段只需向右最多移动 .72 秒，不能为了机械接缝缩短任何声音。
    assert all(row['window_ready'] and not row['needs_scale'] for row in rows)
    assert all(row['scale_factor'] == 1 for row in rows)
    assert [row['window_id'] for row in rows] == [1] * 10
    assert all(row['window_lines'] == 10 for row in rows)
    assert rows[-1]['actual_end'] == pytest.approx(10.72)
    assert max(abs(row['shift_seconds']) for row in rows) == pytest.approx(.72)
    assert_safe_window(rows)
    assert source == [cue(i + 1, i, i + 1, 1) for i in range(10)]


def test_allocator_natural_segment_still_respects_shift_limit_and_known_video_tail():
    source = [cue(i + 1, i, i + 1, 1) for i in range(10)]
    bounded = allocate_voice_windows(source, window_lines=9, max_shift_seconds=.2,
                                     video_duration=12)
    assert any(row['needs_scale'] for row in bounded)
    assert all(abs(row['shift_seconds']) <= .2 + 1e-7 for row in bounded)
    unknown = allocate_voice_windows(source, window_lines=9)
    assert any(row['needs_scale'] for row in unknown)
    assert all(row['allocated_end'] <= 10 + 1e-7 for row in unknown)


@pytest.mark.parametrize('obstacle', ['original', 'silent', 'unknown', 'manual'])
def test_allocator_natural_segments_do_not_cross_hard_obstacles(obstacle):
    source = [cue(i + 1, 10 + i, 11 + i, 1) for i in range(10)]
    barrier = cue(11, 20, 21, 1 if obstacle == 'manual' else None,
                  source=obstacle if obstacle in {'original', 'silent'} else 'dub')
    if obstacle == 'manual':
        barrier.update(fixed_start=20, fixed_end=21)
    source.append(barrier)
    source.extend(cue(i + 12, 22 + i, 23 + i, 1) for i in range(10))
    rows = allocate_voice_windows(source, window_lines=9, video_duration=35)
    left, fixed, right = rows[:10], rows[10], rows[11:]
    assert (fixed['allocated_start'], fixed['allocated_end']) == (20, 21)
    assert fixed['window_id'] is None
    assert all(row['window_ready'] and not row['needs_scale'] for row in left + right)
    assert all(row['window_lines'] == 10 for row in left + right)
    assert {row['window_id'] for row in left} == {1}
    assert {row['window_id'] for row in right} == {2}
    gap = .08 if obstacle in {'original', 'manual'} else 0
    assert left[-1]['actual_end'] + gap <= 20 + 1e-7
    assert right[0]['allocated_start'] >= 21 + gap - 1e-7
    assert_safe_window(left)
    assert_safe_window(right)


def test_allocator_natural_segments_leave_safety_gap_at_original_audio():
    rows = allocate_voice_windows([cue(1, 0, 1, 1.5),
                                  cue(2, 1.6, 2.6, source='original'),
                                  cue(3, 2.6, 3.6, 1)], video_duration=5)
    assert rows[0]['window_ready'] and rows[2]['window_ready']
    assert rows[0]['actual_end'] + .08 <= rows[1]['allocated_start'] + 1e-7
    assert rows[1]['actual_end'] + .08 <= rows[2]['allocated_start'] + 1e-7


def test_allocator_natural_segment_can_exceed_shortening_window_without_moving_distant_lines():
    source = [cue(i + 1, i * 3, i * 3 + 1, .5) for i in range(20)]
    rows = allocate_voice_windows(source, window_lines=3, video_duration=60)
    assert all(row['window_lines'] == 20 and row['window_ready'] for row in rows)
    assert [row['allocated_start'] for row in rows] == [row['start'] for row in source]
    assert all(row['audio_duration'] == .5 and not row['needs_scale'] for row in rows)


def test_allocator_fallback_prefers_existing_blank_for_window_boundary():
    source = [cue(i + 1, i, i + 1, .5) for i in range(3)]
    # 首句过长且相邻句已触及位移上限，整段原速不可行，才进入分窗缩时。
    source[0]['seconds'] = 7
    source.extend(cue(i + 4, 5 + i, 6 + i, .5) for i in range(9))
    rows = allocate_voice_windows(source, window_lines=9, video_duration=20)
    assert [row['window_id'] for row in rows] == [1] * 3 + [2] * 9
    assert not any(row['window_ready'] for row in rows[:3])
    assert all(row['window_ready'] for row in rows[3:])
    assert rows[2]['window_end'] == pytest.approx(3.96)
    assert rows[3]['window_start'] == pytest.approx(4.04)


def test_allocator_unresolvable_safety_gap_is_explicit_not_fake_scaled_audio():
    rows = allocate_voice_windows([cue(1, 0, .01, .2), cue(2, .01, .02, .2)],
                                  max_shift_seconds=0)
    assert all(not row['window_ready'] and row['needs_scale'] for row in rows)
    assert all(row['available_seconds'] == 0 for row in rows)
    assert all(row['actual_end'] > row['allocated_end'] for row in rows)


def test_allocator_custom_gap_and_short_normal_sentence_are_preserved():
    rows = allocate_voice_windows([cue(1, 1, 2, 1.2), cue(2, 2, 3, .5)],
                                  video_duration=5, inter_sentence_gap=.25)
    assert all(row['window_ready'] for row in rows)
    assert_safe_window(rows, gap=.25)
    assert rows[1]['audio_duration'] == .5


def test_allocator_does_not_mutate_input_and_accepts_duration_alias():
    source = [dict(id=1, start=1, end=3, audio_duration=1)]
    saved = [dict(source[0])]
    rows = allocate_voice_windows(source)
    assert source == saved
    assert rows[0]['audio_duration'] == 1
    assert rows[0]['window_ready']
    assert allocate_voice_windows([]) == []


@pytest.mark.parametrize('kwargs', [
    dict(island_gap=-1), dict(max_shift_seconds=-1), dict(max_shift_seconds=math.nan),
    dict(max_shift_seconds=True), dict(window_lines=2), dict(window_lines=16),
    dict(window_lines=3.5), dict(window_lines=True), dict(inter_sentence_gap=-1),
    dict(inter_sentence_gap=math.inf), dict(video_duration=math.nan), dict(video_duration=0),
])
def test_allocator_rejects_invalid_options(kwargs):
    with pytest.raises(DubForgeError) as exc:
        allocate_voice_windows([], **kwargs)
    assert exc.value.code == 'DUB_TIMELINE_INVALID'


def test_allocator_rejects_unknown_source_and_overlapping_source_intervals():
    for source in ([dict(id=1, source='music', start=0, end=1, seconds=1)],
                   [cue(1, 0, 2, 1), cue(2, 1, 3, 1)]):
        with pytest.raises(DubForgeError) as exc:
            allocate_voice_windows(source)
        assert exc.value.code == 'DUB_TIMELINE_INVALID'


def test_allocator_deterministic_randomized_non_overlap_and_bounded_movement():
    import random
    randomizer = random.Random(154)
    for _ in range(150):
        cursor, source = 2.0, []
        for i in range(randomizer.randint(1, 25)):
            width = randomizer.uniform(.3, 3)
            source.append(cue(i + 1, cursor, cursor + width, randomizer.uniform(.2, 4)))
            cursor += width + randomizer.uniform(0, 2)
        rows = allocate_voice_windows(source, video_duration=cursor + 3)
        assert rows == allocate_voice_windows(source, video_duration=cursor + 3)
        for row in rows:
            assert 0 <= row['allocated_start'] <= cursor + 3
            assert abs(row['allocated_start'] - row['start']) <= 2 + 1e-7
            assert row['allocated_end'] <= row['window_end'] + 1e-7
            assert row['actual_end'] == pytest.approx(row['allocated_start'] + row['audio_duration'])
            if row['window_ready']:
                assert not row['needs_scale']
        for previous, current in zip(rows, rows[1:]):
            assert previous['allocated_end'] + .08 <= current['allocated_start'] + 1e-7
