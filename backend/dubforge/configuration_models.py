"""统一设置范围；值、继承关系和修订号一起持久化。"""
from sqlmodel import Field, SQLModel


class ConfigurationScope(SQLModel, table=True):
    key: str = Field(primary_key=True)  # global / series:<id> / project:<uid>
    name: str = ''
    parent: str = ''  # 项目基础值的应用来源；系列仍继承全局，项目只共享其运行连接。
    values: str = '{}'
    baseline: str = '{}'  # 项目创建或主动应用来源时的基础值；人工覆盖仍放在 values。
    revision: int = 0
    archived: bool = False
