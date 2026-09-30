"""
步骤 8: tts —— 批量配音
=======================
输入: 语义时间轴、逐句译文 + preprocess 的 vocal.wav (dynamic/stable 参考源)
后端: config tts.backend → indextts25 | gptsovits
句级缓存: artifacts/tts/{id}.wav 与旁置 .input 指纹匹配才复用 (包括工作台试配音频)
参考解析 (双维度, 移植自工作台验证过的语义):
  preset  → config 路径; stable → vocal 开头切片; dynamic → vocal 按行时间切
dynamic 始终使用本句原声，短句只提示，不改用其他说话人的音频。
"""
import json
import hashlib
import math
from dubforge.progress import RateProgress
import subprocess
from pathlib import Path

from dubforge.backends import tts_gptsovits, tts_indextts25
from dubforge.errors import DubForgeError
from dubforge.domain import crud
from dubforge.utils.fs import atomic_replace
from dubforge.pipeline.line_settings import effective_config
from dubforge.pipeline import dubbing_edit, dubbing_session


def line_fingerprint(project, line, cfg_get=None, *, dependencies=None, _legacy=False):
    """句级缓存绑定实际文本、时间和合成设置，工作台与批量配音共用。"""
    from dubforge.pipeline.dependencies import Dependencies
    deps = dependencies or Dependencies(project)
    cfg_get = effective_config(project, line, cfg_get or deps.cfg)
    backend = cfg_get("tts.backend", "indextts25")
    settings = dict(cfg_get(f"tts.{backend}", {}) or {})
    settings = {k: cfg_get(f"tts.{backend}.{k}", v) for k, v in settings.items()
                if k not in {"preset_baseline", "source_preset", "project_dir", "duration_retry_factors", "request_timeout_seconds"}}
    # 新增控件的空值与旧实现固定传入的值相同，不使已有音频无故失效。
    if backend == "indextts25":
        for key in ("emo_text", "emo_random"):
            if not settings.get(key):
                settings.pop(key, None)
    references = {}
    for part in ("timbre", "emo"):
        if settings.get(f"{part}_mode") == "preset":
            path = Path(settings.get(f"{part}_audio") or "")
            if path.is_file():
                stat = path.stat()
                references[part] = (stat.st_size, stat.st_mtime_ns)
        elif settings.get(f"{part}_mode") in {"stable", "dynamic"}:
            art = deps.arts.get("preprocess/vocal.wav")
            references[part] = art.sha256 if art else "missing"
    # 预估时长、间隙和提示标记不改变合成输入，不能让已合成音频失效。
    keys = ('id', 'text', 'start', 'end', 'duration', 'est_dur', 'gap', 'speed_flag') if _legacy else ('id', 'text', 'start', 'end')
    content = {k: line[k] for k in keys if k in line}
    data = {"line": content, "backend": backend, "settings": settings, "references": references}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def line_cache_valid(project, line, pdir, cfg_get=None, *, dependencies=None, take=1):
    marker = dubbing_edit.take_path(pdir, line['id'], take).with_suffix('.input')
    try:
        wav = marker.with_suffix(".wav")
        if not wav.is_file() or not wav.stat().st_size:
            return False
        if wav.with_suffix('.take.json').exists():
            snapshot = dubbing_edit.read_take(pdir, line, take)
            from dubforge.pipeline.line_settings import identity
            if snapshot.get('identity') != identity(line) or snapshot.get('version') != dubbing_edit.file_version(wav):
                return False
        saved = marker.read_text(encoding="utf-8")
        candidates = [line]
        if 'voice_timing' in line and not wav.with_suffix('.take.json').exists():
            candidates.append({**line, 'start': line['voice_timing'][0], 'end': line['voice_timing'][1]})
        return any(saved == line_fingerprint(project, candidate, cfg_get,
                   dependencies=dependencies, _legacy=legacy) for candidate in candidates for legacy in (False, True))
    except OSError:
        return False


def _cut(src: Path, dst: Path, start: float, dur: float) -> Path:
    """ffmpeg 切片; 目标存在即复用"""
    if dst.exists() and dst.stat().st_size > 1024:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".wav.tmp")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.2f}",
                        "-t", f"{dur:.2f}", "-ar", "32000", "-ac", "1",
                        "-f", "wav", str(tmp)], check=True, capture_output=True)
        atomic_replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def _resolve_ref(ctx, mode: str, custom: str, line: dict,
                 vocal: Path, cache_dir: Path, tag: str, log=lambda m: None) -> Path:
    label = '音色' if tag == 'timbre' else '情感'
    if mode == "preset":
        p = Path(custom or "")
        if not p.is_file():
            raise DubForgeError(
                f"{tag} 参考音频不存在: {p}", code="REF_MISSING",
                suggestion="请在设置页填写有效的参考音频路径, 或切换 stable/dynamic 模式。")
        log(f"句 {line['id']} {label}参考：自定义音频 {p}")
        return p
    if mode == "stable":
        path = _cut(vocal, cache_dir / "stable_ref.wav", 0.0, 8.0)
        log(f"句 {line['id']} {label}参考：开头 0.00–8.00 秒 → {path}")
        return path
    if mode == "dynamic":
        dur = max(0.0, line["end"] - line["start"])
        path = _cut(vocal, cache_dir / f"ref_{tag}_{line['id']}.wav", line["start"], dur)
        log(f"句 {line['id']} {label}参考：本句 {line['start']:.2f}–{line['end']:.2f} 秒 → {path}"
            + ('；参考不足 2 秒，若音色不稳可为本句指定同一说话人的较长参考' if dur < 2 else ''))
        return path
    raise DubForgeError(f"未知参考模式: {mode}", code="REF_MODE")


def synth_line(project, line: dict, pdir: Path, force: bool = False,
               log=lambda m: None, cfg_get=None, take=1, progress=None,
               on_attempt=lambda *args: None, can_retry=lambda: True, check_cancel=lambda: None) -> dict:
    """合成单句 (工作台与批量步骤共用)。返回 {status, dur}"""
    from dubforge import config
    from dubforge.domain import crud

    if cfg_get is None:
        overrides = crud.project_step_params(project)
        cfg_get = lambda key, default=None: overrides.get(key, config.get(key, default))

    from dubforge import tts_service
    cfg_get = effective_config(project, line, cfg_get)
    with tts_service.use(cfg_get):
        return _synth_line(project, line, pdir, force, log, cfg_get, take, progress, on_attempt, can_retry, check_cancel)


def _synth_line(project, line, pdir, force, log, cfg_get, take, progress, on_attempt, can_retry, check_cancel):
    from dubforge.domain import crud

    out = dubbing_edit.take_path(pdir, line['id'], take)
    if (out.is_file() and out.stat().st_size > 0 and not force
            and line_cache_valid(project, line, pdir, cfg_get, take=take)):
        # 工作台先生成的音频也必须回到统一产物索引中。
        if f"tts/{out.name}" not in crud.latest_artifacts(project.id, ["tts"]):
            crud.add_artifact(project.id, "tts", "audio", out.name,
                              out, str(out.relative_to(pdir)))
        return {"status": "skipped", "file": out}

    fingerprint = line_fingerprint(project, line, cfg_get)
    backend = cfg_get("tts.backend", "indextts25")
    out.parent.mkdir(parents=True, exist_ok=True)
    duration_retry = None

    def section(path: str, keys: tuple[str, ...]) -> dict:
        base = dict(cfg_get(path, {}) or {})
        for key in keys:
            base[key] = cfg_get(f"{path}.{key}", base.get(key))
        return base

    if backend == "indextts25":
        cfg = section("tts.indextts25", (
            "url", "lang", "timbre_mode", "timbre_audio", "emo_mode", "emo_audio",
            "emo_weight", "emo_vector", "emo_text", "emo_random", "duration_factor", "do_sample", "top_p",
            "top_k", "temperature", "length_penalty", "num_beams",
            "repetition_penalty", "max_mel_tokens", "max_text_tokens_per_segment", "duration_retry_factors"))
        from dubforge.pipeline import tts_duration
        factors = tts_duration.attempt_factors(cfg['duration_factor'], cfg['duration_retry_factors'])
        vocal = pdir / "audio" / "vocal.wav"
        # 原声与句子改变后不复用上一次切出的参考片段。
        cache_dir = pdir / "artifacts" / "tts" / "refs" / fingerprint
        timbre = _resolve_ref(None, cfg.get("timbre_mode", "preset"),
                              cfg.get("timbre_audio", ""), line, vocal,
                              cache_dir, "timbre", log)
        emo_mode = cfg.get("emo_mode", "preset")
        emo_ref = None
        if emo_mode in ("preset", "stable", "dynamic"):
            emo_ref = _resolve_ref(None, emo_mode, cfg.get("emo_audio", ""),
                                   line, vocal, cache_dir, "emo", log)
        def generate(path, factor):
            active = True

            def forward(value):
                if active and progress is not None:
                    progress(value)

            try:
                return tts_indextts25.synth(
                    cfg.get("url", "http://127.0.0.1:7860/"),
                    line["text"], path, cfg.get("lang", "ZH"),
                    timbre_ref=timbre, emo_mode=emo_mode, emo_ref=emo_ref,
                    emo_weight=float(cfg.get("emo_weight", 1.0)),
                    emo_vector=cfg.get("emo_vector") or [],
                    emo_text=cfg.get("emo_text", ""), emo_random=cfg.get("emo_random", False),
                    duration_factor=factor,
                    advanced={k: cfg[k] for k in (
                        "do_sample", "top_p", "top_k", "temperature", "length_penalty",
                        "num_beams", "repetition_penalty", "max_mel_tokens",
                        "max_text_tokens_per_segment") if k in cfg},
                    log=log, progress=forward if progress is not None else None,
                    check_cancel=check_cancel,
                    timeout_seconds=float(cfg_get('tts.indextts25.request_timeout_seconds', 300)))
            finally:
                active = False  # 旧尝试的迟到进度不能污染下一次缩时重试。
        duration_retry = tts_duration.synthesize(generate, out, line, factors,
            log=log, on_attempt=on_attempt, can_retry=can_retry)
        cfg['duration_factor'] = duration_retry['used_factor']
        if cfg['duration_factor'] != factors[0]:
            from dubforge.pipeline.line_settings import save
            from dubforge.pipeline.dependencies import Dependencies
            # 合成结果只覆盖本句时长因子；重新读取项目，保留前句结果和其他参数。
            with dubbing_session.gate:
                latest = crud.get_project(project.uid)
                save(latest, line, {'tts.indextts25.duration_factor': cfg['duration_factor']}, [], Dependencies(latest).cfg)
                project.params = latest.params
            fingerprint = line_fingerprint(project, line, cfg_get)
            log(f"句 {line['id']} 已保存本句时长因子：{cfg['duration_factor']:g}（采用音频的实际参数）")
    elif backend == "gptsovits":
        gcfg = section("tts.gptsovits", ("url", "character"))
        tts_gptsovits.synth(gcfg.get("url", "http://127.0.0.1:9880/"),
                            line["text"], out, "zh",
                            character=gcfg.get("character"), log=log)
    else:
        raise DubForgeError(f"未知 TTS 后端: {backend}", code="TTS_BACKEND")

    crud.add_artifact(project.id, "tts", "audio", out.name,
                      out, str(out.relative_to(pdir)))
    from dubforge.utils.fs import atomic_write_bytes
    atomic_write_bytes(out.with_suffix(".input"), fingerprint.encode())
    dubbing_edit.record_take(project, line, pdir, take, fingerprint, cfg if backend == 'indextts25' else gcfg,
                            duration_retry=duration_retry)
    return {"status": "synthesized", "file": out}


def run(ctx) -> None:
    from dubforge import tts_service
    from dubforge.utils.gpu_memory import release_unused
    release_unused(ctx.log)
    with tts_service.use(ctx.cfg):
        _run(ctx)


def _run(ctx) -> None:
    from dubforge.runner import StepCancelled
    from dubforge.pipeline.step_dubtask import build_task
    task = build_task(ctx)
    pdir = ctx.pdir
    done = skipped = failed = 0
    selected = getattr(ctx, 'task_data', {}).get('scope') == 'selected'
    if selected:
        targets = {row['id']: row for row in ctx.task_data['lines']}
        lines = [{**line, 'take': targets[line['id']]['take']} for line in task['lines'] if line['id'] in targets]
    else:
        lines = dubbing_edit.synthesis_lines(ctx.project, [line for line in task['lines'] if line['text'].strip()], pdir)
        ctx.task_data = {'scope': 'all', 'lines': [
            {'id': line['id'], 'take': line.get('take') or 1, 'status': 'queued'} for line in lines]}
        targets = {row['id']: row for row in ctx.task_data['lines']}
    ctx.save_task_data()
    ctx.log(f"本次配音范围：{len(lines)} 句" + ('（列表所选）' if selected else '（选用配音）'))
    from dubforge.pipeline.step_dubtask import estimate_seconds
    weights = [max(0.1, float(line.get("est_dur") or estimate_seconds(line["text"]))) for line in lines]
    total_weight = max(sum(weights), .1)
    progress = RateProgress(ctx, total_weight, eta_on_update=True)
    completed = 0.0
    for line, weight in zip(lines, weights):
        ctx.check_pause()
        ctx.check_cancel()
        cached = False
        row = targets[line['id']]
        row['status'] = 'running'
        ctx.save_task_data()
        try:
            take = line.get('take') or 1
            force = bool(getattr(ctx, 'force', False))
            if not force and dubbing_edit.playable(ctx.project, line, pdir, take):
                r = {'status': 'skipped'}
            else:
                with dubbing_session.use(ctx.project, line['id'], from_pipeline=True) as report:
                    sample = {'open': True, 'value': 0.0, 'attempt': 0, 'total': 1}

                    def on_attempt(index, total, factor):
                        sample.update(value=0.0, attempt=index, total=total)
                        row.update(attempt=index, max_attempts=total, duration_factor=factor)
                        report(None, attempt=index, factor=factor)
                        ctx.save_task_data()

                    def can_retry():
                        # 停止时完成当前句的登记，不再额外调用模型；执行器随后处理取消。
                        try:
                            ctx.check_pause()
                            ctx.check_cancel()
                            return True
                        except StepCancelled:
                            return False

                    def report_current(value, *, sample=sample, report=report,
                                       completed=completed, weight=weight):
                        # 部分服务在结束后仍可能回调，不能影响下一句/下一轮任务。
                        if not sample['open'] or not isinstance(value, (int, float)) or not math.isfinite(value):
                            return
                        sample['value'] = max(sample['value'], min(1.0, max(0.0, value)))
                        report(sample['value'])
                        fraction = (sample['attempt'] + sample['value']) / sample['total']
                        ctx.report_progress(99 * (completed + weight * fraction) / total_weight)

                    try:
                        r = synth_line(ctx.project, line, pdir, force=force,
                                       log=ctx.log, cfg_get=ctx.cfg, take=take, progress=report_current,
                                       on_attempt=on_attempt, can_retry=can_retry, check_cancel=ctx.check_cancel)
                    finally:
                        sample['open'] = False
            if selected:
                # 逐句更新最新项目，避免后一句覆盖前一句的声源/版本选择。
                ctx.project = crud.get_project(ctx.project.uid)
                dubbing_edit.save_selection(ctx.project, line, 'dub', take)
            if r["status"] == "skipped":
                skipped += 1
                cached = True
            else:
                done += 1
            row['status'] = 'skipped' if cached else 'done'
            ctx.log(f"句 {line['id']}: {r['status']}")
        except StepCancelled:
            raise
        except Exception as e:  # noqa: BLE001 单句失败不阻断整批 (重跑补)
            if isinstance(e, DubForgeError) and e.code in {
                    'TTS_REQUEST_TIMEOUT', 'TTS_SERVICE_DRAINING', 'TTS_REQUEST_FAILED', 'SERVICE_OFFLINE'}:
                row.update(status='failed', error=e.message)
                ctx.save_task_data()
                raise  # 服务级故障停止整批，不能继续向同一阻塞服务提交几百句。
            failed += 1
            row.update(status='failed', error=e.message if isinstance(e, DubForgeError) else str(e))
            ctx.log(f"句 {line['id']} 失败: {type(e).__name__}: {e}")
            cached = True  # 失败句不贡献已成功合成的测速样本。
        completed += weight
        progress.update(completed, cached=cached)
        ctx.save_task_data()

    if failed and done == 0 and skipped == 0:
        raise DubForgeError(f"全部 {failed} 句合成失败", code="TTS_ALL_FAILED",
                            suggestion="查看逐句日志定位首错; 常见: 服务未启动/参考音频无效。")
    if failed:
        raise DubForgeError(f"仍有 {failed} 句配音失败，已生成的音频保留", code="TTS_PARTIAL_FAILED",
                            suggestion="查看逐句日志后重试，全部句子完成后再合成成片。")
    ctx.log(f"✅ 批量配音完成: 新合成 {done} / 复用 {skipped} / 失败 {failed}")
