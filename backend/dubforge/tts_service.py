"""本机 TTS 启停与日志：只管理本程序启动并核验身份的服务进程。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from urllib.parse import urlsplit

import psutil

from dubforge import config
from dubforge.backends import tts_gptsovits, tts_indextts25
from dubforge.errors import DubForgeError, ServiceStartError, ServiceStopError
from dubforge.utils.fs import atomic_write_bytes

_BACKENDS = {"indextts25": tts_indextts25, "gptsovits": tts_gptsovits}
_processes: dict[int, tuple[str, subprocess.Popen, Path]] = {}
_lock = threading.RLock()
_users: dict[tuple[str, str], int] = {}
_render_reservations = {}


class _RestoredProcess:
    """重新打开 DubForge 后仍可查询原进程；psutil 会防止 PID 被复用。"""
    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    def poll(self):
        try:
            return None if self.process.is_running() and self.process.status() != psutil.STATUS_ZOMBIE else 0
        except psutil.NoSuchProcess:
            return 0


def _record_path(port: int) -> Path:
    return config.data_root() / "logs" / f"tts-service-{port}.json"


def _log_path(backend: str, port: int) -> Path:
    return config.data_root() / "logs" / f"{backend}-{port}.log"


def _progress_bridge(process):
    """只判断已核验的本机进程；外部服务是否扩展进度保持未知。"""
    try:
        command = process.process.cmdline() if isinstance(process, _RestoredProcess) else process.args
        if isinstance(command, (list, tuple)):
            return any(Path(str(arg)).name == 'indextts_runner.py' for arg in command)
    except (AttributeError, psutil.Error):
        pass
    return None


def _record_process(backend, port, process):
    live = psutil.Process(process.pid)
    record = {"backend": backend, "pid": live.pid, "created": live.create_time(),
              "command": live.cmdline(), "executable": live.exe()}
    atomic_write_bytes(_record_path(port), json.dumps(record, ensure_ascii=False).encode("utf-8"))


def _managed_process(port):
    """调用方持有锁；持久记录只有进程身份全部匹配时才恢复管理权限。"""
    if port not in _processes:
        try:
            record = json.loads(_record_path(port).read_text(encoding="utf-8"))
            if record["backend"] not in _BACKENDS:
                return None
            live = psutil.Process(record["pid"])
            if (live.create_time() != record["created"] or live.cmdline() != record["command"]
                    or live.exe() != record["executable"]):
                return None
            _processes[port] = (record["backend"], _RestoredProcess(live), _log_path(record["backend"], port))
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            return None
    return _processes.get(port)


def managed_processes():
    """枚举当前仍存活且身份已核验的服务，包含旧项目留下的端口。"""
    ports = set(_processes)
    for path in (config.data_root() / "logs").glob("tts-service-*.json"):
        try:
            ports.add(int(path.stem.removeprefix("tts-service-")))
        except ValueError:
            continue
    with _lock:
        return [(port, item) for port in sorted(ports)
                if (item := _managed_process(port)) and item[1].poll() is None]


def service_configs():
    """控制台枚举全局、项目覆盖及仍运行的服务；同一后端地址只列一次。"""
    from dubforge.domain import crud
    result, seen = {}, set()

    def add(name, overrides):
        def getter(key, default=None):
            return overrides.get(key, config.get(key, default))
        key = _service_key(getter)
        if key not in seen:
            seen.add(key)
            result[name] = getter

    for backend in _BACKENDS:
        add(backend, {"tts.backend": backend})
    for project in crud.list_projects():
        overrides = crud.project_step_params(project)
        backend = overrides.get("tts.backend", config.get("tts.backend", "indextts25"))
        if backend in _BACKENDS:
            add(f"{backend}@{project.uid}", overrides)
    for port, (backend, process, _) in managed_processes():
        try:
            live = process.process if isinstance(process, _RestoredProcess) else psutil.Process(process.pid)
            command = live.cmdline()
            host_flag = "--host" if backend == "indextts25" else "-a"
            host = command[command.index(host_flag) + 1] if host_flag in command else "127.0.0.1"
            if ":" in host:
                host = f"[{host}]"
            add(f"{backend}@port-{port}", {"tts.backend": backend,
                f"tts.{backend}.url": f"http://{host}:{port}",
                f"tts.{backend}.project_dir": live.cwd()})
        except (psutil.Error, ValueError, IndexError):
            continue
    return result


def service_config(name):
    cfg = service_configs().get(name)
    if cfg is None:
        raise ServiceStartError("服务标识不存在，请输入 services 查看当前列表。")
    return cfg


def _service_key(cfg_get):
    backend = cfg_get("tts.backend", "indextts25")
    url = cfg_get(f"tts.{backend}.url", "").rstrip("/")
    try:
        host, port = _local_endpoint(url)
        url = f"{host}:{port}"
    except ServiceStartError:
        pass
    return backend, url


def _local_key(key):
    """资源归属识别包含本机HTTPS/前缀URL，但不放宽严格进程停止权限。"""
    value = key[1]
    if not isinstance(value, str):
        return False
    if value.rsplit(':', 1)[0] in {'127.0.0.1', 'localhost', '::1'}:
        return True
    try:
        return urlsplit(value).hostname in {'127.0.0.1', 'localhost', '::1'}
    except ValueError:
        return False


def _render_blocks(cfg_get):
    return bool(_render_reservations) and _local_key(_service_key(cfg_get))


@contextmanager
def use(cfg_get):
    """逐句与批量共用占用计数，避免另一个项目关闭正在合成的服务。"""
    key = _service_key(cfg_get)
    with _lock:
        if _render_reservations and _local_key(key):
            raise DubForgeError('NVIDIA成片编码正在使用本机GPU，请等本次编码结束后再配音', code='TTS_SERVICE_RENDER_BUSY')
        _users[key] = _users.get(key, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _users[key] -= 1
            if not _users[key]:
                del _users[key]


@contextmanager
def reserve_for_render(cfg_get):
    """短锁登记硬件编码预约；不杀外部服务，不跨整个编码持锁。"""
    backend = cfg_get('tts.backend', 'indextts25')
    url = cfg_get(f'tts.{backend}.url', '')
    try:
        _, port = _local_endpoint(url)
    except ServiceStartError:
        # 远端不停止、不阻断其请求；本次本机NVENC仍需防新本机模型启动。
        port = None
    key, token = _service_key(cfg_get), object()
    with _lock:
        if any(count and _local_key(service_key) for service_key, count in _users.items()) or _render_reservations:
            raise DubForgeError('本机配音服务仍在使用中，请待当前请求结束后再进行硬件编码', code='ASSEMBLE_ENCODER_BUSY')
        _render_reservations[key] = token
    try:
        from dubforge import tts_requests
        with _lock:
            if _local_key(key) and (tts_requests.pending(url) or tts_requests.needs_recovery(url)):
                raise DubForgeError('配音服务有未核对的请求，不能自动停止或占用硬件编码资源，请先核对服务状态',
                                    code='ASSEMBLE_ENCODER_BUSY')
            managed = _managed_process(port) if port is not None else None
            if managed and managed[0] != backend:
                raise DubForgeError('本机服务登记归属与当前配音后端不一致，不能自动释放资源', code='ASSEMBLE_ENCODER_BUSY')
            if managed and managed[1].poll() is None and cfg_get('runner.auto_tts_vram', True):
                # stop 再核验PID/归属和_users；不发送模型探测请求。
                stop(cfg_get, probe=False)
        yield
    finally:
        with _lock:
            if _render_reservations.get(key) is token:
                _render_reservations.pop(key, None)


def _local_endpoint(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if port < 1:
            raise ValueError
        return ("127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname), port
    except ValueError as exc:
        raise ServiceStartError("快捷启动仅支持本机 HTTP 服务地址；其他地址请在服务所在机器启动。") from exc


def installation_root(backend: str, project_dir: str) -> Path:
    script = "webui.py" if backend == "indextts25" else "api_v2.py"
    candidates = []
    if project_dir:
        root = Path(project_dir).expanduser()
        candidates = [root if root.is_absolute() else config.REPO_ROOT / root]
    else:
        for base in (config.REPO_ROOT.parent, config.REPO_ROOT):
            if backend == "indextts25":
                candidates.append(base / "index-tts")
            else:
                candidates.extend(sorted(base.glob("GPT-SoVITS*")))
    roots = list(dict.fromkeys(p.resolve() for p in candidates if (p / script).is_file()))
    if len(roots) != 1:
        reason = "找到多个安装目录" if roots else "未找到服务安装目录"
        raise ServiceStartError(f"{reason}，请在配音参数中填写安装目录。")
    return roots[0]


def _installation(backend: str, project_dir: str) -> tuple[Path, Path, str]:
    root = installation_root(backend, project_dir)
    script = "webui.py" if backend == "indextts25" else "api_v2.py"
    runtimes = ([root / "runtime" / "python.exe"] if backend == "gptsovits" else [])
    runtimes += [root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"]
    python = next((p for p in runtimes if p.is_file()), None)
    if python is None:
        raise ServiceStartError("未找到服务自带的 Python 环境，请先完成该服务的安装。")
    return root, python, script


def _launch_settings(cfg_get):
    backend = cfg_get("tts.backend", "indextts25")
    if backend not in _BACKENDS:
        raise ServiceStartError("当前配音后端不支持快捷启动。")
    host, port = _local_endpoint(cfg_get(f"tts.{backend}.url", ""))
    root, python, script = _installation(backend, cfg_get(f"tts.{backend}.project_dir", "") or "")
    args = (["--host", host, "--port", str(port)] if backend == "indextts25"
            else ["-a", host, "-p", str(port)])
    if backend == "indextts25" and cfg_get("tts.indextts25.emo_mode") == "text":
        args.append("--qwen_emo")
    entry = [str(Path(tts_indextts25.__file__).with_name('indextts_runner.py')), script] if backend == 'indextts25' else [script]
    return backend, port, host, root, [str(python), "-u", *entry, *args]


def status(cfg_get=config.get, *, probe=True) -> dict:
    from dubforge import tts_requests
    result = _status(cfg_get, probe=probe)
    with _lock:
        if _render_blocks(cfg_get):
            result.update(can_start=False, render_busy=True,
                          message='NVIDIA成片编码正在使用本机GPU，结束后可按需启动配音服务。')
    if result['backend'] == 'indextts25' and tts_requests.needs_recovery(result['url']):
        result['request_pending'] = True
        result['message'] = tts_requests.RECOVERY_HINT
        if result['online']:
            result['status'] = 'draining'
    return result


def _status(cfg_get=config.get, *, probe=True) -> dict:
    backend = cfg_get("tts.backend", "indextts25")
    url = cfg_get(f"tts.{backend}.url", "")
    online = bool(backend in _BACKENDS and _BACKENDS[backend].is_online(url)) if probe else None
    result = {"backend": backend, "url": url, "online": online,
              "status": "online" if online else "offline" if probe else "unknown",
              "can_start": False, "can_stop": False, "managed": False, "pid": None,
              "message": "", "log_path": None, "progress_detail": None}
    # 先看进程，启动期间即使安装路径被修改，也不能再启动第二份模型。
    try:
        _, port = _local_endpoint(url)
        with _lock:
            launched = _managed_process(port)
        log = _log_path(backend, port)
        if log.is_file():
            result["log_path"] = str(log)
        if launched:
            owner, process, log = launched
            code = process.poll()
            if code is None:
                if owner != backend:
                    result.update(message="此端口由另一配音服务使用，请检查服务地址。")
                    return result
                with _lock:
                    busy = bool(_users.get(_service_key(cfg_get)))
                result.update(status="online" if online else "starting", managed=True,
                              can_stop=not busy, pid=process.pid, log_path=str(log),
                              message="正在配音，请先结束任务再停止服务。" if busy else
                              "由 DubForge 在后台运行；关闭网页不会停止服务。" if online else
                              "服务正在启动，首次加载模型可能需要几分钟；可查看日志或停止启动。")
                if backend == 'indextts25':
                    result['progress_detail'] = _progress_bridge(process)
                return result
            if owner == backend:
                result.update(status="failed", log_path=str(log),
                              message=f"服务进程已退出（退出码 {code}），请查看启动日志。")
        if online:
            result.update(status="online", message="已连接外部启动的服务；请在原启动位置或服务所在机器关闭。")
            return result
        if not probe:
            return result
        _launch_settings(cfg_get)
        result["can_start"] = True
    except (ServiceStartError, OSError) as exc:
        result["message"] = str(exc)
    return result


def start(cfg_get=config.get) -> dict:
    with _lock:
        if _render_blocks(cfg_get):
            raise ServiceStartError('NVIDIA成片编码正在使用本机GPU，请等待编码结束后再启动配音服务。')
    # 已在线直接复用；启动请求返回进程状态，由页面单独轮询就绪状态。
    current = status(cfg_get)
    if current["online"] or current["status"] == "starting":
        return current
    if not current["can_start"]:
        raise ServiceStartError(current["message"])
    backend, port, host, root, command = _launch_settings(cfg_get)
    with _lock:
        if _render_blocks(cfg_get):
            raise ServiceStartError('NVIDIA成片编码正在使用本机GPU，请等待编码结束后再启动配音服务。')
        existing = _managed_process(port)
        if existing and existing[1].poll() is None:
            return {**current, "status": "starting", "can_start": False,
                    "message": "服务正在启动，请稍候。", "log_path": str(existing[2])}
        # 端口被其他程序占用时，不先加载一份昂贵模型再等绑定失败。
        try:
            with socket.create_connection((host, port), timeout=0.3):
                raise ServiceStartError(f"端口 {port} 已被占用，但服务尚未就绪；请稍后检测或检查服务地址。")
        except OSError:
            pass
        log = _log_path(backend, port)
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            env["PATH"] = str(Path(command[0]).parent) + os.pathsep + env.get("PATH", "")
            # 日志持续追加，不是步骤产物；无 shell，路径中的空格不会被解释为命令。
            with log.open("ab") as output:
                output.write("\n--- 启动配音服务 ---\n".encode("utf-8"))
                output.flush()
                process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _processes[port] = (backend, process, log)
            from dubforge import tts_requests
            tts_requests.reset(cfg_get(f'tts.{backend}.url', ''))
            try:
                _record_process(backend, port, process)
            except (OSError, psutil.Error) as exc:
                # 服务已启动，记录失败不能再次启动一份；本次会话仍可停止。
                with log.open("ab") as output:
                    output.write(f"\n无法保存进程管理记录，重启 DubForge 后需手动关闭：{exc}\n".encode("utf-8"))
        except OSError as exc:
            raise ServiceStartError(f"启动服务失败：{exc}", detail={"log_path": str(log)}) from exc
    return {**current, "status": "starting", "can_start": False, "can_stop": True,
            "managed": True, "pid": process.pid,
            "message": "服务正在后台启动，可查看日志或停止启动。", "log_path": str(log)}


def _terminate_tree(process):
    """终止已持有身份的进程树，覆盖 Windows 虚拟环境的 Python 子进程。"""
    try:
        parent = process.process if isinstance(process, _RestoredProcess) else psutil.Process(process.pid)
        targets = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for target in reversed(targets):
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for target in alive:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise ServiceStopError("服务进程尚未退出，请稍后重试。")
    if not isinstance(process, _RestoredProcess):
        process.wait(timeout=1)


def stop(cfg_get=config.get, *, probe=True) -> dict:
    backend = cfg_get("tts.backend", "indextts25")
    try:
        _, port = _local_endpoint(cfg_get(f"tts.{backend}.url", ""))
    except ServiceStartError as exc:
        raise ServiceStopError("远程或非本机直连服务，请在服务所在机器关闭。") from exc
    with _lock:
        launched = _managed_process(port)
        if not launched or launched[0] != backend:
            raise ServiceStopError("此服务不是由 DubForge 管理，请在原启动位置关闭。")
        if _users.get(_service_key(cfg_get)):
            raise ServiceStopError("正在配音，请先结束正在使用此服务的任务，再停止服务。")
        _, process, log = launched
        try:
            if process.poll() is None:
                _terminate_tree(process)
            from dubforge import tts_requests
            tts_requests.reset(cfg_get(f'tts.{backend}.url', ''))
            _record_path(port).unlink(missing_ok=True)
            _processes.pop(port, None)
            with log.open("ab") as output:
                output.write("\n--- 已停止配音服务 ---\n".encode("utf-8"))
        except (OSError, psutil.Error, subprocess.TimeoutExpired) as exc:
            raise ServiceStopError(f"停止服务失败：{exc}") from exc
    result = status(cfg_get, probe=probe)
    if not result["online"]:
        result["message"] = "服务已停止；需要配音时可重新启动。"
    return result


def read_log(cfg_get=config.get) -> dict:
    backend = cfg_get("tts.backend", "indextts25")
    if backend not in _BACKENDS:
        raise ServiceStartError("当前后端没有配音服务日志。")
    _, port = _local_endpoint(cfg_get(f"tts.{backend}.url", ""))
    path = _log_path(backend, port)
    try:
        with path.open("rb") as log:
            size = log.seek(0, 2)
            log.seek(max(0, size - 64 * 1024))
            tail = log.read(64 * 1024)
        return {"path": str(path), "text": tail.decode("utf-8", errors="replace"),
                "truncated": size > 64 * 1024}
    except OSError as exc:
        raise ServiceStartError(f"无法读取服务日志：{exc}") from exc
