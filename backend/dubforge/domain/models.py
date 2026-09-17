# -*- coding: utf-8 -*-
"""
DubForge 领域模型 (SQLModel / SQLite)
=====================================
设计要点:
- Project: 一个视频一个项目, 全部中间产物集中在 data/projects/<uid>/
- StepRun: 管线步骤状态机。九个步骤常驻展示, 每步独立状态, 可单独重跑
- Artifact: 步骤产物登记 (含 sha256), 供下游步骤的输入指纹计算与缓存命中判断
- Job: 任务执行记录 (心跳用于崩溃后判 dead)

状态机约定:
  step:    pending → running → done / failed / stale (下游过期) / interrupted
  重跑某步 = 该步置 pending + 清产物 + 所有下游置 stale
"""
import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    uid: str = Field(index=True, unique=True)          # 目录名: 短随机 id
    title: str = ""                                    # 展示名 (视频标题/文件名)
    source_url: str = ""                               # YouTube 链接等 (本地导入为空)
    source_kind: str = "youtube"                       # youtube | local
    status: str = "created"                            # created | processing | done | failed
    created_at: datetime.datetime = Field(default_factory=_now)
    updated_at: datetime.datetime = Field(default_factory=_now)
    params: str = "{}"                                 # 项目级参数覆盖 (JSON)


class StepRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    step_key: str = Field(index=True)                  # download/preprocess/...
    status: str = "pending"                            # pending/running/done/failed/stale/interrupted
    input_fingerprint: str = ""                        # 输入指纹 (配置+上游产物哈希)
    error_code: str = ""
    error_message: str = ""
    error_suggestion: str = ""
    retry_count: int = 0
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None


class Artifact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    step_key: str = Field(index=True)
    kind: str = ""                                     # video/audio/srt/json/table ...
    name: str = ""                                     # 产物逻辑名 (如 vocal.wav)
    rel_path: str = ""                                 # 相对项目目录路径
    sha256: str = ""
    size: int = 0
    created_at: datetime.datetime = Field(default_factory=_now)


class Job(SQLModel, table=True):
    """步骤执行记录: 一个 StepRun 可有多次 Job (重试)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    step_key: str = Field(index=True)
    status: str = "running"                            # running/done/failed/cancelled/dead
    heartbeat_at: datetime.datetime = Field(default_factory=_now)
    log_path: str = ""                                 # 项目 logs/ 下的日志文件
    started_at: datetime.datetime = Field(default_factory=_now)
    finished_at: Optional[datetime.datetime] = None
    error_detail: str = ""                             # 原始异常文本 (中文消息之外的调试信息)
