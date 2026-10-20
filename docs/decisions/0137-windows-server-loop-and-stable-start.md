# 0137 Windows 服务循环与稳定启动

## 状态

已接受

## 背景

Windows 日常启动使用 `uvicorn --reload`。2026-10-12 多次出现 `ValueError: too many file descriptors in select()`， traceback 指向 `asyncio.SelectorEventLoop` 的 `SelectSelector`。此时服务进程可能退出或留下已监听但不能响应的状态，网页表现为无法连接或代理 502。日志还记录了编辑测试、渲染代码时的自动重载，长任务会因此被关闭或中断。

本地安装的 Uvicorn 0.53.0 在 `win32` 且 `use_subprocess=True`（自动重载或多 worker）时选择 `SelectorEventLoop`。该 Windows 循环最多支持 512 个套接字；Python 的原生 `ProactorEventLoop` 使用 IOCP，且支持异步子进程。调小轮询频率能减少连接压力，但不能移除事件循环的硬上限，也不能避免改代码中断任务。

## 决策

1. `app.event_loop:create_event_loop` 作为 Uvicorn 的公开自定义循环工厂：Windows 显式创建 `ProactorEventLoop`，其他平台沿用原生 asyncio 循环。工厂记录实际循环名称，方便日后通过启动日志诊断。Uvicorn 最低版本提高到支持该工厂接口的 0.36；不修改第三方包或 monkey-patch 私有实现。
2. 日常启动默认关闭后端自动重载。编辑后端代码后，由控制台 `restart backend` 载入；运行长任务期间不因文件更新重启后端。Vite 热更新保持原行为。
3. 显式 `-ReloadBackend` 可开启开发调试重载。普通启动和重载、首次启动和控制台后续启动均通过 `Get-DevBackendCommand` 生成同一命令，并传入自定义循环工厂，防止重启后退回 Selector。
4. 不设置人为连接数上限作为主要修复。限流不能处理循环选型错误；本次也不据连接数量推断请求泄漏，后续应分别依据请求、生命周期和连接证据诊断。

## 验证

回归测试通过 Uvicorn 的实际工厂解析，在 Windows 下同时等待 600 个 socket 读取，覆盖普通及 reload 配置。该测试不启动 HTTP 服务、不占用用户端口。PowerShell 回归执行启动分派并拦截进程创建，验证启动与恢复使用相同循环参数、reload 仅在显式开启时加入，且未用连接限制掩盖问题。

## 资料

- [Python 3.13 asyncio 平台支持](https://docs.python.org/3.13/library/asyncio-platforms.html)：Windows Selector 的 512 sockets 上限，以及 Proactor 的子进程支持。
- [Uvicorn 0.36.0 发布说明](https://www.uvicorn.org/release-notes/#0360-september-17-2025)：自定义事件循环工厂支持。
- 本地安装源码：`backend/.venv/Lib/site-packages/uvicorn/loops/asyncio.py` 与 `uvicorn/config.py::get_loop_factory`；事故证据：`data/dev-logs/8601-5273/backend.err.log` 与 `backend-recovery.err.log`（运行日志不入库）。
