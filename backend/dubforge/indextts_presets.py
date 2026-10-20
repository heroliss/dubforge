"""IndexTTS WebUI v1.0 预设：转换当前参数与文件，不写全局或项目设置。"""
import copy
import hashlib
import json
from pathlib import Path
import re
import threading

from dubforge import config
from dubforge.errors import ConfigError, PresetError, ServiceStartError
from dubforge.pipeline.step_params import validate_numeric_updates
from dubforge.tts_service import installation_root
from dubforge.utils.fs import atomic_write_bytes

PREFIX = "tts.indextts25."
ADVANCED = ("do_sample", "top_p", "top_k", "temperature", "length_penalty", "num_beams",
            "repetition_penalty", "max_mel_tokens", "max_text_tokens_per_segment")
EMOTIONS = {0: "same", 1: "preset", 2: "vector", 3: "text"}
EXTENSION = ("lang", "duration_factor", "duration_retry_factors", "timbre_mode", "emo_mode",
             "dynamic_reference_max_seconds", "timbre_reference_line_id", "emo_reference_line_id")
_lock = threading.Lock()


def roots(project_dir="") -> tuple[dict[str, Path], str]:
    result = {"local": config.data_root() / "presets" / "indextts25"}
    warning = ""
    try:
        result["shared"] = installation_root("indextts25", project_dir) / "outputs" / "presets"
    except (ServiceStartError, OSError) as exc:
        warning = str(exc)
    return result, warning


def _folder(storage: str, name: str, project_dir="") -> Path:
    locations, _ = roots(project_dir)
    if storage not in locations:
        raise PresetError("预设目录不可用，请填写 IndexTTS 安装目录，或选择 DubForge 预设库。")
    if (not isinstance(name, str) or not name or name != name.strip()
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name.endswith((".", " "))
            or name in {".", ".."} or len(name) > 100
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name)):
        raise PresetError("预设名称无效；请勿使用路径分隔符、特殊字符或系统保留名称。")
    root = locations[storage].resolve()
    folder = (root / name).resolve()
    if not folder.is_relative_to(root):
        raise PresetError("预设路径超出预设库。")
    return folder


def list_presets(project_dir="") -> dict:
    locations, warning = roots(project_dir)
    entries = []
    try:
        for storage, root in locations.items():
            if root.is_dir():
                entries.extend({"name": p.name, "storage": storage} for p in sorted(root.iterdir())
                               if (p / "preset.json").is_file())
    except OSError as exc:
        raise PresetError(f"读取预设库失败：{exc}") from exc
    return {"presets": entries, "directories": {k: str(v) for k, v in locations.items()}, "warning": warning}


def _audio(folder: Path, raw, label: str) -> str:
    if not raw:
        return ""
    if not isinstance(raw, str):
        raise PresetError(f"{label}路径格式错误。")
    audio = (folder / raw).resolve()
    if not audio.is_relative_to(folder) or not audio.is_file():
        raise PresetError(f"{label}不存在或位于预设目录之外：{raw}")
    return str(audio)


def load(storage: str, name: str, project_dir="") -> dict:
    folder = _folder(storage, name, project_dir)
    try:
        payload = json.loads((folder / "preset.json").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise PresetError(f"无法读取预设「{name}」：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("version", "1.0") != "1.0":
        raise PresetError("预设不是支持的 IndexTTS v1.0 格式。")
    method = payload.get("emo_control_method", 0)
    if type(method) is not int or method not in EMOTIONS:
        raise PresetError("预设的情感模式无效。")
    advanced = payload.get("advanced_params", {})
    extension = payload.get("dubforge", {})
    if not isinstance(advanced, dict) or not isinstance(extension, dict):
        raise PresetError("预设参数格式错误。")
    defaults = config.DEFAULTS["tts"]["indextts25"]
    values = {PREFIX + key: advanced.get(key, defaults[key]) for key in ADVANCED}
    values.update({PREFIX + "timbre_mode": "preset", PREFIX + "emo_mode": EMOTIONS[method],
                   PREFIX + "timbre_audio": _audio(folder, payload.get("prompt_audio"), "音色参考音频"),
                   PREFIX + "emo_audio": _audio(folder, payload.get("emo_audio"), "情感参考音频")})
    for key in ("emo_weight", "emo_vector", "emo_text", "emo_random"):
        values[PREFIX + key] = payload.get(key, 0.65 if key == "emo_weight" else copy.deepcopy(defaults[key]))
    for key in EXTENSION:
        if key in extension:
            values[PREFIX + key] = extension[key]
    for part in ('timbre', 'emo'):
        # 旧稳定预设没有句子选择器；载入时明确回到默认第 1 句，不能继承表单旧选择。
        if values[PREFIX + f'{part}_mode'] == 'stable':
            values.setdefault(PREFIX + f'{part}_reference_line_id', 1)
    try:
        validate_numeric_updates(values)
    except ConfigError as exc:
        raise PresetError(str(exc)) from exc
    warnings = []
    if not extension:
        warnings.append("原生预设不含合成语言、时长因子和超长重试设置，已保留页面当前值。")
    if values[PREFIX + "timbre_mode"] == "preset" and not values[PREFIX + "timbre_audio"]:
        warnings.append("预设没有音色参考音频，请在合成前补充。")
    if values[PREFIX + "emo_mode"] == "preset" and not values[PREFIX + "emo_audio"]:
        warnings.append("预设没有情感参考音频，请补充或改用同音色参考。")
    return {"values": values, "warnings": warnings}


def _copy_audio(raw: str, folder: Path, stem: str) -> str:
    if not raw:
        return ""
    source = Path(raw)
    if not source.is_file():
        raise PresetError(f"参考音频不存在：{raw}")
    data = source.read_bytes()
    # 内容名使覆盖过程先准备新音频，最后替换 JSON；中断不会破坏旧预设。
    name = f"{stem}-{hashlib.sha256(data).hexdigest()[:16]}{source.suffix or '.wav'}"
    target = folder / name
    if not target.is_file() or target.read_bytes() != data:
        atomic_write_bytes(target, data)
    return name


def save(storage: str, name: str, values: dict, *, project_dir="", overwrite=False) -> dict:
    folder = _folder(storage, name, project_dir)
    settings = copy.deepcopy(config.DEFAULTS["tts"]["indextts25"])
    settings.update({key[len(PREFIX):]: value for key, value in values.items() if key.startswith(PREFIX)})
    try:
        validate_numeric_updates({PREFIX + key: val for key, val in settings.items()})
    except ConfigError as exc:
        raise PresetError(str(exc)) from exc
    method = next((key for key, mode in EMOTIONS.items() if mode == settings["emo_mode"]), 1)
    dynamic = settings["timbre_mode"] != "preset" or settings["emo_mode"] in {"stable", "dynamic"}
    if storage == "shared" and dynamic:
        raise PresetError("IndexTTS 不能读取视频原声参考规则；请改用固定参考音频，或保存到 DubForge 预设库。")
    timbre = settings["timbre_audio"] if settings["timbre_mode"] == "preset" else ""
    emotion = settings["emo_audio"] if settings["emo_mode"] == "preset" else ""
    if settings["timbre_mode"] == "preset" and not timbre:
        raise PresetError("请先填写音色参考音频，再保存预设。")
    if settings["emo_mode"] == "preset" and not emotion:
        raise PresetError("请先填写情感参考音频，或改用同音色参考。")
    if any(not Path(path).is_file() for path in (timbre, emotion) if path):
        raise PresetError("参考音频不存在，请检查页面中的音频路径。")
    try:
        with _lock:
            target = folder / "preset.json"
            if target.exists() and not overwrite:
                raise PresetError("同名预设已存在；请改名，或勾选覆盖同名预设。", code="PRESET_EXISTS")
            payload = {"version": "1.0", "emo_control_method": method,
                       **{key: settings[key] for key in ("emo_weight", "emo_vector", "emo_text", "emo_random")},
                       "advanced_params": {key: settings[key] for key in ADVANCED},
                       "prompt_audio": _copy_audio(timbre, folder, "prompt"),
                       "emo_audio": _copy_audio(emotion, folder, "emo_ref"),
                       "dubforge": {key: settings[key] for key in EXTENSION}}
            atomic_write_bytes(target, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError as exc:
        raise PresetError(f"保存预设失败：{exc}") from exc
    return {"name": name, "storage": storage, "path": str(folder / "preset.json")}
