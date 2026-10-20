"""显式启动 README 演示夹具的真实 Vue/只读 API；固定 8611，默认不运行。"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

from prepare_demo import MARKER, REPO, checked_folder, isolated_environment, write_json


def load_fixture(folder: Path) -> dict:
    folder = checked_folder(folder, existing=True)
    data = json.loads((folder / "fixture.json").read_text(encoding="utf-8"))
    if data.get("schema") != MARKER or data.get("synthetic") is not True:
        raise ValueError("不是本准备脚本生成的演示夹具")
    if data.get("data_root") != "fixture-data" or data.get("config") != "fixture-config.yaml":
        raise ValueError("夹具目录合同不匹配，拒绝加载")
    for name in ("fixture-data", "fixture-config.yaml"):
        path = (folder / name).resolve()
        if not path.is_relative_to(folder):
            raise ValueError("夹具目录包含向外的链接，拒绝加载")
    if not (folder / "fixture-data" / "dubforge.db").is_file() or not (folder / "fixture-config.yaml").is_file():
        raise ValueError("演示数据库或专用配置缺失，拒绝隐式创建")
    return data


def serve(folder: Path, max_seconds: int) -> None:
    folder = checked_folder(folder, existing=True)
    fixture = load_fixture(folder)
    if not 30 <= max_seconds <= 900:
        raise ValueError("演示服务自动退出时间必须在 30–900 秒之间")
    if (folder / "runtime.stop").exists():
        raise ValueError("已有 runtime.stop；请保留旧证据并准备新的演示目录")
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("尚无真实 Vue 构建产物；请由主代理在安全窗口统一构建")
    # 先验证端口空闲；不复用、接管或终止任何既有监听。
    with socket.socket() as check:
        check.bind(("127.0.0.1", 8611))
    isolated_environment(folder)
    from dubforge import config, db, tts_service
    if config.data_root().resolve() != (folder / "fixture-data").resolve():
        raise ValueError("数据根未正确隔离")
    settings = config.load_config()
    if settings.get("llm", {}).get("profiles") or settings.get("download", {}).get("cookies_path"):
        raise ValueError("演示配置意外包含模型连接或 cookie 路径")
    # SQLite 只打开新演示库；跳过正常 lifespan 的恢复、扫描和任务监控。
    db.get_engine()
    events, event_lock = [], threading.Lock()

    def record(kind: str, **fields) -> None:
        with event_lock:
            events.append(dict(kind=kind, at=time.time(), **fields))
            write_json(folder / "runtime-events.json", events)

    def forbidden(*_args, **_kwargs):
        record("forbidden_operation_attempt")
        raise RuntimeError("README 演示禁止制作、模型、服务管理、媒体进程和平台请求")

    import subprocess
    from dubforge.backends import llm, tts_indextts25, tts_gptsovits
    from dubforge.publishing import transport
    import httpx
    import urllib.request
    subprocess.Popen = subprocess.run = forbidden
    os.system = forbidden
    llm.chat = transport.call = forbidden
    tts_service.start = tts_service.stop = tts_service.use = forbidden
    tts_indextts25.synth = tts_gptsovits.synth = forbidden
    # 已知 HTTP 出口封锁；ASGI 入站和静态媒体响应不依赖 HTTP 客户端。
    httpx.Client.request = httpx.AsyncClient.request = forbidden
    urllib.request.urlopen = forbidden

    def offline(cfg_get=config.get, **_kwargs):
        backend = cfg_get("tts.backend", "indextts25")
        return dict(backend=backend, url="http://127.0.0.1:1", online=False,
            status="offline", managed=False, can_start=False, can_stop=False,
            pid=None, message="隔离演示未连接配音服务", log_path=None, progress_detail=None)

    tts_service.status = offline
    # prepare 阶段真实探测自绘视频的收据缓存；这里只认该文件，不探测任意路径。
    media = fixture.get("media")
    known_video = None
    if media:
        known_video = (folder / media["relative_path"]).resolve()
        if (not known_video.is_relative_to((folder / "fixture-data").resolve())
                or not known_video.is_file() or known_video.stat().st_size != media["size"]
                or hashlib.sha256(known_video.read_bytes()).hexdigest() != media["sha256"]):
            raise ValueError("自绘视频路径或大小与准备收据不匹配")
    def probe(path):
        if known_video is None or Path(path).resolve() != known_video:
            raise ValueError("演示只接受准备阶段登记的自绘源视频")
        return copy.deepcopy(media["probe"])

    def duration(path, *_args):
        return float(probe(path)["format"]["duration"])

    def recorded_probe(command, **kwargs):
        # 项目总览的固定只读 ffprobe 调用也消费同一真实收据，不启动进程。
        expected = ["ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams"]
        if (isinstance(command, (list, tuple)) and list(command[:-1]) == expected
                and not kwargs.get("shell") and known_video is not None
                and Path(command[-1]).resolve() == known_video):
            return subprocess.CompletedProcess(command, 0,
                json.dumps(probe(known_video)), "")
        return forbidden()

    subprocess.run = recorded_probe
    from dubforge.pipeline import output_cuts, step_assemble
    from fractions import Fraction
    output_cuts._duration = duration
    step_assemble._ffprobe_duration = duration
    output_cuts._frame_rate = lambda path, *_args: float(Fraction(next(
        row for row in probe(path)["streams"] if row["codec_type"] == "video")["r_frame_rate"]))
    from app.main import app
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            record("blocked_write", method=request.method, path=request.url.path)
            return JSONResponse(status_code=403, content={"code": "DEMO_READ_ONLY",
                "message": "隔离演示只读，不执行制作、配置保存或投稿"})
        # 文件选择、环境扫描和任意目录预设不属于文档截图内容。
        if request.url.path.startswith("/api/files") or request.url.path == "/api/settings/environment":
            return JSONResponse(status_code=403, content={"code": "DEMO_SCOPE",
                "message": "隔离演示不访问本机文件选择器、环境扫描或外部预设"})
        if request.url.path == "/api/workbench-presets":
            supplied = request.query_params.get("project_dir")
            if supplied and Path(supplied).resolve() != (folder / "empty-model-services").resolve():
                return JSONResponse(status_code=403, content={"code": "DEMO_SCOPE",
                    "message": "隔离演示只读取自己的空预设目录"})
        return await call_next(request)

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8611,
        lifespan="off", timeout_graceful_shutdown=5, log_level="warning"))
    finished = threading.Event()

    def watch():
        deadline = time.monotonic() + max_seconds
        while not finished.wait(.2):
            if (folder / "runtime.stop").exists() or time.monotonic() >= deadline:
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch, daemon=True, name="readme-demo-stop")
    write_json(folder / "runtime.json", {"pid": os.getpid(), "port": 8611,
        "lifespan": "off", "readonly": True, "max_seconds": max_seconds,
        "metadata_source": "prepare-time ffprobe receipt of self-authored video only"})
    record("start", port=8611)
    watcher.start()
    try:
        asyncio.run(server.serve())
    finally:
        finished.set()
        watcher.join(timeout=1)
        db.reset_engine()
        record("shutdown", watcher_alive=watcher.is_alive())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="显式启动固定 8611 的只读演示服务")
    parser.add_argument("--run-dir", type=Path, help="prepare_demo.py 已准备的隔离目录")
    parser.add_argument("--max-seconds", type=int, default=600, help="自动退出时间，30–900 秒")
    args = parser.parse_args()
    if not args.serve:
        parser.print_help()
        return
    if args.run_dir is None:
        parser.error("--serve 必须提供 --run-dir")
    serve(args.run_dir, args.max_seconds)


if __name__ == "__main__":
    main()
