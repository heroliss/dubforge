# -*- coding: utf-8 -*-
"""步骤参数 schema: 每步骤可在步骤页编辑的配置项 (单一定义处)
================================================================
- path = config.yaml 点分路径; 值实时从 config 读取, 保存走 POST /api/settings
- 各步骤注册表的 cfg_keys 都以整段前缀 (如 "separate") 声明, 新增键自动进
  输入指纹 —— 参数一改, 已完成步骤即标记「已过期」, 重跑按新配置执行
- 前端按 type 渲染控件; 未列入 schema 的 cfg 键在步骤页仍只读展示
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParamField:
    path: str
    label: str
    type: str                      # bool | int | float | str | password | text | select
    help: str = ""
    options: tuple[tuple[str, object], ...] = ()   # select 用: (显示文本, 值)
    min: float | None = None
    max: float | None = None
    step: float | None = None


STEP_PARAM_SCHEMA: dict[str, tuple[ParamField, ...]] = {
    "download": (
        ParamField("download.resolution", "最高分辨率", "select",
                   help="下载视频的最大高度",
                   options=(("360p", 360), ("480p", 480), ("720p", 720),
                            ("1080p", 1080), ("1440p", 1440), ("2160p", 2160))),
        ParamField("download.proxy", "代理", "str",
                   help="例 http://127.0.0.1:7897; 留空 = 读环境变量"),
        ParamField("download.cookies_path", "Cookies 文件", "str",
                   help="YouTube cookies.txt 绝对路径 (年龄限制/会员视频需要)"),
    ),
    "preprocess": (
        ParamField("separate.enabled", "人声/背景分离", "bool",
                   help="关闭后只提取音轨, 不跑 demucs"),
        ParamField("separate.model", "分离模型", "select",
                   help="htdemucs_ft 为精调版, 效果更好但约慢一倍",
                   options=(("htdemucs (标准)", "htdemucs"),
                            ("htdemucs_ft (精调, 更优)", "htdemucs_ft"))),
        ParamField("separate.shifts", "位移平均次数", "int", min=0, max=5, step=1,
                   help="同一音频多次微小位移分离后取平均; 0=最快, 1~2 明显更好且按倍变慢"),
        ParamField("separate.overlap", "分块重叠比例", "float", min=0.05, max=0.5, step=0.05,
                   help="长音频分块处理的重叠比例, 越大接缝越平滑越慢"),
    ),
    "transcribe": (
        ParamField("asr.model", " Whisper 模型", "select",
                   help="更大更准更慢; large-v3-turbo 为速度/精度均衡的默认",
                   options=(("large-v3-turbo", "large-v3-turbo"), ("large-v3", "large-v3"),
                            ("large-v2", "large-v2"), ("medium", "medium"),
                            ("small", "small"), ("base", "base"))),
        ParamField("asr.language", "源语言", "str",
                   help="auto=自动检测; 也可填 zh / en / ja / ko 等加速并更稳"),
    ),
    "split": (
        ParamField("split.max_split_length", "粗切块词数上限", "int", min=8, max=80, step=1,
                   help="NLP 粗切每块的最大词数, 之后由 LLM 语义精切"),
        ParamField("split.use_llm", "LLM 语义切句", "bool",
                   help="关闭则只用 NLP 粗切 (更快, 断句略糙)"),
    ),
    "translate": (
        ParamField("translate.target_language", "目标语言", "str",
                   help="例 简体中文 / 繁體中文 / English"),
        ParamField("translate.style_prompt", "风格提示词", "text",
                   help="翻译人设/语气要求, 例: 口语化, 忌翻译腔"),
        ParamField("translate.whole_doc_max_lines", "整篇直译行数上限", "int", min=20, max=200, step=5,
                   help="≤该行数整篇直译 (上下文最全); 超过走分块+审校"),
        ParamField("translate.global_polish", "全局审校", "bool",
                   help="分块模式翻译完成后整体润色一遍"),
        ParamField("translate.summary_length", "摘要取样字符数", "int", min=2000, max=20000, step=1000,
                   help="长文滚动摘要时每次带给 LLM 的上文长度"),
        ParamField("translate.custom_terms_path", "术语表路径", "str",
                   help="xlsx 绝对路径 (三列: 原文/译文/备注)"),
    ),
    "subtitles": (
        ParamField("tts.subtitles.max_length", "字幕行宽", "int", min=30, max=120, step=5,
                   help="每行最大字符数, 超出自动折行"),
    ),
    "dubtask": (
        ParamField("tts.tolerance", "语速容忍 (秒)", "float", min=0, max=2, step=0.05,
                   help="每句比原声长/短在该秒数内不调速, 减少机械感"),
    ),
    "tts": (
        ParamField("tts.backend", "TTS 后端", "select",
                   options=(("IndexTTS2.5", "indextts25"), ("GPT-SoVITS", "gptsovits"))),
        ParamField("tts.indextts25.url", "IndexTTS 地址", "str",
                   help="IndexTTS2.5 WebUI 地址"),
        ParamField("tts.indextts25.timbre_mode", "音色参考", "select",
                   help="preset=自定义参考音频 / stable=视频开头切片 / dynamic=每句原声 / vector=情感向量",
                   options=(("preset 自定义参考", "preset"), ("stable 开头切片", "stable"),
                            ("dynamic 每句原声", "dynamic"), ("vector 情感向量", "vector"))),
        ParamField("tts.indextts25.emo_mode", "情感参考", "select",
                   options=(("preset 自定义参考", "preset"), ("stable 开头切片", "stable"),
                            ("dynamic 每句原声", "dynamic"), ("vector 情感向量", "vector"))),
        ParamField("tts.indextts25.emo_weight", "情感权重", "float", min=0, max=1, step=0.05),
        ParamField("tts.indextts25.duration_factor", "时长因子", "float", min=0.5, max=2, step=0.05,
                   help="参考音频时长对合成语速的影响系数, 1.0=跟随参考"),
        ParamField("tts.indextts25.lang", "语言", "str", help="ZH / EN / AUTO"),
    ),
    "assemble": (
        ParamField("assemble.bgm_volume", "背景音音量", "float", min=0, max=1, step=0.05,
                   help="合成成片时背景音轨的混入音量"),
        ParamField("assemble.ducking", "人声压低背景 (ducking)", "bool",
                   help="说话时自动压低背景音 (sidechaincompress)"),
        ParamField("assemble.loudnorm", "响度归一 (EBU R128)", "bool"),
        ParamField("assemble.burn_subtitles", "烧录字幕", "bool",
                   help="开=字幕直接烧进画面; 关=输出外挂 srt"),
        ParamField("assemble.crf", "画面质量 CRF", "int", min=14, max=28, step=1,
                   help="越小越清晰越大文件; 18 为视觉无损附近"),
    ),
}


def schema_view(values: dict) -> dict:
    """序列化给前端: {step_key: {fields: [...], values: {path: 当前值}}}
    values 由调用方按 path 从 config 提取"""
    return {
        key: {
            "fields": [
                {"path": f.path, "label": f.label, "type": f.type, "help": f.help,
                 "options": [{"label": lb, "value": v} for lb, v in f.options],
                 "min": f.min, "max": f.max, "step": f.step}
                for f in fields
            ],
            "values": {f.path: values.get(f.path) for f in fields},
        }
        for key, fields in STEP_PARAM_SCHEMA.items()
    }
