"""全流程任务独立于制作产物和发布回执；不保存服务密钥。"""
import datetime
import uuid

from sqlmodel import Field, SQLModel


class WorkflowRun(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    request_id: str = Field(unique=True, index=True)
    project_uid: str = Field(index=True)
    status: str = Field(default='queued', index=True)
    recipe: str = '{}'
    plan: str = '{}'
    cursor: int = 0
    publication_id: str = ''
    phase: str = '等待开始'
    error: str = ''
    next_check: float = 0
    collection_attempts: int = 0
    check_failures: int = 0
    post_processing_paused: bool = False
    execution: str = '{}'
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

