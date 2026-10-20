"""发布资料独立于制作步骤；确认后冻结，不写项目参数或产物指纹。"""
import datetime
import uuid

from sqlmodel import Field, SQLModel


def now():
    return datetime.datetime.now(datetime.timezone.utc)


class Publication(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    project_uid: str = Field(index=True)
    revision: int = 1
    status: str = Field(default='draft', index=True)
    draft: str = '{}'
    snapshot: str = '{}'
    transfers: str = '{}'
    receipt: str = '{}'
    chapter_sync: str = '{}'  # 章节交付单独持久化，不能改写已确认投稿快照或稿件状态。
    progress: float = 0
    error: str = ''
    archived_at: datetime.datetime | None = Field(default=None, index=True)
    deleted_at: datetime.datetime | None = Field(default=None, index=True)
    created_at: datetime.datetime = Field(default_factory=now)
    updated_at: datetime.datetime = Field(default_factory=now)


class PublishAccount(SQLModel, table=True):
    id: str = Field(primary_key=True)  # B站 MID；重新扫码更新同一账号。
    name: str
    credential: str = ''  # 仅系统保护后的密文，不通过 API 输出。
    revision: int = 1


class PublishCollectionRequest(SQLModel, table=True):
    """创建远端合集的去重凭证；响应丢失或重启后不能盲目重发。"""
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    fingerprint: str = Field(index=True, unique=True)
    title: str
    status: str = 'submitting'
    season_id: int | None = None
    error: str = ''
    created_at: datetime.datetime = Field(default_factory=now)


class PublishChange(SQLModel, table=True):
    """远端稿件修改的冻结预览与回执；不能由页面重新拼接提交参数。"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    account_id: str = Field(index=True)
    bvid: str = Field(index=True)
    kind: str
    publication_id: str = Field(default='', index=True)
    status: str = 'prepared'
    payload: str = '{}'
    preview: str = '{}'
    result: str = '{}'
    error: str = ''
    created_at: datetime.datetime = Field(default_factory=now)


class PublicationUpdate(SQLModel, table=True):
    """原BV更新任务；冻结新成片与后处理范围，保留原发布历史。"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    source_publication_id: str = Field(index=True)
    project_uid: str = Field(index=True)
    account_id: str = Field(index=True)
    bvid: str = Field(index=True)
    status: str = Field(default='prepared', index=True)
    phase: str = '更新方案已就绪，等待确认'
    fingerprint: str = ''
    plan: str = '{}'
    preview: str = '{}'
    execution: str = '{}'
    error: str = ''
    error_code: str = ''
    progress: float = 0
    created_at: datetime.datetime = Field(default_factory=now)
    updated_at: datetime.datetime = Field(default_factory=now)
