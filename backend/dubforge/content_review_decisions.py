"""逐条人工复核绑定原始证据；允许误报不会成为项目全局白名单。"""
import copy
import hashlib
import json

from dubforge.content_review import REVIEW_VERSION, blocked_comment
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes


class ContentReviewDecisionError(DubForgeError):
    code = 'CONTENT_REVIEW_DECISION_INVALID'
    suggestion = '请刷新内容审查结果，核对原文、译文和时间后再复核。'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def load(project):
    path = crud.project_dir_by_project(project) / 'content_review_decisions.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return dict(schema=1, revision=0, entries={})
    except (OSError, ValueError) as exc:
        raise ContentReviewDecisionError('人工复核记录无法读取，请检查项目文件',
                                        code='CONTENT_REVIEW_DECISION_STORE') from exc
    if (not isinstance(data, dict) or data.get('schema') != 1
            or type(data.get('revision')) is not int or data['revision'] < 0
            or not isinstance(data.get('entries'), dict)
            or any(not isinstance(key, str) or not isinstance(row, dict)
                   or row.get('decision') not in {'allow', 'block'}
                   or not isinstance(row.get('evidence_fingerprint'), str)
                   for key, row in data['entries'].items())):
        raise ContentReviewDecisionError('人工复核记录格式无效，请检查项目文件',
                                        code='CONTENT_REVIEW_DECISION_STORE')
    return data


def persist(project, data):
    atomic_write_bytes(crud.project_dir_by_project(project) / 'content_review_decisions.json',
                       json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def apply(finding, store):
    result = copy.deepcopy(finding)
    saved = store['entries'].get(result['key'], {})
    valid = saved.get('evidence_fingerprint') == result['evidence_fingerprint']
    result['decision'] = saved['decision'] if valid else 'pending'
    result['active'] = result['decision'] != 'allow'
    if saved and not valid:
        result['decision_stale'] = True
    if valid:
        result['decision_reason'] = saved.get('reason', '')
        result['decided_at'] = saved.get('decided_at')
    return result


def bind_lines(findings, rows, scope, store):
    current = {row['id']: row for row in rows}
    positions = {row['id']: index for index, row in enumerate(rows)}
    result = []
    for finding in findings:
        context = []
        matched_ids = finding.get('context_ids', [finding['id']])
        lo = max(0, min(positions[ident] for ident in matched_ids) - 1)
        hi = min(len(rows), max(positions[ident] for ident in matched_ids) + 2)
        for ident in [row['id'] for row in rows[lo:hi]]:
            row = current[ident]
            base = row.get('base') or {}
            timing = row.get('voice_timing') or [base.get('start', row['start']), base.get('end', row['end'])]
            context.append(dict(id=ident, start=row['start'], end=row['end'],
                source_text=row.get('source_text', row.get('text', '')),
                text=row.get('text', '') if 'source_text' in row else '',
                source_audio_text=base.get('source_text', row.get('source_text', row.get('text', ''))),
                source_audio_start=timing[0], source_audio_end=timing[1]))
        # 附近文字供人阅读；只有实际参与命中的上下文进入身份，避免无关编辑使复核失效。
        fields = finding.get('fields', [])
        evidence_fields = {'id', 'start', 'end', 'source_audio_start', 'source_audio_end'}
        evidence_fields |= set(fields) - {'source_audio'}
        if 'source_audio' in fields:
            evidence_fields.add('source_audio_text')
        evidence = dict(version=REVIEW_VERSION, scope=scope,
                        context_rows=[{key: value for key, value in row.items() if key in evidence_fields}
                                      for row in context if row['id'] in matched_ids],
                        fields=sorted(fields),
                        matched_rules=sorted(finding.get('matched_rules', [])),
                        reasons=sorted(finding.get('reasons', [finding['reason']])))
        item = dict(finding, key=f'{scope}:{finding["id"]}', context_rows=context,
                    evidence_fingerprint=digest(evidence))
        result.append(apply(item, store))
    return result


def comment_finding(item, settings, *, originals=(), store=None):
    """同一命中文字在采集、编辑与历史快照中使用同一证据身份。"""
    enabled, terms = settings
    if not enabled:
        return None
    values = [(key, item.get(key, '')) for key in ('text', 'text_zh', 'display_text', 'source_text')]
    # 保存时冻结已检查的有效译文和权威原文，历史成片不能靠当前缓存猜来源。
    values.extend(('review_evidence', text) for text in item.get('review_evidence_texts', []) if isinstance(text, str))
    values.extend(('original', row.get('text', '')) for row in originals if row)
    evidence, fields, reasons = {}, [], []
    for field, value in values:
        matches = blocked_comment(value or '', custom_terms=terms)
        if not matches:
            continue
        evidence[value] = sorted(matches)
        if field not in fields:
            fields.append(field)
        for reason in matches:
            if reason not in reasons:
                reasons.append(reason)
    if not reasons:
        return None
    ident = item.get('id', '')
    finding = dict(id=ident, ids=[ident], key=f'comment:{ident}',
        source_text=item.get('source_text') or item.get('text', ''),
        text=item.get('display_text') or item.get('text_zh', ''),
        source_audio_text='', fields=fields, context_ids=[], context_rows=[],
        evidence_texts=sorted(evidence),
        reason='；'.join(reasons), reasons=reasons, rule='comment', matched_rules=['comment'],
        evidence_fingerprint=digest(dict(version=REVIEW_VERSION, id=ident, evidence=evidence)))
    return apply(finding, store or dict(entries={}))


def stale_count(store, findings):
    active = {item['key']: item['evidence_fingerprint'] for item in findings}
    return sum(active.get(key) != item['evidence_fingerprint'] for key, item in store['entries'].items())
