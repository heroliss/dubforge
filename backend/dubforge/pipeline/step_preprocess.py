"""
步骤 2: preprocess —— 预处理
============================
产物:
  audio/raw.wav          16kHz 单声道源音轨 (供转录与 ASR 备用)
  audio/vocal.wav        demucs 人声 44.1k 立体声 (供动态情感参考 + 工作台)
  audio/background.wav   demucs 背景音 44.1k 立体声 (供成片混音)
  separate.json          分离元信息

- 分离输入恒为 44.1kHz 立体声 (从原视频抽取, 用完即删) —— htdemucs 的训练规格;
  模型/位移平均/重叠比例可配 (separate.model/shifts/overlap, 步骤页可改)
- 显存不足自动降级; ctx.check_cancel 取消粒度=整曲 (demucs 无细粒度回调)
- "取消不产生半成品" 由临时目录保证: 输出写 artifacts/preprocess/.tmp_* 完成后改名
"""
import shutil
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.progress import RateProgress, _bind
from dubforge.execution_progress import report_stage
from dubforge.utils.fs import atomic_replace


def _extract_audio(ctx, video: Path, out_wav: Path) -> None:
    """ffmpeg 提取 16k mono wav (faster-whisper 与 demucs 的公共输入格式)"""
    tmp = out_wav.with_suffix(".wav.tmp")
    # 临时文件 .tmp 后缀无法让 ffmpeg 推断格式, 须显式 -f wav
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
           "-f", "wav", str(tmp)]
    try:
        rc, _, err = ctx.run_subprocess(cmd)
        if rc != 0:
            raise DubForgeError("音轨提取失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        atomic_replace(tmp, out_wav)
    finally:
        tmp.unlink(missing_ok=True)


def _demucs_callback(ctx, total_sec: float, shifts: int, sr: int, stride_sec=0.0):
    """demucs 分段回调 → 进度上报 + 段间暂停/取消检查点。

    demucs (4.1.0) 的 apply_model 在 GPU 下用 DummyPoolExecutor 同步逐段执行,
    每段计算前后各回调一次 ({state: start|end, segment_offset, shift_idx, ...});
    回调在我们自己的线程里运行 —— 在回调里阻塞即暂停, 抛异常即取消
    (apply_model 捕获后 cancel_futures 终止剩余分段并向上传播)。
    单段是一次前向推理, 原子不可分 —— 检查点粒度 = 分段长度 (htdemucs 每段数秒)。

    注意: segment_offset 单位是采样点 (shifts>0 时是相对位移后分块的),
    须除以 sr 换算成秒; shifts>0 时 demucs 只跑 shifts 遍位移平均 (没有额外
    的干净遍), 遍数分母 = max(1, shifts)。"""
    passes = max(1, shifts)
    progress = RateProgress(ctx, 1.0, start=30, end=85, scope="phase")

    def cb(d: dict) -> None:
        if d.get("state") != "end":
            return
        ctx.check_pause()          # 暂停: 段间阻塞 (显存保持占用, 不再计算)
        ctx.check_cancel()         # 取消: 抛出后 demucs 终止剩余分段
        offset_sec = float(d.get("segment_offset") or 0) / max(sr, 1)
        shift_idx = int(d.get("shift_idx") or 0)
        models = max(1, int(d.get("models") or 1))
        model_idx = int(d.get("model_idx_in_bag") or 0)
        frac = (model_idx * passes + shift_idx
                + min(1.0, (offset_sec + stride_sec) / max(total_sec, 1e-6))) / (passes * models)
        progress.update(frac)
        report_stage(ctx, 'separating', '分离人声与背景音',
                     completed=round(frac * total_sec * passes * models, 2),
                     total=round(total_sec * passes * models, 2), unit='音频秒')
    return cb


def _is_cuda_oom(exc: BaseException) -> bool:
    """torch 各版本 CUDA OOM 的形态: torch.cuda.OutOfMemoryError、
    AcceleratorError('CUDA error: out of memory') 或含 out of memory 的运行时错误。"""
    import torch
    oom_cls = getattr(torch.cuda, "OutOfMemoryError", ())
    if oom_cls and isinstance(exc, oom_cls):
        return True
    text = str(exc).lower()
    return "out of memory" in text


def _run_demucs(ctx, src_wav: Path, out_dir: Path, *, device: str | None = None) -> tuple:
    """htdemucs 分离人声/伴奏; 输入须为 44.1kHz 立体声 (模型训练规格)"""
    from dubforge import config
    from dubforge.environment import require_component
    require_component("separation")
    config.ensure_hf_env()
    import torch
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model_name = str(ctx.cfg("separate.model", "htdemucs"))
    shifts = int(ctx.cfg("separate.shifts", 0) or 0)
    overlap = float(ctx.cfg("separate.overlap", 0.25) or 0.25)
    ctx.log(f"demucs 分离中 (device={device}, model={model_name}, shifts={shifts})")

    tmp_out = out_dir / ".demucs_tmp"
    if tmp_out.exists():
        shutil.rmtree(tmp_out)
    tmp_out.mkdir(parents=True)

    import soundfile as sf
    import torch as th
    from demucs import pretrained
    from demucs.apply import apply_model

    report_stage(ctx, 'loading_separation_model', '加载人声分离模型', model=model_name, device=device)
    model = pretrained.get_model(model_name)
    # apply_model 按 device 搬运当前模型；不要预先把 _ft 的整组模型同时放上显卡。
    model.eval()

    report_stage(ctx, 'loading_audio', '读取分离输入音轨')
    data, sr = sf.read(str(src_wav), dtype="float32", always_2d=True)
    total_sec = len(data) / sr
    seg = float(getattr(model, "segment", 0) or 0)
    passes = max(1, shifts)
    if seg > 0:
        import math
        n_seg = math.ceil(total_sec / (seg * (1 - overlap)))
        ctx.log(f"整轨 {total_sec:.0f}s ≈ {n_seg} 段 × {seg:.0f}s, "
                f"共 {n_seg * passes} 次前向 (暂停/取消在段间生效)")
    if model_name.endswith("_ft") and shifts > 0:
        ctx.log("⚠ htdemucs_ft + shifts>0 计算量约为标准版 3 倍; 显存接近满载时 "
                "Windows 会溢出到共享内存, 速度可能骤降 —— 小显存建议 htdemucs + shifts=0")
    wav = torch.from_numpy(data.T)          # [channels, samples]
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)              # htdemucs 要求立体声输入, 单声道复制
    if sr != model.samplerate:
        ctx.log(f"⚠ 输入采样率 {sr} ≠ 模型规格 {model.samplerate}, 分离质量会下降")
    # Demucs 的整轨输出和叠加权重跟随 mix.device；split=True 只限制前向块大小。
    # 输入留在 CPU，传 device 让模型逐块在 GPU 推理，避免显存随视频时长增长。
    ref = wav.mean(0)
    ref_mean, ref_std = ref.mean(), ref.std()
    wav = (wav - ref_mean) / (ref_std + 1e-8)
    # 还原只需两个标量，及时释放原始整轨和声道均值缓冲。
    del data, ref

    try:
        with th.no_grad():
            sources = apply_model(model, wav[None], device=device,
                                  split=True, overlap=overlap, shifts=shifts,
                                  progress=False,
                                  callback=_demucs_callback(ctx, total_sec,
                                                            shifts, sr, seg * (1 - overlap)))[0]
        _bind(ctx, None)
        # htdemucs stems 顺序: drums, bass, other, vocals
        vocals = sources[-1]
        background = sources[:-1].sum(0)

        vocal_path = tmp_out / "vocal.wav"
        bg_path = tmp_out / "background.wav"
        report_stage(ctx, 'saving_stems', '保存人声与背景音轨')
        sf.write(vocal_path, (vocals * ref_std + ref_mean).cpu().numpy().T, sr)
        sf.write(bg_path, (background * ref_std + ref_mean).cpu().numpy().T, sr)

        final_v = out_dir / "vocal.wav"
        final_b = out_dir / "background.wav"
        atomic_replace(vocal_path, final_v)
        atomic_replace(bg_path, final_b)
        return final_v, final_b
    finally:
        # 取消、写盘失败或原子替换失败都不能留下可被下次误认的临时目录。
        shutil.rmtree(tmp_out, ignore_errors=True)


def run(ctx) -> None:
    report_stage(ctx, 'checking_audio', '检查视频与已有音轨')
    audio_dir = ctx.pdir / "audio"
    audio_dir.mkdir(exist_ok=True)
    video = ctx.upstream("download", "video.mp4")
    raw_wav = audio_dir / "raw.wav"

    if getattr(ctx, "rebuild", False):
        # 配置/上游变化时不能把旧音轨当成幂等缓存继续使用。
        for old in (raw_wav, audio_dir / "vocal.wav", audio_dir / "background.wav"):
            old.unlink(missing_ok=True)

    if raw_wav.exists() and not getattr(ctx, "rebuild", False):
        report_stage(ctx, 'reuse_audio', '复用源音轨', reused=True)
        ctx.log("raw.wav 已存在, 跳过音轨提取")
    else:
        report_stage(ctx, 'extracting_audio', '提取语音识别用音轨')
        ctx.log("提取 16kHz 单声道音轨...")
        _extract_audio(ctx, video, raw_wav)
    ctx.register_file("raw.wav", raw_wav, "audio")
    ctx.report_progress(20)

    if not ctx.cfg("separate.enabled", True):
        report_stage(ctx, 'separation_disabled', '按设置跳过人声分离', state='skipped')
        ctx.log("配置关闭了人声分离, 跳过 demucs")
        ctx.write_json("separate.json", {"separated": False})
        return

    vocal = audio_dir / "vocal.wav"
    bg = audio_dir / "background.wav"
    if vocal.exists() and bg.exists() and not getattr(ctx, "rebuild", False):
        report_stage(ctx, 'reuse_stems', '复用人声与背景音轨', reused=True)
        ctx.log("vocal/background 已存在, 跳过分离")
    else:
        ctx.report_progress(30)
        ctx.check_pause()
        # htdemucs 按 44.1kHz 立体声训练 —— 喂 16k 单声道会频谱错位、严重劣化。
        # 从原视频抽全规格音轨作分离输入, 用完即删 (重跑重新提取只需几秒)。
        src441 = audio_dir / ".tmp_src44100.wav"
        report_stage(ctx, 'extracting_separation_audio', '提取人声分离用音轨')
        cmd = ["ffmpeg", "-y", "-i", str(video), "-vn",
               "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
               "-f", "wav", str(src441)]
        rc, _, err = ctx.run_subprocess(cmd)
        if rc != 0:
            raise DubForgeError("分离输入音轨提取失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        try:
            ctx.check_cancel()
            ctx.log("分段分离中 —— 进度逐段上报; 暂停/取消在段间检查点生效 (数秒粒度)")
            from dubforge.environment import require_component
            require_component("separation")
            import torch
            # 记录首发设备；异常后硬件可用性可能改变，不能据此误判实际失败的设备。
            device = "cuda" if torch.cuda.is_available() else "cpu"
            try:
                vocal, bg = _run_demucs(ctx, src441, audio_dir, device=device)
            except Exception as exc:
                # 显存不足自动降级 (模块承诺的行为, 此前缺失): CUDA OOM 时
                # 归还缓存后用 CPU 重跑, 批量流程不因显存竞争而中断。
                from dubforge.utils.gpu_memory import release_unused
                if not (device == "cuda" and _is_cuda_oom(exc)):
                    raise
                ctx.log(f"⚠ CUDA 显存不足，自动降级为 CPU 分离 (速度约慢一个量级；"
                        f"可在设置把分离模型换为 htdemucs、shifts 设为 0，"
                        f"或暂停配音服务释放显存): {exc}")
                release_unused(ctx.log)
                report_stage(ctx, 'separation_cpu_fallback', '显存不足，改用 CPU 分离', device='cpu')
                vocal, bg = _run_demucs(ctx, src441, audio_dir, device="cpu")
        finally:
            src441.unlink(missing_ok=True)
        ctx.report_progress(85)
        ctx.check_cancel()
    ctx.register_file("vocal.wav", vocal, "audio")
    report_stage(ctx, 'saving', '登记预处理音轨与结果')
    ctx.register_file("background.wav", bg, "audio")
    ctx.write_json("separate.json",
                   {"separated": True, "model": str(ctx.cfg("separate.model", "htdemucs"))})
    ctx.log("✅ 预处理完成 (raw + vocal + background)")
