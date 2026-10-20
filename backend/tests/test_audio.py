import numpy as np

from dubforge.utils import audio as au


def test_mix_over_timeline_rounds_near_frame_boundary():
    """接近下一采样点的浮点时间不能被 int 向下错放一帧。"""
    clip = np.array([1.0, 2.0], dtype=np.float32)
    start = (10 + 0.999999) / au.SR

    mixed = au.mix_over_timeline([(start, clip)], total_seconds=1.0)

    assert mixed[10] == 0.0
    np.testing.assert_array_equal(mixed[11:13], clip)


def test_mix_over_timeline_crops_negative_start_without_wrapping():
    clip = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)

    mixed = au.mix_over_timeline([(-2 / au.SR, clip)], total_seconds=1.0)

    np.testing.assert_array_equal(mixed[:2], [3.0, 4.0])
    assert np.all(mixed[2:] == 0.0)
    # 负起点不能把数据错误地写到轨道末尾。
    assert np.all(mixed[-4:] == 0.0)


def test_mix_over_timeline_adds_overlapping_entries_and_preserves_inputs():
    first = np.array([0.25, 0.5, 0.75], dtype=np.float32)
    second = np.array([1.0, 2.0], dtype=np.float32)
    first_before = first.copy()
    second_before = second.copy()

    mixed = au.mix_over_timeline(
        [(0.0, first), (1 / au.SR, second)], total_seconds=1.0
    )

    np.testing.assert_array_equal(mixed[:3], [0.25, 1.5, 2.75])
    np.testing.assert_array_equal(first, first_before)
    np.testing.assert_array_equal(second, second_before)
