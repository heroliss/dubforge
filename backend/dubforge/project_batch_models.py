"""混合项目批次只保存已确认配方、分组和子任务回执，不保存凭据。"""
import datetime
import uuid

from sqlmodel import Field, SQLModel

from dubforge.release_batch_models import now


class ProjectBatch(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    request_id: str = Field(unique=True, index=True)
    status: str = Field(default='ready', index=True)
    phase: str = '已创建，等待确认开始'
    error: str = ''
    plan: str = '{}'
    groups: str = '[]'
    requests: str = '{}'
    created_at: datetime.datetime = Field(default_factory=now)
    updated_at: datetime.datetime = Field(default_factory=now)
