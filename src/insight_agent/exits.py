"""退出码语义：让调用方知道"该不该重试"。

**为什么不用 0/1/2。** `cli.research` 原来只有三个出口，其中 `1` 同时覆盖
"验证层算不出幻觉率"和"上游 API 限流"。定时调用的脚本读到 `1` 时无法判断：
前者重试也没用，后者退避后重试就能成。这两种情况在定时任务里天天遇到
（免费额度被打穿时一整批都是限流），让脚本只能全当失败处理，等于白跑一轮。

**不破坏已有约定。** `0` 仍然是成功、`2` 仍然是用法错（POSIX 惯例），
所以脚本里已有的 `if [ $? -eq 0 ]` 与 `case $? in 2)` 不会破。

    0  completed            完成
    1  verify_failed        跑完了但验证层异常（幻觉率算不出来等）
    2  usage_error          参数 / 配置问题，改命令，不重试
    3  rate_limited         上游 429 或额度耗尽 —— 退避后重试
    4  upstream_unavailable 上游 5xx / 连接失败 —— 退避后重试
    5  internal_error       自己的 bug —— 上报，别重试
    130 interrupted         Ctrl-C / SIGTERM

`should_retry` 把"该不该重试"这件事收在一处，而不是让每个调用方自己
记一份码表 —— 码表散出去就会漂。
"""

from __future__ import annotations

from enum import IntEnum


class ExitKind(IntEnum):
    """退出原因。值就是进程退出码，不要改。"""

    COMPLETED = 0
    VERIFY_FAILED = 1
    USAGE_ERROR = 2
    RATE_LIMITED = 3
    UPSTREAM_UNAVAILABLE = 4
    INTERNAL_ERROR = 5
    INTERRUPTED = 130


#: 该重试的两种：外部依赖的问题，等一会儿自己会好
RETRYABLE = frozenset({ExitKind.RATE_LIMITED, ExitKind.UPSTREAM_UNAVAILABLE})


def should_retry(kind: ExitKind) -> bool:
    """这一次失败值得退避后重试吗。"""
    return kind in RETRYABLE


def classify_exception(exc: BaseException) -> ExitKind:
    """把异常映射到退出原因。

    只认**确定**的信号：限流用 `openai.RateLimitError`（评测层 `eval/runner.py`
    用的同一个类，口径必须一致），上游不可用用 SDK 的 APIStatusError 里
    5xx 的部分。判不出来的归 `INTERNAL_ERROR` —— 宁可说"不知道"，
    也不要猜成"限流"，那会让脚本无限重试一个永远不会成功的任务。

    注意这里的边界：某些厂商额度耗尽时返回 200 + body 里带错误信息，
    那种情况在本地代码里看起来就是成功。本函数管不了它，只能问厂商。
    """
    try:
        from openai import APIConnectionError, APIStatusError, RateLimitError
    except ImportError:  # 没装 openai 就全归内部异常，不猜
        return ExitKind.INTERNAL_ERROR

    if isinstance(exc, RateLimitError):
        return ExitKind.RATE_LIMITED
    if isinstance(exc, APIConnectionError):
        return ExitKind.UPSTREAM_UNAVAILABLE
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return ExitKind.UPSTREAM_UNAVAILABLE
        if status == 429:
            return ExitKind.RATE_LIMITED
    if isinstance(exc, KeyboardInterrupt):
        return ExitKind.INTERRUPTED
    return ExitKind.INTERNAL_ERROR