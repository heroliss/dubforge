# -*- coding: utf-8 -*-
"""数据库初始化与连接 (SQLite; 启动时建表 + 目录自举)"""
import sqlmodel

from dubforge import config
from dubforge.domain.models import Artifact, Job, Project, StepRun  # noqa: F401 (注册表结构)

_engine = None


def get_engine():
    """惰性单例 engine; DATA_ROOT 可被环境变量重定向 (测试夹具)"""
    global _engine
    if _engine is None:
        config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _engine = sqlmodel.create_engine(
            f"sqlite:///{config.db_path()}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    sqlmodel.SQLModel.metadata.create_all(get_engine())


def reset_engine() -> None:
    """测试辅助: 换 DATA_ROOT 后重置单例"""
    global _engine
    _engine = None
