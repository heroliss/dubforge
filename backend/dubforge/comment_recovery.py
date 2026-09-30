"""评论局部失败隔离；不改写输入，不把服务故障伪装成内容问题。"""
from dubforge.errors import DubForgeError

ITEM_ERRORS = {'LLM_CONTENT_REJECTED', 'LLM_OUTPUT_INVALID', 'LLM_EMPTY_RESPONSE', 'LLM_OUTPUT_TRUNCATED'}


def item_error_codes(error):
    if error.code in ITEM_ERRORS:
        return {error.code}
    if error.code == 'LLM_PROVIDERS_FAILED' and isinstance(error.detail, dict):
        providers = error.detail.get('providers', [])
        codes = {row.get('code') for row in providers if isinstance(row, dict)}
        if codes and codes <= ITEM_ERRORS:
            return codes
    return set()


class Recovery:
    """正常批处理；内容拒绝才拆半，连续三个单条拒绝或额外24次请求即止损。"""
    def __init__(self, *, policy, check, remaining, execute, failed, trace):
        self.policy, self.check, self.remaining = policy, check, remaining
        self.execute, self.failed, self.trace = execute, failed, trace
        self.extra = 0
        self.consecutive = 0
        self.stopped = False
        self.last_error = None

    def process(self, group, *, isolated=False):
        self.check()
        group = self.remaining(group)
        if not group or self.stopped:
            return
        if isolated:
            if self.extra >= 24:
                self.stopped = True
                self.trace('recovery_stop', '已达到本次额外诊断请求上限，剩余评论保留待处理。')
                return
            self.extra += 1
            self.trace('retry_strategy', f'缩小到 {len(group)} 条评论定位失败；原文与评分要求保持不变。')
        try:
            self.execute(group, isolated=isolated)
            self.consecutive = 0
        except DubForgeError as error:
            codes = item_error_codes(error)
            if self.policy == 'stop' or not codes:
                raise
            self.last_error = error
            pending = self.remaining(group)
            if len(pending) < len(group):
                self.consecutive = 0
            if not pending:
                return
            # 格式纠错已在执行器内有界进行，不再叠加第二套格式重试。
            if len(pending) > 1 and codes != {'LLM_OUTPUT_INVALID'}:
                middle = (len(pending) + 1) // 2
                for half in (pending[:middle], pending[middle:]):
                    self.process(half, isolated=True)
                return
            self.failed(pending, error, codes == {'LLM_CONTENT_REJECTED'})
            if codes == {'LLM_CONTENT_REJECTED'} and len(pending) == 1:
                self.consecutive += 1
                if self.consecutive >= 3:
                    self.stopped = True
                    self.trace('recovery_stop', '连续三条独立请求被审核拦截，可能与共享背景或规则有关；停止自动尝试，未测试项不标为被拦截。')
