# -*- coding: utf-8 -*-
"""
DubForge 配置管理
=================
- 单一配置源: 仓库根 config.yaml (含密钥, 永不入库; 仓库提供 config.example.yaml 模板)
- 读写均走本模块; 键不存在时返回默认值, 写入保留未触及的键
- 路径约定:
    REPO_ROOT   = backend/ 的上级
    DATA_ROOT   = REPO_ROOT/data (环境变量 DUBFORGE_DATA_ROOT 可覆盖, 测试夹具用)
    CONFIG_PATH = REPO_ROOT/config.yaml (环境变量 DUBFORGE_CONFIG 可覆盖)
"""
import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """配置文件路径 (惰性取环境变量: 测试夹具在导入后才注入, 必须每次读)"""
    return Path(os.environ.get("DUBFORGE_CONFIG", REPO_ROOT / "config.yaml"))


def data_root() -> Path:
    """数据根目录 (惰性取环境变量, 同上; 测试隔离依赖此语义)"""
    return Path(os.environ.get("DUBFORGE_DATA_ROOT", REPO_ROOT / "data"))

PROJECTS_DIR_NAME = "projects"
DB_FILE_NAME = "dubforge.db"

# 默认配置 (config.yaml 缺失/缺键时生效; 模板见 config.example.yaml)
DEFAULTS: dict[str, Any] = {
    "llm": {
        # OpenAI 兼容接口 (智谱/GLM 等); 翻译与分句语义切共用
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4.7",
        "max_workers": 1,          # LLM 并发请求数 (本地/弱限流设 1)
        "temperature": 0.1,
    },
    "download": {
        "resolution": 1080,         # 最高分辨率
        "cookies_path": "",         # YouTube cookies.txt (可选)
        "proxy": "",                # 例: http://127.0.0.1:7897 (空=读环境变量)
    },
    "separate": {
        "enabled": True,            # demucs 人声/背景分离
        "speaker_diarization": False,  # pyannote 说话人分离 (需 hf_token)
        "hf_token": "",
    },
    "asr": {
        "backend": "faster_whisper",
        "model": "large-v3-turbo",
        "language": "auto",         # 源语言, auto=自动检测
    },
    "split": {
        "max_split_length": 22,     # 粗切每块词数上限
    },
    "translate": {
        "target_language": "简体中文",
        "style_prompt": "",          # 自定义风格提示词 (翻译人设/语气)
        "whole_doc_max_lines": 60,   # ≤该行数走整篇直译 (ADR-0002 三档策略)
        "global_polish": True,       # 分块模式启用全局审校第三步
        "summary_length": 8000,      # 摘要取样字符数
        "custom_terms_path": "",     # 术语表 xlsx (src/dst/备注 三列)
    },
    "tts": {
        "backend": "indextts25",
        "indextts25": {
            "url": "http://127.0.0.1:7860/",
            "project_dir": "",       # index-tts 项目根 (空=自动查找)
            "lang": "ZH",
            # 双维度参考 (沿用工作台验证过的语义):
            #   preset=自定义参考 / stable=视频开头切片 / dynamic=每句原声 / vector=情感向量(仅情感)
            "timbre_mode": "preset",
            "timbre_audio": "",
            "emo_mode": "preset",
            "emo_audio": "",
            "emo_weight": 1.0,
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],  # 喜怒哀惧厌恶低落惊喜平静
            "duration_factor": 1.0,
        },
        "gptsovits": {
            "url": "http://127.0.0.1:9880/",
            "character": "",
        },
        "subtitles": {"max_length": 75},
    },
    "assemble": {
        "bgm_volume": 0.3,          # 背景音混入音量
        "ducking": True,            # 人声段自动压低背景音 (sidechaincompress)
        "loudnorm": True,           # EBU R128 响度归一
        "burn_subtitles": False,    # 烧录字幕 (默认外挂)
        "crf": 18,
    },
    "server": {
        "port": 8600,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并: override 覆盖 base, 嵌套 dict 深合并, 其余类型整体替换"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读取配置 (默认值 ∪ config.yaml); 每次调用重读, 改配置即时生效"""
    cp = config_path()
    if cp.exists():
        with open(cp, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULTS, user)
    return copy.deepcopy(DEFAULTS)


def get(key: str, default: Any = None) -> Any:
    """点分路径取配置值, 例: get('tts.indextts25.url')"""
    node: Any = load_config()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_key(key: str, value: Any) -> None:
    """点分路径写配置值并落盘 (保留其他键; 原子写)"""
    user: dict = {}
    cp = config_path()
    if cp.exists():
        with open(cp, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    node = user
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(user, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, cp)          # 原子替换, 避免写一半损坏


def projects_dir() -> Path:
    """全部视频项目的数据根: data/projects/"""
    p = data_root() / PROJECTS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_root() / DB_FILE_NAME
