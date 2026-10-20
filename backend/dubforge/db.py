"""数据库初始化与连接 (SQLite; 启动时建表 + 目录自举)"""
import threading

import sqlmodel

from dubforge import config
from dubforge.domain.models import (  # noqa: F401 (注册表结构)
    Artifact,
    Job,
    Project,
    StepRun,
)
from dubforge.publishing.models import Publication, PublishAccount, PublishCollectionRequest, PublishChange, PublicationUpdate  # noqa: F401
from dubforge.workflow_models import WorkflowRun  # noqa: F401
from dubforge.release_batch_models import ReleaseBatch  # noqa: F401
from dubforge.project_batch_models import ProjectBatch  # noqa: F401
from dubforge.pipeline.assembly_models import AssemblyResult  # noqa: F401
from dubforge.configuration_models import ConfigurationScope  # noqa: F401
from dubforge.project_import_models import ProjectImport  # noqa: F401

_engine = None
_ENGINE_LOCK = threading.RLock()


def get_engine():
    """惰性单例 engine; DATA_ROOT 可被环境变量重定向 (测试夹具)"""
    with _ENGINE_LOCK:
        return _get_engine()


def _get_engine():
    global _engine
    if _engine is None:
        root = config.data_root().resolve()
        root.mkdir(parents=True, exist_ok=True)
        # WAL + 较长 busy 超时: 执行器线程与 API 线程并发读写同一库,
        # 默认 journal 模式下一边写一边读很容易 "database is locked"
        engine = sqlmodel.create_engine(
            f"sqlite:///{root / config.DB_FILE_NAME}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        # 监听器和目录都准备好后才发布单例，其他线程不会拿到半初始化的引擎。
        config._set_runtime_data_root(root)
        _engine = engine
    return _engine


def init_db() -> None:
    sqlmodel.SQLModel.metadata.create_all(get_engine())
    _ensure_columns()
    from dubforge.configuration_migration import migrate
    migrated = migrate(get_engine())
    from dubforge.configuration_migration import freeze_project_baselines
    fixed = freeze_project_baselines(get_engine())
    if migrated or fixed:
        from dubforge.domain import crud
        if migrated:
            with sqlmodel.Session(get_engine()) as session:
                uids = session.exec(sqlmodel.select(Project.uid)).all()
        else:
            uids = fixed
        for uid in uids:
            crud.write_manifest(uid)


def _ensure_columns() -> None:
    """轻量列迁移: create_all 不会给已有表加新列 (Project.dir_name 为语义化目录名加)"""
    with get_engine().connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(project)")}
        if "dir_name" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE project ADD COLUMN dir_name VARCHAR DEFAULT '' NOT NULL")
            conn.commit()
        if "title_mode" not in cols:
            # 旧记录无法可靠区分自动与手工名称，默认保留；可显式恢复自动命名。
            conn.exec_driver_sql(
                "ALTER TABLE project ADD COLUMN title_mode VARCHAR DEFAULT 'manual' NOT NULL")
            conn.commit()
        if "archived_at" not in cols:
            # 旧项目默认仍在活动列表；归档与制作状态独立。
            conn.exec_driver_sql("ALTER TABLE project ADD COLUMN archived_at DATETIME DEFAULT NULL")
            conn.commit()
        cols2 = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(steprun)")}
        if "progress" not in cols2:
            conn.exec_driver_sql(
                "ALTER TABLE steprun ADD COLUMN progress FLOAT DEFAULT 0 NOT NULL")
            conn.commit()
        if "eta_seconds" not in cols2:
            conn.exec_driver_sql(
                "ALTER TABLE steprun ADD COLUMN eta_seconds FLOAT DEFAULT 0 NOT NULL")
            conn.commit()
        if "input_snapshot" not in cols2:
            conn.exec_driver_sql(
                "ALTER TABLE steprun ADD COLUMN input_snapshot VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()
        artifact_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(artifact)")}
        if "job_id" not in artifact_cols:
            conn.exec_driver_sql("ALTER TABLE artifact ADD COLUMN job_id INTEGER DEFAULT NULL")
            conn.commit()
        if "settings_snapshot_hash" not in artifact_cols:
            conn.exec_driver_sql("ALTER TABLE artifact ADD COLUMN settings_snapshot_hash VARCHAR DEFAULT '' NOT NULL")
            conn.commit()
        scope_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(configurationscope)")}
        if "baseline" not in scope_cols:
            conn.exec_driver_sql("ALTER TABLE configurationscope ADD COLUMN baseline VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()
        job_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job)")}
        if "task_data" not in job_cols:
            conn.exec_driver_sql(
                "ALTER TABLE job ADD COLUMN task_data VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()
        if 'execution' not in job_cols:
            conn.exec_driver_sql("ALTER TABLE job ADD COLUMN execution VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()

        workflow_cols = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(workflowrun)')}
        if 'execution' not in workflow_cols:
            conn.exec_driver_sql("ALTER TABLE workflowrun ADD COLUMN execution VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()
        if 'post_processing_paused' not in workflow_cols:
            conn.exec_driver_sql('ALTER TABLE workflowrun ADD COLUMN post_processing_paused BOOLEAN DEFAULT 0 NOT NULL')
            conn.commit()
        # 旧版停止操作只留下状态和阶段；不能因新增列的默认值恢复发布后处理。
        # 每次启动精确回填也覆盖加列后中断的情况，其他失败或普通中断不推断为用户停止。
        conn.exec_driver_sql(
            'UPDATE workflowrun SET post_processing_paused = 1 '
            'WHERE post_processing_paused = 0 AND '
            '((status = ? AND phase = ?) OR (status = ? AND phase = ?))',
            ('attention', '已停止自动查询；不会撤回B站稿件',
             'stopping', '正在停止；已发送的投稿将等待回执'))
        conn.commit()
        import_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(projectimport)")}
        if "created_at" not in import_cols:
            conn.exec_driver_sql("ALTER TABLE projectimport ADD COLUMN created_at VARCHAR DEFAULT '' NOT NULL")
            conn.commit()
        publication_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(publication)")}
        if 'chapter_sync' not in publication_cols:
            # 章节交付独立于投稿回执，避免章节异常被误判成需要重新投稿。
            conn.exec_driver_sql("ALTER TABLE publication ADD COLUMN chapter_sync VARCHAR DEFAULT '{}' NOT NULL")
            conn.commit()
        for name in ("archived_at", "deleted_at"):
            if name not in publication_cols:
                # 本地管理状态独立于投稿状态，旧回执与账号身份完整保留。
                conn.exec_driver_sql(f"ALTER TABLE publication ADD COLUMN {name} DATETIME DEFAULT NULL")
                conn.commit()


def reset_engine() -> None:
    """测试辅助: 换 DATA_ROOT 后重置单例"""
    global _engine
    with _ENGINE_LOCK:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        config._set_runtime_data_root(None)
