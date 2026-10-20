"""顺序发布批次保存确认范围和执行回执，不保存凭据。"""
import datetime
import uuid

from sqlmodel import Field, SQLModel


def now():
    return datetime.datetime.now(datetime.timezone.utc)


class ReleaseBatch(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    request_id: str = Field(unique=True, index=True)
    status: str = Field(default='ready', index=True)
    scope: str = '{}'
    plan: str = '{}'
    items: str = '[]'
    cursor: int = 0
    phase: str = '已创建，等待确认开始'
    error: str = ''
    requests: str = '{}'
    order_started: bool = False
    created_at: datetime.datetime = Field(default_factory=now)
    updated_at: datetime.datetime = Field(default_factory=now)
