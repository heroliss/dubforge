"""统一设置范围；值、继承关系和修订号一起持久化。"""
from sqlmodel import Field, SQLModel


class ConfigurationScope(SQLModel, table=True):
    key: str = Field(primary_key=True)  # global / series:<id> / project:<uid>
    name: str = ''
    parent: str = ''  # 项目可绑定一个 series 范围；系列固定继承全局。
    values: str = '{}'
    revision: int = 0
    archived: bool = False
