"""恢复预检的只读说明；起点和执行队列使用同一份依赖快照。"""
import datetime
import hashlib
from functools import lru_cache

from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP


@lru_cache(maxsize=128)
def _file_digest(path, size, mtime_ns, ctime_ns):
    """只缓存同一文件版本的核验，重查无需反复读取已经核验的大文件。"""
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class RecoveryDependencies(Dependencies):
    """文本完整核验；大媒体仅在登记后被修改时核验，避免预检扫描整部视频。"""

    def file_matches(self, producer, name):
        art = self.arts.get(f'{producer}/{name}')
        path = self.path(producer, name)
        if not art or not path or not path.is_file():
            return False
        try:
            stat = path.stat()
            if stat.st_size != art.size:
                return False
            if not art.sha256:
                return True
            created = art.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            # 文本即使等长替换也会改变消费内容；未修改的大媒体沿用廉价登记检查。
            if stat.st_size > 1024 * 1024 and stat.st_mtime_ns <= int(created.timestamp() * 1e9):
                return True
            expected = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            digest = _file_digest(str(path), *expected)
            now = path.stat()
            return digest == art.sha256 and expected == (now.st_size, now.st_mtime_ns, now.st_ctime_ns)
        except OSError:
            return False

    def _input_view(self, producer, name, desc):
        item = super()._input_view(producer, name, desc)
        if item['ready'] and not self.file_matches(producer, name):
            item.update(ready=False, state='stale', reason='已登记文件的内容发生变化，需重新生成')
        return item

    def fresh(self, key):
        ready = super().fresh(key)
        if ready and key != 'tts':
            ready = all(self.file_matches(art.step_key, art.name)
                        for art in self.arts.values() if art.step_key == key)
        return ready


def fresh(deps, key):
    """版本成片沿用正式版本入口的复用判定，不拿旧聚合指纹代替版本状态。"""
    if key == 'assemble' and deps.assembly_versions is not None:
        from dubforge.pipeline.assembly_versions import is_current, result_artifact
        step = deps.steps.get(key)
        return bool(step and step.status == 'done' and deps.assembly_versions
                    and all(is_current(deps, v) and (not isinstance(deps, RecoveryDependencies)
                        or deps.file_matches('assemble', result_artifact(deps, v).name)) for v in deps.assembly_versions))
    return deps.fresh(key)


def describe(deps, steps, *, cursor=0, previous_steps=None, previous_cursor=0, changed=False):
    """有效产物复用，失败重试，失效更新；不把所有旧文件当成需要重做。"""
    steps = list(steps)
    previous_steps = list(previous_steps or [])
    required = set(steps)
    visited = set()

    def visit(key):
        if key in visited or key not in STEP_MAP:
            return
        visited.add(key)
        for producer, _, _ in deps.inputs(key):
            required.add(producer)
            visit(producer)

    for key in steps:
        visit(key)
    pending = set(steps[cursor:])
    rows = []
    execute = []
    for key in [k for k in STEP_KEYS if k in required]:
        step = deps.steps.get(key)
        # 队列之外只展示本次消费的有效依赖，兄弟文件缺失不要求重跑生产者。
        reused = key not in pending or fresh(deps, key)
        if reused:
            action, reason = 'reuse', '本次所需产物有效，直接复用'
        elif key == 'tts':
            summary = deps.dubbing_summary()
            if summary['known']:
                action = 'create'
                reason = f"复用 {summary['ready']} 句有效配音，补齐 {summary['pending']} 句待配音"
            else:
                action, reason = 'update', '先更新原文与译文，再核对并补齐选用配音'
        elif step and step.status in {'failed', 'interrupted'}:
            action, reason = 'retry', '上次执行未完成，按当前已保存设置重试'
            upstream = [item['label'] for item in deps.unready(key)]
            if upstream:
                reason = '先准备' + '、'.join(dict.fromkeys(upstream)) + '，再重试本步'
        elif step and (step.input_fingerprint or step.status in {'done', 'stale'}):
            action = 'update'
            damaged = isinstance(deps, RecoveryDependencies) and any(
                not deps.file_matches(art.step_key, art.name) for art in deps.arts.values() if art.step_key == key)
            reason = '已生成文件丢失或内容变化，需要重新生成' if damaged else deps.explain(key)['summary']
        else:
            action, reason = 'create', '尚未生成有效产物，补齐本步'
        rows.append(dict(key=key, label=STEP_MAP[key].label, action=action, reason=reason))
        if key in pending and not reused:
            execute.append(key)
    if 'comments' in pending:
        item = dict(key='comments', label='评论准备', action='run', reason='按已保存规则准备评论，并复用仍有效的采集结果')
        pos = next((i for i, row in enumerate(rows) if row['key'] == 'assemble'), len(rows))
        rows.insert(pos, item)
        execute = [k for k in steps[cursor:] if k == 'comments' or k in execute]
    start = execute[0] if execute else None
    previous = previous_steps[previous_cursor] if previous_cursor < len(previous_steps) else None
    label = lambda key: '评论准备' if key == 'comments' else STEP_MAP[key].label if key in STEP_MAP else None
    reason = next((r['reason'] for r in rows if r['key'] == start), '制作产物均有效，继续后续草稿或发布阶段')
    return dict(start_step=start, start_label=label(start), reason=reason,
                previous_step=previous, previous_label=label(previous),
                rows=rows, execute_steps=execute,
                reusable_steps=[r['key'] for r in rows if r['action'] == 'reuse'],
                cursor=cursor, previous_cursor=previous_cursor, changed=changed)
