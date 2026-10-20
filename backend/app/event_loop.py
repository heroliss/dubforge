"""服务启动使用的事件循环工厂；Windows 长任务使用 IOCP。"""

import asyncio
import logging
import sys


def create_event_loop() -> asyncio.AbstractEventLoop:
    # Uvicorn 的 reload 子进程会在 Windows 改用 Selector；它只有 512 sockets。
    # 显式工厂让普通启动和自动重载都沿用 Windows 原生的 Proactor。
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    logging.getLogger("uvicorn.error").info("后端事件循环：%s", type(loop).__name__)
    return loop
