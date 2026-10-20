"""恢复预检的只读说明；起点和执行队列使用同一份依赖快照。"""
import datetime
import hashlib
from functools import lru_cache

from dubforge.errors import DubForgeError
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


def _verified_failed_cache(deps, key):
    """下游已验证的版本成品可以证明文字步骤仍可复用。

    LLM 步骤可能在重新请求时被供应商内容审核拒绝；这会把 StepRun 标成
    failed，但不会覆盖原子写入的文本产物。若本次已冻结的所有版本成品仍
    以这些产物为输入且通过版本指纹校验，重新调用模型既没有必要，也可能
    再次触发同一审核规则。这里要求完整的版本成品证据和本步文件哈希，
    不把“文件存在”单独当作成功凭据。
    """
    if key not in {"split", "translate"}:
        return False
    step = deps.steps.get(key)
    if not step or step.status not in {"failed", "interrupted", "stale", "done"}:
        return False
    versions = getattr(deps, "assembly_versions", None) or []
    if not versions:
        return False
    try:
        artifacts = [art for art in deps.arts.values() if art.step_key == key]
        # ``all([])`` 会把没有任何产物登记的失败步骤错误地当作可复用。
        # 恢复缓存必须至少有一个已登记文件，而且登记时要有摘要，不能
        # 退化为“文件存在即可跳过”。
        if not artifacts or any(not art.sha256 for art in artifacts):
            return False
        if not all(deps.file_matches(art.step_key, art.name) for art in artifacts):
            return False
        from dubforge.pipeline import assembly_versions
        return all(assembly_versions.is_current(deps, version)
                   for version in versions)
    except (DubForgeError, OSError, TypeError, ValueError, KeyError, AttributeError):
        return False


def cache_usable(deps, key):
    """返回恢复阶段能安全复用的步骤缓存。

    ``Dependencies.fresh`` 只把正式 ``done`` 当作新鲜；LLM 步骤可能在
    重试时被供应商拒绝而变成 ``failed``，但原子写入的文本和下游成片仍
    可通过指纹、登记文件摘要和版本记录证明有效。所有恢复入口必须使用
    这一统一判定，避免某一处把这种缓存重新排队。
    """
    return fresh(deps, key) or _verified_failed_cache(deps, key)


def describe(deps, steps, *, cursor=0, previous_steps=None, previous_cursor=0, changed=False):
    """有效产物复用，失败重试，失效更新；不把所有旧文件当成需要重做。"""
    steps = list(steps)
    previous_steps = list(previous_steps or [])
    required = set(steps)
    pending = set(steps[cursor:])
    invalidated = set()

    def producers(key):
        """返回步骤实际消费的生产者，并在动态依赖暂时无法解析时保底。

        配音输入合同损坏时 ``Dependencies.inputs('assemble')`` 可能因为无法
        构造逐句生产列表而少报 ``tts``。恢复计划仍需知道成片方案需要配音，
        否则旧流程中只保存了 ``split/comments/assemble`` 会跳过译文和配音。
        静态注册表只作为动态解析失败时的保底，不改变正常的按方案裁剪。
        """
        try:
            result = list(deps.inputs(key))
        except (DubForgeError, OSError, KeyError, TypeError, ValueError):
            result = []
        if not result and key in STEP_MAP:
            result = list(STEP_MAP[key].inputs)
        names = {item[0] for item in result}
        if key == 'assemble':
            from dubforge.pipeline.assembly_outputs import dimensions
            variants = getattr(deps, 'assembly_versions', None) or getattr(deps, 'assemble_variants', None) or []
            if any((item if isinstance(item, dict) else dimensions(item)).get('dubbing')
                   for item in variants):
                names.add('tts')
                names.add('translate')
                names.add('split')
        return names

    # 旧流程只保存了末端步骤；从本次确实要执行或已过期的节点向上递归，
    # 只补齐它们真正需要的生产者。不能把所有历史步骤都当成待执行，
    # 否则一个只需重做成片的恢复会退化为从下载开始的全量重跑。
    queue = []
    for key in steps:
        # 游标后的步骤只是本次待检查；只有产物过期才会使其及其消费者失效。
        # 否则一个已经完成但尚未落盘游标的流程会把所有有效缓存误标为重跑。
        step = deps.steps.get(key)
        cache_valid = cache_usable(deps, key)
        stale = (key in STEP_MAP and
                 (not cache_valid or
                  (step and step.status in {'failed', 'stale', 'interrupted'}
                   and not _verified_failed_cache(deps, key))))
        if stale:
            pending.add(key)
            invalidated.add(key)
            queue.append(key)
    while queue:
        key = queue.pop()
        for producer in producers(key):
            if producer not in STEP_MAP:
                continue
            if producer not in required:
                required.add(producer)
            if not cache_usable(deps, producer) and producer not in pending:
                pending.add(producer)
                invalidated.add(producer)
                queue.append(producer)
    # ``deps.inputs`` 只能看到文件级依赖；逐句配音还要求 split 与
    # translate 的编号/句数严格相同。旧流程可能只保存了 ``tts``，把这
    # 两个生产者误当成有效缓存。恢复时把合同错误定位到真正的生产者，
    # 让它进入执行队列，避免再次从 tts 开始而重复得到 TEXT_ALIGNMENT。
    alignment = deps.dubbing_alignment_error() if 'tts' in required else None
    alignment_key, alignment_error = alignment or (None, None)
    if alignment_key:
        required.add(alignment_key)
        pending.add(alignment_key)
        invalidated.add(alignment_key)

    # 仅对过期节点递归追溯上游；同时将过期上游的影响传播给已经纳入本次
    # 计划的下游消费者。这样 split 重跑会使 translate、tts、assemble 一起
    # 失效，而不会扩大一个只运行 split 的普通手动链。
    changed_pending = True
    while changed_pending:
        changed_pending = False
        for key in list(required):
            if key not in STEP_MAP:
                continue
            if key in pending:
                for producer in producers(key):
                    if producer not in required:
                        required.add(producer)
                        changed_pending = True
                    if producer in STEP_MAP and producer not in pending and not cache_usable(deps, producer):
                        pending.add(producer)
                        invalidated.add(producer)
                        changed_pending = True
            elif any(producer in invalidated for producer in producers(key)):
                pending.add(key)
                invalidated.add(key)
                changed_pending = True
    rows = []
    execute = []
    for key in [k for k in STEP_KEYS if k in required]:
        step = deps.steps.get(key)
        # 队列之外只展示本次消费的有效依赖，兄弟文件缺失不要求重跑生产者。
        key_fresh = key != alignment_key and (
            cache_usable(deps, key))
        # 上游在本次恢复中重建时，即使旧文件的指纹暂时仍相符，下游也
        # 必须按新输入重跑；只有原计划排队但已经独立完成的步骤可复用。
        reused = key not in pending or (key_fresh and key not in invalidated)
        if reused:
            action, reason = 'reuse', '本次所需产物有效，直接复用'
        elif key == 'tts':
            if alignment_error:
                action = 'update'
                reason = f'先更新{STEP_MAP[alignment_key].label}：{alignment_error.message}'
            else:
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
        # 保留上面按依赖递归加入的 producer；旧实现按显式 steps 过滤，
        # 会把恢复所需的 translate/split 再次丢掉。
        if 'comments' not in execute:
            pos = next((i for i, key in enumerate(execute) if key == 'assemble'), len(execute))
            execute.insert(pos, 'comments')
    start = execute[0] if execute else None
    previous = previous_steps[previous_cursor] if previous_cursor < len(previous_steps) else None
    label = lambda key: '评论准备' if key == 'comments' else STEP_MAP[key].label if key in STEP_MAP else None
    reason = next((r['reason'] for r in rows if r['key'] == start), '制作产物均有效，继续后续草稿或发布阶段')
    return dict(start_step=start, start_label=label(start), reason=reason,
                previous_step=previous, previous_label=label(previous),
                rows=rows, execute_steps=execute,
                alignment_step=alignment_key,
                reusable_steps=[r['key'] for r in rows if r['action'] == 'reuse'],
                cursor=cursor, previous_cursor=previous_cursor, changed=changed)
