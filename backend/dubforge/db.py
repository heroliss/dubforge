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
        config.data_root().mkdir(parents=True, exist_ok=True)
        # WAL + 较长 busy 超时: 执行器线程与 API 线程并发读写同一库,
        # 默认 journal 模式下一边写一边读很容易 "database is locked"
        _engine = sqlmodel.create_engine(
            f"sqlite:///{config.db_path()}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        from sqlalchemy import event

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return _engine


def init_db() -> None:
    sqlmodel.SQLModel.metadata.create_all(get_engine())
    _ensure_columns()


def _ensure_columns() -> None:
    """轻量列迁移: create_all 不会给已有表加新列 (Project.dir_name 为语义化目录名加)"""
    with get_engine().connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(project)")}
        if "dir_name" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE project ADD COLUMN dir_name VARCHAR DEFAULT '' NOT NULL")
            conn.commit()
        cols2 = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(steprun)")}
        if "progress" not in cols2:
            conn.exec_driver_sql(
                "ALTER TABLE steprun ADD COLUMN progress FLOAT DEFAULT 0 NOT NULL")
            conn.commit()


def reset_engine() -> None:
    """测试辅助: 换 DATA_ROOT 后重置单例"""
    global _engine
    _engine = None
