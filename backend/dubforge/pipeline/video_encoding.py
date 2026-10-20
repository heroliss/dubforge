"""显式成片编码合同；硬件能力只在执行边界检查，不改变已冻结的选择。"""
import copy
import json
import math
import re
import subprocess
import threading
from contextlib import contextmanager

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage

ENCODER_KEY = 'assemble.video_encoder'
CQ_KEY = 'assemble.nvenc_cq'
ENCODING_KEYS = frozenset({ENCODER_KEY, CQ_KEY, 'assemble.crf'})
NVENC_PROFILE = 'h264-nvenc-p6-hq-v1'
MIN_FREE_MIB = 1024


class EncodingError(DubForgeError):
    code = 'ASSEMBLE_ENCODER_INVALID'
    suggestion = '请检查视频编码设置；硬件不可用不会自动改用软件，可明确选择软件编码后重新检查。'


def normalize_values(values):
    """旧冻结记录缺字段时显式沿用软件，绝不借用后来改变的全局默认。"""
    result = copy.deepcopy(values)
    result.setdefault(ENCODER_KEY, 'software')
    result.setdefault(CQ_KEY, 18)
    return result


def profile(values):
    mode = values.get(ENCODER_KEY, 'software')
    if not isinstance(mode, str) or mode not in {'software', 'nvenc'}:
        raise EncodingError('视频编码方式无效，请明确选择软件或NVIDIA硬件编码')
    key, default = (CQ_KEY, 18) if mode == 'nvenc' else ('assemble.crf', 18)
    value = values.get(key, default)
    minimum, maximum = (14, 28) if mode == 'nvenc' else (0, 51)
    if (type(value) not in (int, float) or not math.isfinite(value) or int(value) != value
            or not minimum <= value <= maximum):
        raise EncodingError('视频画质数值无效，请重新选择有效的整数画质')
    value = int(value)
    if mode == 'software':
        return dict(mode=mode, encoder='libx264', preset='medium', rate_control='crf', quality=value)
    return dict(mode=mode, version=NVENC_PROFILE, encoder='h264_nvenc', preset='p6',
                tune='hq', rate_control='vbr', quality=value)


def frozen_profile(version):
    expected = profile(version.get('values') or {})
    saved = version.get('encoder_profile')
    if saved is not None and saved != expected:
        raise EncodingError('冻结的视频编码配置不一致，请重新检查成片版本')
    return expected


def require_software_baseline(saved):
    def object_value(value):
        return value if isinstance(value, dict) else {}
    review = object_value(object_value(saved.get('review_render')).get('encoder'))
    recorded = object_value(object_value(saved.get('video_encoding')).get('profile'))
    frozen = object_value(saved.get('encoder_profile'))
    if (profile(saved.get('values') or {})['mode'] != 'software'
            or review.get('name') == 'h264_nvenc'
            or any(value.get('mode') == 'nvenc' or value.get('encoder') == 'h264_nvenc'
                   for value in (recorded, frozen))):
        raise EncodingError('该成片使用硬件编码，当前局部修订、所选更新与成片剪辑暂不支持此编码基准；请完整生成普通版本。',
                            code='ASSEMBLY_ENCODER_BASELINE_UNSUPPORTED')


def needs_encoding(version):
    return bool(version.get('burn_subtitles') or version.get('include_comments') or version.get('output_cuts')
                or version.get('partial') or version.get('update') or version.get('timeline'))


def arguments(value):
    if value['mode'] == 'software':
        return ['-c:v', 'libx264', '-crf', str(value['quality']), '-preset', 'medium']
    return ['-c:v', 'h264_nvenc', '-gpu', '0', '-preset', value['preset'], '-tune', value['tune'],
            '-rc', 'vbr', '-cq', str(value['quality']), '-b:v', '0']


def context_profile(ctx):
    version = getattr(ctx, 'version', None)
    if version is not None:
        return frozen_profile(version)
    return profile({key: ctx.cfg(key, default) for key, default in
                    ((ENCODER_KEY, 'software'), (CQ_KEY, 18), ('assemble.crf', 18))})


def video_arguments(ctx, *, required=True):
    return arguments(context_profile(ctx)) if required else ['-c:v', 'copy']


def _run(command):
    """有界小诊断，不加载CUDA/PyTorch；测试必须替换此能力入口。"""
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EncodingError('无法核对NVIDIA编码能力，请检查FFmpeg与显卡服务后重试',
                            code='ASSEMBLE_ENCODER_UNAVAILABLE', detail=str(exc)) from exc
    if result.returncode:
        error = (result.stderr or '')[-2000:]
        if any(word in error.lower() for word in ('nvenc', 'cuda')) and any(
                word in error.lower() for word in ('out of memory', 'allocate memory', 'memory allocation')):
            raise EncodingError('NVIDIA编码预检显存分配失败，请释放闲置GPU任务后重试',
                                code='ASSEMBLE_ENCODER_MEMORY', detail=error)
        raise EncodingError('NVIDIA编码能力核对失败，请检查FFmpeg与显卡服务后重试',
                            code='ASSEMBLE_ENCODER_UNAVAILABLE', detail=error)
    return result.stdout


def inspect_capability(video, value, *, check=lambda: None):
    def run(command):
        check()
        output = _run(command)
        check()
        return output
    encoders = run(['ffmpeg', '-hide_banner', '-encoders'])
    if not re.search(r'\bh264_nvenc\b', encoders):
        raise EncodingError('当前FFmpeg没有NVIDIA H.264编码器，请安装支持NVENC的FFmpeg或明确选择软件编码',
                            code='ASSEMBLE_ENCODER_UNAVAILABLE')
    help_text = run(['ffmpeg', '-hide_banner', '-h', 'encoder=h264_nvenc'])
    if not re.search(r'\bp6\b', help_text) or not re.search(r'\bhq\b', help_text):
        raise EncodingError('当前FFmpeg不支持已冻结的NVENC画质预设，请检查版本后重试',
                            code='ASSEMBLE_ENCODER_UNAVAILABLE')
    memory = run(['nvidia-smi', '--id=0', '--query-gpu=memory.free', '--format=csv,noheader,nounits'])
    try:
        free = int(memory.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise EncodingError('显卡可用显存未知，暂不能确认硬件编码资源', code='ASSEMBLE_ENCODER_UNAVAILABLE') from exc
    if free < MIN_FREE_MIB:
        raise EncodingError(f'NVIDIA可用显存仅{free}MiB，低于硬件编码的保守准入余量{MIN_FREE_MIB}MiB；请释放闲置GPU任务后重试',
                            code='ASSEMBLE_ENCODER_MEMORY', detail=dict(free_mib=free, required_mib=MIN_FREE_MIB))
    try:
        media = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,pix_fmt,color_range,color_space,color_transfer,color_primaries,r_frame_rate',
            '-of', 'json', str(video)]))['streams'][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise EncodingError('原视频画面格式未知，不能确认NVENC会保留原画质', code='ASSEMBLE_ENCODER_FORMAT') from exc
    if (media.get('pix_fmt') not in {'yuv420p', 'nv12'} or media.get('color_range') == 'pc'
            or media.get('color_transfer') in {'smpte2084', 'arib-std-b67'}
            or type(media.get('width')) is not int or type(media.get('height')) is not int
            or media['width'] <= 0 or media['height'] <= 0 or media['width'] % 2 or media['height'] % 2):
        raise EncodingError('此原视频的位深、色彩或画幅不在当前NVENC保真支持范围；不会自动降位深、转SDR或缩放，请明确选择软件编码',
                            code='ASSEMBLE_ENCODER_FORMAT', detail=media)
    # 一帧同画幅初始化只核对设备/驱动/profile，不当作实片画质或速度证明。
    # 采用源画幅，避免小于NVENC最小尺寸的任意测试图造成错误拒绝。
    run(['ffmpeg', '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i',
         f'color=c=black:s={media["width"]}x{media["height"]}:r=1', '-frames:v', '1', '-an',
         *arguments(value), '-f', 'null', '-'])
    return dict(free_mib=free, gpu_index=0, source=media)


@contextmanager
def admission(ctx, video, value):
    """仅硬件编码借用GPU槽位及配音预约；软件完全沿原路径。"""
    if value['mode'] != 'nvenc':
        yield dict(profile=copy.deepcopy(value))
        return
    from dubforge import tts_service
    from dubforge.runner import runner
    check = getattr(ctx, 'check_cancel', lambda: None)
    check()
    setter = getattr(ctx, 'set_eta_provider', None)
    if callable(setter):
        setter(None, scope='phase')
    report_stage(ctx, 'waiting_encoder_resources', '等待硬件编码资源')
    slot = None
    gpu_slot = runner._slot_key('tts')
    if getattr(ctx, '_resource_slot', None) != gpu_slot:
        slot = runner.acquire_slot('tts', getattr(ctx, '_cancel', None) or threading.Event())
    try:
        check()
        with tts_service.reserve_for_render(ctx.cfg):
            report_stage(ctx, 'checking_video_encoder', '核对NVIDIA编码能力与画面格式')
            capability = inspect_capability(video, value, check=check)
            check()
            report = dict(profile=copy.deepcopy(value), **capability)
            log = getattr(ctx, 'log', None)
            if callable(log):
                log(f'视频编码：NVIDIA H.264 / {value["preset"]} / CQ{value["quality"]}；CQ与软件CRF不等价，保持原画幅与帧率。')
            yield report
    finally:
        if slot is not None:
            runner.release_slot('tts', slot)


def run(ctx, video, command, *, cwd, required=True, on_stdout=None, on_ready=None):
    from dubforge.pipeline.render_resources import bounded_ffmpeg_command
    from dubforge.utils.ffmpeg import complex_filter_command
    value = context_profile(ctx) if required else dict(mode='copy')
    with admission(ctx, video, value) as report:
        ctx._video_encoding_report = report
        with complex_filter_command(bounded_ffmpeg_command(command), cwd) as prepared:
            if on_ready:
                on_ready()
            result = ctx.run_subprocess(prepared, cwd=cwd, on_stdout=on_stdout)
        if result[0] and value['mode'] == 'nvenc':
            detail = str(result[2])[-2500:]
            lower = detail.lower()
            if any(word in lower for word in ('nvenc', 'cuda')):
                if any(word in lower for word in ('out of memory', 'allocate memory', 'memory allocation')):
                    raise EncodingError('NVIDIA硬件编码显存分配失败，旧成片保留；请释放闲置GPU任务后重试',
                                        code='ASSEMBLE_ENCODER_MEMORY', detail=detail)
                if any(word in lower for word in ('unsupported device', 'no nvenc capable', 'cannot load',
                                                 'initializeencoder failed', 'minimum required driver')):
                    raise EncodingError('NVIDIA硬件编码初始化失败，旧成片保留；请检查设备与驱动或明确选择软件编码',
                                        code='ASSEMBLE_ENCODER_UNAVAILABLE', detail=detail)
        return result
