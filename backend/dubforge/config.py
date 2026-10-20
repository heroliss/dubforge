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
import threading
from pathlib import Path
from typing import Any

import yaml

from dubforge.errors import ConfigError
from dubforge.utils.fs import atomic_write_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_LOCK = threading.RLock()
_runtime_data_root: Path | None = None


def config_path() -> Path:
    """配置文件路径 (惰性取环境变量: 测试夹具在导入后才注入, 必须每次读)"""
    return Path(os.environ.get("DUBFORGE_CONFIG", REPO_ROOT / "config.yaml"))


def data_root() -> Path:
    """数据根目录: 环境变量 DUBFORGE_DATA_ROOT (测试/部署) >
    config.yaml server.data_root (设置页可改, 重启生效) > 默认 <程序目录>/data"""
    with _CONFIG_LOCK:
        if _runtime_data_root is not None:
            return _runtime_data_root
    env = os.environ.get("DUBFORGE_DATA_ROOT")
    if env:
        return Path(env)
    custom = str(get("server.data_root") or "").strip()
    if custom:
        p = Path(custom)
        return p if p.is_absolute() else REPO_ROOT / p
    return REPO_ROOT / "data"


def _set_runtime_data_root(root: Path | None) -> None:
    """由数据库生命周期绑定/释放目录，防止配置热更新把数据库与产物拆到两处。"""
    global _runtime_data_root
    with _CONFIG_LOCK:
        _runtime_data_root = root.resolve() if root is not None else None

PROJECTS_DIR_NAME = "projects"
DB_FILE_NAME = "dubforge.db"

# 默认配置 (config.yaml 缺失/缺键时生效; 模板见 config.example.yaml)
DEFAULTS: dict[str, Any] = {
    "run": {"mode": "draft"},
    "assembly": {"target_version_ids": None},  # 空值表示尚未明确选择项目制作目标。
    "publish": {
        "project_target": None,  # 原BV只保存在本项目配置，禁止全局或模板继承。
        # 只用于新发布流程；已确认稿件的章节策略仍以其冻结快照为准。
        "chapters_enabled": True,
        "chapters_translate": True,
        "chapters_permanent": True,
        "chapters_instruction": "",
    },
    "llm": {
        # OpenAI 兼容接口 (智谱/GLM 等); 翻译与分句语义切共用
        # 多组 API 配置池: 按顺序降级 (旧版单 base_url/api_key 自动兼容)
        "profiles": [],
        "timeout": 300,            # 整理/翻译单次请求总时限；其他 LLM 请求的读超时秒数
        # temperature / thinking 属于请求参数, 不做全局配置 ——
        # 在各组 profiles 的专属参数里按模型填写 (留空 = 不发送, 用服务商默认)
        # GLM-5.3/5.3-Flash 通常只能用 reasoning_effort=low 降低思考量;
        # 其他支持开关的模型再按服务商填写 thinking={type: disabled}
    },
    "ui": {
        "llm_stream": {
            # 流式文字显示的追赶距离: 距离越大越平滑, 但显示会多一点延迟
            "reference_distance_chars": 500,
        },
    },
    "download": {
        "resolution": 1080,         # 最高分辨率
        "cookies_path": "",         # YouTube cookies.txt (可选)
        "proxy": "",                # 例: http://127.0.0.1:7897 (空=读环境变量)
    },
    "separate": {
        "enabled": True,            # demucs 人声/背景分离
        "model": "htdemucs",        # htdemucs | htdemucs_ft (精调: 更优, 约慢一倍)
        "shifts": 0,                # 位移平均次数 (0=快; 1~2 更好, 按倍变慢)
        "overlap": 0.25,            # 长音频分块处理的重叠比例
        # 说话人分离 (pyannote, 需 hf_token) 为规划中的可选项, 尚未实现 ——
        # 不预置死配置, 实现时随功能一并加入
    },
    "asr": {
        "input_mode": "speech",     # speech=识别音频；platform_subtitle=显式导入的平台字幕
        "backend": "faster_whisper",
        "model": "large-v3-turbo",
        "language": "auto",         # 源语言, auto=自动检测
        "segmentation_mode": "phrase", # phrase=短语时间轴; word=词级时间轴发送给 LLM
        "pause_threshold_seconds": 0.6,  # 词间静音超过此值, 形成转录短语锚点
        "max_segment_seconds": 2.0,      # 仅整理识别结果的短语锚点; 不切 Whisper 识别上下文
    },
    "split": {
        "use_llm": True,             # 启用 LLM 语义整理
        "llm_profile": "",           # 空=跟随全局降级链；填写配置组名称可固定该组
        # 模型上下文硬限；短稿可全文提交，长稿按核心词数分块并为完整输出预留空间。
        # 默认使用 0.2M，界面允许按服务商能力调到最高 1M。
        "llm_context_tokens": 200_000,
        "llm_chunk_target_words": 500,
        "llm_chunk_overlap_words": 80,
        "llm_chunk_max_retries": 3,
        "thinking_mode": "low",      # 支持的模型请求低思考档位；其他模型使用简短任务引导
        "target_word_count": 10,     # LLM 语义软目标；中文按自然语义词理解，不按字符机械切分
        "max_split_length": 22,      # 仅关闭 LLM 时的本地字符断句设置；保留旧值含义
        "max_segment_seconds": 7.0,  # LLM 单句时长软参考；关闭 LLM 时用于本地时长兜底
    },
    "content_review": {
        "enabled": True,          # 输出前按本地规则检查中英文字，命中整句不展示
        "mute_audio": True,       # 对命中时段应用音频屏蔽
        "audio_mode": "voice_only", # voice_only=保留分离背景；all_audio=完整混音静音
        "blocked_terms": "",      # 补充屏蔽词，每行一个；不修改识别原稿或译文
        "blocked_ranges": "",     # 人工核定时段，每行 开始秒-结束秒:原因，使用源视频时间轴
    },
    "subtitles": {
        "mode": "all",             # all/source/target/bilingual
        "source_first": False,     # 默认译文在上，原文在下
        "min_duration": 1.5,        # 仅利用句间空隙延长显示；0=原始时长
    },
    "translate": {
        "target_language": "简体中文",
        "style_prompt": "",          # 自定义风格提示词 (翻译人设/语气)
        "llm_profile": "",            # 空=跟随全局降级链；填写配置组名称可固定该组
        "thinking_mode": "low",      # 仅本步骤请求低思考档位；follow_profile保留模型参数
        "max_context_tokens": 200000,  # 单次翻译总预算; 界面允许按服务商能力调到 1M
        "max_window_lines": 100,       # token 预算之外的单窗口字幕行数安全上限; 0=仅按 token 分窗
        "custom_terms_path": "",     # 术语表 xlsx (src/dst/备注 三列)
    },
    "tts": {
        "backend": "indextts25",
        "tolerance": 0.4,          # 兼容旧任务标记；当前 UI、超长判断和重试不使用
        "placement": {
            "enabled": True,
            "auto_repair": True,  # 先排布，仍有冲突才有限缩短；保留原声音历史
            "max_shift_seconds": 2.0,  # 每句相对源时间允许的最大前后移动
            "window_lines": 9,         # 整段原速不可行后的局部缩时分析窗口
            "inter_sentence_gap": 0.08,  # 相邻配音间预留的呼吸间隔
        },
        "indextts25": {
            "url": "http://127.0.0.1:7860/",
            "project_dir": "",       # index-tts 项目根 (空=自动查找)
            "lang": "ZH",
            # 双维度参考 (沿用工作台验证过的语义):
            #   preset=自定义参考 / stable=指定句原声 / dynamic=每句原声 / vector=情感向量(仅情感)
            "timbre_mode": "preset",
            "timbre_audio": "",
            "timbre_reference_line_id": 1,  # stable 模式固定使用指定源句，默认第一句
            "emo_mode": "preset",
            "emo_audio": "",
            "emo_reference_line_id": 1,  # 音色与情感可分别选取源句
            "emo_weight": 1.0,
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],  # 喜怒哀惧厌恶低落惊喜平静
            "emo_text": "",         # 文本情感模式；留空按待合成文本理解
            "emo_random": False,    # 向量/文本情感的随机采样
            "duration_factor": 1.0,
            "duration_retry_factors": "0.7, 0.5",  # 配音超出可用时长时逐次缩短；留空关闭
            "dynamic_reference_max_seconds": 5.0,  # dynamic 音色/情感参考上限；模型服务最多读取 15 秒
            "request_timeout_seconds": 600,  # 单次模型请求（含服务排队）上限；不改变已有音频指纹
            # IndexTTS WebUI 高级推理参数 (预设应用与逐句合成共用)
            "do_sample": True,
            "top_p": 0.8,
            "top_k": 30,
            "temperature": 0.8,
            "length_penalty": 0.0,
            "num_beams": 3,
            "repetition_penalty": 10.0,
            "max_mel_tokens": 1500,
            "max_text_tokens_per_segment": 120,
            "source_preset": "",
            "preset_baseline": {},
        },
        "gptsovits": {
            "url": "http://127.0.0.1:9880/",
            "project_dir": "",       # GPT-SoVITS 安装目录（空=自动查找）
            "character": "",
        },
    },
    "assemble": {
        "bgm_volume": 1.0,          # 背景音混入音量
        "voice_volume": 1.0,        # 人声音量，与配音编辑页共用
        "ducking": True,            # 人声段自动压低背景音 (sidechaincompress)
        "loudnorm": True,           # EBU R128 响度归一
        "final_loudness_mode": "off",  # off 保留混音；normalize 保留动态；uniform 统一响度并在必要时控制峰值
        "final_loudness_lufs": -16.0,
        "final_true_peak_db": -1.5,
        "sentence_leveling": False,  # 保守均衡明显句间差异，默认保留原表现
        "sentence_leveling_max_db": 3.0,
        "burn_subtitles": False,    # 烧录字幕 (默认外挂)
        "subtitle_track": "trans",  # source/trans/bilingual，仅烧录时需要
        "subtitle_font": "Microsoft YaHei",
        "subtitle_size": 48,        # 字号、描边与边距按 1080 高画面等比例缩放
        "subtitle_auto_fit": True,  # 长字幕优先缩小字号，再按宽度上限换行。
        "subtitle_shrink_width_percent": 80,  # 超过画面此宽度时尝试缩小字号。
        "subtitle_wrap_width_percent": 90,  # 缩至下限后可用的宽度上限，再长则换行。
        "subtitle_min_size": 32,
        "subtitle_color": "#FFFFFF",
        "subtitle_outline_color": "#000000",
        "subtitle_outline": 2,
        "subtitle_margin": 50,
        "subtitle_source_style_enabled": False,  # 关闭时原文跟随译文样式。
        "subtitle_source_font": "Microsoft YaHei",
        "subtitle_source_size": 40,
        "subtitle_source_min_size": 28,
        "subtitle_source_color": "#FFFFFF",
        "subtitle_source_outline_color": "#000000",
        "subtitle_source_outline": 2,
        "subtitle_background_color": "#000000",
        "subtitle_background_opacity": 0,  # 0 完全透明；不改变旧字幕的无底色效果
        "crf": 18,
        "video_encoder": "software",
        "nvenc_cq": 18,
    },
    "comments": {
        "failure_policy": "skip",  # 单条内容失败继续；鉴权、网络和存储错误仍暂停。
        # 仅作为新评论方案的起点；已确认方案及已保存全流程配方使用自己的快照。
        "overlay": {
            "start": 5.0, "interval": 15.0, "duration": 6.0,
            "position": "top_left", "font_size": 36, "min_font_size": 18, "margin": 60,
            "width_percent": 0,  # 0 自动同 100%，使用扣除边距后的可用宽度；其他值限制占画面宽度。
            "height_percent": 30,  # 软高度；缩至最小字号后仍放不下时允许完整文字向下延伸。
            "show_author": False, "language_mode": "translated", "source_first": False,
            "color": "#FFFFFF", "outline_color": "#000000", "outline": 2,
            "background_color": "#000000", "background_opacity": 50,
            "font": "Microsoft YaHei", "source_style_enabled": False,
            "source_font": "Microsoft YaHei", "source_font_size": 30, "source_min_font_size": 18,
            "source_color": "#FFFFFF", "source_outline_color": "#000000", "source_outline": 2,
        },
    },
    "runner": {
        # 全局线程池大小 (应 ≥ 各步骤并行上限之和的预期峰值)
        "pool_size": 12,
        # 每步骤并行上限: 下载/LLM 类可并行, GPU 类保持 1 保护显存。修改后重启生效。
        "step_concurrency": {
            "download": 2, "preprocess": 1, "transcribe": 1, "split": 2,
            "translate": 2, "subtitles": 2, "dubtask": 2, "tts": 1, "assemble": 1,
        },
        # 资源组互斥: 同组步骤 (即使分属不同项目) 同时只能执行一个, 防止争用
        # 同类资源 (GPU 显存/算力) 导致性能骤降甚至超时。留空表示不参与分组。
        "step_resources": {
            "preprocess": "gpu", "transcribe": "gpu", "tts": "gpu",
        },
        # 各资源组的并行上限; 调大需自行确认显存与算力余量。
        "resource_concurrency": {
            "gpu": 1,
        },
        # 自动运行链内步骤失败后的自动重试次数 (仅链内; 0 = 失败即挂起等人工处理)。
        # 网络/代理类瞬时失败 (如下载) 建议至少 1。
        "step_retries": {
            "download": 1,
        },
        # 自动重试前的等待秒数 (给网络/代理恢复留时间; 等待期间槽位让给其他项目)。
        "retry_delay": 15,
        # 全自动显存协调: 分离/转写前空闲显存低于阈值时自动停止托管配音服务,
        # 配音步骤前自动启动并等待就绪。false = 完全手动管理服务。
        "auto_tts_vram": True,
        # 触发自动停止配音服务的空闲显存阈值 (MiB)。
        "gpu_free_reserve_mb": 4096,
        # 自动启动配音服务后的就绪等待上限 (秒); 模型首载可能较慢。
        "tts_start_timeout": 300,
        # 每个步骤进入 running 后的最大活动运行时间 (秒); 排队和暂停不计时。
        # 设为 0 可关闭该步骤的兜底, 一般只建议在排查问题时临时使用。
        "step_timeout_seconds": {
            "download": 3600, "preprocess": 1800, "transcribe": 3600,
            "split": 1800, "translate": 3600, "subtitles": 600,
            # 长视频配音/双版本编码可能跨越数小时；暂停和排队不计入。
            "dubtask": 600, "tts": 43200, "assemble": 43200,
        },
    },
    "server": {
        "port": 8600,
        # 总工作目录: 所有项目数据 (项目目录/数据库/上传收件箱) 的根。
        # 空 = 默认 <程序目录>/data; 修改后需重启程序生效 (数据库连接启动时建立)。
        # 相对路径相对程序目录。
        "data_root": "",
    },
    "paths": {
        # HuggingFace 模型缓存；空值优先使用 HF_HOME，否则使用当前数据目录/hf。
        "hf_home": "",
    },
}


def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
    """递归合并；历史数值空值回退默认值，保留合法的 0。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if v is None and type(out.get(k)) in (int, float):
            continue
        if isinstance(out.get(k), dict):
            if v is None:
                continue
            key = f"{path}.{k}" if path else str(k)
            if not isinstance(v, dict):
                raise ConfigError(f"配置项必须是对象: {key}")
            out[k] = _deep_merge(out[k], v, key)
        else:
            out[k] = v
    return out


def _read_user_config(cp: Path) -> dict:
    try:
        with cp.open("r", encoding="utf-8") as f:
            user = yaml.safe_load(f)
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, UnicodeError) as e:
        raise ConfigError("config.yaml YAML 格式错误，请检查文件格式与编码") from e
    except OSError as e:
        raise ConfigError("无法读取 config.yaml，请检查文件权限") from e
    if user is None:
        return {}
    if not isinstance(user, dict):
        raise ConfigError("config.yaml 顶层必须是对象")
    return user


_config_file_cache: dict = {}


def load_config() -> dict:
    """读取独立配置快照。以 (路径, mtime, size) 短路缓存「解析+合并」结果:
    轮询路径每秒调用数十次, 命中缓存可免去文件 IO / YAML 解析 / 深合并;
    外部编辑或 update_config 落盘都会改变 stat → 自动失效。返回深拷贝,
    调用方对快照的改动不会污染缓存。"""
    with _CONFIG_LOCK:
        cp = config_path()
        try:
            st = os.stat(cp)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        key = (str(cp), stamp)
        cached = _config_file_cache.get("settings") if _config_file_cache.get("key") == key else None
        if cached is None:
            user = _read_user_config(cp)
            settings = _deep_merge(DEFAULTS, user)
            # 兼容旧字幕设置；显式的新设置始终优先。
            legacy_subtitles = settings["tts"].get("subtitles") or {}
            if (isinstance(legacy_subtitles, dict) and legacy_subtitles.get("min_duration") is not None
                    and "min_duration" not in (user.get("subtitles") or {})):
                settings["subtitles"]["min_duration"] = legacy_subtitles["min_duration"]
            cached = settings
            _config_file_cache["key"] = key
            _config_file_cache["settings"] = cached
        return copy.deepcopy(cached)


def get(key: str, default: Any = None, *, settings: dict | None = None) -> Any:
    """点分路径取配置值, 例: get('tts.indextts25.url')"""
    node: Any = load_config() if settings is None else settings
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_key(key: str, value: Any) -> None:
    """点分路径写配置值并落盘 (保留其他键; 原子写)"""
    update_config({key: value})


def update_config(updates: dict[str, Any]) -> None:
    """整批更新：任一键无效则不落盘，并发保存从最新文件合并后一次替换。"""
    if not updates:
        return
    with _CONFIG_LOCK:
        cp = config_path()
        user = _read_user_config(cp)
        for key in updates:
            if not isinstance(key, str) or not all(key.split(".")):
                raise ConfigError("配置键不能为空，也不能包含空的路径段")
            parts = key.split(".")
            if any(".".join(parts[:i]) in updates for i in range(1, len(parts))):
                raise ConfigError(f"不能同时更新配置项及其子项: {key}")
        for key, value in updates.items():
            node = user
            parts = key.split(".")
            for p in parts[:-1]:
                existing = node.get(p)
                if existing is None:
                    node[p] = {}
                elif not isinstance(existing, dict):
                    raise ConfigError(f"配置路径不是对象: {p}")
                node = node[p]
            node[parts[-1]] = copy.deepcopy(value)
        merged = _deep_merge(DEFAULTS, user)  # 已知配置段必须保持对象结构，完整校验后才能写入。
        if any(key == 'assemble' or key.startswith('assemble.subtitle_') for key in updates):
            # 点分局部更新也要核对合并后的宽度关系；不依赖调用方是否已校验。
            # 校验器内部同时处理样式规则，失败时整个批次保持原文件。
            from dubforge.pipeline.step_params import validate_numeric_updates
            subtitle_values = {f'assemble.{key}': value for key, value in merged['assemble'].items()
                               if key.startswith('subtitle_')}
            # 合并将旧 null 视为跟随默认；本次明确写入的空值仍应报错，不能静默保存。
            section_updates = updates.get('assemble')
            if isinstance(section_updates, dict):
                subtitle_values.update({f'assemble.{key}': value for key, value in section_updates.items()
                                        if key.startswith('subtitle_')})
            subtitle_values.update({key: value for key, value in updates.items()
                                    if key.startswith('assemble.subtitle_')})
            validate_numeric_updates(subtitle_values)
        try:
            data = yaml.safe_dump(user, allow_unicode=True, sort_keys=False).encode("utf-8")
            atomic_write_bytes(cp, data)
        except yaml.YAMLError as e:
            raise ConfigError("配置值无法保存为 YAML，请检查输入") from e
        except OSError as e:
            raise ConfigError("无法保存 config.yaml，请检查文件权限及剩余空间") from e


def projects_dir() -> Path:
    """全部视频项目的数据根: data/projects/"""
    p = data_root() / PROJECTS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_root() / DB_FILE_NAME


def resolve_hf_home() -> Path:
    """HF 模型缓存目录：显式配置 > HF_HOME > 当前数据目录/hf。"""
    explicit = get("paths.hf_home")
    if explicit:
        return Path(explicit)
    env = os.environ.get("HF_HOME")
    if env:
        return Path(env)
    return data_root() / "hf"


def ensure_hf_env() -> Path:
    """把解析结果写入进程环境 (faster-whisper/demucs 等 hf 下载器读取 HF_HOME);
    必须在首次 import 模型库之前调用"""
    p = resolve_hf_home()
    p.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(p)
    return p
