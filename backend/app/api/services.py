"""主控制台复用网页的服务管理逻辑，不另起进程绕过配音占用保护。"""
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from dubforge import tts_service

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
def services():
    configs = list(tts_service.service_configs().items())
    def describe(item):
        name, cfg = item
        return {"id": name, **tts_service.status(cfg)}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(configs)))) as pool:
        return {"services": list(pool.map(describe, configs))}


@router.post("/{name}/start")
def start(name: str):
    return tts_service.start(tts_service.service_config(name))


@router.post("/{name}/stop")
def stop(name: str):
    return tts_service.stop(tts_service.service_config(name))


@router.get("/{name}/log")
def log(name: str):
    return tts_service.read_log(tts_service.service_config(name))
