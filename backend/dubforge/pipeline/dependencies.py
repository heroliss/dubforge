"""按实际消费的文件解析依赖；顺序只用于展示和执行计划排序。"""
import copy
import hashlib
import datetime
import json
import os
from pathlib import Path

from dubforge import config
from dubforge.domain import crud
from dubforge.errors import DubForgeError, DubbingIncompleteError
from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP


def selected_steps(project):
    selected = crud.project_params(project).get("auto_steps")
    return [k for k in STEP_KEYS if k != "dubtask" and (selected is None or k in selected)]


class Dependencies:
    """一次请求共用一个快照，避免状态轮询重复读配置、数据库及递归检查。"""

    def __init__(self, project, *, assemble_variants=None, settings=None, overrides=None):
        self.project = project
        self.root = crud.project_dir_by_project(project).resolve()
        self.settings = copy.deepcopy(settings) if settings is not None else config.load_config()
        from dubforge.pipeline.assembly_outputs import validate_variants
        self.assemble_variants = validate_variants(assemble_variants) if assemble_variants is not None else None
        self.assembly_versions = None
        self.version = None
        from dubforge.configuration import step_values
        self.overrides = (copy.deepcopy(overrides) if overrides is not None
                          else step_values(project, settings=self.settings))
        self.steps = {s.step_key: s for s in crud.get_steps(project.id)}
        self.arts = crud.latest_artifacts(project.id, None)
        self._fresh = {}
        self._current = {}
        self._voice_lines = None
        self._dubbing_lines = None
        # 配音输入合同的解析结果与逐句任务分开缓存：普通列表预检仍可在
        # 产物缺失时显示依赖状态，但需要制作/恢复时必须知道是 split 还是
        # translate 破坏了逐句对齐，不能把异常伪装成空句列表。
        self._dubbing_alignment = None
        self._inputs = {}
        self._assembly_state = None
        self._paths = {}
        self._production_cache = {}
        self._assembly_records = None
        self._assembly_configuration = None
        # 同一运行快照内逐句依赖经常指向同一份大原声；只按文件版本
        # 复用真实摘要，不缓存“与登记相符”的结论，登记摘要仍逐次比较。
        self._input_digests = {}
        # 同一状态请求的版本核对共用逐句记录和排布；文件身份每次仍实时核对。
        self._take_snapshots = {}
        self._voice_timing_cache = {}
        self._project_params_cache = None

    def read_project_params(self, project=None):
        """仅供读取的请求内参数快照；原 JSON 变化时立即失效，不缓存制作有效性。"""
        project = project if project is not None else self.project
        raw = project.params
        previous = self._project_params_cache
        if previous is None or previous[0] != raw:
            self._project_params_cache = (raw, crud.project_params(project))
        return self._project_params_cache[1]

    def with_assembly_variants(self, variants):
        """复用本次读取快照，仅重算成片组合依赖，不重复读取配置和逐句索引。"""
        from dubforge.pipeline.assembly_outputs import validate_variants
        result = copy.copy(self)
        result.assemble_variants = validate_variants(variants)
        for name in ('_inputs', '_fresh', '_current'):
            values = dict(getattr(self, name))
            values.pop('assemble', None)
            setattr(result, name, values)
        result._assembly_state = None
        return result

    def cfg(self, key, default=None):
        if self.version and key in {'assemble.video_encoder', 'assemble.nvenc_cq'}:
            return self.version['values'].get(key, 'software' if key == 'assemble.video_encoder' else 18)
        if self.version and key in self.version['values']:
            return self.version['values'][key]
        if key in self.overrides:
            return self.overrides[key]
        value = self.settings
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        value = copy.deepcopy(value)
        if isinstance(value, dict):
            for path, item in self.overrides.items():
                if path.startswith(key + "."):
                    node = value
                    parts = path[len(key) + 1:].split(".")
                    for part in parts[:-1]:
                        node = node.setdefault(part, {})
                    node[parts[-1]] = item
        return value

    def with_versions(self, versions):
        result = copy.copy(self)
        result.assembly_versions = versions
        result.version = None
        result._inputs, result._fresh, result._current = {}, {}, {}
        result._assembly_state = None
        return result

    def for_version(self, version):
        result = self.with_versions(None)
        result.version = version
        return result

    def path(self, step, name):
        key = f"{step}/{name}"
        if key in self._paths:
            return self._paths[key]
        art = self.arts.get(key)
        if art is None:
            return None
        path = Path(art.rel_path)
        if not path.is_absolute():
            path = (self.root / path).resolve()
            if not path.is_relative_to(self.root):
                return None
        # 只复用本请求冻结登记中的路径；文件存在、大小和内容核验仍由消费者实时执行。
        self._paths[key] = path
        return path

    def inputs(self, key, *, line=None):
        # 一个请求已固定项目、配置与产物快照；依赖表只解析一次。
        if line is not None:
            return self._resolve_inputs(key, line=line)
        if key not in self._inputs:
            self._inputs[key] = self._resolve_inputs(key)
        return list(self._inputs[key])

    def _resolve_inputs(self, key, *, line=None):
        if key == 'transcribe' and self.cfg('asr.input_mode', 'speech') == 'platform_subtitle':
            return []
        if key == "subtitles" and self.cfg("subtitles.mode", "all") == "source":
            return [item for item in STEP_MAP[key].inputs if item[0] == "split"]
        if key in {"tts", "workbench"}:
            result = self.dubbing_inputs()
            from dubforge.pipeline.line_settings import effective_config
            from dubforge.pipeline.dubbing_edit import synthesis_lines
            source = self.dubbing_lines()
            requested = [line] if line is not None else synthesis_lines(self.project, source, self.root, dependencies=self)
            params = self.read_project_params()
            configs = [effective_config(self.project, item, self.cfg, params=params)
                       for item in requested]
            if not source:
                configs = [self.cfg]
            if self.cfg("tts.backend") == "indextts25" and any(
                get(f"tts.indextts25.{part}_mode") in {"stable", "dynamic"}
                for get in configs for part in ("timbre", "emo")):
                result.append(("preprocess", "vocal.wav", "原声参考（当前音色或情感模式需要）"))
            return result
        if key == "assemble":
            if self.assembly_versions is not None:
                return list(dict.fromkeys(item for version in self.assembly_versions for item in self.for_version(version).inputs(key)))
            if self.version and (self.version.get('update') or self.version.get('timeline')):
                # 所选更新消费不可变目标清单，未采用的工作稿不能阻塞或改变本次任务。
                return []
            from dubforge.pipeline.assembly_outputs import default_variants, dimensions
            variants = self.assemble_variants or default_variants(self.cfg)
            specs = [self.version] if self.version else [dimensions(v) for v in variants]
            result = [("download", "video.mp4", "源视频画面")]
            if any(v['burn_subtitles'] for v in specs):
                if self.version:
                    result.append(('split', 'split.txt', '字幕原文与时间轴'))
                    if self.cfg('assemble.subtitle_track') != 'source':
                        result.append(('translate', 'translate.txt', '字幕译文'))
                else:
                    name = self.cfg("assemble.subtitle_track", "trans") + ".srt"
                    result.append(("subtitles", name, "烧录字幕"))
            from dubforge.pipeline.assembly_versions import requires_dubbing
            if not any(requires_dubbing(v) for v in specs):
                return result  # 原声烧录直接使用源视频音轨，不需要配音、分离或混音。
            result.extend(self.dubbing_inputs())
            from dubforge.pipeline.dubbing_edit import take_name
            lines = self.production_lines()
            result.extend(("tts", take_name(line['id'], line['take']), f"第 {line['id']} 句 · 配音 {line['take']}")
                          for line in lines if line['source'] == 'dub' and line['take'] is not None)
            if any(line['source'] == 'original' for line in lines):
                result.append(('preprocess', 'vocal.wav', '列表中选择原声的句子'))
            if not lines:
                result.append(("tts", "<句id>.wav", "逐句配音（译文就绪后检查每句）"))
            if float(self.cfg("assemble.bgm_volume", 1.0)) > 0:
                result.append(("preprocess", "background.wav", "背景混音（音量设为 0 可省略）"))
            return result
        return list(STEP_MAP[key].inputs)

    @staticmethod
    def dubbing_inputs():
        return [("split", "split.txt", "语义整理后的原文与时间轴"),
                ("translate", "translate.txt", "与原文逐句对应的译文")]

    def upstream(self, step, name):
        """只读任务准备与运行上下文使用同一套产物定位。"""
        path = self.path(step, name)
        if path is None or not path.is_file():
            raise DubForgeError(f"缺少 {name}，请先完成「{STEP_MAP[step].label}」", code="DEPS_NOT_MET")
        return path

    def dubbing_lines(self, *, required=False):
        from dubforge.pipeline.step_dubtask import build_task
        try:
            if self._dubbing_lines is None:
                self._dubbing_lines = build_task(self)["lines"]
            return self._dubbing_lines
        except (DubForgeError, OSError):
            if required:
                raise
            return []

    def dubbing_alignment_error(self):
        """返回逐句输入合同的破坏位置和原始错误；缺失依赖不算格式错误。

        ``dubbing_lines()`` 的宽松模式是为了让依赖列表在 split/translate
        尚未生成时仍能展示 missing 状态。恢复预检需要更细的证据：同样的
        文件可能存在但句数或编号已不再对应，此时应把真正的生产者加入
        重试队列。仅识别文本合同错误，DEPS_NOT_MET 留给依赖计划处理。
        """
        if self._dubbing_alignment is not None:
            return self._dubbing_alignment
        from dubforge.pipeline.text_source import source_lines, translated_lines
        alignment_codes = {"TEXT_ALIGNMENT", "TEXT_EMPTY", "TEXT_TIMELINE"}
        try:
            source = source_lines(self)
        except DubForgeError as exc:
            self._dubbing_alignment = ('split', exc) if exc.code in alignment_codes else None
            return self._dubbing_alignment
        except OSError:
            self._dubbing_alignment = None
            return None
        try:
            translated_lines(self, source)
        except DubForgeError as exc:
            self._dubbing_alignment = ('translate', exc) if exc.code in alignment_codes else None
        except OSError:
            self._dubbing_alignment = None
        else:
            self._dubbing_alignment = None
        return self._dubbing_alignment

    def production_lines(self):
        """一个依赖快照只解析一次逐句选择；不缓存音频是否有效。"""
        from dubforge.pipeline.dubbing_edit import production_lines
        source = self.dubbing_lines()
        identity = (self.project.params, tuple((row['id'], row['text'], row['start'], row['end']) for row in source))
        if self._production_cache.get('identity') != identity:
            self._production_cache.update(identity=identity,
                lines=production_lines(self.project, source, self.root, dependencies=self))
        return self._production_cache['lines']

    def input_view(self, key, *, line=None):
        return [self._input_view(*item) for item in self.inputs(key, line=line)]

    def _input_view(self, producer, name, desc):
        art = self.arts.get(f"{producer}/{name}")
        path = self.path(producer, name)
        step = self.steps.get(producer)
        reason, state, config_blocked = "", "ready", False
        if producer == "preprocess" and name in {"vocal.wav", "background.wav"} and not self.cfg("separate.enabled", True):
            reason = "人声/背景分离已关闭；请启用分离或调整本步设置以省略此输入"
            config_blocked, state = True, "blocked"
        elif producer == "subtitles" and name not in self.subtitle_outputs():
            reason = "字幕导出未选择此类型，请调整导出内容"
            config_blocked, state = True, "blocked"
        elif step and step.status == "running":
            state, reason = "updating", "正在更新" if art else "正在生成"
        elif not art:
            state, reason = "missing", "尚未生成"
        elif not path or not path.is_file():
            state, reason = "deleted", "文件已丢失"
        elif path.stat().st_size != art.size:
            state, reason = "stale", "依赖已过期：文件内容已变化，需更新"
        # 下游只关心它实际消费的文件是否变了。生产者自己的设置变化不会
        # 传播到下游；只有生产者重新生成/替换产物后，文件登记摘要才变化。
        elif (not self.voice_current(name) if producer == 'tts' else not self.artifact_current(producer, name)):
            state, reason = "stale", "依赖已过期：上游产物已变化，需更新"
        return {"step": producer, "name": name, "desc": desc,
                "label": STEP_MAP[producer].label, "ready": state == "ready", "state": state,
                "config_blocked": config_blocked, "reason": reason, "file": str(path) if path else ""}

    def artifact_current(self, producer, name):
        """只核对登记产物本身，不把生产者当前设置变化传递给消费者。"""
        art = self.arts.get(f'{producer}/{name}')
        path = self.path(producer, name)
        if not art or not path or not path.is_file():
            return False
        try:
            stat = path.stat()
            if stat.st_size != art.size:
                return False
            # 文本和小型 JSON 全量核对；大媒体用 mtime/大小避免列表轮询读满磁盘。
            if art.sha256 and stat.st_size <= 1024 * 1024:
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != art.sha256:
                        return False
            elif art.created_at and stat.st_mtime_ns:
                created = art.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.timezone.utc)
                if stat.st_mtime_ns > int(created.timestamp() * 1e9) + 1_000_000:
                    return False
        except OSError:
            return False
        return True

    def freshness_flags(self, key):
        """返回独立的本步设置变化和上游产物变化维度。"""
        step = self.steps.get(key)
        result = dict(settings_changed=False, upstream_changed=False, artifact_missing=False)
        if not step or not step.input_fingerprint:
            return result
        if self.display_fresh(key):
            return result
        try:
            stored = json.loads(step.input_snapshot or '{}')
            if not isinstance(stored, dict) or not isinstance(stored.get('entries'), dict):
                return result
            from dubforge.pipeline.stale_explanation import capture, _safe_previous
            current = capture(self, key, scope=stored.get('scope', 'full'),
                              fingerprint=stored.get('fingerprint') if stored.get('scope') == 'outputs' else None,
                              _current=True)
            for name in set(stored['entries']) | set(current.get('entries', {})):
                old = _safe_previous(name, stored['entries'].get(name))
                new = current.get('entries', {}).get(name)
                if old and new and old.get('value') == new.get('value'):
                    continue
                if name.startswith(('input.', 'required.', 'source.')) or name.startswith('output.'):
                    result['upstream_changed'] = True
                elif old or new:
                    result['settings_changed'] = True
            # ``input.*`` entries contain the last registered artifact digest.
            # A file can change in place without a new Artifact row, so compare
            # the live dependency view as well; otherwise the consumer would
            # show stale while both historical entry maps still look identical.
            for item in self.input_view(key):
                if item['state'] in {'stale', 'deleted', 'missing'}:
                    result['upstream_changed'] = True
            # 当前文件缺失/被截断属于独立的产物完整性问题，同时归入上游维度，
            # 让消费者看到“文件变化”而不是误以为设置变了。
            if not self.fresh(key):
                own = [a for a in self.arts.values() if a.step_key == key]
                result['artifact_missing'] = bool(own) and not crud.step_artifacts_exist(
                    self.project.id, key, arts=self.arts, pdir=self.root)
                if result['artifact_missing']:
                    result['upstream_changed'] = True
        except (DubForgeError, OSError, TypeError, ValueError, KeyError):
            pass
        return result

    def dubbing_summary(self):
        """与正式合成相同的逐句就绪条件，不把缺失配音计为主动选用原声。"""
        from dubforge.pipeline.dubbing_edit import take_name
        lines = self.production_lines()
        required = [line for line in lines if line['source'] == 'dub' and line['take'] is not None]
        pending = [line['id'] for line in required
                   if not self._input_view('tts', take_name(line['id'], line['take']), '')['ready']]
        known = bool(lines) and all(self._input_view(*item)['ready'] for item in self.dubbing_inputs())
        return dict(known=known, total=len(lines), required=len(required), ready=len(required) - len(pending),
                    pending=len(pending), pending_ids=pending,
                    original=sum(line['source'] == 'original' for line in lines),
                    silent=sum(line['source'] == 'dub' and line['take'] is None for line in lines))

    def require_dubbing(self):
        summary = self.dubbing_summary()
        if not summary['known']:
            raise DubbingIncompleteError('无法确认配音完整性，请先完成原文与译文的准备')
        if summary['pending']:
            ids = '、'.join(map(str, summary['pending_ids'][:8]))
            raise DubbingIncompleteError(
                f"配音尚未齐全：已就绪 {summary['ready']}/{summary['required']} 句，"
                f"待补 {summary['pending']} 句（句子编号：{ids}{'等' if summary['pending'] > 8 else ''}）。不会自动改用原声合成。")
        return summary

    def unready(self, key, *, line=None):
        """所有阻塞项；包括缺失、过期、正在更新和设置冲突。"""
        return [item for item in self.input_view(key, line=line) if not item["ready"]]

    def missing(self, key):
        """只有未生成或已丢失的文件才算缺失，旧产物不列入此处。"""
        return [item for item in self.input_view(key) if item["state"] in {"missing", "deleted"}]

    def voice_current(self, name):
        """试配逐句生成的有效音频也能直接用于成片，无需补跑批量步骤。"""
        import re
        match = re.fullmatch(r'(\d+)(?:_v(\d+))?\.wav', name)
        if not match:
            return False
        if self._voice_lines is None:
            self._voice_lines = {l['id']: l for l in self.dubbing_lines()}
        line = self._voice_lines.get(int(match[1]))
        if not line:
            return False
        from dubforge.pipeline.dubbing_edit import playable
        return playable(self.project, line, self.root, int(match[2] or 1), dependencies=self)

    def subtitle_outputs(self):
        mode = self.cfg("subtitles.mode", "all")
        outputs = {"all": ["source.srt", "trans.srt", "bilingual.srt"],
                "source": ["source.srt"], "target": ["trans.srt"],
                "bilingual": ["bilingual.srt"]}.get(mode)
        if outputs is None:
            raise DubForgeError("请选择有效的字幕导出类型", code="CONFIG_INVALID")
        return outputs

    def fresh(self, key):
        if key not in self._fresh:
            # 复用构造时已加载的产物快照与项目目录: 免去每步两条查询的 N+1。
            exists = (key == 'tts' or crud.step_artifacts_exist(
                self.project.id, key, arts=self.arts, pdir=self.root))
            self._fresh[key] = self.current(key) and exists
            # 试合成片段不是正式成片；不能因为 preview_*.mp4 存在就把整步视为最新。
            if key == "assemble" and self._fresh[key]:
                self._fresh[key] = any(
                    not a.name.startswith("preview_")
                    for a in self.arts.values() if a.step_key == "assemble"
                )
        return self._fresh[key]

    def current(self, key):
        """生产者的输入是否仍一致；不以兄弟产物的缺失阻塞本文件。"""
        if key not in self._current:
            step = self.steps.get(key)
            if key == 'tts':
                summary = self.dubbing_summary()
                self._current[key] = summary['known'] and not summary['pending'] and bool(step and step.status not in {'running', 'queued'})
                return self._current[key]
            # 未记录指纹的早期登记/导入产物仍可消费；有指纹的产物严格校验。
            self._current[key] = bool(step and step.status == "done"
                and (not step.input_fingerprint or
                     (self.matches_fingerprint(key)
                      and not self.unready(key))))
        return self._current[key]

    def status(self, key):
        if key == "tts" and self.steps[key].status != 'failed' and self.fresh(key):
            return "done"
        return self.steps[key].status

    def assembly_state(self):
        if self._assembly_state is None:
            from dubforge.pipeline.assembly_outputs import completed_outputs_state
            self._assembly_state = completed_outputs_state(self)
        return self._assembly_state

    def display_fresh(self, key):
        return self.assembly_state()['fresh'] if key == 'assemble' else self.fresh(key)

    def freshness_feedback(self, key):
        """列表与步骤页共用轻量说明；逐句待配音不冒称整步配置失效。"""
        if key == 'assemble':
            state = self.assembly_state()
            return dict(freshness_label=state['label'], freshness_reason=state['reason'])
        if key != 'tts' or self.steps[key].status in {'running', 'queued'} or self.fresh(key):
            return {}
        summary = self.dubbing_summary()
        if not summary['known']:
            return dict(freshness_label='待核对配音输入', freshness_reason='需先准备最新原文和译文，再核对配音。')
        if summary['pending']:
            return dict(freshness_label='仍有待配音句', freshness_reason=f"仍有 {summary['pending']} 句缺少有效的选用音频；只需核对并补齐这些句子。")
        return {}

    def explain(self, key, stored=None):
        """只读比较上次成功输入与当前生效输入，绝不补写历史值。"""
        from dubforge.pipeline.stale_explanation import explain
        return explain(self, key, stored)

    def matches_fingerprint(self, key):
        from dubforge.runner import compute_fingerprint
        step = self.steps.get(key)
        if not step or not step.input_fingerprint:
            return False
        if step.input_fingerprint == compute_fingerprint(self.project, key, dependencies=self):
            return True
        try:
            if step.input_fingerprint == compute_fingerprint(
                    self.project, key, dependencies=self, legacy_execution=True):
                return True
        except (DubForgeError, ValueError, TypeError):
            pass  # 模型执行设置损坏不能阻止核对已经保存的内容。
        if key == 'translate' and self.cfg('translate.custom_terms_path'):
            return False  # 旧指纹只记录路径，无法证明术语文件内容仍匹配。
        if key in {'split', 'translate'} and step.input_fingerprint == compute_fingerprint(
                self.project, key, dependencies=self, legacy_config=True):
            return True
        # 未改输出语义的旧缓存继续可用，避免升级后重新下载或识别。
        compatible = key in {"preprocess", "transcribe", "split", "translate"}
        if compatible:
            for mode in ({}, {'legacy_config': True}, {'legacy_execution': True}):
                try:
                    if step.input_fingerprint == compute_fingerprint(
                            self.project, key, dependencies=self, legacy=True, **mode):
                        return True
                except (DubForgeError, ValueError, TypeError):
                    continue
        return False

    def require(self, key, *, line=None):
        blockers = self.unready(key, line=line)
        if blockers:
            detail = "；".join(f"{i['name']}（{i['desc']}）：{i['reason']}，来自「{i['label']}」"
                              for i in blockers[:8])
            raise DubForgeError(f"上游依赖未就绪：{detail}", code="DEPS_NOT_MET",
                                suggestion="缺失产物需生成，过期产物需更新；可使用「准备依赖并运行」。")
        for producer, name, _ in self.inputs(key, line=line):
            art = self.arts[f"{producer}/{name}"]
            digest = self._input_digest(self.path(producer, name))
            if art.sha256 and digest != art.sha256:
                raise DubForgeError(f"依赖已过期：{name} 内容已变化，请重新运行「{STEP_MAP[producer].label}」",
                                    code="DEPS_NOT_MET")

    @staticmethod
    def _file_version(stat):
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)

    def _input_digest(self, path):
        """摘要只在本次依赖快照内复用；原地改写、替换及读取中变动均须重验。"""
        name = str(path)
        try:
            path_version = self._file_version(path.stat())
            with path.open('rb') as stream:
                # 路径在 stat 与打开之间也可能被原子替换，不能把另一个文件
                # 的摘要登记在旧版本下。读取前后同时核对句柄和当前路径。
                opened = self._file_version(os.fstat(stream.fileno()))
                # Windows 的 path.stat 与 os.fstat 对 ctime 的定义可能不同；
                # 跨接口仅比对其余身份，ctime 各自与同接口的前后值比较。
                if opened[:2] + opened[3:] != path_version[:2] + path_version[3:]:
                    raise OSError('file changed before verification')
                # Windows 路径 ctime 可能只表示创建时间；只读打开句柄取
                # change time 也能识别同大小改写后恢复 mtime 的文件。
                version = (path_version, opened)
                cached = self._input_digests.get(name)
                if cached is not None and cached[0] == version:
                    digest = cached[1]
                else:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if (self._file_version(os.fstat(stream.fileno())) != opened
                        or self._file_version(path.stat()) != path_version):
                    raise OSError('file changed during verification')
        except OSError as exc:
            self._input_digests.pop(name, None)
            raise DubForgeError(f'依赖文件在核验期间发生变化或无法读取：{path.name}，请稍后重试。',
                               code='DEPS_NOT_MET') from exc
        self._input_digests[name] = (version, digest)
        return digest

    def downstream(self, key):
        affected = {key}
        for candidate in STEP_KEYS:
            if any(p in affected for p, _, _ in self.inputs(candidate)):
                affected.add(candidate)
        return [k for k in STEP_KEYS if k in affected and k != key]

    def plan(self, targets, include_dependencies=False):
        if not targets or any(k not in STEP_MAP for k in targets):
            raise DubForgeError("请选择有效的运行步骤", code="CHAIN_INVALID")
        planned = set(targets)
        blockers = []
        requirements = {}
        visited = set()

        def visit(key):
            if key in visited:
                return
            visited.add(key)
            for item in self.unready(key):
                producer = item["step"]
                requirements[(producer, item["name"])] = item
                if item["config_blocked"]:
                    blockers.append({**item, "consumer": key})
                    continue
                if include_dependencies:
                    planned.add(producer)
                if producer in planned:
                    visit(producer)
                else:
                    blockers.append({**item, "consumer": key})

        for key in targets:
            visit(key)
        return {"steps": [k for k in STEP_KEYS if k in planned],
                "added": [k for k in STEP_KEYS if k in planned and k not in targets],
                "requirements": list(requirements.values()),
                "blockers": blockers,
                "missing": [i for i in blockers if i["state"] in {"missing", "deleted"}],
                "stale": [i for i in blockers if i["state"] == "stale"]}


def project_status(project, deps=None):
    deps = deps or Dependencies(project)
    if any(s.status in {"running", "queued"} for s in deps.steps.values()):
        return "processing"
    from dubforge.project_execution import production_completion
    scope = production_completion(deps)
    if scope['failed']:
        return "failed"
    if scope['complete']:
        return "done"
    return "created"
