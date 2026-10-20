"""准备 README 截图专用的隔离演示数据；默认只显示帮助，不启动服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / "data" / "diagnostics" / "readme-demo" / "runs"
MARKER = "dubforge.readme-demo.v1"
SECONDS = 16
LINES = [
    (0.5, 2.5, "Welcome to our color studio.", "欢迎来到色彩工作室。"),
    (2.5, 5.0, "A blue square moves across the canvas.", "蓝色方块缓缓穿过画布。"),
    (5.0, 7.5, "Each shape follows a simple rhythm.", "每个图形都按自己的节奏移动。"),
    (7.5, 10.0, "We check the words and timing together.", "我们一起校对文字和时间轴。"),
    (10.0, 12.5, "A short pause leaves room to breathe.", "短暂的停顿让画面更从容。"),
    (12.5, 15.5, "Keep the original and try another version.", "保留原声，也可以尝试另一个版本。"),
]
VERSIONS = [
    dict(id="original", name="原声双语版", dubbing=False, burn_subtitles=True,
         include_comments=False, overrides={}),
    dict(id="dubbed", name="中文配音版", dubbing=True, burn_subtitles=True,
         include_comments=False, overrides={"assemble.voice_volume": 0.9}),
]


def checked_folder(value: str | Path, *, existing: bool = False) -> Path:
    """只接受固定演示根下的直接子目录，拒绝复用和目录逃逸。"""
    folder = Path(value).resolve()
    allowed = RUNS.resolve()
    if folder.parent != allowed:
        raise ValueError(f"运行目录必须是 {allowed} 下的新直接子目录")
    if existing:
        if not folder.is_dir():
            raise ValueError("演示运行目录不存在")
    elif folder.exists():
        raise ValueError("演示运行目录已存在，禁止覆盖或复用；请使用新名称")
    return folder


def atomic_bytes(path: Path, content: bytes) -> None:
    """先独占写同目录临时文件；失败时保留原目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".demo-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def isolated_environment(folder: Path) -> None:
    """必须在导入任何产品模块之前调用，不读取用户配置或模型目录。"""
    if any(name == "dubforge" or name.startswith("dubforge.") for name in sys.modules):
        raise RuntimeError("产品模块已导入，拒绝在当前进程中切换数据目录")
    os.environ.pop("DUBFORGE_DEBUG_DATADIR", None)
    os.environ["DUBFORGE_DATA_ROOT"] = str(folder / "fixture-data")
    os.environ["DUBFORGE_CONFIG"] = str(folder / "fixture-config.yaml")
    os.environ["HF_HOME"] = str(folder / "empty-model-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO / "backend"))


def demo_config(folder: Path) -> dict:
    empty = str(folder / "empty-model-services")
    return {
        "server": {"host": "127.0.0.1", "port": 8611},
        "llm": {"profiles": []},
        "download": {"cookies_path": "", "proxy": ""},
        "paths": {"hf_home": str(folder / "empty-model-cache")},
        "tts": {"backend": "indextts25", "indextts25": {
            "url": "http://127.0.0.1:1", "project_dir": empty,
            "timbre_mode": "preset", "emo_mode": "preset"},
            "gptsovits": {"url": "http://127.0.0.1:1", "project_dir": empty}},
        "content_review": {"enabled": False},
    }


def media_command(ffmpeg: str, temporary: Path) -> list[str]:
    # 全部输入为自写 lavfi 图形或静音；没有文件输入、网络输入或人声。
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-n",
        "-filter_complex_threads", "1", "-f", "lavfi", "-i",
        f"color=c=0x101d35:s=960x540:r=24:d={SECONDS},drawgrid=w=80:h=80:t=1:c=0x263b59@0.6",
        "-f", "lavfi", "-i", f"color=c=0x5da9ed:s=136x136:r=24:d={SECONDS}",
        "-f", "lavfi", "-i", f"color=c=0xf4ad61:s=88x88:r=24:d={SECONDS}",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
        "-filter_complex",
        "[0:v][1:v]overlay=x='120+75*sin(t*0.8)':y='170+50*cos(t*0.6)'[a];"
        "[a][2:v]overlay=x='640+60*cos(t*0.6)':y='220+90*sin(t*0.8)'[visual]",
        "-map", "[visual]", "-map", "3:a", "-t", str(SECONDS), "-shortest",
        "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart",
        "-f", "mp4", str(temporary)]


def make_media(path: Path) -> dict:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("--with-media 需要 PATH 中的 ffmpeg 和 ffprobe；不会安装或下载")
    temporary = path.with_name(path.name + ".tmp")
    if path.exists() or temporary.exists():
        raise ValueError("自绘视频目标或临时文件已存在，拒绝覆盖")
    command = media_command(ffmpeg, temporary)
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("生成自绘视频失败：" + result.stderr[-1500:])
    # 只探测本次独占生成的临时文件；不接受任意输入媒体路径。
    result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(temporary)], capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=True)
    probe = json.loads(result.stdout)
    video = next(row for row in probe["streams"] if row["codec_type"] == "video")
    duration = float(probe["format"]["duration"])
    if not 10 <= duration <= 20 or (video["width"], video["height"]) != (960, 540):
        raise RuntimeError("自绘视频不满足 10–20 秒、960×540 的演示合同")
    os.replace(temporary, path)
    return {"relative_path": "", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size, "probe": probe,
            "origin": "self-authored lavfi geometry and silent AAC; no external input"}


def prepare(folder: Path, *, with_media: bool = False) -> dict:
    folder = checked_folder(folder)
    folder.mkdir(parents=True, exist_ok=False)
    # JSON 是 YAML 子集；专用配置不复制真实 config.yaml 的任何字段。
    write_json(folder / "fixture-config.yaml", demo_config(folder))
    isolated_environment(folder)
    from dubforge import configuration, db
    from dubforge.configuration_models import ConfigurationScope
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline import subtitle_document, output_cuts
    from sqlmodel import Session, select
    from dubforge.domain.models import Job, StepRun
    from dubforge.publishing.models import PublishAccount, Publication

    db.init_db()
    template = configuration.create_template("演示 · 双语视频模板")
    template = configuration.save(template["key"], {
        "revision": template["revision"], "updates": {
            "asr.language": "en", "translate.target_language": "简体中文",
            "assembly.versions": VERSIONS,
            "assembly.target_version_ids": ["original", "dubbed"],
            "assemble.bgm_volume": 0, "assemble.subtitle_track": "bilingual"}})

    def initialize(session, project):
        session.add(ConfigurationScope(key="project:" + project.uid,
            name="本项目", parent=template["key"]))

    projects = [crud.create_project(title, source_kind="local", initialize=initialize)
        for title in ("演示 · 色彩与节奏", "演示 · 形状观察课", "演示 · 双语入门练习")]
    main = projects[0]
    root = crud.project_dir(main.uid)
    source = "\n".join(f"{i}. {start:.3f}-{end:.3f}: {en}"
        for i, (start, end, en, zh) in enumerate(LINES, 1)) + "\n"
    target = "\n".join(f"{i}. {zh}" for i, (_, _, _, zh) in enumerate(LINES, 1)) + "\n"
    for step, name, text in (("split", "split.txt", source), ("translate", "translate.txt", target)):
        path = root / "artifacts" / step / name
        atomic_bytes(path, text.encode("utf-8"))
        crud.add_artifact(main.id, step, "manual-demo-input", name, path,
            str(path.relative_to(root)))
    media = None
    if with_media:
        video = root / "media" / "video.mp4"
        media = make_media(video)
        media["relative_path"] = str(video.relative_to(folder))
        main.source_url = "media/video.mp4"
        crud.save_project(main)
        crud.add_artifact(main.id, "download", "manual-demo-input", "video.mp4", video,
            str(video.relative_to(root)))
        # 原生剪辑草稿只描述待删除区间，不登记任何已合成成片。
        deps = Dependencies(main)
        context = output_cuts.source_context(deps)
        write_json(root / "output_cuts.json", {"schema": 1, "enabled": True,
            "source_fingerprint": context["source_fingerprint"], "ranges": [
                {"id": "demo-pause", "start": 10.5, "end": 11.5,
                 "reason": "演示：精简一秒停顿，尚未合成", "enabled": True}]})
    for project in projects:
        crud.write_manifest(project.uid)
    rows = subtitle_document.read(Dependencies(main), strict=True)
    if len(rows) != len(LINES):
        raise AssertionError("手写双语校对稿未按真实领域读取合同解析")
    # 不把有输入文件冒充步骤完成；所有制作步骤继续待运行。
    with Session(db.get_engine()) as session:
        if session.exec(select(Job)).first() or session.exec(select(PublishAccount)).first() or session.exec(select(Publication)).first():
            raise AssertionError("演示库意外包含任务、账号或投稿记录")
        if any(step.status != "pending" for step in session.exec(select(StepRun)).all()):
            raise AssertionError("演示库不能伪造步骤完成状态")
    fixture = {"schema": MARKER, "synthetic": True, "data_root": "fixture-data",
        "config": "fixture-config.yaml", "port": 8611,
        "template": {"id": template["key"][7:], "name": template["name"]},
        "projects": [{"uid": p.uid, "title": p.title} for p in projects],
        "main_project_uid": main.uid, "media": media, "manual_line_count": len(rows),
        "claims": {"all_steps_pending": True, "model_calls": 0, "tts_calls": 0,
                   "publications": 0, "accounts": 0, "formal_assembly_results": 0}}
    write_json(folder / "fixture.json", fixture)
    screens = [
        ("01-projects", "/", "项目列表、模板与批量入口", "可勾选项目；不点击运行或导入确认。"),
        ("02-overview", f"/projects/{main.uid}", "九个工作阶段与依赖", "全部待运行，文件只作为手写演示输入。"),
        ("03-subtitles", f"/projects/{main.uid}/steps/tts", "六句双语校对稿与原视频播放器", "没有语音产物；不点击试配、生成或服务启动。"),
        ("04-versions", f"/projects/{main.uid}/steps/assemble", "原声与配音两版本、待应用剪辑草稿", "不点击试片、抓帧或正式生成；没有成片回执。"),
        ("05-settings", "/settings?section=production&stage=assemble", "制作设置与继承入口", "无真实连接和账号；不保存或测试连接。"),
    ]
    write_json(folder / "screens.json", {"viewport": {"width": 1180, "height": 900},
        "base_url": "http://127.0.0.1:8611", "screens": [
            dict(name=name, path=path, purpose=purpose, boundary=boundary)
            for name, path, purpose, boundary in screens],
        "disclosure": "隔离演示数据；几何动画及双语文字均为自写，制作和投稿没有执行。"})
    db.reset_engine()
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="显式创建新演示目录、数据库和手写稿")
    parser.add_argument("--run-dir", type=Path, help=f"必须是 {RUNS} 下的新直接子目录")
    parser.add_argument("--with-media", action="store_true", help="显式用 ffmpeg 自绘 16 秒几何动画与静音 AAC")
    args = parser.parse_args()
    if not args.prepare:
        parser.print_help()
        return
    if args.run_dir is None:
        parser.error("--prepare 必须提供 --run-dir；禁止隐式使用真实工作目录")
    fixture = prepare(args.run_dir, with_media=args.with_media)
    print(json.dumps({"prepared": str(args.run_dir.resolve()), "project_count": len(fixture["projects"]),
        "with_media": bool(fixture["media"]), "server_started": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
