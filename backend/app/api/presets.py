"""预设只读入表单或写入预设库；参数应用统一走项目参数保存接口。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field
from dubforge import config, indextts_presets

router = APIRouter(prefix="/api/workbench-presets", tags=["workbench-presets"])


@router.get("")
def list_presets(project_dir: str | None = None):
    return indextts_presets.list_presets(
        config.get("tts.indextts25.project_dir", "") if project_dir is None else project_dir)


class PresetBody(BaseModel):
    name: str
    storage: str = "local"
    project_dir: str | None = None

    def directory(self):
        return config.get("tts.indextts25.project_dir", "") if self.project_dir is None else self.project_dir


@router.post("/load")
def load(body: PresetBody):
    return indextts_presets.load(body.storage, body.name, body.directory())


class SaveBody(PresetBody):
    values: dict = Field(default_factory=dict)
    overwrite: bool = False


@router.post("/save")
def save(body: SaveBody):
    return indextts_presets.save(body.storage, body.name, body.values,
                               project_dir=body.directory(), overwrite=body.overwrite)
