"""轻量环境检查，不导入模型、不下载权重，也不修改运行中的 Python 环境。"""
import importlib.util
import shutil
import sys

from dubforge import config
from dubforge.errors import DubForgeError, ServiceStartError
from dubforge.tts_service import _installation

COMPONENTS = {
    "asr": ("本地语音识别", ("faster_whisper", "torch"), "-Asr"),
    "separation": ("人声与背景音分离", ("demucs", "torch", "torchaudio"), "-Separation"),
}


def module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def require_component(key: str) -> None:
    label, modules, flag = COMPONENTS[key]
    if not all(module_available(module) for module in modules):
        raise DubForgeError(f"尚未安装「{label}」的可选依赖", code="OPTIONAL_COMPONENT_MISSING",
                            suggestion=f"关闭 DubForge 后，在程序目录运行 .\\scripts\\install.ps1 {flag} -SkipFrontend，再重新启动。也可在设置页查看环境安装说明。")


def report() -> dict:
    components = [{"key": key, "label": label,
                   "installed": all(module_available(module) for module in modules),
                   "command": f".\\scripts\\install.ps1 {flag} -SkipFrontend"}
                  for key, (label, modules, flag) in COMPONENTS.items()]
    services = []
    for backend, label, docs in (
        ("indextts25", "IndexTTS 2.5", "https://github.com/index-tts/index-tts#-getting-started"),
        ("gptsovits", "GPT-SoVITS", "https://github.com/RVC-Boss/GPT-SoVITS#installation"),
    ):
        item = {"label": label, "backend": backend, "url": config.get(f"tts.{backend}.url"),
                "docs": docs, "installed": False, "directory": "", "message": ""}
        try:
            root, _, _ = _installation(backend, config.get(f"tts.{backend}.project_dir") or "")
            item.update(installed=True, directory=str(root))
        except (ServiceStartError, OSError) as exc:
            item["message"] = str(exc)
        services.append(item)
    return {"program_directory": str(config.REPO_ROOT), "python": sys.version.split()[0],
            "tools": [{"name": name, "path": shutil.which(name)} for name in ("ffmpeg", "ffprobe", "uv", "node", "npm")],
            "components": components, "services": services}
