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


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_demucs_keeps_track_on_cpu_and_preserves_restored_audio(
        isolated_env, monkeypatch, tmp_path, channels, device):
    """CUDA 只作为分块推理参数；CPU 模型桩核对整轨归一、还原与原子产物。"""
    from dubforge import config, environment
    from dubforge.pipeline import step_preprocess

    monkeypatch.setattr(config, "ensure_hf_env", lambda: None)
    monkeypatch.setattr(environment, "require_component", lambda key: None)
    # 即便机器有显卡，测试也不得分配 CUDA 张量或加载真实模型。
    original_to = th.Tensor.to

    def cpu_only_to(tensor, *args, **kwargs):
        requested = kwargs.get("device", args[0] if args else None)
        assert not str(requested).startswith("cuda"), "整轨张量不得搬到 CUDA"
        return original_to(tensor, *args, **kwargs)

    monkeypatch.setattr(th.Tensor, "to", cpu_only_to)

    class Model:
        samplerate = SR
        segment = .1

        def eval(self):
            return self

        def to(self, device):
            raise AssertionError("模型搬运应交给 Demucs 的分块/模型组执行入口")

    model = Model()
    monkeypatch.setattr("demucs.pretrained.get_model", lambda name: model)
    source = np.linspace(-.2, .4, 512, dtype="float32")[:, None]
    if channels == 2:
        source = np.concatenate((source, source * .5 - .03), axis=1)
    src = tmp_path / "input.wav"
    sf.write(src, source, SR, subtype="FLOAT")
    stereo = th.from_numpy(source.T.copy())
    if channels == 1:
        stereo = stereo.repeat(2, 1)
    ref = stereo.mean(0)
    normalized = (stereo - ref.mean()) / (ref.std() + 1e-8)
    coefficients = (.1, .2, .3, .4)
    calls = []

    def apply(current, mix, **options):
        assert current is model and mix.device.type == "cpu"
        th.testing.assert_close(mix[0], normalized)
        assert options["device"] == device
        assert options["split"] is True and options["shifts"] == 0
        assert options["overlap"] == .25
        assert options["progress"] is False
        calls.append(options)
        options["callback"]({"state": "end", "segment_offset": 0})
        return th.stack([mix * factor for factor in coefficients], dim=1)

    monkeypatch.setattr("demucs.apply.apply_model", apply)
    out = tmp_path / "audio"
    out.mkdir()
    replaced = []
    original_replace = step_preprocess.atomic_replace

    def replace(pending, final):
        assert pending.parent == out / ".demucs_tmp" and pending.is_file()
        assert not final.exists()
        replaced.append(final.name)
        original_replace(pending, final)

    monkeypatch.setattr(step_preprocess, "atomic_replace", replace)
    vocal, background = _run_demucs(_Ctx(tmp_path), src, out, device=device)
    expected_vocal = (normalized * coefficients[-1] * ref.std() + ref.mean()).numpy().T
    expected_background = (sum(normalized * factor for factor in coefficients[:-1])
                           * ref.std() + ref.mean()).numpy().T
    for path, expected in ((vocal, expected_vocal), (background, expected_background)):
        actual, sr = sf.read(path, dtype="float32", always_2d=True)
        assert sr == SR and actual.shape == (len(source), 2)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1 / 32768)
    assert len(calls) == 1 and replaced == ["vocal.wav", "background.wav"]
    assert not (out / ".demucs_tmp").exists()
