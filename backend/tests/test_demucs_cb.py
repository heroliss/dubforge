# -*- coding: utf-8 -*-
"""
demucs 分段回调测试
==================
_run_demucs 通过 demucs 4.1.0 的 apply_model callback 实现:
  - 进度: 每段算完 (state=end) 按段偏移/遍数上报 (30→85 区间)
  - 取消: 回调里抛 StepCancelled → apply_model 终止剩余分段并向上传播
  - 暂停: 回调里阻塞 (段间检查点), 继续后放行
GPU 下 demucs 用 DummyPoolExecutor 同步执行, 回调跑在调用方线程 —— 行为可测。
"""
import threading
import time

import numpy as np
import pytest
import soundfile as sf
import torch as th

from dubforge.errors import DubForgeError
from dubforge.pipeline.step_preprocess import _demucs_callback, _run_demucs


class _Ctx:
    """StepContext 的最小替身 (只暴露 _run_demucs/_demucs_callback 用到的面)"""

    def __init__(self, pdir):
        self.pdir = pdir
        self.force = False
        self.logs: list[str] = []
        self.prog: list[float] = []
        self._cancel = threading.Event()
        self._pause = threading.Event()

    def log(self, m):
        self.logs.append(m)

    def report_progress(self, p):
        self.prog.append(p)

    def cfg(self, key, default=None):
        return {"separate.model": "htdemucs", "separate.shifts": 0,
                "separate.overlap": 0.25}.get(key, default)

    def check_pause(self):
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.01)
        self.check_cancel()

    def check_cancel(self):
        from dubforge.runner import StepCancelled
        if self._cancel.is_set():
            raise StepCancelled("取消")


SR = 44100


def test_callback_reports_monotonic_progress():
    ctx = _Ctx(pdir=None)
    cb = _demucs_callback(ctx, total_sec=100.0, shifts=0, sr=SR)
    cb({"state": "start", "segment_offset": 0})          # start 不计进度
    assert ctx.prog == []
    for sec in (0, 50, 99):                              # offset 单位 = 采样点
        cb({"state": "end", "segment_offset": sec * SR})
    assert ctx.prog == sorted(ctx.prog)                  # 单调递增
    assert 30 <= ctx.prog[0] < ctx.prog[-1] <= 85        # 映射在 30→85 区间
    assert ctx.prog[-1] > 80                             # 尾段应接近区间上沿


def test_callback_offset_is_in_samples_not_seconds():
    """回归: segment_offset 单位是采样点 —— 曾误当秒数, 进度从第一段起就卡死 57.5%"""
    ctx = _Ctx(pdir=None)
    cb = _demucs_callback(ctx, total_sec=600.0, shifts=0, sr=SR)
    for sec in range(0, 600, 10):
        cb({"state": "end", "segment_offset": sec * SR})
    assert max(ctx.prog) > 80                            # 修前: 恒为 57.5


def test_callback_shifts_single_pass_reaches_end():
    """shifts=1 时 demucs 只跑一遍位移平均 (range(shifts)), 分母不得按 shifts+1 折算"""
    ctx = _Ctx(pdir=None)
    cb = _demucs_callback(ctx, total_sec=100.0, shifts=1, sr=SR)
    for sec in (0, 50, 99):
        cb({"state": "end", "segment_offset": sec * SR, "shift_idx": 0})
    assert ctx.prog[-1] > 80                             # 修前: 封顶 57.5


def test_callback_pause_blocks_and_cancel_aborts():
    ctx = _Ctx(pdir=None)
    cb = _demucs_callback(ctx, total_sec=100.0, shifts=0, sr=SR)
    # 暂停: 回调阻塞, 直到清除暂停或取消
    ctx._pause.set()
    done = threading.Event()
    threading.Thread(target=lambda: (cb({"state": "end", "segment_offset": 0}), done.set()),
                     daemon=True).start()
    time.sleep(0.15)
    assert not done.is_set(), "暂停期间回调应阻塞"
    ctx._pause.clear()
    assert done.wait(2)

    # 取消: 回调抛 StepCancelled (apply_model 会终止剩余分段并向上传播)
    ctx._cancel.set()
    with pytest.raises(DubForgeError):
        cb({"state": "end", "segment_offset": 50 * SR})


def test_run_demucs_cancel_mid_segments(monkeypatch, tmp_path):
    """整链: 分段回调里取消 → _run_demucs 中止且清掉临时目录 (不留半成品)"""
    import dubforge.pipeline.step_preprocess as sp
    from dubforge.runner import StepCancelled

    src = tmp_path / "src.wav"
    sf.write(str(src), np.zeros((44100 * 2, 2), dtype="float32"), 44100,
             subtype="PCM_16")

    class _DummyModel:
        samplerate = 44100
        segment = 1.0

        def to(self, device):
            return self

        def eval(self):
            return self

    computed = []

    def fake_apply(model, mix, **kw):
        cb = kw["callback"]
        for off in (0, 44100):                # 模拟两段
            cb({"state": "start", "segment_offset": off, "shift_idx": 0})
            computed.append(off)
            cb({"state": "end", "segment_offset": off, "shift_idx": 0})
        return th.zeros(1, 4, 1, 100)         # [batch, stems, ch, samples]

    monkeypatch.setattr("demucs.pretrained.get_model", lambda name: _DummyModel())
    monkeypatch.setattr("demucs.apply.apply_model", fake_apply)

    ctx = _Ctx(pdir=tmp_path)
    out_dir = tmp_path / "audio"
    out_dir.mkdir()
    (out_dir / ".demucs_tmp").mkdir()
    ctx._cancel.set_after = None
    # 第一段之后置位取消 (模拟用户在分离中途点停止)
    original_check = ctx.check_cancel

    def check():
        if len(computed) >= 2:
            ctx._cancel.set()
        original_check()

    ctx.check_cancel = check
    with pytest.raises(StepCancelled):
        _run_demucs(ctx, src, out_dir)
    assert computed == [0, 44100]             # 第二段跑完即中止 (没有第三段)
    assert not (out_dir / ".demucs_tmp").exists()   # 工作目录被清理
    assert not (out_dir / "vocal.wav").exists()     # 无半成品
