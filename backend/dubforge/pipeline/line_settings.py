"""句级配音覆盖；与步骤共用字段定义、验证和实际合成配置。"""
import copy
import hashlib
import json

from dubforge.domain import crud
from dubforge.errors import ConfigError
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates

# 服务连接、语言和运行保护属于步骤；逐句只开放声音表现参数。
LINE_PATHS = {f.path for f in STEP_PARAM_SCHEMA['tts']
              if f.path.startswith('tts.indextts25.')
              and f.path.rsplit('.', 1)[-1] not in {'url', 'project_dir', 'lang', 'request_timeout_seconds'}}


def identity(line):
    """文本或时间轴改变后，不把旧句的设置悄悄套给新句。"""
    content = {k: line[k] for k in ('id', 'text', 'start', 'end')}
    if 'voice_timing' in line:
        content['start'], content['end'] = line['voice_timing']
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def overrides(project, line):
    saved = crud.project_params(project).get('tts_lines', {}).get(str(line['id']), {})
    if saved.get('identity') != identity(line):
        return {}
    return {k: v for k, v in saved.get('settings', {}).items() if k in LINE_PATHS}


def effective_config(project, line, base):
    values = overrides(project, line)
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


def save(project, line, updates, reset, base):
    if not isinstance(updates, dict) or not isinstance(reset, list):
        raise ConfigError('句子参数格式无效')
    if (any(k not in LINE_PATHS for k in updates)
            or any(not isinstance(k, str) or k not in LINE_PATHS for k in reset)):
        raise ConfigError('本句只能调整音色、情感及合成表现参数；服务与语言请在步骤设置中调整')
    validate_numeric_updates(updates)
    values = overrides(project, line)
    values.update(updates)
    for key in reset:
        values.pop(key, None)
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
