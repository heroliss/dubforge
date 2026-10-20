"""句级配音覆盖；与步骤共用字段定义、验证和实际合成配置。"""
import copy
import hashlib
import json

from dubforge.domain import crud
from dubforge.errors import ConfigError
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates
from dubforge.pipeline import pronunciation

# 服务连接、语言和运行保护属于步骤；逐句只开放声音表现参数。
LINE_PATHS = {f.path for f in STEP_PARAM_SCHEMA['tts']
              if f.path.startswith('tts.indextts25.')
              and f.path.rsplit('.', 1)[-1] not in {'url', 'project_dir', 'lang', 'request_timeout_seconds'}}
LINE_PATHS.add(pronunciation.PATH)


def project_parameters(project, dependencies=None):
    """批量读取借用本次依赖快照；独立调用仍解析自己的参数，不产生跨请求缓存。"""
    reader = getattr(dependencies, 'read_project_params', None)
    return reader(project) if reader is not None else crud.project_params(project)


def identity(line):
    """文本或时间轴改变后，不把旧句的设置悄悄套给新句。"""
    content = {k: line[k] for k in ('id', 'text', 'start', 'end')}
    if 'voice_timing' in line:
        content['start'], content['end'] = line['voice_timing']
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def overrides(project, line, *, params=None):
    params = crud.project_params(project) if params is None else params
    saved = params.get('tts_lines', {}).get(str(line['id']), {})
    if saved.get('identity') != identity(line):
        return {}
    values = {k: v for k, v in saved.get('settings', {}).items() if k in LINE_PATHS}
    if pronunciation.PATH in values:
        try:
            value = pronunciation.validate(values[pronunciation.PATH], line['text'])
        except pronunciation.PronunciationError:
            # 历史数据类型损坏时回退译文，其余音色精调仍可保留。
            value = ''
        if value:
            values[pronunciation.PATH] = value
        else:
            values.pop(pronunciation.PATH, None)
    return values


def effective_config(project, line, base, *, params=None):
    values = overrides(project, line, params=params)
    def get(key, default=None):
        if key in values:
            return copy.deepcopy(values[key])
        result = base(key, default)
        if isinstance(result, dict):
            result = copy.deepcopy(result)
            for path, value in values.items():
                if path.startswith(key + '.'):
                    node = result
                    parts = path[len(key) + 1:].split('.')
                    for part in parts[:-1]:
                        node = node.setdefault(part, {})
                    node[parts[-1]] = copy.deepcopy(value)
        return result
    return get


def updated_values(project, line, updates, reset):
    """只校验和合并；运行队列可与设置在同一次项目写入中提交。"""
    if not isinstance(updates, dict) or not isinstance(reset, list):
        raise ConfigError('句子参数格式无效')
    if (any(k not in LINE_PATHS for k in updates)
            or any(not isinstance(k, str) or k not in LINE_PATHS for k in reset)):
        raise ConfigError('本句只能调整朗读文本、音色、情感及合成表现参数；服务与语言请在步骤设置中调整')
    validate_numeric_updates(updates)
    updates = copy.deepcopy(updates)
    if pronunciation.PATH in updates:
        updates[pronunciation.PATH] = pronunciation.validate(updates[pronunciation.PATH], line['text'])
    values = overrides(project, line)
    values.update(updates)
    for key in reset:
        values.pop(key, None)
    if not values.get(pronunciation.PATH):
        values.pop(pronunciation.PATH, None)
    return values


def save(project, line, updates, reset, base):
    values = updated_values(project, line, updates, reset)
    params = crud.project_params(project)
    records = params.setdefault('tts_lines', {})
    if values:
        records[str(line['id'])] = {'identity': identity(line), 'settings': values}
    else:
        records.pop(str(line['id']), None)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    crud.write_manifest(project.uid)
    return values
