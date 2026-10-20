"""本次制作的配音意图；参数、历史音频与成片选择各自保留。"""
import copy
import hashlib
import json
import re
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, line_settings


DEFAULT_POLICY = dict(mode='fill', target='current', slot=None,
                      line_settings='preserve', include_manual_sources=False)


def normalize(raw=None):
    """策略属于本次运行，不写入项目制作参数或全局配置。"""
    if raw is None:
        return copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULT_POLICY) | {'repair_timing'}):
        raise DubForgeError('本次配音处理方式格式无效', code='DUB_POLICY_INVALID')
    value = {**DEFAULT_POLICY, **copy.deepcopy(raw)}
    if not isinstance(value['mode'], str) or value['mode'] not in {'fill', 'changed', 'all'}:
        raise DubForgeError('请选择补齐、按当前内容与设置更新或重新生成配音', code='DUB_POLICY_INVALID')
    if not isinstance(value['target'], str) or value['target'] not in {'new', 'current', 'slot'}:
        raise DubForgeError('请选择新配音槽、各句当前槽或指定配音槽', code='DUB_POLICY_INVALID')
    if (not isinstance(value['line_settings'], str) or value['line_settings'] not in {'preserve', 'project'}
            or type(value['include_manual_sources']) is not bool
            or 'repair_timing' in value and type(value['repair_timing']) is not bool):
        raise DubForgeError('逐句精调与原声、静音处理方式无效', code='DUB_POLICY_INVALID')
    if value['target'] == 'slot':
        if type(value['slot']) is not int or not 1 <= value['slot'] <= 10000:
            raise DubForgeError('指定配音槽须为 1–10000 的整数', code='DUB_POLICY_INVALID')
    elif value['slot'] is not None:
        raise DubForgeError('仅指定配音槽时可以填写槽号', code='DUB_POLICY_INVALID')
    # 补齐是一条固定的保护合同，隐藏的重做选项不能改变它的行为。
    return ({**copy.deepcopy(DEFAULT_POLICY), **({'repair_timing': value['repair_timing']}
                                               if 'repair_timing' in value else {})}
            if value['mode'] == 'fill' else value)


def current(raw=None):
    """新建或重新确认运行沿用普通配音设置；旧快照仅在原任务执行时保留。"""
    value = normalize(raw)
    value.pop('repair_timing', None)
    return value


def repair_settings(cfg, policy=None):
    """运行冻结配置是默认来源；明确存在的旧任务快照保留历史语义。"""
    from dubforge.errors import ConfigError
    configured = cfg('tts.placement.auto_repair', True)
    if type(configured) is not bool:
        raise ConfigError('请为自动修复配音时间冲突选择开启或关闭。')
    legacy = isinstance(policy, dict) and 'repair_timing' in policy
    enabled = policy['repair_timing'] if legacy else configured
    return dict(enabled=enabled, configured=configured, legacy=legacy)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def synthesis_project(project, policy):
    """仅本次合成忽略逐句参数；保存的精调和历史版本不删除。"""
    if policy['line_settings'] == 'preserve':
        return project
    params = copy.deepcopy(crud.project_params(project))
    from dubforge.pipeline.pronunciation import preserve_records
    readings = preserve_records(params.get('tts_lines', {}))
    if readings:
        params['tts_lines'] = readings
    else:
        params.pop('tts_lines', None)
    return project.model_copy(update={'params': json.dumps(params, ensure_ascii=False)})


def _slot_inventory(root, lines):
    # 扫描一次目录，长稿不对每句重复 glob；旧分句留下的版本也占用槽号。
    physical = {}
    for path in (root / 'artifacts' / 'tts').glob('*.wav'):
        match = re.fullmatch(r'(\d+)(?:_v(\d+))?\.wav', path.name)
        if match and 1 <= int(match[2] or 1) <= 10000:
            physical.setdefault(int(match[1]), set()).add(int(match[2] or 1))
    slots = {take for values in physical.values() for take in values}
    # 尚未生成的句子在正式制作意图中回填 take=1；这不是用户占用的槽。
    slots.update(line['take'] for line in lines if line.get('explicit') and type(line.get('take')) is int)
    return physical, sorted(slots)


def _previous(project, operation_id, *, exclude_job_id=None):
    if not operation_id:
        return None
    from sqlmodel import Session, select
    from dubforge.db import get_engine
    from dubforge.domain.models import Job
    with Session(get_engine()) as session:
        query = select(Job).where(Job.project_id == project.id, Job.step_key == 'tts').order_by(Job.id.desc())
        for job in session.exec(query):
            if job.id == exclude_job_id:
                continue
            try:
                value = json.loads(job.task_data)
            except (TypeError, ValueError):
                continue
            if value.get('scope') == 'policy' and value.get('operation_id') == operation_id and value.get('prepared'):
                return value
    return None


def _lines(project, deps):
    lines = deps.dubbing_lines(required=True)
    return dubbing_edit.production_lines(project, lines, deps.root, dependencies=deps)


def _input_identity(lines):
    return _digest([line_settings.identity(line) for line in lines])


def _protected(line, policy):
    return (policy['mode'] == 'fill' or not policy['include_manual_sources']) and line['explicit'] and (
        line['source'] == 'original' or line['take'] is None)


def _new_plan(project, deps, policy, lines):
    from dubforge.pipeline.step_tts import line_cache_valid
    physical, slots = _slot_inventory(deps.root, lines)
    next_slot = max(slots, default=0) + 1
    view = synthesis_project(project, policy)
    rows, settings_changed = [], 0
    for line in lines:
        if _protected(line, policy):
            continue
        selected = line['take']
        valid = dubbing_edit.playable(project, line, deps.root, selected, dependencies=deps)
        changed = valid and not line_cache_valid(view, line, deps.root, deps.cfg, dependencies=deps, take=selected)
        settings_changed += bool(changed)
        generate = not valid or policy['mode'] == 'all' or policy['mode'] == 'changed' and changed
        if not generate:
            continue
        # 补齐只修复当前选用槽；它不替换有效音频、不另外创建比较版本。
        if policy['mode'] == 'fill':
            take = selected or 1
        elif policy['target'] == 'slot':
            take = policy['slot']
        elif policy['target'] == 'current' and selected is not None:
            take = selected
        else:
            take = next_slot
        dubbing_edit.take_name(line['id'], take)
        rows.append(dict(id=line['id'], take=take, identity=line_settings.identity(line),
                         status='queued', force=take in physical.get(line['id'], set()),
                         source_before=line['source'], take_before=selected))
    return rows, settings_changed, next_slot, slots


def _plan(project, deps, policy, *, operation_id=None, exclude_job_id=None):
    lines = _lines(project, deps)
    previous = _previous(project, operation_id, exclude_job_id=exclude_job_id)
    try:
        previous_policy = normalize(previous.get('policy')) if previous else None
    except DubForgeError:
        previous_policy = None
    resumed = bool(previous and previous_policy and current(previous_policy) == current(policy)
                   and previous.get('input_identity') == _input_identity(lines))
    reset = bool(previous and not resumed)
    completed = 0
    if resumed:
        from dubforge.pipeline.step_tts import line_cache_valid
        view = synthesis_project(project, policy)
        _, slots = _slot_inventory(deps.root, lines)
        next_slot = previous.get('new_slot') or max(slots, default=0) + 1
        changed = sum(not _protected(line, policy)
            and dubbing_edit.playable(project, line, deps.root, line['take'], dependencies=deps)
            and not line_cache_valid(view, line, deps.root, deps.cfg, dependencies=deps, take=line['take'])
            for line in lines)
        by_id = {line['id']: line for line in lines}
        rows = copy.deepcopy(previous['lines'])
        safe_rows = []
        for row in rows:
            if row.get('kind') == 'repair' and not repair_settings(deps.cfg, policy)['enabled']:
                continue
            line = by_id.get(row['id'])
            if not line or _protected(line, policy):
                continue
            path = dubbing_edit.take_path(deps.root, row['id'], row['take'])
            valid = dubbing_edit.playable(project, line, deps.root, row['take'], dependencies=deps)
            successful = (row.get('status') in {'done', 'skipped'} and valid
                          and row.get('result_version') == dubbing_edit.file_version(path))
            row['resume_completed'] = bool(successful)
            row['status'] = 'skipped' if successful else 'queued'
            completed += successful
            safe_rows.append(row)
        rows = safe_rows
    else:
        rows, changed, next_slot, slots = _new_plan(project, deps, policy, lines)
    inspection = dict(repair_rows=[], unresolved_ids=[], blocked=[], requirements=[])
    if repair_settings(deps.cfg, policy)['enabled']:
        from dubforge.pipeline import timing_repair
        ordinary_ids = [row['id'] for row in rows if row.get('kind') != 'repair']
        inspection = timing_repair.inspect(deps, lines, exclude_ids=ordinary_ids)
        existing = {row['id'] for row in rows}
        rows.extend(row for row in inspection['repair_rows'] if row['id'] not in existing)
    return lines, rows, changed, next_slot, slots, resumed, reset, completed, inspection


def _execution_dependencies(project, deps, policy):
    if policy['line_settings'] == 'project':
        from dubforge.pipeline.dependencies import Dependencies
        return Dependencies(synthesis_project(project, policy), settings=deps.settings, overrides=deps.overrides)
    return deps


def _requirements(deps, lines):
    result = [item for line in lines for item in deps.unready('workbench', line=line)]
    if deps.cfg('tts.backend') == 'indextts25':
        for line in lines:
            get = line_settings.effective_config(deps.project, line, deps.cfg)
            for part, label in [('timbre', '音色'), ('emo', '情感')]:
                if get(f'tts.indextts25.{part}_mode') != 'preset':
                    continue
                reference = get(f'tts.indextts25.{part}_audio') or ''
                try:
                    if Path(reference).is_file() and Path(reference).stat().st_size:
                        continue
                except OSError:
                    pass
                result.append(dict(step='tts', label='字幕与配音', name=reference or f'{label}参考文件',
                    desc=f'{label}参考', state='missing', ready=False, config_blocked=True,
                    reason=f'{label}参考文件不存在，请重新选择', file=reference))
    # 同一缺失原文、原声或参考文件不因句子数重复成几百条提示。
    unique = {}
    for item in result:
        key = (item.get('step'), item.get('name'), item.get('reason'))
        unique.setdefault(key, item)
    return list(unique.values())


def preview(project, raw=None, *, dependencies=None, operation_id=None):
    """只读影响摘要；未准备字幕时，实际范围在配音开始前解析。"""
    from dubforge.pipeline.dependencies import Dependencies
    policy = normalize(raw)
    deps = dependencies or Dependencies(project)
    result = dict(policy=policy, total=0, generate=0, reuse=0, customized=0,
                  original=0, silent=0, settings_changed=0, overwrite=0,
                  confirmed=0, new_slot=None, slots=[], target_label='',
                  deferred=False, resumed=False, resume_reset=False, completed=0, requirements=[],
                  repair=0, repair_ids=[], repair_blocked=[], timing_unresolved_ids=[],
                  repair_settings=repair_settings(deps.cfg, policy))
    try:
        lines, rows, changed, next_slot, slots, resumed, reset, completed, inspection = _plan(
            project, deps, policy, operation_id=operation_id)
    except DubForgeError as exc:
        if exc.code in {'TEXT_ALIGNMENT', 'TEXT_EMPTY', 'TEXT_TIMELINE', 'DEPS_NOT_MET', 'ARTIFACT_MISSING'}:
            result.update(deferred=True, target_label='原文与译文准备好后，在配音开始时确定范围和目标槽')
            return result
        raise
    active = [row for row in rows if not row.get('resume_completed')]
    ordinary = [row for row in active if row.get('kind') != 'repair']
    repairs = [row for row in active if row.get('kind') == 'repair']
    execution_deps = _execution_dependencies(project, deps, policy)
    by_id = {line['id']: line for line in lines}
    requirements = (_requirements(execution_deps, [by_id[row['id']] for row in ordinary])
                    + inspection['requirements'])
    protected = [line for line in lines if _protected(line, policy)]
    overwrite = [row for row in ordinary if dubbing_edit.take_path(deps.root, row['id'], row['take']).is_file()]
    confirmed = _confirmed_count(deps.root, overwrite, project=project, lines=by_id, dependencies=deps)
    target_label = ('沿用各句当前配音槽' if policy['mode'] == 'fill' else
                    f'新配音槽 {next_slot}' if policy['target'] == 'new' else
                    '各句当前配音槽（未选槽的句子写入新槽）' if policy['target'] == 'current' else
                    f"配音槽 {policy['slot']}")
    result.update(total=len(lines), generate=len(ordinary), reuse=len(lines) - len(protected) - len(active),
                  customized=sum(bool(line_settings.overrides(project, line)) for line in lines),
                  original=sum(line['source'] == 'original' for line in protected),
                  silent=sum(line['source'] == 'dub' and line['take'] is None for line in protected),
                  settings_changed=changed, overwrite=len(overwrite), confirmed=confirmed,
                  new_slot=next_slot if any(row['take'] == next_slot for row in rows) else None,
                  slots=slots, target_label=target_label, resumed=resumed, resume_reset=reset,
                  completed=completed, requirements=requirements,
                  repair=len(repairs), repair_ids=[row['id'] for row in repairs],
                  repair_blocked=inspection['blocked'], timing_unresolved_ids=inspection['unresolved_ids'])
    return result


def _confirmed_count(root, rows, *, project, lines, dependencies):
    try:
        path = root / 'artifacts' / 'tts' / 'workbench_confirm.json'
        if not path.is_file():
            path = root / 'workbench_confirm.json'
        records = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    if not isinstance(records, dict):
        return 0
    result = 0
    for row in rows:
        if not dubbing_edit.playable(project, lines[row['id']], root, row['take'], dependencies=dependencies):
            continue
        path = dubbing_edit.take_path(root, row['id'], row['take'])
        saved = records.get(dubbing_edit.confirm_key(row['id'], row['take']), {})
        if not isinstance(saved, dict):
            continue
        try:
            fingerprint = path.with_suffix('.input').read_text(encoding='utf-8')
        except OSError:
            continue
        result += saved.get('fingerprint') == fingerprint and saved.get('version') == dubbing_edit.file_version(path)
    return result


def prepare(project, deps, raw=None, operation_id=None, *, exclude_job_id=None):
    """运行开始时冻结本轮句子和目标槽；恢复同一轮仅补未完成目标。"""
    policy = normalize(raw)
    lines, rows, changed, next_slot, slots, resumed, reset, completed, inspection = _plan(
        project, deps, policy, operation_id=operation_id, exclude_job_id=exclude_job_id)
    by_id = {line['id']: line for line in lines}
    deps = _execution_dependencies(project, deps, policy)
    active = [by_id[row['id']] for row in rows if not row.get('resume_completed') and row.get('kind') != 'repair']
    blocked = _requirements(deps, active) + inspection['requirements']
    if blocked:
        raise DubForgeError('配音输入未就绪：' + '；'.join(dict.fromkeys(item['reason'] for item in blocked)),
                           code='DEPS_NOT_MET', suggestion='请在字幕与配音页修正参考文件或先准备缺失的制作输入。',
                           detail={'requirements': blocked})
    for row in rows:
        if not row.get('resume_completed') and row.get('kind') != 'repair':
            deps.require('workbench', line=by_id[row['id']])
    return dict(scope='policy', prepared=True, policy=policy, operation_id=operation_id,
                repair_settings=repair_settings(deps.cfg, policy),
                input_identity=_input_identity(lines), lines=rows,
                new_slot=next_slot if any(row['take'] == next_slot for row in rows) else None,
                resumed=resumed, resume_reset=reset, completed=completed)


def record_generation_policy(root, line, take, policy, operation_id):
    """版本记录实际选用的参数来源，区分统一项目参数与保留精调的合成。"""
    from dubforge.utils.fs import atomic_write_bytes
    data = dubbing_edit.read_take(root, line, take)
    if not data:
        return
    data.update(line_settings_mode=policy['line_settings'], operation_id=operation_id)
    path = dubbing_edit.take_path(root, line['id'], take).with_suffix('.take.json')
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False).encode())


def apply_recovery(summary, recovery):
    """恢复起点以本轮配音意图为准，完整旧配音不能吞掉主动重做。"""
    if not summary or not isinstance(recovery, dict):
        return recovery
    active = summary.get('generate', 0) > 0 or summary.get('repair', 0) > 0 or (
        summary.get('deferred') and summary.get('policy', {}).get('mode') != 'fill')
    if not active:
        return recovery
    from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
    rows = recovery.setdefault('rows', [])
    row = next((item for item in rows if item.get('key') == 'tts'), None)
    reason = ('原文与译文准备好后，按本次选择确定配音范围和目标槽' if summary.get('deferred') else
              f"本轮生成 {summary['generate']} 句，自动缩时修复 {summary.get('repair', 0)} 句，复用 {summary['reuse']} 句；{summary['target_label']}")
    if row is None:
        row = dict(key='tts', label=STEP_MAP['tts'].label)
        position = next((index for index, item in enumerate(rows)
                         if item.get('key') in {'subtitles', 'comments', 'assemble'}), len(rows))
        rows.insert(position, row)
    row.update(action='update' if summary.get('overwrite') else 'create', reason=reason)
    execute = set(recovery.get('execute_steps', [])) | {'tts'}
    assemble = next((item for item in rows if item.get('key') == 'assemble'), None)
    if assemble is not None:
        assemble.update(action='update', reason='本轮配音生成完成后，按新的选用配音核对并生成成片')
        execute.add('assemble')
    order = [key for key in STEP_KEYS if key != 'dubtask']
    order.insert(order.index('assemble'), 'comments')
    recovery['execute_steps'] = [key for key in order if key in execute]
    recovery['reusable_steps'] = [key for key in recovery.get('reusable_steps', []) if key not in {'tts', 'assemble'}]
    start = recovery['execute_steps'][0] if recovery['execute_steps'] else None
    first = next((item for item in rows if item.get('key') == start), {})
    recovery.update(start_step=start, start_label=first.get('label'), reason=first.get('reason', reason))
    return recovery
