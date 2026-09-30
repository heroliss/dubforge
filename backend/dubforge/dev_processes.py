"""开发控制台退出：结束本次前后端，保留独立的配音服务进程树。"""
import argparse
import json

import psutil

from dubforge import tts_service
from dubforge.errors import ServiceStopError


def stop_development_process(pid: int, created: float) -> dict:
    try:
        root = psutil.Process(pid)
        if abs(root.create_time() - created) > .01:
            raise ServiceStopError("启动进程身份已变化，跳过关闭，避免影响其他程序。")
        protected = set()
        for _, (_, process, _) in tts_service.managed_processes():
            live = process.process if isinstance(process, tts_service._RestoredProcess) else psutil.Process(process.pid)
            try:
                protected.add(live.pid)
                protected.update(child.pid for child in live.children(recursive=True))
            except psutil.NoSuchProcess:
                pass
        targets = [item for item in [root, *root.children(recursive=True)] if item.pid not in protected]
        for item in targets:
            try:
                item.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(targets, timeout=4)
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        if alive:
            raise ServiceStopError("部分开发进程未退出，请检查 status。")
        return {"stopped": len(targets), "preserved": len(protected)}
    except psutil.NoSuchProcess:
        return {"stopped": 0, "preserved": 0}
    except psutil.Error as exc:
        raise ServiceStopError(f"关闭开发进程失败：{exc}") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("created", type=float)
    args = parser.parse_args()
    try:
        print(json.dumps(stop_development_process(args.pid, args.created)))
    except ServiceStopError as exc:
        print(exc.message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
