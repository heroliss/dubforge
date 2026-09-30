"""成片版本定义与最后一次成功产物；不与分P序号绑定。"""
from sqlmodel import SQLModel, Field


class AssemblyResult(SQLModel, table=True):
    key: str = Field(primary_key=True)  # 项目 uid:版本 id。
    project_uid: str = Field(index=True)
    version_id: str
    artifact_id: int
    artifact_hash: str
    fingerprint: str
    snapshot: str = '{}'
