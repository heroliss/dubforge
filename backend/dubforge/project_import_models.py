"""批量创建回执持久化；与项目在同一事务登记，断线重试不重复创建。"""
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class ProjectImport(SQLModel, table=True):
    id: str = Field(primary_key=True)
    input_hash: str
    plan: str
    results: str = '{}'
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
